"""Own-profile editing operations (intro, about, and section entries).

Extracted from the monolithic extractor: everything needed to open LinkedIn's
edit overlays, fill localized form fields, and save. Field labels, dropdown
values, and button names resolve through :mod:`.i18n`, so these operations work
regardless of the account's UI language.

The class is a mixin composed into ``LinkedInExtractor`` — it operates on the
same page/session and relies on the extractor's navigation helpers.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.core.utils import detect_rate_limit

from . import i18n
from .dom import DIALOG_SELECTOR as _DIALOG_SELECTOR

logger = logging.getLogger(__name__)


class ProfileEditMixin:
    """Edit operations for the authenticated user's own profile.

    Mixed into ``LinkedInExtractor``; the attributes and the navigation stub
    below are provided by the extractor at runtime.
    """

    # Provided by LinkedInExtractor.__init__
    _page: Page
    _my_username_cache: str | None

    async def _navigate_to_page(self, url: str) -> None:
        """Navigation helper provided by ``LinkedInExtractor``.

        The subclass's own definition wins in the MRO; this stub only anchors
        the contract for type checking and fails loudly if the mixin is ever
        used standalone.
        """
        raise NotImplementedError("ProfileEditMixin requires LinkedInExtractor")

    async def _fill_field_by_label(
        self,
        label_text: str,
        value: str,
        *,
        scope: str = '[role="dialog"], dialog[open], main',
        exact: bool = False,
    ) -> bool:
        """Find an input/textarea by its associated label text and fill it.

        Searches for labels whose text matches (contains or exact), then locates
        the associated input via the for attribute or by being a child element.
        """
        filled = await self._page.evaluate(
            """({ labelText, value, scope, exact }) => {
                const normalize = v => (v || '').replace(/\\s+/g, ' ').trim();
                const roots = Array.from(document.querySelectorAll(scope));
                const searchRoots = roots.length ? roots : [document.body];
                const labels = searchRoots.flatMap(r => Array.from(r.querySelectorAll('label')));
                for (const label of labels) {
                    const text = normalize(label.innerText || label.textContent);
                    const match = exact
                        ? text.toLowerCase() === labelText.toLowerCase()
                        : text.toLowerCase().includes(labelText.toLowerCase());
                    if (!match) continue;

                    let input = null;
                    const forAttr = label.getAttribute('for');
                    if (forAttr) {
                        input = document.getElementById(forAttr);
                    }
                    if (!input) {
                        input = label.querySelector('input, textarea, select');
                    }
                    if (!input) {
                        const parent = label.closest('div');
                        if (parent) input = parent.querySelector('input, textarea, select');
                    }
                    if (input) {
                        const proto = input.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : input.tagName === 'SELECT'
                                ? window.HTMLSelectElement.prototype
                                : window.HTMLInputElement.prototype;
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            proto, 'value'
                        ).set;
                        nativeInputValueSetter.call(input, value);
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                }
                return false;
            }""",
            {"labelText": label_text, "value": value, "scope": scope, "exact": exact},
        )
        return bool(filled)

    async def _select_dropdown_by_label(
        self,
        label_text: str,
        option_text: str,
        *,
        scope: str = '[role="dialog"], dialog[open], main',
    ) -> bool:
        """Find a dropdown (select or custom listbox) by label and choose an option."""
        selected = await self._page.evaluate(
            """({ labelText, optionText, scope }) => {
                const normalize = v => (v || '').replace(/\\s+/g, ' ').trim();
                const roots = Array.from(document.querySelectorAll(scope));
                const searchRoots = roots.length ? roots : [document.body];
                const labels = searchRoots.flatMap(r => Array.from(r.querySelectorAll('label')));
                for (const label of labels) {
                    const text = normalize(label.innerText || label.textContent);
                    if (!text.toLowerCase().includes(labelText.toLowerCase())) continue;

                    let select = null;
                    const forAttr = label.getAttribute('for');
                    if (forAttr) select = document.getElementById(forAttr);
                    if (!select) select = label.querySelector('select');
                    if (!select) {
                        const parent = label.closest('div');
                        if (parent) select = parent.querySelector('select');
                    }

                    if (select && select.tagName === 'SELECT') {
                        const options = Array.from(select.options);
                        const match = options.find(o =>
                            normalize(o.text).toLowerCase().includes(optionText.toLowerCase())
                        );
                        if (match) {
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLSelectElement.prototype, 'value'
                            ).set;
                            nativeSetter.call(select, match.value);
                            select.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                    }
                }
                return false;
            }""",
            {"labelText": label_text, "optionText": option_text, "scope": scope},
        )
        return bool(selected)

    async def _click_save_in_dialog(self, *, timeout: int = 5000) -> bool:
        """Click the Save/primary button inside the open dialog.

        Matches by the button's *accessible name* (robust to nested
        screen-reader spans and aria-labels that break anchored text matching)
        against a per-locale table of save labels (EN + PT-BR at least), scoped
        to the topmost dialog. Disabled buttons are skipped — a click on one
        times out and the next candidate is tried.
        """
        dialog = self._page.locator(_DIALOG_SELECTOR).last
        for name in i18n.SAVE_BUTTONS:
            btn = dialog.get_by_role(
                "button", name=re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
            )
            if await btn.count() == 0:
                continue
            # A Premium upsell panel can overlap the footer and intercept a
            # normal click, so retry with force (bypasses the overlap check).
            for force in (False, True):
                try:
                    await btn.first.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    await btn.first.click(timeout=timeout, force=force)
                    await asyncio.sleep(1.5)
                    return True
                except Exception:
                    logger.debug(
                        "Save click failed for %r (force=%s)",
                        name,
                        force,
                        exc_info=True,
                    )

        # Last resort: dispatch a click via JS on a save-labelled, enabled
        # button in the topmost dialog — ignores any overlapping element.
        clicked = await self._page.evaluate(
            """(words) => {
                const norm = v => (v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const wanted = words.map(w => w.toLowerCase());
                const dialogs = document.querySelectorAll('[role="dialog"], dialog[open]');
                const scope = dialogs.length ? dialogs[dialogs.length - 1] : document;
                const btns = Array.from(scope.querySelectorAll('button, [role="button"]'));
                const disabled = b => b.disabled || b.getAttribute('aria-disabled') === 'true';
                const nameOf = b => norm(
                    (b.getAttribute('aria-label') || '') + ' ' + (b.innerText || b.textContent || '')
                ).split(/\\s+/);
                const match = btns.find(
                    b => !disabled(b) && nameOf(b).some(w => wanted.includes(w))
                );
                if (match) {
                    match.scrollIntoView({ block: 'center' });
                    match.click();
                    return true;
                }
                return false;
            }""",
            i18n.SAVE_BUTTONS,
        )
        if clicked:
            await asyncio.sleep(1.5)
            return True
        return False

    async def _dialog_buttons(self) -> list[dict[str, Any]]:
        """Return the buttons in the topmost open dialog (diagnostics).

        Surfaced in edit responses when the Save button could not be clicked,
        so the exact button texts, aria-labels, and disabled state are known
        without blind guessing.
        """
        try:
            buttons = await self._page.evaluate(
                """() => {
                    const norm = v => (v || '').replace(/\\s+/g, ' ').trim();
                    const dialogs = document.querySelectorAll('[role="dialog"], dialog[open]');
                    const scope = dialogs.length ? dialogs[dialogs.length - 1] : document;
                    return Array.from(
                        scope.querySelectorAll('button, [role="button"]')
                    ).map(b => ({
                        text: norm(b.innerText || b.textContent).slice(0, 40),
                        aria: norm(b.getAttribute('aria-label') || '').slice(0, 40),
                        disabled: !!(b.disabled || b.getAttribute('aria-disabled') === 'true'),
                    })).slice(0, 30);
                }"""
            )
            return buttons if isinstance(buttons, list) else []
        except Exception:
            return []

    async def _edit_anchors(self) -> list[str]:
        """Return profile-page hrefs pointing at edit/overlay controls.

        Diagnostic for when an edit pencil cannot be located: navigates to the
        profile and reports the real href pattern LinkedIn uses per section, so
        the pencil selector can be corrected without blind guessing.
        """
        try:
            username = await self._resolve_my_username()
            await self._navigate_to_page(f"https://www.linkedin.com/in/{username}/")
            await asyncio.sleep(1.0)
            hrefs = await self._page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.getAttribute('href') || '')
                    .filter(h => /\\/(edit|overlay)\\//.test(h))
                    .filter((h, i, arr) => arr.indexOf(h) === i)
                    .slice(0, 40)"""
            )
            return hrefs if isinstance(hrefs, list) else []
        except Exception:
            return []

    async def _about_diagnostics(self) -> dict[str, Any]:
        """Structure snapshot for the About editor (diagnostics on failure)."""
        try:
            return await self._page.evaluate(
                """() => ({
                    url: location.href,
                    dialogs: document.querySelectorAll('[role="dialog"], dialog[open]').length,
                    contenteditables: document.querySelectorAll('[contenteditable="true"]').length,
                    textareas: document.querySelectorAll('textarea').length,
                    buttons: Array.from(document.querySelectorAll(
                        '[role="dialog"] button, dialog button, main button'
                    )).map(b => ({
                        text: (b.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 30),
                        aria: (b.getAttribute('aria-label') || '').slice(0, 30),
                    })).filter(b => b.text || b.aria).slice(0, 20),
                })"""
            )
        except Exception:
            return {}

    async def _open_edit_overlay(
        self,
        *,
        overlay_url: str,
        pencil_href: str | None = None,
        timeout: int = 15000,
    ) -> bool:
        """Open a profile edit/create overlay reliably.

        LinkedIn's edit modals frequently do not mount on a cold direct
        navigation to the overlay URL — the client router only opens the
        dialog as an in-app transition. So warm the SPA by loading the
        profile page first, then prefer clicking the in-page pencil anchor
        (a genuine in-app action), falling back to navigating to the overlay
        route with the SPA now warm. Returns True once a dialog is visible.
        """
        username = await self._resolve_my_username()
        await self._navigate_to_page(f"https://www.linkedin.com/in/{username}/")
        await detect_rate_limit(self._page)
        await asyncio.sleep(1.0)

        if pencil_href:
            anchor = self._page.locator(f'a[href*="{pencil_href}"]')
            try:
                if await anchor.count() > 0:
                    target = anchor.first
                    try:
                        await target.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    await target.click(timeout=5000)
                    await self._page.wait_for_selector(
                        _DIALOG_SELECTOR, timeout=timeout
                    )
                    await asyncio.sleep(1.0)
                    return True
            except Exception:
                logger.debug(
                    "Pencil-click overlay open failed for %s",
                    pencil_href,
                    exc_info=True,
                )

        # Fallback: navigate to the overlay route with the SPA now warm.
        await self._navigate_to_page(overlay_url)
        await detect_rate_limit(self._page)
        try:
            await self._page.wait_for_selector(
                "dialog[open], [role='dialog'], main form", timeout=timeout
            )
            await asyncio.sleep(1.0)
            return True
        except PlaywrightTimeoutError:
            return False

    async def _fill_field_by_labels(
        self, labels: list[str], value: str, *, exact: bool = False
    ) -> bool:
        """Fill the first field whose label matches any of *labels*.

        LinkedIn renders field labels in the account's UI language, so each
        logical field is tried against several localized aliases (e.g.
        ["Headline", "Título"]) until one matches.
        """
        for label in labels:
            if await self._fill_field_by_label(label, value, exact=exact):
                return True
        return False

    async def _fill_localized(self, label: str, value: str) -> bool:
        """Fill a field given its canonical English label.

        Expands the label to its localized aliases via :mod:`i18n`, then tries
        accessible-name matching (reaches aria-label-only fields) before falling
        back to ``<label>`` matching.
        """
        aliases = i18n.field_aliases(label)
        return await self._fill_by_accessible_name(
            aliases, value
        ) or await self._fill_field_by_labels(aliases, value)

    async def _select_localized(self, label: str, value: str) -> bool:
        """Select a dropdown option given canonical English label and value.

        Both the label and the option value are expanded to localized aliases
        (e.g. label "Employment type"/"Tipo de vínculo", value "January"/
        "Janeiro"), and every combination is tried until one matches.
        """
        for label_alias in i18n.field_aliases(label):
            for value_alias in i18n.value_aliases(value):
                if await self._select_dropdown_by_label(label_alias, value_alias):
                    return True
        return False

    async def _fill_by_accessible_name(self, labels: list[str], value: str) -> bool:
        """Fill a dialog control matched by its accessible name.

        Unlike ``_fill_field_by_label`` (which only inspects ``<label>``
        elements), this resolves each control's accessible name from
        ``aria-label``, ``aria-labelledby``, an associated/wrapping
        ``<label>``, or ``placeholder`` — so it reaches fields like the
        headline textarea that LinkedIn labels via ``aria-label`` only.
        Matches case-insensitively against any of *labels* (localized
        aliases), and handles input, textarea, and contenteditable editors.
        """
        result = await self._page.evaluate(
            """({ labels, value }) => {
                const norm = v => (v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const wants = labels.map(norm).filter(Boolean);
                const dialogs = document.querySelectorAll('[role="dialog"], dialog[open]');
                const scope = dialogs.length ? dialogs[dialogs.length - 1] : document;
                const accName = el => {
                    const al = el.getAttribute('aria-label');
                    if (al) return al;
                    const lb = el.getAttribute('aria-labelledby');
                    if (lb) {
                        const t = lb.split(/\\s+/).map(id => {
                            const e = document.getElementById(id);
                            return e ? (e.innerText || e.textContent || '') : '';
                        }).join(' ');
                        if (t.trim()) return t;
                    }
                    if (el.id) {
                        const lab = scope.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        if (lab) return lab.innerText || lab.textContent || '';
                    }
                    const wrap = el.closest('label');
                    if (wrap) return wrap.innerText || wrap.textContent || '';
                    return el.getAttribute('placeholder') || '';
                };
                const controls = Array.from(scope.querySelectorAll(
                    'input, textarea, [contenteditable="true"], [role="combobox"], [role="textbox"]'
                ));
                for (const el of controls) {
                    const name = norm(accName(el));
                    if (!name || !wants.some(w => name.includes(w))) continue;
                    if (el.isContentEditable) {
                        el.focus();
                        el.textContent = value;
                        el.dispatchEvent(new InputEvent('input', { bubbles: true }));
                        return { ok: true, tag: 'contenteditable', name };
                    }
                    const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, value);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return { ok: true, tag: el.tagName, name };
                }
                return { ok: false };
            }""",
            {"labels": labels, "value": value},
        )
        if isinstance(result, dict) and result.get("ok"):
            logger.debug(
                "Filled control %r (accessible name %r)",
                result.get("tag"),
                result.get("name"),
            )
            return True
        return False

    async def _dialog_field_labels(self) -> list[dict[str, Any]]:
        """Return the controls in the open dialog with their accessible names.

        Surfaced in edit responses when no field could be filled, so the exact
        control tags and localized accessible names are known without blind
        guessing. Reports every input/textarea/contenteditable/combobox — not
        just ``<label>`` elements — since key fields (e.g. the headline) are
        labeled via ``aria-label`` rather than a ``<label>`` tag.
        """
        try:
            controls = await self._page.evaluate(
                """() => {
                    const dialogs = document.querySelectorAll('[role="dialog"], dialog[open]');
                    const scope = dialogs.length ? dialogs[dialogs.length - 1] : document;
                    const accName = el => {
                        const al = el.getAttribute('aria-label');
                        if (al) return al;
                        const lb = el.getAttribute('aria-labelledby');
                        if (lb) {
                            const t = lb.split(/\\s+/).map(id => {
                                const e = document.getElementById(id);
                                return e ? (e.innerText || e.textContent || '') : '';
                            }).join(' ');
                            if (t.trim()) return t;
                        }
                        if (el.id) {
                            const lab = scope.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                            if (lab) return lab.innerText || lab.textContent || '';
                        }
                        const wrap = el.closest('label');
                        if (wrap) return wrap.innerText || wrap.textContent || '';
                        return el.getAttribute('placeholder') || '';
                    };
                    return Array.from(scope.querySelectorAll(
                        'input, textarea, [contenteditable="true"], [role="combobox"], [role="textbox"]'
                    )).map(el => ({
                        tag: el.tagName + (el.type ? '[' + el.type + ']' : ''),
                        name: (accName(el) || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
                        editable: el.isContentEditable,
                    })).slice(0, 40);
                }"""
            )
            return controls if isinstance(controls, list) else []
        except Exception:
            return []

    async def edit_profile_intro(
        self,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        headline: str | None = None,
        location: str | None = None,
        industry: str | None = None,
    ) -> dict[str, Any]:
        """Edit the profile intro section (name, headline, location, industry).

        Navigates to the intro edit overlay and fills in the provided fields.
        Fields set to None are left unchanged.
        """
        username = await self._resolve_my_username()
        url = f"https://www.linkedin.com/in/{username}/overlay/edit/intro/"
        if not await self._open_edit_overlay(
            overlay_url=url, pencil_href="/edit/intro/"
        ):
            return {
                "url": url,
                "status": "edit_failed",
                "message": "Edit intro form did not open.",
                "anchors_seen": await self._edit_anchors(),
            }

        fields_updated: list[str] = []

        # Field labels resolve to localized aliases through the central i18n
        # table; _fill_localized tries accessible-name then <label> matching.
        fill = self._fill_localized

        if first_name is not None:
            if await fill("First name", first_name):
                fields_updated.append("first_name")
        if last_name is not None:
            if await fill("Last name", last_name):
                fields_updated.append("last_name")
        if headline is not None:
            if await fill("Headline", headline):
                fields_updated.append("headline")
            else:
                # In PT-BR (and other locales) the headline is a contenteditable
                # <div> with no accessible name and no linked <label>, so no
                # name/label match reaches it. It is the only rich editor in the
                # intro dialog — target it structurally and fill it directly.
                editor = self._page.locator(
                    '[role="dialog"] [contenteditable="true"], '
                    'dialog [contenteditable="true"]'
                ).first
                try:
                    await editor.wait_for(state="visible", timeout=3000)
                    await editor.click()
                    await editor.fill(headline)
                    fields_updated.append("headline")
                except Exception:
                    logger.debug(
                        "Headline contenteditable fallback failed", exc_info=True
                    )
        if location is not None:
            # Try City first (plain text input) — fills directly without typeahead.
            # Country/Region is a typeahead; trying it first would short-circuit City
            # and silently fail since LinkedIn ignores unconfirmed typeahead values.
            if await fill("City", location):
                fields_updated.append("location")
            elif await fill("Country/Region", location):
                # Country/Region requires selecting from typeahead suggestions
                await asyncio.sleep(1.0)
                typeahead = self._page.locator(
                    '[role="listbox"] [role="option"], [role="listbox"] li'
                )
                if await typeahead.count() > 0:
                    await typeahead.first.click()
                    await asyncio.sleep(0.5)
                    fields_updated.append("location")
            elif await fill("Location", location):
                fields_updated.append("location")
        if industry is not None:
            if await fill("Industry", industry):
                # Industry is a custom autocomplete — must select from the suggestions
                # list so LinkedIn registers the value; DOM value alone is ignored on save.
                # Only count it as updated when a suggestion was actually selected.
                await asyncio.sleep(1.0)
                typeahead = self._page.locator(
                    '[role="listbox"] [role="option"], [role="listbox"] li'
                )
                if await typeahead.count() > 0:
                    await typeahead.first.click()
                    await asyncio.sleep(0.5)
                    fields_updated.append("industry")

        if not fields_updated:
            return {
                "url": url,
                "status": "no_changes",
                "message": "No fields were modified.",
                "labels_seen": await self._dialog_field_labels(),
            }

        saved = await self._click_save_in_dialog()

        if not saved:
            return {
                "url": url,
                "status": "save_failed",
                "message": "Could not find the Save button.",
                "fields_updated": fields_updated,
                "buttons_seen": await self._dialog_buttons(),
            }
        return {
            "url": url,
            "status": "saved",
            "message": f"Updated: {', '.join(fields_updated)}",
            "fields_updated": fields_updated,
        }

    async def edit_profile_about(self, about_text: str) -> dict[str, Any]:
        """Edit the About/Summary section of the profile.

        LinkedIn calls the About section "summary" and its edit control's href
        varies by account (``/edit/about/``, ``/overlay/edit/about/``, or
        ``/edit/forms/summary/...``). Warm the profile SPA, open the editor by
        clicking whichever in-page control exists (falling back to direct
        navigation), then fill the contenteditable/textarea editor and save.
        """
        username = await self._resolve_my_username()
        await self._navigate_to_page(f"https://www.linkedin.com/in/{username}/")
        await detect_rate_limit(self._page)
        await asyncio.sleep(1.0)

        # Open the editor by clicking its in-page control (href pattern varies).
        opened = False
        for frag in ("/edit/forms/summary/", "/overlay/edit/about/", "/edit/about/"):
            anchor = self._page.locator(f'a[href*="{frag}"]')
            if await anchor.count() == 0:
                continue
            try:
                await anchor.first.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                await anchor.first.click(timeout=5000)
                opened = True
                break
            except Exception:
                logger.debug("About control click failed for %s", frag, exc_info=True)

        # Fallback: navigate directly to the summary/about edit routes.
        if not opened:
            for direct in (
                f"https://www.linkedin.com/in/{username}/edit/forms/summary/new/",
                f"https://www.linkedin.com/in/{username}/overlay/edit/about/",
            ):
                await self._navigate_to_page(direct)
                await detect_rate_limit(self._page)
                await asyncio.sleep(1.5)

        # LinkedIn's About editor is a contenteditable div (textarea fallback),
        # rendered either in a dialog or inline on the form page.
        editor = self._page.locator(
            'dialog [contenteditable="true"], [role="dialog"] [contenteditable="true"], '
            'main [contenteditable="true"], [contenteditable="true"], '
            'dialog textarea, [role="dialog"] textarea, main textarea, textarea'
        ).first
        try:
            await editor.wait_for(state="visible", timeout=6000)
            await editor.click()
            await editor.fill(about_text)
        except Exception:
            return {
                "url": self._page.url,
                "status": "edit_failed",
                "message": "Could not locate the About editor.",
                "diagnostics": await self._about_diagnostics(),
            }

        saved = await self._click_save_in_dialog()

        if not saved:
            return {
                "url": self._page.url,
                "status": "save_failed",
                "message": "Could not find the Save button.",
                "diagnostics": await self._about_diagnostics(),
            }
        return {
            "url": self._page.url,
            "status": "saved",
            "message": "About section updated.",
        }

    async def _resolve_my_username(self) -> str:
        """Navigate to /in/me/ and return the logged-in user's username (cached)."""
        if self._my_username_cache:
            return self._my_username_cache
        await self._navigate_to_page("https://www.linkedin.com/in/me/")
        match = re.search(r"/in/([^/?#]+)", self._page.url)
        if not match or match.group(1) == "me":
            raise LinkedInScraperException(
                f"Could not resolve own profile username from {self._page.url}"
            )
        self._my_username_cache = match.group(1)
        return self._my_username_cache

    async def _edit_profile_section_entry(
        self,
        section_slug: str,
        *,
        fields: dict[str, str],
        dropdowns: dict[str, str] | None = None,
        required_fields: set[str] | None = None,
    ) -> dict[str, Any]:
        """Generic method to add a new entry in a profile section.

        Opens the add-new form for a given section, fills fields by label,
        and saves. Works for experience, education, certifications, etc.

        Args:
            required_fields: Field labels that must be filled before saving.
                If any required field is not filled, save is aborted.
        """
        username = await self._resolve_my_username()
        url = f"https://www.linkedin.com/in/{username}/overlay/create/new/?profileFormEntryPoint=PROFILE_SECTION&profileSectionId={section_slug}"
        if not await self._open_edit_overlay(overlay_url=url):
            return {
                "url": url,
                "status": "edit_failed",
                "message": f"Add {section_slug} form did not open.",
                "section": section_slug,
                "anchors_seen": await self._edit_anchors(),
            }

        await asyncio.sleep(0.5)
        fields_filled: list[str] = []

        for label, value in fields.items():
            if await self._fill_localized(label, value):
                fields_filled.append(label)

        if dropdowns:
            for label, value in dropdowns.items():
                if await self._select_localized(label, value):
                    fields_filled.append(label)

        if not fields_filled:
            return {
                "url": url,
                "status": "no_changes",
                "message": f"Could not fill any fields for {section_slug}.",
                "section": section_slug,
                "labels_seen": await self._dialog_field_labels(),
            }

        # Check required fields are filled before saving to avoid partial entries
        if required_fields:
            missing = required_fields - set(fields_filled)
            if missing:
                return {
                    "url": url,
                    "status": "edit_failed",
                    "message": f"Required fields could not be filled: {', '.join(sorted(missing))}",
                    "section": section_slug,
                    "fields_filled": fields_filled,
                }

        saved = await self._click_save_in_dialog()

        if not saved:
            return {
                "url": url,
                "status": "save_failed",
                "message": "Could not find the Save button.",
                "section": section_slug,
                "fields_filled": fields_filled,
                "buttons_seen": await self._dialog_buttons(),
            }
        return {
            "url": url,
            "status": "saved",
            "message": f"Added {section_slug} entry: {', '.join(fields_filled)}",
            "section": section_slug,
            "fields_filled": fields_filled,
        }

    async def add_experience(
        self,
        *,
        title: str,
        company: str,
        start_month: str | None = None,
        start_year: str | None = None,
        end_month: str | None = None,
        end_year: str | None = None,
        description: str | None = None,
        location: str | None = None,
        employment_type: str | None = None,
    ) -> dict[str, Any]:
        """Add a new experience entry to the profile."""
        fields: dict[str, str] = {"Title": title, "Company name": company}
        dropdowns: dict[str, str] = {}
        if location:
            fields["Location"] = location
        if description:
            fields["Description"] = description
        if start_month:
            dropdowns["Start date month"] = start_month
        if start_year:
            dropdowns["Start date year"] = start_year
        if end_month:
            dropdowns["End date month"] = end_month
        if end_year:
            dropdowns["End date year"] = end_year
        if employment_type:
            dropdowns["Employment type"] = employment_type

        return await self._edit_profile_section_entry(
            "EXPERIENCE",
            fields=fields,
            dropdowns=dropdowns,
            required_fields={"Title", "Company name"},
        )

    async def add_education(
        self,
        *,
        school: str,
        degree: str | None = None,
        field_of_study: str | None = None,
        start_year: str | None = None,
        end_year: str | None = None,
        description: str | None = None,
        grade: str | None = None,
        activities: str | None = None,
    ) -> dict[str, Any]:
        """Add a new education entry to the profile."""
        fields: dict[str, str] = {"School": school}
        dropdowns: dict[str, str] = {}
        if degree:
            fields["Degree"] = degree
        if field_of_study:
            fields["Field of study"] = field_of_study
        if start_year:
            dropdowns["Start date year"] = start_year
        if end_year:
            dropdowns["End date year"] = end_year
        if grade:
            fields["Grade"] = grade
        if activities:
            fields["Activities and societies"] = activities
        if description:
            fields["Description"] = description

        return await self._edit_profile_section_entry(
            "EDUCATION", fields=fields, dropdowns=dropdowns, required_fields={"School"}
        )

    async def add_skill(self, skill_name: str) -> dict[str, Any]:
        """Add a skill to the profile."""
        username = await self._resolve_my_username()
        url = f"https://www.linkedin.com/in/{username}/overlay/create/new/?profileFormEntryPoint=PROFILE_SECTION&profileSectionId=SKILLS"
        if not await self._open_edit_overlay(overlay_url=url):
            return {
                "url": url,
                "status": "edit_failed",
                "message": "Add skill form did not open.",
                "anchors_seen": await self._edit_anchors(),
            }

        # Fill the skill name field (localized label aliases via i18n)
        filled = await self._fill_localized("Skill", skill_name)
        if not filled:
            # Try the first input in the dialog
            input_el = self._page.locator('dialog input, [role="dialog"] input').first
            try:
                await input_el.fill(skill_name)
                filled = True
            except Exception:
                pass

        if not filled:
            return {
                "url": url,
                "status": "edit_failed",
                "message": "Could not fill the skill name field.",
            }

        # Wait for typeahead suggestions and select if available
        await asyncio.sleep(1.0)
        typeahead = self._page.locator(
            '[role="listbox"] [role="option"], [role="listbox"] li'
        )
        if await typeahead.count() > 0:
            await typeahead.first.click()
            await asyncio.sleep(0.5)

        saved = await self._click_save_in_dialog()
        await asyncio.sleep(1.0)

        return {
            "url": url,
            "status": "saved" if saved else "save_failed",
            "message": f"Skill '{skill_name}' added."
            if saved
            else "Could not save the skill.",
        }

    async def add_certification(
        self,
        *,
        name: str,
        issuing_organization: str,
        issue_month: str | None = None,
        issue_year: str | None = None,
        expiration_month: str | None = None,
        expiration_year: str | None = None,
        credential_id: str | None = None,
        credential_url: str | None = None,
    ) -> dict[str, Any]:
        """Add a certification to the profile."""
        fields: dict[str, str] = {
            "Name": name,
            "Issuing organization": issuing_organization,
        }
        dropdowns: dict[str, str] = {}
        if credential_id:
            fields["Credential ID"] = credential_id
        if credential_url:
            fields["Credential URL"] = credential_url
        if issue_month:
            dropdowns["Issue date month"] = issue_month
        if issue_year:
            dropdowns["Issue date year"] = issue_year
        if expiration_month:
            dropdowns["Expiration date month"] = expiration_month
        if expiration_year:
            dropdowns["Expiration date year"] = expiration_year

        return await self._edit_profile_section_entry(
            "CERTIFICATIONS",
            fields=fields,
            dropdowns=dropdowns,
            required_fields={"Name", "Issuing organization"},
        )

    async def add_volunteer_experience(
        self,
        *,
        organization: str,
        role: str,
        cause: str | None = None,
        start_month: str | None = None,
        start_year: str | None = None,
        end_month: str | None = None,
        end_year: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Add a volunteer experience entry to the profile."""
        fields: dict[str, str] = {"Organization": organization, "Role": role}
        dropdowns: dict[str, str] = {}
        if cause:
            dropdowns["Cause"] = cause
        if description:
            fields["Description"] = description
        if start_month:
            dropdowns["Start date month"] = start_month
        if start_year:
            dropdowns["Start date year"] = start_year
        if end_month:
            dropdowns["End date month"] = end_month
        if end_year:
            dropdowns["End date year"] = end_year

        return await self._edit_profile_section_entry(
            "VOLUNTEERING_EXPERIENCE",
            fields=fields,
            dropdowns=dropdowns,
            required_fields={"Organization", "Role"},
        )

    async def add_project(
        self,
        *,
        name: str,
        description: str | None = None,
        start_month: str | None = None,
        start_year: str | None = None,
        end_month: str | None = None,
        end_year: str | None = None,
        project_url: str | None = None,
    ) -> dict[str, Any]:
        """Add a project to the profile."""
        fields: dict[str, str] = {"Name": name}
        dropdowns: dict[str, str] = {}
        if description:
            fields["Description"] = description
        if project_url:
            fields["Project URL"] = project_url
        if start_month:
            dropdowns["Start date month"] = start_month
        if start_year:
            dropdowns["Start date year"] = start_year
        if end_month:
            dropdowns["End date month"] = end_month
        if end_year:
            dropdowns["End date year"] = end_year

        return await self._edit_profile_section_entry(
            "PROJECTS",
            fields=fields,
            dropdowns=dropdowns,
            required_fields={"Name"},
        )

    async def add_publication(
        self,
        *,
        title: str,
        publisher: str | None = None,
        publication_date_month: str | None = None,
        publication_date_year: str | None = None,
        description: str | None = None,
        publication_url: str | None = None,
    ) -> dict[str, Any]:
        """Add a publication to the profile."""
        fields: dict[str, str] = {"Title": title}
        dropdowns: dict[str, str] = {}
        if publisher:
            fields["Publisher"] = publisher
        if description:
            fields["Description"] = description
        if publication_url:
            fields["Publication URL"] = publication_url
        if publication_date_month:
            dropdowns["Publication date month"] = publication_date_month
        if publication_date_year:
            dropdowns["Publication date year"] = publication_date_year

        return await self._edit_profile_section_entry(
            "PUBLICATIONS",
            fields=fields,
            dropdowns=dropdowns,
            required_fields={"Title"},
        )

    async def add_course(
        self,
        *,
        name: str,
        number: str | None = None,
        associated_with: str | None = None,
    ) -> dict[str, Any]:
        """Add a course to the profile."""
        fields: dict[str, str] = {"Course name": name}
        dropdowns: dict[str, str] = {}
        if number:
            fields["Number"] = number
        if associated_with:
            # "Associated with" is a <select> of existing education entries;
            # route through _select_dropdown_by_label which searches by option text
            dropdowns["Associated with"] = associated_with

        return await self._edit_profile_section_entry(
            "COURSES",
            fields=fields,
            dropdowns=dropdowns,
            required_fields={"Course name"},
        )

    async def add_language(
        self,
        *,
        name: str,
        proficiency: str | None = None,
    ) -> dict[str, Any]:
        """Add a language to the profile.

        LinkedIn's language name field is an autocomplete — must select from
        the suggestions list for the value to persist (same pattern as add_skill).
        """
        username = await self._resolve_my_username()
        url = f"https://www.linkedin.com/in/{username}/overlay/create/new/?profileFormEntryPoint=PROFILE_SECTION&profileSectionId=LANGUAGES"
        if not await self._open_edit_overlay(overlay_url=url):
            return {
                "url": url,
                "status": "edit_failed",
                "message": "Add language form did not open.",
                "anchors_seen": await self._edit_anchors(),
            }

        # Fill the language name autocomplete (localized label aliases via i18n)
        filled = await self._fill_localized(
            "Language", name
        ) or await self._fill_localized("Name", name)
        if not filled:
            return {
                "url": url,
                "status": "edit_failed",
                "message": "Could not fill the language name field.",
                "labels_seen": await self._dialog_field_labels(),
            }

        # Language name requires typeahead selection — same as Skills and Industry
        await asyncio.sleep(1.0)
        typeahead = self._page.locator(
            '[role="listbox"] [role="option"], [role="listbox"] li'
        )
        if await typeahead.count() > 0:
            await typeahead.first.click()
            await asyncio.sleep(0.5)
        else:
            logger.debug("No typeahead suggestions for language %r", name)

        # Select proficiency level if provided (localized label + value aliases)
        if proficiency:
            await self._select_localized("Proficiency", proficiency)

        saved = await self._click_save_in_dialog()
        await asyncio.sleep(1.0)

        return {
            "url": url,
            "status": "saved" if saved else "save_failed",
            "message": f"Language '{name}' added."
            if saved
            else "Could not save the language.",
        }

    async def add_honor(
        self,
        *,
        title: str,
        issuer: str | None = None,
        issue_month: str | None = None,
        issue_year: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Add an honor or award to the profile."""
        fields: dict[str, str] = {"Title": title}
        dropdowns: dict[str, str] = {}
        if issuer:
            fields["Issuer"] = issuer
        if description:
            fields["Description"] = description
        if issue_month:
            dropdowns["Issue date month"] = issue_month
        if issue_year:
            dropdowns["Issue date year"] = issue_year

        return await self._edit_profile_section_entry(
            "HONORS",
            fields=fields,
            dropdowns=dropdowns,
            required_fields={"Title"},
        )
