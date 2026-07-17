"""Core extraction engine using innerText instead of DOM selectors."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote_plus, urlparse


from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)

from . import i18n

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]

# Pacing between page navigations
_NAV_DELAY = 2.0

# Backoff before retrying a temporarily blocked page
_RATE_LIMIT_RETRY_DELAY = 5.0

# Returned as section text when LinkedIn rate-limits the page
_RATE_LIMITED_MSG = "[Rate limited] LinkedIn blocked this section. Try again later or request fewer sections."

# LinkedIn shows 25 results per page
_PAGE_SIZE = 25

# Normalization maps for job search filters
_DATE_POSTED_MAP = {
    "past_hour": "r3600",
    "past_24_hours": "r86400",
    "past_week": "r604800",
    "past_month": "r2592000",
}

_EXPERIENCE_LEVEL_MAP = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid_senior": "4",
    "director": "5",
    "executive": "6",
}

_JOB_TYPE_MAP = {
    "full_time": "F",
    "part_time": "P",
    "contract": "C",
    "temporary": "T",
    "volunteer": "V",
    "internship": "I",
    "other": "O",
}

_WORK_TYPE_MAP = {"on_site": "1", "remote": "2", "hybrid": "3"}

_SORT_BY_MAP = {"date": "DD", "relevance": "R"}

# Valid tokens for the people-search ``network`` facet.
# LinkedIn accepts "F" (1st-degree), "S" (2nd-degree), "O" (3rd-degree and beyond).
_NETWORK_TOKENS = ("F", "S", "O")

_DIALOG_PREMIUM_LINK_SELECTOR = (
    'dialog[open] a[href*="/premium/"], [role="dialog"] a[href*="/premium/"]'
)
_DIALOG_TEXTAREA_SELECTOR = '[role="dialog"] textarea, dialog textarea'

_MESSAGING_COMPOSE_LINK_SELECTOR = 'main a[href*="/messaging/compose/"]'

# The messaging surface labels its controls in the account's UI language, so
# every text-bearing selector is generated from the i18n alias tables
# (case-insensitive via the CSS ``i`` flag). Structural alternatives that do
# not depend on text are kept as fallbacks.
_MESSAGING_COMPOSE_SELECTOR = i18n.attr_contains_selector(
    'div[role="textbox"][contenteditable="true"]',
    "aria-label",
    i18n.MESSAGE_COMPOSE_BOX,
)
_MESSAGING_COMPOSE_FALLBACK_SELECTORS = (
    _MESSAGING_COMPOSE_SELECTOR,
    'main div[role="textbox"][contenteditable="true"]',
    'main [contenteditable="true"][aria-label*="message" i], '
    'main [contenteditable="true"][aria-label*="mensagem" i]',
)
_MESSAGING_ENABLED_SEND_SELECTOR = (
    'button[type="submit"]:not([disabled]), '
    + i18n.attr_contains_selector(
        "button", "aria-label", i18n.SEND_BUTTON, suffix=":not([disabled])"
    )
)
_MESSAGING_RECIPIENT_PICKER_SELECTOR = (
    i18n.attr_contains_selector("input", "placeholder", i18n.RECIPIENT_PICKER)
    + ", "
    + i18n.attr_contains_selector("input", "aria-label", i18n.RECIPIENT_PICKER)
    + ", "
    + i18n.attr_contains_selector("input", "placeholder", i18n.RECIPIENT_PICKER_MULTI)
)
_MESSAGING_CLOSE_SELECTOR = i18n.attr_contains_selector(
    "button", "aria-label", i18n.DRAFT_CLOSE
)

# Shared JS function that walks up from any /messaging/compose/ anchor
# inside <main> to find the smallest ancestor that satisfies the
# action-root predicate (>=2 interactive children, >=1 button). This is
# the top-card action row regardless of LinkedIn's class names.
#
# Inlined into both _ACTION_SIGNALS_JS and _OPEN_MORE_BUTTON_JS so a
# single change to the heuristic propagates to both call sites.
_FIND_ACTION_ROOT_FN_JS = r"""
function findActionRoot(main) {
  const composeAnchors = main.querySelectorAll('a[href*="/messaging/compose/"]');
  for (const a of composeAnchors) {
    let el = a.parentElement;
    while (el && el !== main) {
      const interactive = el.querySelectorAll('button, a').length;
      const buttons = el.querySelectorAll('button').length;
      if (interactive >= 2 && buttons >= 1) {
        return el;
      }
      el = el.parentElement;
    }
  }
  return null;
}
"""

# Shared JS function that fingerprints the incoming-request action row.
# Incoming-request profiles render no Message button in the top card, so
# findActionRoot (compose-anchor walk) cannot locate their action row and
# would mis-anchor on sidebar mutual-connection cards instead. This walk
# anchors on button[aria-expanded] (the More button) and validates the
# smallest multi-button ancestor against the fingerprint verified live
# 2026-06-11 on two German-locale incoming-request profiles:
#
#   [button aria-label (Accept)] [button aria-label (Ignore)]
#   [button aria-expanded, no aria-label (More)]
#
# All checks are attribute presence and structural counts per the
# AGENTS.md Scraping Rules — no label values are read. Every guard kills
# a known false positive: total-button-count === 3 and labeled === 2
# exclude video-player control bars (play/mute/captions all carry
# aria-label); the unlabeled-expander check excludes player settings
# expanders (the profile More button never carries aria-label); the
# DOM-order guard excludes bars with trailing labeled buttons; the
# compose/invite/labeled-anchor exclusions kill follow_only, pending,
# connected top cards and sidebar cards. The scan continues over ALL
# expander candidates because cover-video profiles render the player's
# expander before the top-card row in DOM order.
#
# The search is scoped to the top card — the first <section> of <main>
# (falling back to main's first child, then main). Profile pages render
# the action row in the top card; feed, "people also viewed", and other
# widgets live in later sections. Without the scope an unrelated widget
# elsewhere in main with the same button shape could be misclassified and
# its first labeled button clicked.
#
# Inlined into _ACTION_SIGNALS_JS and _CLICK_INCOMING_ACCEPT_JS so a
# single change to the fingerprint propagates to both call sites.
_FIND_INCOMING_ACTION_ROW_FN_JS = r"""
function findIncomingActionRow(main) {
  const scope = main.querySelector('section') || main.firstElementChild || main;
  const matches = [];
  for (const expander of scope.querySelectorAll('button[aria-expanded]')) {
    let el = expander.parentElement;
    while (el && el !== scope && el !== main) {
      if (el.querySelectorAll('button').length >= 2) {
        const buttons = el.querySelectorAll('button');
        const labeled = el.querySelectorAll('button[aria-label]');
        const expanders = el.querySelectorAll('button[aria-expanded]');
        if (
          buttons.length === 3 &&
          labeled.length === 2 &&
          expanders.length === 1 &&
          !expanders[0].hasAttribute('aria-label') &&
          expanders[0].compareDocumentPosition(labeled[1]) &
            Node.DOCUMENT_POSITION_PRECEDING &&
          !el.querySelector('a[href*="/messaging/compose/"]') &&
          !el.querySelector('a[href*="/preload/custom-invite/"]') &&
          !el.querySelector('a[aria-label]')
        ) {
          matches.push(el);
        }
        break;
      }
      el = el.parentElement;
    }
  }
  // Require a unique match: a profile's top card has exactly one action
  // row. Ambiguity (two rows matching the shape) is treated as no match so
  // the irreversible Accept click never fires on a guessed control.
  return matches.length === 1 ? matches[0] : null;
}
"""

# Locale-independent connection-state probe. Returns four booleans;
# per AGENTS.md Scraping Rules, every signal is based on URL patterns
# or ARIA-attribute *presence* — never on label text values.
#
# - hasInvite: vanityName-scoped invite anchor anywhere in document.
#   Searches document (not main) so a post-More-menu reread sees
#   portal-rendered menu items. The vanityName parameter is unique to
#   the target user, so document-wide search has no false-positive risk.
# - hasComposeInActionRoot: any /messaging/compose/ anchor exists inside
#   the action root. Scoped to main (not document) to avoid the More
#   menu's "Send profile in a message" anchor, which is a compose URL
#   but lives outside the action area.
# - hasEditIntro: edit-intro URL exists, only rendered on own profile.
# - hasLabeledActionButton: at least one <button[aria-label]> inside the
#   action root. Primary action buttons (Follow / Connect /
#   Save in Sales Navigator) carry aria-label for screen readers; the
#   profile More button uses aria-expanded instead and is not counted.
# - hasLabeledActionAnchor: at least one <a[aria-label]> inside the
#   action root. LinkedIn renders the Pending state as an anchor (linking
#   back to the profile URL) carrying aria-label like "Pending, click to
#   withdraw…". The Message anchor has only aria-disabled, so a labeled
#   anchor is the locale-independent Pending signal.
# - hasIncomingActionRow: the incoming-request fingerprint matched (see
#   _FIND_INCOMING_ACTION_ROW_FN_JS). Computed independently of
#   findActionRoot, which cannot locate the top-card row on incoming
#   profiles (no compose anchor there) and would mis-anchor on sidebar
#   cards.
#
# The username is CSS-escaped before interpolation into attribute
# selectors to defend against malformed inputs containing characters
# that would otherwise break the selector syntax (quotes, brackets).
_ACTION_SIGNALS_JS = (
    r"""
((username) => {
"""
    + _FIND_ACTION_ROOT_FN_JS
    + _FIND_INCOMING_ACTION_ROW_FN_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return null;

  const safe = CSS.escape(username);
  const inviteSel = `a[href*="/preload/custom-invite/?vanityName=${safe}"]`;
  const editSel = `a[href*="/in/${safe}/edit/intro/"]`;

  const hasInvite = !!document.querySelector(inviteSel);
  const hasEditIntro = !!main.querySelector(editSel);

  const actionRoot = findActionRoot(main);

  let hasComposeInActionRoot = false;
  let hasLabeledActionButton = false;
  let hasLabeledActionAnchor = false;
  if (actionRoot) {
    hasComposeInActionRoot =
      !!actionRoot.querySelector('a[href*="/messaging/compose/"]');
    for (const b of actionRoot.querySelectorAll('button')) {
      if (b.hasAttribute('aria-label')) {
        hasLabeledActionButton = true;
        break;
      }
    }
    for (const a of actionRoot.querySelectorAll('a')) {
      if (a.hasAttribute('aria-label')) {
        hasLabeledActionAnchor = true;
        break;
      }
    }
  }

  return {
    hasInvite,
    hasComposeInActionRoot,
    hasEditIntro,
    hasLabeledActionButton,
    hasLabeledActionAnchor,
    hasIncomingActionRow: !!findIncomingActionRow(main),
  };
})
"""
)

# Open the profile's More button, located inside the action root via the
# aria-expanded attribute. The aria-expanded attribute uniquely identifies
# the menu opener without text labels (the More button has no aria-label,
# while Follow/Connect/Pending buttons do — the inverse pattern). Returns
# true iff the click landed; the caller waits for [role='menu'] visibility
# before re-scanning signals.
_OPEN_MORE_BUTTON_JS = (
    r"""
(() => {
"""
    + _FIND_ACTION_ROOT_FN_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return false;
  const actionRoot = findActionRoot(main);
  if (!actionRoot) return false;
  const moreBtn = actionRoot.querySelector('button[aria-expanded]');
  if (!moreBtn) return false;
  moreBtn.click();
  return true;
})
"""
)

# Click Accept on an incoming-request profile. Accept is the FIRST labeled
# button in the fingerprinted row — primary actions render first in
# top-card action rows (Connect/Message lead on other profile states; the
# inverse of dialogs, where the primary button renders last). Clicking the
# second button would silently and irreversibly Ignore the request, so the
# click only fires when the full fingerprint matched.
_CLICK_INCOMING_ACCEPT_JS = (
    r"""
(() => {
"""
    + _FIND_INCOMING_ACTION_ROW_FN_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return false;
  const row = findIncomingActionRow(main);
  if (!row) return false;
  row.querySelectorAll('button[aria-label]')[0].click();
  return true;
})
"""
)


def _connection_result(
    url: str,
    status: str,
    message: str,
    *,
    note_sent: bool = False,
    profile: str = "",
) -> dict[str, Any]:
    """Build a structured response for a profile connection attempt."""
    result: dict[str, Any] = {
        "url": url,
        "status": status,
        "message": message,
        "note_sent": note_sent,
    }
    if profile:
        result["profile"] = profile
    return result


def _normalize_csv(value: str, mapping: dict[str, str]) -> str:
    """Normalize a comma-separated filter value using the provided mapping."""
    parts = [v.strip() for v in value.split(",")]
    return ",".join(mapping.get(p, p) for p in parts)


def _encode_list_facet(values: list[str]) -> str:
    """Encode a list of string values for a LinkedIn people-search list facet.

    LinkedIn's people-search URL uses JSON-list encoded facets of the form
    ``["A","B"]``. This helper URL-encodes the rendered JSON so the final URL
    contains e.g. ``%5B%22F%22%5D`` for ``["F"]``.
    """
    return quote_plus(json.dumps(values, separators=(",", ":")))


# Patterns that mark the start of LinkedIn page chrome (sidebar/footer).
# Everything from the earliest match onwards is stripped.
_NOISE_MARKERS: list[re.Pattern[str]] = [
    # Footer nav links: "About" immediately followed by "Accessibility" or "Talent Solutions"
    re.compile(r"^About\n+(?:Accessibility|Talent Solutions)", re.MULTILINE),
    # Sidebar profile recommendations
    re.compile(r"^More profiles for you$", re.MULTILINE),
    # Sidebar premium upsell
    re.compile(r"^Explore premium profiles$", re.MULTILINE),
    # InMail upsell in contact info overlay
    re.compile(r"^Get up to .+ replies when you message with InMail$", re.MULTILINE),
    # Footer nav clusters in profile/posts pages
    re.compile(
        r"^(?:Careers|Privacy & Terms|Questions\?|Select language)\n+"
        r"(?:Privacy & Terms|Questions\?|Select language|Advertising|Ad Choices|"
        r"[A-Za-z]+ \([A-Za-z]+\))",
        re.MULTILINE,
    ),
]

_NOISE_LINES: list[re.Pattern[str]] = [
    re.compile(r"^(?:Play|Pause|Playback speed|Turn fullscreen on|Fullscreen)$"),
    re.compile(r"^(?:Show captions|Close modal window|Media player modal window)$"),
    re.compile(r"^(?:Loaded:.*|Remaining time.*|Stream Type.*)$"),
]


@dataclass
class ExtractedSection:
    """Text and compact references extracted from a loaded LinkedIn section."""

    text: str
    references: list[Reference]
    error: dict[str, Any] | None = None


_FEED_RSC_MARKER = "sduiid=com.linkedin.sdui.pagers.feed.mainFeed"
# Matches a LinkedIn post permalink in either plain or JSON-escaped form
# (the initial /feed/ HTML embeds the RSC flight data with \u002f for slashes,
# while paginated responses use plain slashes). Captures the slug portion so
# we can rebuild a canonical URL regardless of the source encoding.
_POST_SLUG_URL_RE = re.compile(
    r"linkedin\.com(?:\\u002[fF]|/)posts(?:\\u002[fF]|/)"
    r"(?P<slug>[A-Za-z0-9_-]+?-(?:ugcPost|activity|share)-\d+-[A-Za-z0-9_-]+)"
)
_FEED_DOCUMENT_URLS = {
    "https://www.linkedin.com/feed",
    "https://www.linkedin.com/feed/",
}


def _is_feed_payload_response(url: str) -> bool:
    """True if the response URL is one that carries `postSlugUrl` fields."""
    if _FEED_RSC_MARKER in url:
        return True
    return url.split("?", 1)[0] in _FEED_DOCUMENT_URLS


def _build_feed_references(
    raw_references: list[Any],
    captured_urls: list[str],
) -> list[Reference]:
    """Compose feed references from DOM anchors + SDUI captures.

    The feed page renders many anchors that are not post permalinks:
    sidebar widgets, profile cards, employer logos, etc. Mixing them
    into ``references["feed"]`` blurs the contract and competes with
    SDUI permalinks for the per-section cap. We keep only the
    ``feed_post`` slice from the DOM:

    - DOM anchors → ``feed_post`` entries with ``/feed/update/<urn>/``
      URLs (whatever ``classify_link`` recognises).
    - SDUI captures → ``feed_post`` entries with ``/posts/<slug>`` URLs
      for permalinks that the DOM does not surface as an anchor.

    Both are deduped on exact URL string. The two shapes pointing at
    the same underlying post will *not* collapse — ``dedupe_references``
    matches strings, not URNs. Both are valid LinkedIn permalinks, so
    consumers should treat ``feed_post`` as polymorphic on URL form;
    URN-based equivalence is left to the consumer.
    """
    refs = [
        ref
        for ref in build_references(raw_references, "feed")
        if ref["kind"] == "feed_post"
    ]
    existing = {r["url"] for r in refs}
    for sdui_url in captured_urls:
        # AGENTS.md mandates relative paths for LinkedIn references.
        # The SDUI capture carries fully-qualified URLs like
        # https://www.linkedin.com/posts/<slug>; strip the host so the
        # relative-path convention holds. ``classify_link`` does not
        # currently route ``/posts/<slug>`` paths to any kind, so we
        # bypass it for this fallback append.
        parsed = urlparse(sdui_url)
        if not parsed.path.startswith("/posts/"):
            continue
        relative = parsed.path
        if relative in existing:
            continue
        refs.append({"kind": "feed_post", "url": relative, "context": "feed"})
        existing.add(relative)
    # Cap kept in sync with _REFERENCE_CAPS["feed"] in link_metadata.py;
    # changing one without the other will drop or duplicate entries
    # silently. Matches get_feed's num_posts ceiling (Field(ge=1, le=50)).
    return dedupe_references(refs, cap=50)


async def _drain_listener_tasks(pending: list[asyncio.Task[None]]) -> None:
    """Bounded teardown for fire-and-forget response listener tasks.

    The feed scroll loop appends a read task per matching response;
    those tasks must finish (or be cancelled) before we leave the
    extractor or the event loop's "Task exception was never retrieved"
    warnings will surface unrelated errors. The caps below let a stuck
    ``resp.body()`` call burn at most three seconds of teardown budget.
    """
    if not pending:
        return
    _done, leftover = await asyncio.wait(pending, timeout=2.0)
    for task in leftover:
        task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=1.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "SDUI feed listener tasks did not drain after cancel; leaking %d task(s)",
            sum(1 for t in pending if not t.done()),
        )


class FilterValidationError(ValueError):
    """Invalid ``search_people`` filter input (network token / URN shape).

    Subclassing ``ValueError`` keeps backward-compatible behaviour for
    direct extractor callers (``pytest.raises(ValueError)`` matches), while
    letting the MCP tool wrapper catch this case precisely and surface the
    actionable message past ``mask_error_details``.
    """


def strip_linkedin_noise(text: str) -> str:
    """Remove LinkedIn page chrome (footer, sidebar recommendations) from innerText.

    Finds the earliest occurrence of any known noise marker and truncates there.
    """
    cleaned = _truncate_linkedin_noise(text)
    return _filter_linkedin_noise_lines(cleaned)


def _filter_linkedin_noise_lines(text: str) -> str:
    """Remove known media/control noise lines from already-truncated content."""
    filtered_lines = [
        line
        for line in text.splitlines()
        if not any(pattern.match(line.strip()) for pattern in _NOISE_LINES)
    ]
    return "\n".join(filtered_lines).strip()


def _truncate_linkedin_noise(text: str) -> str:
    """Trim known LinkedIn chrome blocks before any per-line noise filtering."""
    earliest = len(text)
    for pattern in _NOISE_MARKERS:
        match = pattern.search(text)
        if match and match.start() < earliest:
            earliest = match.start()

    return text[:earliest].strip()


# Messaging-page chrome around an opened conversation thread. innerText on
# /messaging/thread/ pages carries no URL or attribute signal separating the
# inbox sidebar from the thread, so the boundaries are matched on visible
# strings — guarded by an explicit per-locale table (CLAUDE.md → Scraping
# Rules). BrowserManager forces the context locale to en-US (core/browser.py),
# so the "en" entry is the operative one; a locale without a table entry
# passes through unstripped.
@dataclass(frozen=True)
class _MessagingChromeTable:
    # Sidebar pagination control; the last line of the inbox sidebar. Pins
    # the thread header so quoted UI text inside messages can't move the
    # start boundary.
    sidebar_end: str
    # Screen-reader label on the options dropdown; appears once per sidebar
    # entry and once in the opened thread's header. The thread's own line is
    # the first occurrence after ``sidebar_end``.
    thread_header_prefix: str
    # First control of the trailing message-composer block.
    composer_start: str
    # Standalone controls of the composer block, matched exactly. At least
    # one must follow a ``composer_start`` candidate to confirm it is the
    # real composer rather than a message quoting the label. Controls whose
    # text embeds the participant name (the Attach lines) are deliberately
    # excluded: they would need prefix matching, and any prefix match lets
    # quoted control text with a suffix confirm a false boundary.
    composer_companions: tuple[str, ...]


# How far below a composer-label candidate a companion control may sit and
# still count as the same block. The observed block spans 6 lines; the slack
# covers extra controls LinkedIn injects (e.g. "Press Enter to Send").
_COMPOSER_COMPANION_WINDOW = 8

_MESSAGING_CHROME_STRINGS: dict[str, _MessagingChromeTable] = {
    "en": _MessagingChromeTable(
        sidebar_end="Load more conversations",
        thread_header_prefix="Open the options list in your conversation with",
        composer_start="Maximize compose field",
        composer_companions=(
            "Open GIF Keyboard",
            "Open Emoji Keyboard",
            "Open send options",
        ),
    ),
}


def strip_conversation_chrome(text: str, locale: str = "en") -> str:
    """Trim messaging chrome around an opened conversation thread.

    A conversation page's innerText embeds the thread between three chrome
    blocks: the messaging header, the inbox sidebar (which previews *other*
    conversations), and the trailing message composer. Drops everything
    through the thread-header line and everything from the composer onward.
    Each boundary independently falls back to keeping the text when its
    marker is absent (unknown locale, layout change), so a failed match
    leaks chrome rather than dropping messages.
    """
    table = _MESSAGING_CHROME_STRINGS.get(locale)
    if table is None:
        return text

    lines = text.splitlines()

    # End boundary: the last composer-label line, accepted only when an
    # exact companion control follows within the next few lines. The real
    # composer block is contiguous (label + controls observed within 6
    # lines), so a nearby companion confirms chrome, while a message that
    # quotes the label — or control text with any suffix — falls through to
    # the missing-marker fallback. A verbatim multi-line reproduction of the
    # block inside a message remains indistinguishable from the block itself;
    # that ambiguity is inherent to text-only stripping.
    end = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() != table.composer_start:
            continue
        if any(
            lines[j].strip() in table.composer_companions
            for j in range(i + 1, min(i + 1 + _COMPOSER_COMPANION_WINDOW, len(lines)))
        ):
            end = i
        break

    # Start boundary: the sidebar's pagination line, when present, pins the
    # real thread header as the first options line after it; quoted UI text
    # inside messages can no longer pull the boundary into the thread. The
    # sidebar omits the pagination control when there are few conversations —
    # then fall back to the last options line before the composer.
    start = 0
    sidebar_end = next(
        (i for i in range(end) if lines[i].strip() == table.sidebar_end), None
    )
    if sidebar_end is not None:
        header = next(
            (
                i
                for i in range(sidebar_end + 1, end)
                if lines[i].strip().startswith(table.thread_header_prefix)
            ),
            None,
        )
        start = (header + 1) if header is not None else sidebar_end + 1
    else:
        for i in range(end - 1, -1, -1):
            if lines[i].strip().startswith(table.thread_header_prefix):
                start = i + 1
                break

    return "\n".join(lines[start:end]).strip()
