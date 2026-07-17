"""Connection operations: reading action signals and sending invites.

Extracted from the monolithic extractor as a feature mixin composed into
``LinkedInExtractor``. Covers reading a profile's action signals (connect /
pending / follow state), opening the More menu, submitting the invite dialog
(with an optional note), and accepting incoming requests.

Connection-state *analysis* lives in ``connection.py`` (locale-independent DOM
signals); this module drives the *actions* on top of that analysis.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.scraping.connection import ActionSignals

from .base import (
    _ACTION_SIGNALS_JS,
    _DIALOG_TEXTAREA_SELECTOR,
    _connection_result,
)
from .dom import DIALOG_SELECTOR as _DIALOG_SELECTOR

if TYPE_CHECKING:
    from linkedin_mcp_server.callbacks import ProgressCallback

logger = logging.getLogger(__name__)


class ConnectionMixin:
    """Connect/invite/accept operations on a person's profile.

    Mixed into ``LinkedInExtractor``; ``_page`` and the navigation/dialog/menu
    helpers below are provided by the extractor at runtime.
    """

    _page: Page

    if TYPE_CHECKING:

        async def _navigate_to_page(self, url: str) -> None: ...

        async def _dialog_is_open(self, *, timeout: int = 1000) -> bool: ...

        async def _click_dialog_primary_button(
            self, *, timeout: int = 5000
        ) -> bool: ...

        async def _fill_dialog_textarea(
            self, value: str, *, timeout: int = 5000
        ) -> bool: ...

        async def _dismiss_dialog(self) -> None: ...

        async def _get_premium_upsell_message(
            self, *, timeout: int = 2500
        ) -> str | None: ...

        async def _open_more_menu(self) -> bool: ...

        async def _click_incoming_accept(self) -> bool: ...

        async def scrape_person(
            self,
            username: str,
            requested: set[str],
            callbacks: ProgressCallback | None = None,
            max_scrolls: int | None = None,
            *,
            main_profile_already_loaded: bool = False,
        ) -> dict[str, Any]: ...

    async def _read_action_signals(self, username: str) -> ActionSignals:
        """Read locale-independent structural signals for a profile's
        relationship state.

        Detection uses URL patterns and ARIA attribute presence only — never
        text values — per the AGENTS.md Scraping Rules. The vanityName invite
        anchor is searched document-wide because LinkedIn renders the More
        menu's contents in a portal-mounted ``[role='menu']`` outside ``<main>``;
        the URL is uniquely scoped to the target user, so document-wide
        search introduces no false positives. The compose anchor used for
        action-root discovery is scoped to ``<main>`` to avoid the
        portal-rendered "Send profile in a message" anchor that appears
        inside the More menu after click.
        """
        data = await self._page.evaluate(_ACTION_SIGNALS_JS, username)
        if not isinstance(data, dict):
            return ActionSignals(
                has_invite_anchor=False,
                has_compose_anchor_in_action_root=False,
                has_edit_intro_anchor=False,
                has_labeled_action_button=False,
                has_labeled_action_anchor=False,
                has_incoming_action_row=False,
            )
        return ActionSignals(
            has_invite_anchor=bool(data.get("hasInvite")),
            has_compose_anchor_in_action_root=bool(data.get("hasComposeInActionRoot")),
            has_edit_intro_anchor=bool(data.get("hasEditIntro")),
            has_labeled_action_button=bool(data.get("hasLabeledActionButton")),
            has_labeled_action_anchor=bool(data.get("hasLabeledActionAnchor")),
            has_incoming_action_row=bool(data.get("hasIncomingActionRow")),
        )

    async def _submit_invite_dialog(
        self, note: str | None
    ) -> tuple[bool, bool, str | None]:
        """Submit the invite dialog opened by the custom-invite deeplink.

        Returns ``(submitted, note_sent, note_limit_message)``.

        ``note_sent`` reports *delivery*, not textarea fill — it stays
        False on any failure path, including the Premium upsell that
        LinkedIn shows when the free personalized-note quota is exhausted.
        ``note_limit_message`` is the raw LinkedIn Premium dialog text when
        the upsell was detected; in that case ``submitted`` is False, the
        dialog is dismissed, and callers should surface that text directly.

        All interaction uses structural selectors and positional indexing
        — no localized text matching. Owns dialog cleanup: the dialog is
        dismissed on every failure path, callers must not dismiss again.
        """
        if not await self._dialog_is_open(timeout=5000):
            return False, False, None

        note_filled = False
        if note:
            textarea_count = await self._page.locator(_DIALOG_TEXTAREA_SELECTOR).count()
            if textarea_count == 0:
                # Reveal the note textarea via the secondary action.
                # Two layouts are now in the wild and both place "Add a
                # note" at index ``btn_count - 2``:
                #   * Legacy invite dialog (3 buttons): dismiss, secondary
                #     "Add a note", primary "Send" -> nth(1) is secondary.
                #   * "Add a note to your invitation?" gating dialog (2
                #     buttons, rolled out 2026-05): "Add a note",
                #     "Send without a note" -> nth(0) is the only path
                #     that mounts the textarea. See issue #455.
                # If LinkedIn ever serves a 2-button dismiss/primary
                # no-note layout, the click below misroutes to dismiss;
                # the textarea-presence recheck via _fill_dialog_textarea
                # then fails and the caller returns connect_unavailable
                # without sending — the same outcome as today.
                buttons = self._page.locator(
                    f"{_DIALOG_SELECTOR} button, {_DIALOG_SELECTOR} [role='button']"
                )
                btn_count = await buttons.count()
                if btn_count >= 2:
                    await buttons.nth(btn_count - 2).click()
                    try:
                        await self._page.wait_for_selector(
                            _DIALOG_TEXTAREA_SELECTOR,
                            state="visible",
                            timeout=3000,
                        )
                    except PlaywrightTimeoutError:
                        logger.debug("Note textarea did not appear")
                    note_limit_message = await self._get_premium_upsell_message()
                    if note_limit_message is not None:
                        logger.info("Premium upsell blocked opening invite note editor")
                        await self._dismiss_dialog()
                        return False, False, note_limit_message

            note_filled = await self._fill_dialog_textarea(note)
            if not note_filled:
                note_limit_message = await self._get_premium_upsell_message()
                if note_limit_message is not None:
                    logger.info("Premium upsell blocked filling invite note")
                    await self._dismiss_dialog()
                    return False, False, note_limit_message
                await self._dismiss_dialog()
                return False, False, None

        sent = await self._click_dialog_primary_button()
        if not sent:
            # Fallback: focus the primary button positionally so a subsequent
            # Enter targets it instead of a focused textarea (where Enter
            # would just insert a newline).
            buttons = self._page.locator(
                f"{_DIALOG_SELECTOR} button, {_DIALOG_SELECTOR} [role='button']"
            )
            btn_count = await buttons.count()
            if btn_count > 0:
                try:
                    await buttons.nth(btn_count - 1).focus()
                    await self._page.keyboard.press("Enter")
                    sent = not await self._dialog_is_open(timeout=2000)
                except Exception:
                    logger.debug("Keyboard submit fallback failed", exc_info=True)
            if not sent:
                # The Send click can also fail because LinkedIn swapped the
                # invite dialog for the Premium upsell at submit time — the
                # original primary button is then detached or pointer-event
                # covered, so the click raises or times out. Check for the
                # upsell here so we surface the raw note-limit message
                # instead of dismissing silently and returning
                # connect_unavailable.
                if note:
                    note_limit_message = await self._get_premium_upsell_message()
                    if note_limit_message is not None:
                        logger.info(
                            "Premium upsell modal intercepted invite submit click"
                        )
                        await self._dismiss_dialog()
                        return False, False, note_limit_message
                await self._dismiss_dialog()
                return False, False, None

        # LinkedIn may swap the invite dialog for a Premium upsell when the
        # free note quota is exhausted. The textarea was filled but the
        # invite was not delivered — surface LinkedIn's raw dialog text.
        if note:
            note_limit_message = await self._get_premium_upsell_message()
            if note_limit_message is not None:
                logger.info("Premium upsell modal intercepted invite submit")
                await self._dismiss_dialog()
                return False, False, note_limit_message

        try:
            await self._page.wait_for_selector(
                _DIALOG_SELECTOR, state="hidden", timeout=5000
            )
        except PlaywrightTimeoutError:
            logger.debug("Invite dialog did not close after submit")

        return True, note_filled, None

    async def _probe_invite_note_limit(self) -> str | None:
        """Open the note editor only to read a Premium note-quota message.

        This is used when the profile did not expose the normal invite anchor.
        Navigating to the custom-invite deeplink and opening the note editor is
        non-destructive, but submitting would weaken the write gate for
        follow-only/unavailable profiles. Therefore this helper never clicks
        the primary Send button: it returns the raw LinkedIn Premium dialog
        text if LinkedIn shows it while opening the note editor, then
        dismisses the dialog.
        """
        if not await self._dialog_is_open(timeout=5000):
            return None
        note_limit_message = await self._get_premium_upsell_message(timeout=500)
        if note_limit_message is not None:
            await self._dismiss_dialog()
            return note_limit_message

        try:
            textarea_count = await self._page.locator(_DIALOG_TEXTAREA_SELECTOR).count()
        except Exception:
            textarea_count = 0
        if textarea_count > 0:
            await self._dismiss_dialog()
            return None

        buttons = self._page.locator(
            f"{_DIALOG_SELECTOR} button, {_DIALOG_SELECTOR} [role='button']"
        )
        try:
            btn_count = await buttons.count()
        except Exception:
            btn_count = 0
        if btn_count >= 3:
            try:
                await buttons.nth(btn_count - 2).click()
            except Exception:
                logger.debug("Could not open invite note editor", exc_info=True)
            try:
                await self._page.wait_for_selector(
                    _DIALOG_TEXTAREA_SELECTOR,
                    state="visible",
                    timeout=3000,
                )
            except PlaywrightTimeoutError:
                logger.debug("Note textarea did not appear during quota probe")

        note_limit_message = await self._get_premium_upsell_message()
        await self._dismiss_dialog()
        return note_limit_message

    async def connect_with_person(
        self,
        username: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Send a LinkedIn connection request or accept an incoming one.

        Detection is locale-independent: classification uses URL patterns
        (vanityName invite anchor, edit-intro anchor) and ARIA-attribute
        presence on top-card buttons (`aria-label` for primary actions,
        `aria-expanded` for the More-menu opener). The deeplink-submit
        path is gated strictly on `has_invite_anchor=True` *after* the
        optional More-menu retry, so Pending and follow-only profiles
        cannot trigger a write. If a note was requested but no invite
        anchor is visible, the custom-invite deeplink may still be opened
        only as a non-submitting note-quota probe. Sending itself uses the
        ``/preload/custom-invite/?vanityName=`` deeplink, which works
        whether the user-visible Connect button is in the action bar
        or buried under the More menu.
        """
        from linkedin_mcp_server.scraping.connection import detect_connection_state

        url = f"https://www.linkedin.com/in/{username}/"

        profile = await self.scrape_person(username, {"main_profile"})
        page_text = profile.get("sections", {}).get("main_profile", "")
        if not page_text:
            return _connection_result(
                url, "unavailable", "Could not read profile page."
            )

        signals = await self._read_action_signals(username)
        state = detect_connection_state(signals)
        logger.info(
            "Connection signals for %s: state=%s signals=%s", username, state, signals
        )

        if state == "self_profile":
            return _connection_result(
                url,
                "connect_unavailable",
                "Cannot send a connection request to your own profile.",
                profile=page_text,
            )
        if state == "already_connected":
            return _connection_result(
                url,
                "already_connected",
                "You are already connected with this profile.",
                profile=page_text,
            )
        if state == "pending":
            return _connection_result(
                url,
                "pending",
                "A connection request is already pending for this profile.",
                profile=page_text,
            )

        if state == "incoming_request":
            # Accept clicks the first labeled button in the fingerprinted
            # row. There is deliberately no locale-text fallback: clicking
            # a button matched by exact text anywhere in the page risks
            # hitting the wrong control (or the Ignore button in another
            # locale), and accepting/ignoring is irreversible. When the
            # fingerprint does not match we report send_failed rather than
            # guess.
            clicked = await self._click_incoming_accept()
            if not clicked:
                return _connection_result(
                    url,
                    "send_failed",
                    "Could not find or click the Accept button.",
                    profile=page_text,
                )
            # LinkedIn propagates the accepted state asynchronously; an
            # immediate re-read can still render the old top card and
            # would report send_failed for a successful accept (observed
            # live 2026-06-11). Verify with one settle retry.
            verified_text = ""
            verified_state = None
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(3.0)
                verified = await self.scrape_person(username, {"main_profile"})
                verified_text = verified.get("sections", {}).get("main_profile", "")
                verified_signals = await self._read_action_signals(username)
                verified_state = detect_connection_state(verified_signals)
                if verified_state == "already_connected":
                    break
            if verified_state != "already_connected":
                return _connection_result(
                    url,
                    "send_failed",
                    "Accepted, but the profile did not transition to 1st-degree.",
                    profile=verified_text or page_text,
                )
            return _connection_result(
                url,
                "accepted",
                "Connection request accepted.",
                profile=verified_text,
            )

        # Follow-only profiles may have Connect hidden under the More menu
        # (high-follower / creator-mode profiles). Try opening it and
        # re-reading signals; if the vanityName invite anchor surfaces in
        # the menu, we can proceed with the deeplink. (The
        # has_invite_anchor=False guard is implicit: detect_connection_state
        # only returns "follow_only" after the has_invite_anchor branch
        # has already failed, so reaching this branch already implies it.)
        if state == "follow_only":
            opened = await self._open_more_menu()
            if opened:
                signals = await self._read_action_signals(username)
                # Close the menu before any subsequent navigation so it
                # doesn't intercept the upcoming page transition.
                try:
                    await self._page.keyboard.press("Escape")
                except Exception:
                    logger.debug("Escape after More-menu reread failed", exc_info=True)
                logger.info("Post-More signals for %s: signals=%s", username, signals)

        invite_url = (
            "https://www.linkedin.com/preload/custom-invite/"
            f"?vanityName={quote_plus(username)}"
        )

        # Write-gate: submit only when LinkedIn exposed the vanityName invite
        # anchor. When a note is requested without that anchor, open the
        # deeplink only as a non-submitting probe so we can report the Premium
        # note-quota block without accidentally sending from a follow-only or
        # otherwise unavailable profile.
        if not signals.has_invite_anchor:
            if note:
                logger.info(
                    "No visible invite anchor for %s; probing custom-invite deeplink "
                    "because a personalized note was requested",
                    username,
                )
                await self._navigate_to_page(invite_url)
                note_limit_message = await self._probe_invite_note_limit()
                if note_limit_message is not None:
                    return _connection_result(
                        url,
                        "custom_note_limit_reached",
                        note_limit_message,
                        note_sent=False,
                        profile=page_text,
                    )
            return _connection_result(
                url,
                "connect_unavailable",
                "LinkedIn did not expose a usable Connect action for this profile.",
                profile=page_text,
            )

        await self._navigate_to_page(invite_url)

        submitted, note_sent, note_limit_message = await self._submit_invite_dialog(
            note
        )
        if note_limit_message is not None:
            return _connection_result(
                url,
                "custom_note_limit_reached",
                note_limit_message,
                note_sent=False,
                profile=page_text,
            )
        if not submitted:
            return _connection_result(
                url,
                "connect_unavailable",
                "LinkedIn did not open a usable invite dialog for this profile.",
                profile=page_text,
            )

        verified = await self.scrape_person(username, {"main_profile"})
        verified_text = verified.get("sections", {}).get("main_profile", "")
        verified_signals = await self._read_action_signals(username)
        verified_state = detect_connection_state(verified_signals)

        if verified_signals.has_invite_anchor:
            return _connection_result(
                url,
                "send_failed",
                "Submitted the invite dialog but the profile still exposes Connect.",
                note_sent=note_sent,
                profile=verified_text or page_text,
            )

        return _connection_result(
            url,
            "connected",
            "Connection request sent."
            + (f" State after send: {verified_state}." if verified_state else ""),
            note_sent=note_sent,
            profile=verified_text or page_text,
        )
