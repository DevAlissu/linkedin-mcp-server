"""Messaging operations: inbox, conversations, and sending messages.

Extracted from the monolithic extractor as a feature mixin composed into
``LinkedInExtractor``. Covers reading the inbox, opening/searching conversation
threads, resolving the compose surface, and sending a message. All text-bearing
selectors resolve through :mod:`.base` (which builds them from :mod:`.i18n`), so
the flows work regardless of the account's UI language.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote_plus, urljoin

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.core.utils import detect_rate_limit, handle_modal_close
from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)

from .base import (
    _MESSAGING_CLOSE_SELECTOR,
    _MESSAGING_COMPOSE_FALLBACK_SELECTORS,
    _MESSAGING_COMPOSE_LINK_SELECTOR,
    _MESSAGING_COMPOSE_SELECTOR,
    _MESSAGING_RECIPIENT_PICKER_SELECTOR,
    strip_conversation_chrome,
    strip_linkedin_noise,
)

logger = logging.getLogger(__name__)


class MessagingMixin:
    """Inbox, conversation, and message-sending operations.

    Mixed into ``LinkedInExtractor``; ``_page`` and the navigation/extraction/
    DOM helpers below are provided by the extractor at runtime.
    """

    _page: Page

    if TYPE_CHECKING:

        async def _navigate_to_page(self, url: str) -> None: ...

        async def _extract_root_content(
            self, selectors: list[str]
        ) -> dict[str, Any]: ...

        async def _click_first(self, selector: str, *, timeout: int = 5000) -> None: ...

        async def _locator_is_visible(
            self, selector: str, *, timeout: int = 2000
        ) -> bool: ...

        async def _scroll_main_scrollable_region(
            self,
            *,
            position: Literal["top", "bottom"],
            attempts: int,
            pause_time: float = 0.5,
        ) -> None: ...

        async def _wait_for_main_text(
            self,
            *,
            minimum_length: int = 100,
            timeout: int = 10000,
            log_context: str,
        ) -> None: ...

        @staticmethod
        def _message_action_result(
            url: str,
            status: str,
            message: str,
            *,
            recipient_selected: bool = False,
            sent: bool = False,
        ) -> dict[str, Any]: ...

        @staticmethod
        def _single_section_result(
            url: str,
            section_name: str,
            text: str,
            references: list[Reference] | None = None,
        ) -> dict[str, Any]: ...

    async def _resolve_message_compose_href(self) -> str | None:
        """Return the direct recipient-specific compose URL from a profile page."""
        href = await self._page.evaluate(
            """(selector) => {
                const isVisible = element =>
                    !!(
                        element &&
                        (element.offsetWidth ||
                            element.offsetHeight ||
                            element.getClientRects().length)
                    );

                const anchor = Array.from(
                    document.querySelectorAll(selector)
                ).find(isVisible);
                if (!anchor) return null;
                return anchor.getAttribute('href') || anchor.href || null;
            }""",
            _MESSAGING_COMPOSE_LINK_SELECTOR,
        )
        if not isinstance(href, str) or not href.strip():
            return None
        return urljoin("https://www.linkedin.com", href.strip())

    async def _read_profile_display_name(self) -> str | None:
        """Read the visible profile name from the current person page."""
        display_name = await self._page.evaluate(
            """() => {
                const heading = document.querySelector('main h1');
                const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                if (heading) {
                    const headingText = normalize(
                        heading.innerText || heading.textContent || ''
                    );
                    if (headingText) return headingText;
                }

                const main = document.querySelector('main');
                if (!main) return '';
                const lines = (main.innerText || '')
                    .split('\\n')
                    .map(normalize)
                    .filter(Boolean);
                return lines[0] || '';
            }"""
        )
        if not isinstance(display_name, str):
            return None
        display_name = display_name.strip()
        return display_name or None

    async def _wait_for_message_surface(
        self,
    ) -> Literal["composer", "recipient_picker"] | None:
        """Wait for either the recipient picker or the real composer to appear.

        The recipient-picker probe uses a short 2 s cap so we fall through
        quickly to the composer check, which uses the page-level default
        (``BrowserConfig.default_timeout``, configurable via ``--timeout``).
        """
        if await self._locator_is_visible(
            _MESSAGING_RECIPIENT_PICKER_SELECTOR, timeout=2000
        ):
            return "recipient_picker"
        if await self._wait_for_message_composer():
            return "composer"
        return None

    async def _select_message_recipient(self, *candidates: str) -> bool:
        """Select the intended recipient from LinkedIn's New message picker."""
        normalized_candidates = [value.strip() for value in candidates if value.strip()]
        if not normalized_candidates:
            return False

        selected = await self._page.evaluate(
            """({ candidates }) => {
                const normalize = value =>
                    (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const isVisible = element =>
                    !!(
                        element &&
                        (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
                    );
                const pickerInput = Array.from(document.querySelectorAll('input')).find(
                    element =>
                        isVisible(element) &&
                        /type a name|multiple names/i.test(
                            `${element.placeholder || ''} ${
                                element.getAttribute('aria-label') || ''
                            }`
                        )
                );
                const pickerRoot =
                    pickerInput?.closest('section, dialog, [role="dialog"], aside, div') ||
                    document.body;
                const rows = Array.from(
                    pickerRoot.querySelectorAll(
                        '[role="option"], [role="listitem"], li, button, a, div'
                    )
                ).filter(element => {
                    if (!isVisible(element)) return false;
                    const text = normalize(element.innerText || element.textContent);
                    return text.length > 0 && text !== 'new message';
                });

                for (const candidate of candidates.map(normalize)) {
                    const exact = rows.find(element =>
                        normalize(element.innerText || element.textContent) === candidate
                    );
                    if (exact) {
                        exact.click();
                        return true;
                    }
                }

                for (const candidate of candidates.map(normalize)) {
                    const partial = rows.find(element =>
                        normalize(element.innerText || element.textContent).includes(candidate)
                    );
                    if (partial) {
                        partial.click();
                        return true;
                    }
                }

                return false;
            }""",
            {"candidates": normalized_candidates},
        )
        if selected:
            await asyncio.sleep(0.75)
        return bool(selected)

    async def _wait_for_message_composer(self) -> bool:
        """Wait for the usable LinkedIn message composer to appear."""
        return await self._resolve_message_compose_box() is not None

    async def _resolve_message_compose_box(self) -> Any | None:
        """Resolve the visible compose box used for writing a LinkedIn message.

        Uses the page-level default timeout (``BrowserConfig.default_timeout``)
        so the ``--timeout`` CLI flag is respected.
        """
        for selector in _MESSAGING_COMPOSE_FALLBACK_SELECTORS:
            locator = self._page.locator(selector)
            candidate_count: int | None = None
            try:
                candidate_count = await locator.count()
            except Exception:
                logger.debug(
                    "Could not count compose box candidates for selector %r",
                    selector,
                    exc_info=True,
                )

            logger.debug(
                "Message compose selector %r matched %s candidate(s)",
                selector,
                candidate_count if candidate_count is not None else "unknown",
            )

            # patchright quirk: locator.wait_for(state="visible") times out on
            # the contenteditable compose div even though count() > 0 and the
            # element is fully visible by every CSS/DOM criterion (display:block,
            # visibility:visible, opacity:1, non-zero bbox, no inert ancestor).
            # This appears to be a patchright bug with React-hydrated contenteditable
            # elements in isolated worlds. Skip the actionability wait when count()
            # already confirmed the element is present — downstream interactions
            # use page.evaluate() which bypasses the same check.
            if candidate_count and candidate_count > 0:
                return locator.last

            # Fallback: when count() raised an exception above (candidate_count
            # is None), attempt the original wait_for path.  This is unlikely to
            # succeed given the same patchright quirk, but preserves the prior
            # behaviour for non-patchright drivers where wait_for works normally.
            candidate = locator.last
            try:
                await candidate.wait_for(state="visible")
                return candidate
            except PlaywrightTimeoutError:
                continue

        return None

    async def _compose_page_matches_recipient(self, *candidates: str) -> bool:
        """Verify the compose page visibly identifies the intended recipient."""
        normalized_candidates = [value.strip() for value in candidates if value.strip()]
        if not normalized_candidates:
            return False

        matched = await self._page.evaluate(
            """({ candidates }) => {
                const normalize = value =>
                    (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const isVisible = element =>
                    !!(
                        element &&
                        (element.offsetWidth ||
                            element.offsetHeight ||
                            element.getClientRects().length)
                    );

                const targetValues = candidates.map(normalize).filter(Boolean);
                const root = document.querySelector('main') || document.body;
                if (!root) return false;

                const entries = Array.from(
                    root.querySelectorAll(
                        'button, [role="button"], a, span, div, li, p, h1, h2, h3'
                    )
                )
                    .filter(isVisible)
                    .map(element =>
                        [
                            normalize(element.innerText || element.textContent || ''),
                            normalize(element.getAttribute('aria-label') || ''),
                        ].filter(Boolean)
                    )
                    .flat();

                return targetValues.some(candidate =>
                    entries.some(entry => entry === candidate || entry.includes(candidate))
                );
            }""",
            {"candidates": normalized_candidates},
        )
        return bool(matched)

    async def _message_text_visible(self, message: str) -> bool:
        """Wait until the compose page visibly contains the just-sent message text.

        Uses the page-level default timeout (``BrowserConfig.default_timeout``).
        """
        try:
            await self._page.wait_for_function(
                """({ expected }) => {
                    const normalize = value =>
                        (value || '').replace(/\\s+/g, ' ').trim();
                    const bodyText = normalize(document.body?.innerText || '');
                    return bodyText.includes(normalize(expected));
                }""",
                arg={"expected": message},
            )
            return True
        except PlaywrightTimeoutError:
            return False

    async def _dismiss_message_ui(self) -> None:
        """Best-effort dismissal for the profile messaging UI."""
        if not await self._locator_is_visible(_MESSAGING_CLOSE_SELECTOR, timeout=750):
            return
        try:
            await self._click_first(_MESSAGING_CLOSE_SELECTOR, timeout=1500)
            await asyncio.sleep(0.5)
        except Exception:
            logger.debug("Could not dismiss LinkedIn messaging UI", exc_info=True)

    @staticmethod
    def _extract_thread_id(url: str) -> str | None:
        """Parse a LinkedIn thread id from a messaging thread URL."""
        match = re.search(r"/messaging/thread/([^/?#]+)/", url)
        return match.group(1) if match else None

    async def _resolve_conversation_thread_urls(self, display_name: str) -> list[str]:
        """Return all thread URLs whose participant name matches display_name.

        Enumerates the plain messaging inbox (`/messaging/`) plus click-to-capture
        because LinkedIn renders the messaging sidebar with no anchor hrefs, no
        data-thread attributes, and no embedded URNs — clicking each row and
        reading the resulting SPA URL is the only available extraction path.
        The inbox is used rather than `?searchTerm=` because LinkedIn's
        messaging search frequently returns "We didn't find anything" for a
        participant whose thread is plainly present in the inbox (issue #434).
        ``name_filter`` is passed to the enumerator so only the matching row is
        clicked — clicking a row may mark it read, so unrelated threads stay
        untouched.

        Matches by case-insensitive equality on the cleaned participant name
        derived from the row's aria-label, which tolerates duplicate threads
        with the same participant. Browser locale is forced to en-US so the
        verb prefix strips reliably; in any other locale the comparison fails
        cleanly with "Could not find a conversation" rather than returning
        a wrong-thread match. If the inbox scan finds nothing (a thread buried
        below the scrolled rows), it falls back to the `?searchTerm=` search as
        a last resort.

        For a participant with multiple threads, the returned set — and thus
        ``index`` selection in the caller — covers the threads visible in the
        scanned inbox; the search fallback only runs when the inbox scan is
        empty. Open a buried duplicate thread directly via ``thread_id``
        (enumerate IDs with ``search_conversations``).
        """
        target_name = display_name.strip().lower()

        def _match(refs: list[Reference]) -> list[str]:
            # name_filter already gated the clicks; this enforces the same
            # exact-equality match Python-side and tolerates duplicate threads.
            return [
                f"https://www.linkedin.com{ref['url']}"
                for ref in refs
                if (ref.get("text") or "").strip().lower() == target_name
            ]

        # Primary path: enumerate the plain inbox. Reliable for the recent
        # threads that the verify-after-send workflow needs (issue #434).
        await self._navigate_to_page("https://www.linkedin.com/messaging/")
        await detect_rate_limit(self._page)
        await self._wait_for_main_text(log_context="Messaging inbox")
        await handle_modal_close(self._page)
        await self._scroll_main_scrollable_region(
            position="bottom", attempts=2, pause_time=0.5
        )
        urls = _match(
            await self._extract_conversation_thread_refs(
                limit=None, context="inbox", name_filter=display_name
            )
        )
        if urls:
            return urls

        # Fallback: LinkedIn's messaging search. Unreliable (often returns
        # "We didn't find anything" even for present threads, see #434), so it
        # runs only when the inbox scan came up empty — e.g. a thread buried
        # below the scrolled inbox window.
        await self._navigate_to_page(
            f"https://www.linkedin.com/messaging/?searchTerm={quote_plus(display_name)}"
        )
        await detect_rate_limit(self._page)
        await handle_modal_close(self._page)
        await self._wait_for_main_text(log_context="Messaging search results")
        return _match(
            await self._extract_conversation_thread_refs(
                limit=None, context="search", name_filter=display_name
            )
        )

    async def _open_conversation_by_username(
        self, linkedin_username: str, index: int = 0
    ) -> None:
        """Open the ``index``-th conversation thread for the named participant.

        ``index`` is 0-based and orders threads as the search-results sidebar
        renders them (LinkedIn surfaces newest activity first).
        """
        if index < 0:
            raise LinkedInScraperException(f"index must be non-negative (got {index}).")

        profile_url = f"https://www.linkedin.com/in/{linkedin_username}/"
        await self._navigate_to_page(profile_url)
        await detect_rate_limit(self._page)

        try:
            await self._page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Profile page did not load for %s", linkedin_username)

        await handle_modal_close(self._page)
        display_name = await self._read_profile_display_name()
        if not display_name:
            raise LinkedInScraperException(
                f"Could not resolve a display name for {linkedin_username}."
            )

        try:
            thread_urls = await self._resolve_conversation_thread_urls(display_name)
            if not thread_urls:
                raise LinkedInScraperException(
                    f"Could not find a conversation for {linkedin_username}."
                )
            if index >= len(thread_urls):
                raise LinkedInScraperException(
                    f"index {index} out of range: only {len(thread_urls)} "
                    f"thread(s) exist for {linkedin_username}."
                )

            await self._navigate_to_page(thread_urls[index])
        except PlaywrightTimeoutError as exc:
            raise LinkedInScraperException(
                "Messaging search results did not load in time."
            ) from exc

    async def get_inbox(self, limit: int = 20) -> dict[str, Any]:
        """List recent conversations from the messaging inbox."""
        url = "https://www.linkedin.com/messaging/"
        await self._navigate_to_page(url)
        await detect_rate_limit(self._page)
        await self._wait_for_main_text(log_context="Messaging inbox")
        await handle_modal_close(self._page)

        scrolls = max(1, limit // 10)
        await self._scroll_main_scrollable_region(
            position="bottom", attempts=scrolls, pause_time=0.5
        )

        raw_result = await self._extract_root_content(["main"])
        raw = raw_result["text"]
        cleaned = strip_linkedin_noise(raw) if raw else ""
        references: list[Reference] = (
            build_references(raw_result["references"], "inbox") if cleaned else []
        )

        # LinkedIn's conversation sidebar uses JS click handlers instead of
        # <a> tags, so anchor extraction cannot capture thread IDs.  Click each
        # conversation item and read the resulting SPA URL to build references.
        conversation_refs = await self._extract_conversation_thread_refs(
            limit=limit, context="inbox"
        )
        if conversation_refs:
            references = dedupe_references(conversation_refs + references)

        return self._single_section_result(
            url,
            "inbox",
            cleaned,
            references=references,
        )

    async def _extract_conversation_thread_refs(
        self, limit: int | None, context: str, *, name_filter: str | None = None
    ) -> list[Reference]:
        """Click each visible conversation item and capture the thread URL.

        Works for both the inbox sidebar and the URL-driven search-results
        sidebar (`/messaging/?searchTerm=…`), which share the same DOM shape:
        each conversation row is an ``<li>`` containing a ``<label>`` with an
        ``aria-label`` attribute carrying the participant name.

        LinkedIn renders the sidebar with no ``<a href>`` tags, no
        ``data-thread-id`` attributes, and no embedded URNs — clicking each
        row and reading the SPA URL is the only reliable extraction path.
        Pass ``limit=None`` to capture every visible row.

        When ``name_filter`` is provided, every row's aria-label is still read
        but only rows whose cleaned participant name equals it (case-insensitive)
        are clicked; non-matching rows are skipped without clicking. Clicking a
        row may mark it as read, so the filter keeps the read-marking side effect
        scoped to the requested participant when resolving by username.
        """
        # The conversation list mounts after main text settles, so wait
        # explicitly for at least one label rather than relying on
        # _wait_for_main_text alone (which only checks chrome text). LinkedIn
        # routinely takes several seconds to hydrate the messaging sidebar
        # after a navigation; an empty sidebar (zero matches) returns on
        # timeout.
        #
        # Selector is structural (`main li label[aria-label]`) rather than
        # text-prefix-based (`aria-label^="Select conversation"`) so it
        # survives any LinkedIn locale — the verb in the aria-label is
        # locale-dependent, the attribute's presence inside a list-item label
        # is not.
        #
        # Wait on `state="attached"` instead of the default `visible`:
        # Ember-managed labels are reliably attached but Playwright's
        # visibility heuristic doesn't always consider them visible.
        try:
            await self._page.wait_for_selector(
                "main li label[aria-label]",
                state="attached",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            logger.debug(
                "conversation labels did not appear within 10s (context=%s)",
                context,
            )
            return []

        # The Ember click handler lives on an inner div; the <li> and <label>
        # don't trigger SPA navigation.  No role/aria attributes exist on the
        # clickable element, so class-name selectors are unavoidable here.
        # The aria-label value flows through unmodified — Python strips any
        # known locale prefix to derive a clean participant name for refs.
        conversations: list[dict[str, str]] = await self._page.evaluate(
            """async ({ limit, nameFilter }) => {
                const labels = Array.from(document.querySelectorAll(
                    'main li label[aria-label]'
                ));
                const cap = (limit == null)
                    ? labels.length
                    : Math.min(labels.length, limit);
                // Normalize the optional participant filter the same way the
                // Python prefix-strip does (en-US "Select conversation with"
                // verb, collapsed whitespace) so the JS-side comparison
                // matches. Only the matching row is clicked — clicking marks a
                // row read, so unrelated threads must not be clicked.
                const wanted = (nameFilter || '')
                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                const results = [];
                for (let i = 0; i < cap; i++) {
                    const label = labels[i];
                    const ariaLabel = label.getAttribute('aria-label') || '';
                    const rowName = ariaLabel
                        .replace(/^Select conversation with\\s+/i, '')
                        .replace(/\\s+/g, ' ').trim().toLowerCase();
                    if (wanted && rowName !== wanted) continue;
                    const clickTarget = label.closest('li')
                        ?.querySelector('div[class*="listitem__link"]');
                    if (!clickTarget) continue;
                    const before = location.href;
                    clickTarget.click();
                    // Poll for the SPA URL to settle on the thread route. The
                    // Ember click handler can take a moment to bind after the
                    // label mounts, and a fixed sleep races the initial click.
                    let after = before;
                    for (let waits = 0; waits < 12; waits++) {
                        await new Promise(r => setTimeout(r, 100));
                        after = location.href;
                        if (after !== before
                            && /\\/messaging\\/thread\\//.test(after)) break;
                    }
                    const match = after.match(
                        /\\/messaging\\/thread\\/([^/?#]+)/
                    );
                    if (match) {
                        results.push({ ariaLabel, threadId: match[1] });
                    }
                }
                return results;
            }""",
            {"limit": limit, "nameFilter": name_filter},
        )
        refs: list[Reference] = []
        for conv in conversations:
            ref: Reference = {
                "kind": "conversation",
                "url": f"/messaging/thread/{conv['threadId']}/",
                "context": context,
            }
            name = self._strip_select_conversation_prefix(conv.get("ariaLabel", ""))
            if name:
                ref["text"] = name
            refs.append(ref)
        return refs

    # Best-effort prefix strip for the en-US "Select conversation with " verb.
    # Browser locale is forced to en-US (see BrowserManager) so this normally
    # succeeds; the regex falls through silently for any other locale, in
    # which case the full aria-label flows into the ref's text field rather
    # than a stripped name.
    _SELECT_CONVERSATION_PREFIX_RE = re.compile(
        r"^Select conversation with\s+", re.IGNORECASE
    )

    @classmethod
    def _strip_select_conversation_prefix(cls, aria_label: str) -> str:
        return cls._SELECT_CONVERSATION_PREFIX_RE.sub("", aria_label).strip()

    async def get_conversation(
        self,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: int = 0,
    ) -> dict[str, Any]:
        """Read a specific messaging conversation by thread ID or username.

        ``index`` (0-based) selects which thread to open when a participant has
        multiple conversation threads — e.g. an organic 1-on-1 plus a separate
        InMail. Ignored when ``thread_id`` is provided. Use
        ``search_conversations`` to enumerate thread IDs first if disambiguation
        by index is impractical.

        Side effect when looked up by username: resolution enumerates the
        messaging inbox and click-visits only the row(s) matching the
        participant's display name to capture the thread ID (no anchor hrefs or
        thread-id attributes exist in the sidebar). Each visit selects the row
        in the LinkedIn UI and may mark it as read. Pass ``thread_id`` directly
        to skip this enumeration.
        """
        if not linkedin_username and not thread_id:
            raise LinkedInScraperException(
                "Provide at least one of linkedin_username or thread_id"
            )

        if thread_id:
            await self._navigate_to_page(
                f"https://www.linkedin.com/messaging/thread/{thread_id}/"
            )
        else:
            await self._open_conversation_by_username(
                linkedin_username or "", index=index
            )

        await detect_rate_limit(self._page)
        await self._wait_for_main_text(log_context="Conversation")
        await handle_modal_close(self._page)
        await self._scroll_main_scrollable_region(
            position="top", attempts=3, pause_time=0.5
        )

        raw_result = await self._extract_root_content(["main"])
        raw = raw_result["text"]
        # Conversation chrome first: a sidebar preview containing a generic
        # noise marker would otherwise truncate the page before the thread
        # markers are ever seen.
        cleaned = strip_conversation_chrome(raw) if raw else ""
        cleaned = strip_linkedin_noise(cleaned) if cleaned else ""
        references = (
            build_references(raw_result["references"], "conversation")
            if cleaned
            else []
        )
        return self._single_section_result(
            self._page.url,
            "conversation",
            cleaned,
            references=references,
        )

    async def search_conversations(
        self, keywords: str, limit: int = 20
    ) -> dict[str, Any]:
        """Search messages by keyword.

        Uses LinkedIn's ``?searchTerm=`` URL parameter to drive the search
        rather than typing into the searchbox — the URL form is reliable
        regardless of how soon the messaging SPA mounts its searchbox role,
        and (critically) preserves the search filter across click-to-capture
        navigations so per-thread refs can be enumerated.

        ``limit`` caps how many search-result rows the click-to-capture loop
        visits. Each visit selects the row in LinkedIn's UI (and may mark it
        as read), so a low cap is preferable for noisy queries.
        """
        search_url = (
            f"https://www.linkedin.com/messaging/?searchTerm={quote_plus(keywords)}"
        )
        await self._navigate_to_page(search_url)
        await detect_rate_limit(self._page)
        await handle_modal_close(self._page)
        await self._wait_for_main_text(log_context="Messaging search")

        raw_result = await self._extract_root_content(["main"])
        raw = raw_result["text"]
        cleaned = strip_linkedin_noise(raw) if raw else ""
        references: list[Reference] = (
            build_references(raw_result["references"], "search_results")
            if cleaned
            else []
        )

        # Same click-to-capture path as get_inbox: LinkedIn's search sidebar
        # has no anchor hrefs or thread-id attributes, so the only way to
        # surface per-result thread IDs is to click each row and read the SPA
        # URL. URL-driven search keeps the filter active across clicks.
        conversation_refs = await self._extract_conversation_thread_refs(
            limit=limit, context="search_results"
        )
        if conversation_refs:
            references = dedupe_references(conversation_refs + references)

        return self._single_section_result(
            self._page.url,
            "search_results",
            cleaned,
            references=references,
        )

    async def send_message(
        self,
        linkedin_username: str,
        message: str,
        *,
        confirm_send: bool,
        profile_urn: str | None = None,
    ) -> dict[str, Any]:
        """Send a message to a LinkedIn user with explicit confirmation gating.

        Args:
            linkedin_username: LinkedIn username of the recipient.
            message: The message text to send.
            confirm_send: Must be True to actually send (False does a dry run).
            profile_urn: Optional profile URN (e.g. ACoAAB...) to construct the
                compose URL directly, bypassing the Message-button lookup.
        """
        profile_url = f"https://www.linkedin.com/in/{linkedin_username}/"
        await self._navigate_to_page(profile_url)
        await detect_rate_limit(self._page)

        try:
            await self._page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Profile page did not load for %s", linkedin_username)

        await handle_modal_close(self._page)
        display_name = await self._read_profile_display_name()
        if profile_urn:
            # Build the full compose URL that LinkedIn's own Message button
            # generates. The minimal ?recipient=<URN> form works for established
            # connections but shows a "Say hello" widget (no compose box) for new
            # connections. Adding profileUrn + screenContext + interop=msgOverlay
            # consistently opens the real composer regardless of connection age.
            _encoded = quote_plus(f"urn:li:fsd_profile:{profile_urn}")
            compose_url: str | None = (
                f"https://www.linkedin.com/messaging/compose/"
                f"?profileUrn={_encoded}"
                f"&recipient={profile_urn}"
                f"&screenContext=NON_SELF_PROFILE_VIEW"
                f"&interop=msgOverlay"
            )
        else:
            compose_url = await self._resolve_message_compose_href()
        if not compose_url:
            return self._message_action_result(
                profile_url,
                "message_unavailable",
                "LinkedIn did not expose a usable Message action for this profile.",
            )

        await self._navigate_to_page(compose_url)
        await detect_rate_limit(self._page)

        try:
            await self._page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Compose page did not fully load for %s", linkedin_username)

        await handle_modal_close(self._page)
        message_surface = await self._wait_for_message_surface()
        logger.debug(
            "Message surface for %s before hydration was %s",
            linkedin_username,
            message_surface,
        )

        recipient_selected = False
        if message_surface == "recipient_picker":
            recipient_selected = await self._select_message_recipient(
                display_name or "",
                linkedin_username,
            )
            logger.debug(
                "Recipient picker selection for %s returned %s",
                linkedin_username,
                recipient_selected,
            )
            if not recipient_selected:
                await self._dismiss_message_ui()
                return self._message_action_result(
                    self._page.url,
                    "recipient_resolution_failed",
                    "LinkedIn opened a compose page, but the visible recipient did not match the requested profile.",
                )
            message_surface = await self._wait_for_message_surface()
            logger.debug(
                "Message surface for %s after recipient selection was %s",
                linkedin_username,
                message_surface,
            )

        compose_box = await self._resolve_message_compose_box()
        if compose_box is None:
            await self._dismiss_message_ui()
            return self._message_action_result(
                self._page.url,
                "composer_unavailable",
                "LinkedIn did not expose a usable message composer.",
                recipient_selected=recipient_selected,
            )

        logger.debug(
            "Message compose box resolved for %s after hydration",
            linkedin_username,
        )

        if not await self._compose_page_matches_recipient(
            display_name or "",
            linkedin_username,
        ):
            logger.debug(
                "Recipient match still failed for %s after compose hydration",
                linkedin_username,
            )
            await self._dismiss_message_ui()
            return self._message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "LinkedIn opened a compose page, but the visible recipient did not match the requested profile.",
                recipient_selected=recipient_selected,
            )
        recipient_selected = True

        if not confirm_send:
            await self._dismiss_message_ui()
            return self._message_action_result(
                self._page.url,
                "confirmation_required",
                "Set confirm_send=true to send the message.",
                recipient_selected=recipient_selected,
            )

        # patchright quirk: compose_box.click() and press_sequentially() use
        # actionability checks internally and hit the same wait_for timeout.
        # Instead: focus via page.evaluate() (no actionability check) and type
        # via page.keyboard.type() which operates on the active element directly
        # and fires the real keydown/input/keyup events React needs to enable Send.
        #
        # DOM dependency: innerText extraction is not applicable here — we need
        # to call .focus() on the element reference, which requires querySelector.
        # Selectors use only role + contenteditable + aria-label (ARIA attributes,
        # not layout class names) so they are stable across LinkedIn UI changes.
        focused = await self._page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                el.focus();
                return true;
            }""",
            _MESSAGING_COMPOSE_SELECTOR
            + ', div[role="textbox"][contenteditable="true"]',
        )
        if not focused:
            await self._dismiss_message_ui()
            return self._message_action_result(
                self._page.url,
                "compose_interact_failed",
                "Could not focus compose box via JavaScript.",
                recipient_selected=recipient_selected,
            )
        await asyncio.sleep(0.1)
        await self._page.keyboard.type(message, delay=15)
        await asyncio.sleep(0.3)

        # patchright actionability also blocks send_button.click(). Use JS click
        # on any visible, enabled send button; fall back to Enter key which
        # LinkedIn's composer also accepts for submission.
        #
        # DOM dependency: we need btn.click() on the element reference — not
        # achievable via innerText or URL navigation. Selectors use only type,
        # aria-label, and data attributes (no layout class names).
        await asyncio.sleep(1.0)  # allow React to process keyboard input
        sent_via_js = await self._page.evaluate(
            """() => {
                const btn = Array.from(document.querySelectorAll(
                    'button[type="submit"], button[aria-label*="Send"], button[aria-label*="send"],'
                    + 'button[data-control-name="send"]'
                )).find(b => !b.disabled && (b.offsetWidth || b.offsetHeight || b.getClientRects().length));
                if (!btn) return false;
                btn.click();
                return true;
            }"""
        )
        if not sent_via_js:
            await self._page.keyboard.press("Enter")

        if not await self._message_text_visible(message):
            await self._dismiss_message_ui()
            return self._message_action_result(
                self._page.url,
                "send_unavailable",
                "LinkedIn did not confirm that the message was sent.",
                recipient_selected=recipient_selected,
            )

        return self._message_action_result(
            self._page.url,
            "sent",
            "Message sent.",
            recipient_selected=recipient_selected,
            sent=True,
        )

    async def _extract_root_content(
        self,
        selectors: list[str],
    ) -> dict[str, Any]:
        """Extract innerText and raw anchor metadata from the first matching root."""
        result = await self._page.evaluate(
            """({ selectors }) => {
                const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                const containerSelector = 'section, article, li, div';
                const headingSelector = 'h1, h2, h3';
                const directHeadingSelector = ':scope > h1, :scope > h2, :scope > h3';
                const MAX_HEADING_CONTAINERS = 300;
                const MAX_REFERENCE_ANCHORS = 500;

                const getHeadingText = element => {
                    if (!element) return '';

                    const heading =
                        element.matches && element.matches(headingSelector)
                            ? element
                            : element.querySelector
                              ? element.querySelector(directHeadingSelector)
                              : null;

                    return normalize(heading?.innerText || heading?.textContent);
                };

                const getPreviousHeading = node => {
                    let sibling = node?.previousElementSibling || null;
                    for (let index = 0; sibling && index < 3; index += 1) {
                        const heading = getHeadingText(sibling);
                        if (heading) {
                            return heading;
                        }
                        sibling = sibling.previousElementSibling;
                    }
                    return '';
                };

                const root = selectors
                    .map(selector => document.querySelector(selector))
                    .find(Boolean);
                const source = root ? 'root' : 'body';
                const container = root || document.body;
                const text = container ? (container.innerText || '').trim() : '';
                const headingMap = new WeakMap();

                const candidateContainers = [
                    container,
                    ...Array.from(container.querySelectorAll(containerSelector)).slice(
                        0,
                        MAX_HEADING_CONTAINERS,
                    ),
                ];
                candidateContainers.forEach(node => {
                    const ownHeading = getHeadingText(node);
                    const previousHeading = getPreviousHeading(node);
                    const heading = ownHeading || previousHeading;
                    if (heading) {
                        headingMap.set(node, heading);
                    }
                });

                const findHeading = element => {
                    let current = element.closest(containerSelector) || container;
                    for (let depth = 0; current && depth < 4; depth += 1) {
                        const heading = headingMap.get(current);
                        if (heading) {
                            return heading;
                        }
                        if (current === container) {
                            break;
                        }
                        current = current.parentElement?.closest(containerSelector) || null;
                    }
                    return '';
                };

                const references = Array.from(container.querySelectorAll('a[href]'))
                    .slice(0, MAX_REFERENCE_ANCHORS)
                    .map(anchor => {
                        const rawHref = (anchor.getAttribute('href') || '').trim();
                        if (!rawHref || rawHref === '#') {
                            return null;
                        }

                        const href = rawHref.startsWith('#')
                            ? rawHref
                            : (anchor.href || rawHref);

                        return {
                            href,
                            text: normalize(anchor.innerText || anchor.textContent),
                            aria_label: normalize(anchor.getAttribute('aria-label')),
                            title: normalize(anchor.getAttribute('title')),
                            heading: findHeading(anchor),
                            in_article: Boolean(anchor.closest('article')),
                            in_nav: Boolean(anchor.closest('nav')),
                            in_footer: Boolean(anchor.closest('footer')),
                        };
                    })
                    .filter(Boolean);

                return { source, text, references };
            }""",
            {"selectors": selectors},
        )
        return result
