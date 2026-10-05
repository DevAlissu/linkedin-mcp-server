"""Read and write the signed-in member's own profile through LinkedIn's edit forms.

Implements ``profile_edit.service.ProfileEditorPort``. Each write opens the
field's own edit form by URL, checks that the visible value is still the
expected "before" value, refuses if the form would notify the member's network,
changes only that control, saves, and waits for the dialog to close. It never
reports success itself: the service re-reads the field afterwards and compares.
Locators all come from ``profile_selectors``.
"""

from __future__ import annotations

from typing import Any, Literal

import logging
import re

from patchright.async_api import Locator, Page

import linkedin_mcp_server.linkedin.profile_selectors as sel
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession
from linkedin_mcp_server.profile_edit.changeset import skill_key
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import (
    NewPosition,
    format_start,
    ExperienceForm,
    ExperienceSummary,
    Skill,
    TextField,
    normalize_text,
)

logger = logging.getLogger(__name__)

_DIALOG_TIMEOUT_MS = 15_000
_SAVE_TIMEOUT_MS = 15_000
_FIELD_TIMEOUT_MS = 10_000
_SETTLE_MS = 500
_SETTLE_READS = 8
_EMPTY_SETTLE_MS = 4_000
_VIEW_SETTLE_MS = 2_500
# A rich-text box read line by line, the way it displays: every block is one
# line and an empty block is a blank line. innerText cannot tell those apart,
# because LinkedIn saves each line as its own <p>, which innerText separates
# with a blank line: a description typed as consecutive lines read back as
# separate paragraphs and failed verification after a good save (measured on a
# pt-BR position description, 4 October 2026). A <br> inside a block stays a
# line break.
_RICH_TEXT_JS = r"""
(el) => {
  const BLOCK = /^(P|DIV|LI|H[1-6]|BLOCKQUOTE|PRE|UL|OL|SECTION|ARTICLE)$/;
  const isBlock = (n) => n.nodeType === 1 && BLOCK.test(n.tagName);
  const lines = [];
  let inline = null;
  const flush = () => {
    if (inline !== null) lines.push(inline.replace(/\n$/, ''));
    inline = null;
  };
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (isBlock(child)) {
        flush();
        if ([...child.childNodes].some(isBlock)) walk(child);
        else lines.push(child.innerText.replace(/\n$/, ''));
      } else if (child.nodeType === 1) {
        inline = (inline ?? '') + (child.tagName === 'BR' ? '\n' : child.innerText);
      } else if (child.nodeType === 3) {
        inline = (inline ?? '') + child.textContent;
      }
    }
    flush();
  };
  walk(el);
  return lines.join('\n');
}
"""
# LinkedIn allows up to 100 skills on a profile (Help answer a549047), and a
# skills view loads about ten per scroll: a 100-skill pt-BR account read 80
# after 8 scrolls and was refused as INCOMPLETE_READ (4 October 2026). Fifteen
# covers a full profile with margin; a view that never stops still ends there.
_MAX_VIEW_SCROLLS = 15
_SCROLL_SETTLE_MS = 1_500
_OPTION_TIMEOUT_MS = 6_000
_DATE_RANGE = re.compile(r"\b(19|20)\d{2}\b")


class ProfileEditor:
    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        *,
        locale: str | None = None,
    ):
        self._session = session
        self._navigator = navigator
        # None: read from the first page this editor opens (see _detect_locale).
        self._locale = locale
        self._labels = sel.LABELS.get(locale or "", sel.LABELS[sel.DEFAULT_LOCALE])
        self._vanity: str | None = None
        self._navigations = 0
        self._experience_forms: dict[str, str] = {}
        self._location: str | None = None
        self._notify: bool | None = None
        self._last_notify: str | None = None

    @property
    def _page(self) -> Page:
        return self._session.page

    # ── navigation ──────────────────────────────────────────────────────────
    async def pause(self, seconds: float) -> None:
        await self._session.delay(seconds)

    async def _goto(self, url: str) -> None:
        if self._navigations:
            await self._session.delay(NAV_DELAY)
        self._navigations += 1
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        if self._locale is None:
            await self._detect_locale()

    async def _detect_locale(self) -> None:
        """Pick the label table from the page's own language, once per editor."""
        try:
            lang = await self._page.evaluate(sel.PAGE_LANG_JS)
        except Exception:
            return  # read again on the next navigation
        self._locale = sel.locale_for(lang if isinstance(lang, str) else None)
        self._labels = sel.LABELS[self._locale]
        logger.debug("Profile editor labels: %s (page lang %r)", self._locale, lang)

    async def _vanity_name(self) -> str:
        if self._vanity is None:
            await self._goto(sel.OWN_PROFILE_URL)
            m = sel.VANITY_FROM_URL.search(self._page.url)
            if not m or m.group(1) == "me":
                raise ProfileEditError(
                    ProfileEditErrorCode.PROFILE_NOT_FOUND, url=self._page.url
                )
            self._vanity = m.group(1)
        return self._vanity

    def _dialog(self) -> Locator:
        """The visible edit dialog; hidden ad dialogs are ignored."""
        page = self._page
        return page.locator(sel.DIALOG).filter(has=page.locator(sel.DIALOG_HAS)).first

    async def _open_dialog(self, url: str, what: str) -> None:
        await self._goto(url)
        try:
            await self._dialog().wait_for(state="visible", timeout=_DIALOG_TIMEOUT_MS)
        except Exception:
            raise await self._not_found(what, url, "edit dialog did not open") from None

    async def _open_form(self, urls: tuple[str, ...], spec: sel.FieldSpec) -> Locator:
        """Navigate to the first URL whose dialog contains *spec*; return the field."""
        last: ProfileEditError | None = None
        for url in urls:
            try:
                await self._open_dialog(url, spec.name)
                return await self._field(spec, url)
            except ProfileEditError as e:
                if e.code is not ProfileEditErrorCode.SELECTOR_NOT_FOUND:
                    raise
                last = e
        assert last is not None
        raise last

    async def _field(self, spec: sel.FieldSpec, url: str) -> Locator:
        dialog = self._dialog()
        # Rich-text editors mount after the dialog itself appears.
        selector = ", ".join(spec.css) if spec.css else "input, select"
        try:
            await dialog.locator(selector).first.wait_for(
                state="visible", timeout=_FIELD_TIMEOUT_MS
            )
        except Exception:
            pass  # reported below with the dialog's actual controls
        for css in spec.css:
            loc = dialog.locator(css)
            if await loc.count() == 1:
                return loc
        for key in spec.label_keys:
            for text in self._labels.get(key, ()):
                loc = dialog.get_by_label(text, exact=True)
                if await loc.count() == 1:
                    return loc
        raise await self._not_found(spec.name, url, "no unique control matched")

    async def _not_found(self, what: str, url: str, reason: str) -> ProfileEditError:
        try:
            described = await self._page.evaluate(sel.DESCRIBE_DIALOG_JS)
        except Exception:
            described = None
        return ProfileEditError(
            ProfileEditErrorCode.SELECTOR_NOT_FOUND,
            f"Could not find the {what} control: {reason}.",
            control=what,
            url=url,
            currentUrl=self._page.url,
            dialog=described,
        )

    # ── field primitives ────────────────────────────────────────────────────
    @staticmethod
    async def _is_plain(loc: Locator) -> bool:
        return await loc.evaluate(
            "(el) => ['input', 'textarea'].includes(el.tagName.toLowerCase())"
        )

    async def _read(self, loc: Locator) -> TextField:
        if await self._is_plain(loc):
            value = await loc.input_value()
        else:
            # A rich-text editor fills itself after it mounts; accept a value only
            # once two reads agree, so a baseline is never taken mid-load.
            value = await loc.evaluate(_RICH_TEXT_JS)
            for _ in range(_SETTLE_READS):
                await self._page.wait_for_timeout(_SETTLE_MS)
                again = await loc.evaluate(_RICH_TEXT_JS)
                if again == value:
                    break
                value = again
            # Two empty reads agree before a slow editor has filled in, so an
            # empty value is accepted only once it has stayed empty for
            # _EMPTY_SETTLE_MS; a real value usually arrives well before that.
            waited = 0
            while not value.strip() and waited < _EMPTY_SETTLE_MS:
                await self._page.wait_for_timeout(_SETTLE_MS)
                waited += _SETTLE_MS
                value = await loc.evaluate(_RICH_TEXT_JS)
        return TextField(value=value, max_length=await self._limit(loc))

    async def _limit(self, loc: Locator) -> int | None:
        """The field's own limit: its maxlength, or a maximum stated in its label.

        Never a counter found elsewhere in the dialog: a stray "1/7" in the intro
        form was once read as the headline's limit. Unknown means the default.
        """
        raw = await loc.get_attribute("maxlength")
        if raw and raw.isdigit():
            return int(raw)
        label = await loc.get_attribute("aria-label") or ""
        if m := sel.STATED_MAX.search(label):
            return int(re.sub(r"\D", "", m.group(1)))
        return None

    async def _type(self, loc: Locator, value: str) -> None:
        if await self._is_plain(loc):
            await loc.fill(value)
            return
        # A rich-text box: replace its content. LinkedIn separates paragraphs
        # with an empty paragraph, so a blank line is typed as two Enters; a
        # single line break is Shift+Enter.
        keyboard = self._page.keyboard
        await loc.click()
        await keyboard.press("ControlOrMeta+A")
        await keyboard.press("Delete")
        for i, paragraph in enumerate(value.split("\n\n")):
            if i:
                await keyboard.press("Enter")
                await keyboard.press("Enter")
            for j, line in enumerate(paragraph.split("\n")):
                if j:
                    await keyboard.press("Shift+Enter")
                if line:
                    await keyboard.insert_text(line)

    # ── network notification ────────────────────────────────────────────────
    def set_network_notification(self, notify: bool | None) -> None:
        """The user's answer to whether LinkedIn should notify their network."""
        self._notify = notify

    def last_network_notification(self) -> str | None:
        """What the last saved form did: "on", "off" or "not_offered"."""
        return self._last_notify

    async def _settle_notify_switch(self, field: str) -> None:
        """Set LinkedIn's notify-your-network switch to the user's decision.

        A form without the switch cannot notify and says so in the result. With
        no decision (the editor used outside an approved apply), a switch that
        is on is refused rather than saved, so nothing is broadcast by default.
        """
        self._last_notify = None
        switches = self._dialog().locator(sel.NOTIFY_SWITCH)
        count = await switches.count()
        if not count:
            self._last_notify = "not_offered"
            return
        for i in range(count):
            switch = switches.nth(i)
            if self._notify is None:
                if await switch.is_checked():
                    raise ProfileEditError(
                        ProfileEditErrorCode.NOTIFY_DECISION_REQUIRED, field=field
                    )
                continue
            if await switch.is_checked() != self._notify:
                try:
                    await switch.set_checked(self._notify, timeout=_FIELD_TIMEOUT_MS)
                except Exception:
                    # A visually hidden switch: toggle it the way the page does.
                    await switch.evaluate(
                        "(el, on) => { if (el.checked !== on) el.click(); }",
                        self._notify,
                    )
            if await switch.is_checked() != self._notify:
                raise ProfileEditError(
                    ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                    f"The {field} form's notify-your-network switch could not be "
                    "set as the user decided; nothing was saved.",
                    field=field,
                    notifyNetwork=self._notify,
                )
        self._last_notify = "on" if self._notify else "off"

    async def _replace(
        self, loc: Locator, *, expected: str, value: str, field: str, url: str
    ) -> None:
        current = normalize_text((await self._read(loc)).value)
        if current != normalize_text(expected):
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                f"The {field} on LinkedIn no longer matches the change set; nothing was typed.",
                field=field,
                expected=expected,
                actual=current,
            )
        await self._settle_notify_switch(field)
        await self._type(loc, value)
        typed = normalize_text((await self._read(loc)).value)
        if typed != normalize_text(value):
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                f"The {field} field did not take the exact value (a shorter limit or "
                "reformatting); nothing was saved.",
                field=field,
                typedLength=len(typed),
                wantedLength=len(value),
            )
        await self._save(field, url)

    async def _button(self, key: str, scope: Locator | None = None) -> Locator | None:
        within = scope if scope is not None else self._dialog()
        for text in self._labels.get(key, ()):
            loc = within.get_by_role("button", name=text, exact=True)
            if await loc.count() == 1:
                return loc
        return None

    async def _save(self, field: str, url: str) -> None:
        dialog = self._dialog()
        save = None
        for css in sel.SAVE_BUTTON:
            loc = dialog.locator(css)
            if await loc.count() == 1:
                save = loc
                break
        save = save or await self._button("save")
        if save is None:
            raise await self._not_found(
                f"{field} save button", url, "no unique save control"
            )
        await save.click()
        try:
            await self._dialog().wait_for(state="hidden", timeout=_SAVE_TIMEOUT_MS)
        except Exception:
            errors: list[str] = []
            for css in sel.FORM_ERROR:
                found = self._dialog().locator(css)
                for i in range(await found.count()):
                    t = (await found.nth(i).inner_text()).strip()
                    if t:
                        errors.append(t[:200])
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                f"LinkedIn did not close the {field} form after Save.",
                field=field,
                formErrors=errors,
                currentUrl=self._page.url,
            ) from None
        await self._session.check_rate_limit()

    # ── reads ───────────────────────────────────────────────────────────────
    async def account(self) -> str:
        """The signed-in member's profile URL, resolved from /in/me/ per call."""
        return sel.profile_url(await self._vanity_name())

    async def read_identity(self) -> tuple[str, str | None, str | None]:
        vanity = await self._vanity_name()
        name = None
        h1 = self._page.locator("main h1")
        if await h1.count():
            name = (await h1.first.inner_text()).strip() or None
        if name is None:  # the page title is "<Name> | LinkedIn"
            name = (await self._page.title()).rsplit(" | ", 1)[0].strip() or None
        return sel.profile_url(vanity), name, None

    async def read_headline(self) -> TextField:
        url = sel.intro_form_url(await self._vanity_name())
        field = await self._read(await self._open_form((url,), sel.HEADLINE))
        try:
            location = await self._field(sel.LOCATION, url)
            self._location = normalize_text(await location.input_value()) or None
        except ProfileEditError:
            self._location = None  # display only; never blocks a read
        return field

    async def read_location(self) -> str | None:
        return self._location

    async def read_about(self) -> TextField:
        urls = sel.about_form_urls(await self._vanity_name())
        return await self._read(await self._open_form(urls, sel.ABOUT))

    async def list_experiences(self) -> list[ExperienceSummary]:
        vanity = await self._vanity_name()
        await self._goto(sel.experience_list_url(vanity))
        await self._session.scroll_body(pause_time=0.8, max_scrolls=8)
        items = await self._page.evaluate(
            sel.LIST_ITEMS_JS, sel.EXPERIENCE_EDIT_HREF.pattern
        )
        out: list[ExperienceSummary] = []
        for item in items:
            self._experience_forms[item["id"]] = item["href"]
            out.append(_summary(item))
        return out

    async def read_experience(self, experience_id: str) -> ExperienceForm:
        vanity = await self._vanity_name()
        if experience_id not in self._experience_forms:
            listed = await self.list_experiences()
            if experience_id not in {e.id for e in listed}:
                raise ProfileEditError(
                    ProfileEditErrorCode.EXPERIENCE_NOT_FOUND,
                    experienceId=experience_id,
                )
        url = sel.experience_form_url(vanity, experience_id)
        title = await self._read(await self._open_form((url,), sel.EXPERIENCE_TITLE))
        description = await self._read(
            await self._field(sel.EXPERIENCE_DESCRIPTION, url)
        )
        company = None
        try:
            company_field = await self._field(sel.EXPERIENCE_COMPANY, url)
            company = normalize_text(await company_field.input_value()) or None
        except ProfileEditError:
            pass  # display only
        start_month, start_year = await self._read_start()
        return ExperienceForm(
            id=experience_id,
            title=title,
            description=description,
            company=company,
            start_month=start_month,
            start_year=start_year,
        )

    async def list_skills(self) -> list[Skill]:
        """Every skill, merged by LinkedIn's skill id across the page's views.

        The default view lists only some skills; the category views list the
        rest. Order is first appearance: the default view, then each category.
        """
        vanity = await self._vanity_name()
        await self._goto(sel.skills_list_url(vanity))
        await self._session.scroll_body(pause_time=0.8, max_scrolls=10)
        found: dict[str, str] = {}

        async def collect() -> None:
            # Each view also loads more as it scrolls: read, scroll, and stop
            # once a scroll brings nothing new.
            for attempt in range(_MAX_VIEW_SCROLLS):
                before = len(found)
                items = await self._page.evaluate(
                    sel.LIST_ITEMS_JS, sel.SKILL_EDIT_HREF.pattern
                )
                for item in items:
                    if item["lines"] and item["id"] not in found:
                        found[item["id"]] = item["lines"][0]
                if len(found) == before and attempt > 0:
                    break
                # The list scrolls inside its own container, not the window:
                # bringing the last loaded item into view scrolls whichever
                # container holds it, which loads the next batch.
                await self._page.evaluate(
                    sel.SCROLL_LAST_ITEM_JS, sel.SKILL_EDIT_HREF.pattern
                )
                await self._page.wait_for_timeout(_SCROLL_SETTLE_MS)
            else:
                # Every scroll up to the cap still brought new skills, so the
                # view may hold more. A partial list must never be used to
                # decide what is a duplicate or to verify an edit.
                raise ProfileEditError(
                    ProfileEditErrorCode.INCOMPLETE_READ,
                    skillsRead=len(found),
                    scrolls=_MAX_VIEW_SCROLLS,
                )

        await collect()
        filters = self._page.locator(sel.SKILL_FILTER_BUTTONS)
        for i in range(await filters.count()):
            button = filters.nth(i)
            if await button.get_attribute("aria-current") == "true":
                continue
            await button.click()
            await self._page.wait_for_timeout(_VIEW_SETTLE_MS)
            await collect()
        return [
            Skill(name=name, position=n + 1, ref=ref)
            for n, (ref, name) in enumerate(found.items())
        ]

    # ── writes ──────────────────────────────────────────────────────────────
    async def write_headline(self, *, expected: str, value: str) -> None:
        url = sel.intro_form_url(await self._vanity_name())
        loc = await self._open_form((url,), sel.HEADLINE)
        await self._replace(
            loc, expected=expected, value=value, field="headline", url=url
        )

    async def write_about(self, *, expected: str, value: str) -> None:
        urls = sel.about_form_urls(await self._vanity_name())
        loc = await self._open_form(urls, sel.ABOUT)
        await self._replace(
            loc, expected=expected, value=value, field="about", url=self._page.url
        )

    async def write_experience(
        self,
        experience_id: str,
        *,
        field: Literal["title", "description"],
        expected: str,
        value: str,
    ) -> None:
        url = sel.experience_form_url(await self._vanity_name(), experience_id)
        spec = sel.EXPERIENCE_TITLE if field == "title" else sel.EXPERIENCE_DESCRIPTION
        loc = await self._open_form((url,), spec)
        await self._replace(
            loc, expected=expected, value=value, field=f"experience {field}", url=url
        )

    async def add_skill(self, name: str) -> str:
        url = sel.new_skill_form_url(await self._vanity_name())
        box = await self._open_form((url,), sel.SKILL_INPUT)
        # Typing a whole name key by key lost characters while the suggestion
        # list re-rendered ("Large LanguagModels"). Set all but the last
        # character at once, type the last to trigger suggestions, and only
        # continue when the box holds exactly the intended name.
        await box.fill(name[:-1])
        await box.press_sequentially(name[-1], delay=120)
        if await box.input_value() != name:
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                "The skill box did not take the exact name; nothing was added.",
                skill=name,
                typed=await box.input_value(),
            )
        options = self._page.locator(sel.TYPEAHEAD_OPTION)
        try:
            await options.first.wait_for(state="visible", timeout=_OPTION_TIMEOUT_MS)
        except Exception:
            raise ProfileEditError(
                ProfileEditErrorCode.SKILL_NOT_FOUND,
                f"LinkedIn offered no suggestions for '{name}'.",
                skill=name,
            ) from None
        offered: list[str] = []
        for i in range(await options.count()):
            text = normalize_text((await options.nth(i).inner_text()).split("\n")[0])
            offered.append(text)
            if skill_key(text) == skill_key(name):
                await options.nth(i).click()
                await self._settle_notify_switch("skill")
                await self._save("skill", url)
                return text
        raise ProfileEditError(
            ProfileEditErrorCode.SKILL_NOT_FOUND,
            f"LinkedIn has no skill named exactly '{name}'. Nothing was added; "
            "choose one of the offered names.",
            skill=name,
            offered=offered,
        )

    async def _read_start(self) -> tuple[int | None, int | None]:
        """The start month (1 to 12) and year shown by the open form, if any."""
        try:
            month = await self._field(sel.START_MONTH, self._page.url)
            year = await self._field(sel.START_YEAR, self._page.url)
        except ProfileEditError:
            return None, None
        index = await month.evaluate("(el) => el.selectedIndex")
        text = await year.evaluate(
            "(el) => (el.options[el.selectedIndex] || {}).textContent || ''"
        )
        text = text.strip()
        return (index or None), (int(text) if text.isdigit() else None)

    async def write_experience_start(
        self, experience_id: str, *, expected: str, month: int, year: int
    ) -> None:
        """Change a position's start month and year, checking the "before" first."""
        url = sel.experience_form_url(await self._vanity_name(), experience_id)
        await self._open_form((url,), sel.EXPERIENCE_TITLE)
        current_month, current_year = await self._read_start()
        current = (
            format_start(current_month, current_year)
            if current_month and current_year
            else ""
        )
        if current != expected:
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                "The start date on LinkedIn no longer matches the change set; nothing was changed.",
                field="start",
                expected=expected,
                actual=current,
            )
        await self._settle_notify_switch("experience start date")
        month_box = await self._field(sel.START_MONTH, url)
        year_box = await self._field(sel.START_YEAR, url)
        await month_box.select_option(index=month)
        await year_box.select_option(label=str(year))
        if await self._read_start() != (month, year):
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                "The start date did not take; nothing was saved.",
                wanted=format_start(month, year),
            )
        await self._save("experience start date", url)

    async def add_experience(self, position: NewPosition) -> None:
        """Fill the new-position form exactly as approved, check it, then save.

        Every control is read back before Save: a value the form did not take
        stops the add with nothing saved.
        """
        url = sel.new_position_form_url(await self._vanity_name())
        title = await self._open_form((url,), sel.NEW_POSITION_TITLE)
        await title.fill(position.title)
        if normalize_text(await title.input_value()) != position.title:
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                "The title box did not take the exact title; nothing was saved.",
                typed=await title.input_value(),
            )
        company = await self._field(sel.NEW_POSITION_COMPANY, url)
        await self._choose_option(company, position.company, field="company")
        for spec, value in (
            (sel.EMPLOYMENT_TYPE, position.employment_type),
            (sel.LOCATION_TYPE, position.location_type),
        ):
            if value is not None:
                await self._select_canonical(spec, value, url)
        await self._keep_current_role(url)
        month = await self._field(sel.START_MONTH, url)
        await month.select_option(index=position.start_month)
        year = await self._field(sel.START_YEAR, url)
        await year.select_option(label=str(position.start_year))
        chosen = (
            await month.evaluate("(el) => el.selectedIndex"),
            await year.evaluate(
                "(el) => el.options[el.selectedIndex].textContent.trim()"
            ),
        )
        if chosen != (position.start_month, str(position.start_year)):
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                "The start date did not take; nothing was saved.",
                selected={"month": chosen[0], "year": chosen[1]},
            )
        if position.description:
            description = await self._field(sel.EXPERIENCE_DESCRIPTION, url)
            await self._type(description, position.description)
            typed = normalize_text((await self._read(description)).value)
            if typed != position.description:
                raise ProfileEditError(
                    ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                    "The description did not take the exact text; nothing was saved.",
                    typedLength=len(typed),
                    wantedLength=len(position.description),
                )
        await self._settle_notify_switch("new position")
        await self._save("new position", url)

    async def _choose_option(self, box: Locator, text: str, *, field: str) -> None:
        """Type into a typeahead and pick the suggestion that reads exactly *text*."""
        await box.fill(text[:-1])
        await box.press_sequentially(text[-1], delay=120)
        options = self._page.locator(sel.TYPEAHEAD_OPTION)
        try:
            await options.first.wait_for(state="visible", timeout=_OPTION_TIMEOUT_MS)
        except Exception:
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR,
                f"LinkedIn offered no {field} suggestions for '{text}'; nothing was saved.",
                **{field: text},
            ) from None
        offered: list[str] = []
        for i in range(await options.count()):
            label = normalize_text((await options.nth(i).inner_text()).split("\n")[0])
            offered.append(label)
            if label == text:
                await options.nth(i).click()
                break
        else:
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR,
                f"LinkedIn has no {field} named exactly '{text}'; nothing was saved.",
                offered=offered,
            )
        if normalize_text(await box.input_value()) != text:
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                f"The {field} box did not keep '{text}'; nothing was saved.",
                typed=await box.input_value(),
            )

    async def _select_canonical(
        self, spec: sel.FieldSpec, value: str, url: str
    ) -> None:
        """Choose a dropdown option by its canonical value, through the locale table."""
        wanted = (
            sel.OPTION_TEXT.get(self._locale or "", {}).get(spec.name, {}).get(value)
        )
        if wanted is None:
            raise await self._not_found(
                spec.name,
                url,
                f"no option text measured for {value!r} in locale {self._locale!r}",
            )
        select = await self._field(spec, url)
        await select.select_option(label=wanted)
        shown = await select.evaluate(
            "(el) => el.options[el.selectedIndex].textContent.trim()"
        )
        if shown != wanted:
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                f"The {spec.name} dropdown did not take '{wanted}'; nothing was saved.",
                selected=shown,
            )

    async def _keep_current_role(self, url: str) -> None:
        """Leave "I currently work here" checked: only current roles are added."""
        boxes = self._dialog().locator(sel.CURRENT_ROLE_CHECKBOX)
        count = await boxes.count()
        if count != 1:
            raise await self._not_found(
                "current role checkbox",
                url,
                f"expected one plain checkbox, found {count}",
            )
        box = boxes.first
        if not await box.is_checked():
            try:
                await box.set_checked(True, timeout=_FIELD_TIMEOUT_MS)
            except Exception:
                await box.evaluate("(el) => { if (!el.checked) el.click(); }")
        if not await box.is_checked():
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                "The current-role box could not be checked; nothing was saved.",
            )

    async def remove_skill(self, skill: Skill) -> None:
        if not skill.ref:
            raise ProfileEditError(
                ProfileEditErrorCode.UNSUPPORTED_FIELD,
                "This skill has no edit control.",
                skill=skill.name,
            )
        url = sel.skill_form_url(await self._vanity_name(), skill.ref)
        await self._open_dialog(url, "skill")
        dialog = self._dialog()
        # The dialog names its skill only in its heading ("Edit React.js").
        heading = normalize_text(await dialog.locator(sel.HEADINGS).first.inner_text())
        if not skill_key(heading).endswith(skill_key(skill.name)):
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                "The skill form is for a different skill; nothing was deleted.",
                expected=skill.name,
                actual=heading,
            )
        await self._settle_notify_switch("skill")
        # Refuse before the first click when this locale's confirmation label is
        # unknown: if that dialog never appeared, the click alone could delete.
        if not self._labels.get("confirm_delete"):
            raise await self._not_found(
                "delete confirmation",
                url,
                f"no confirmation label measured for locale {self._locale!r}",
            )
        delete = await self._button("delete_skill")
        if delete is None:
            raise await self._not_found(
                "delete skill button", url, "no unique delete control"
            )
        await delete.click()
        confirm = await self._button("confirm_delete", self._page.locator(sel.DIALOG))
        if confirm is None:
            raise await self._not_found(
                "delete confirmation", url, "no unique confirm control"
            )
        await confirm.click()
        try:
            await self._dialog().wait_for(state="hidden", timeout=_SAVE_TIMEOUT_MS)
        except Exception:
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                "The skill dialog did not close after deleting.",
                skill=skill.name,
            ) from None


def _summary(item: dict[str, Any]) -> ExperienceSummary:
    """Best-effort display fields from a position's list text.

    Used to show and match experiences, never to identify one for a write (the
    position id does that) and never as a "before" value (the form does that).
    """
    lines: list[str] = item.get("lines") or []
    group: list[str] = item.get("groupLines") or []
    title = lines[0] if lines else ""
    company = employment = date_range = location = None
    rest = lines[1:]
    if not group and rest and _DATE_RANGE.search(rest[0]):
        # A role grouped under its company, rendered without the nested list
        # that would give groupLines: the line after the title is already the
        # date range, so the company is unknown rather than that date (measured
        # on a pt-BR account, 4 October 2026).
        return ExperienceSummary(
            id=item["id"],
            title=title,
            company=None,
            employment_type=None,
            date_range=rest[0],
            location=None,
            description_preview=" ".join(rest[1:])[:280] or None,
            editable=True,
        )
    if group:  # a role grouped under its company: the group's first line is the company
        company = group[0]
    elif rest:
        company, _, employment = (s.strip() for s in rest.pop(0).partition("·"))
        employment = employment or None
    for i, line in enumerate(rest):
        if _DATE_RANGE.search(line):
            date_range = line
            if i + 1 < len(rest) and not _DATE_RANGE.search(rest[i + 1]):
                location = rest[i + 1]
            break
    if location in rest:
        preview_from = rest.index(location) + 1
    elif date_range in rest:
        preview_from = rest.index(date_range) + 1
    else:
        preview_from = len(rest)
    preview = " ".join(rest[preview_from:])[:280] or None
    return ExperienceSummary(
        id=item["id"],
        title=title,
        company=company,
        employment_type=employment,
        date_range=date_range,
        location=location,
        description_preview=preview,
        editable=True,
    )
