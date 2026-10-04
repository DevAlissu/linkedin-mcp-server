"""The own-profile editor picks its label table from the page's language.

The browser context is forced to en-US, yet LinkedIn renders a pt-BR account in
Portuguese (measured 4 October 2026). These browser-DOM tests route synthetic
pages that carry ``<html lang>`` and the labels measured on that account, and
check that the editor finds the localized controls, that a language without a
table fails at SELECTOR_NOT_FOUND, and that a skill removal is refused before
any click when the locale's confirmation label was never measured.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from patchright.async_api import Page, Route, async_playwright

import linkedin_mcp_server.linkedin.profile_selectors as sel
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_editor import ProfileEditor
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import Skill

pytestmark = pytest.mark.xdist_group("browser_runtime")


@pytest.mark.parametrize(
    ("lang", "table"),
    [
        ("pt", "pt"),
        ("pt-BR", "pt"),
        (" PT-br ", "pt"),
        ("en-US", "en"),
        ("en", "en"),
        ("de-DE", "en"),
        ("", "en"),
        (None, "en"),
    ],
)
def test_locale_for_maps_the_page_language_to_a_label_table(
    lang: str | None, table: str
) -> None:
    assert sel.locale_for(lang) == table


BASE = "https://www.linkedin.com"

STATE_JS = """
const S = Object.assign({title201: 'Desenvolvedor', skills: ['Frontend Developer']},
  JSON.parse(localStorage.getItem('S') || '{}'));
const save = () => localStorage.setItem('S', JSON.stringify(S));
"""


def page_html(lang: str, body: str, script: str = "") -> str:
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        "<title>Joao | LinkedIn</title></head>"
        f"<body><main>{body}</main><script>{STATE_JS}{script}</script></body></html>"
    )


def experience_list(lang: str) -> str:
    return page_html(
        lang,
        '<div><a href="/in/joao/details/experience/edit/forms/201/">'
        "<div>Desenvolvedor</div><div>INOVA · Tempo integral</div><div>jan de 2024 - o momento</div></a>"
        '<a href="/in/joao/details/experience/edit/forms/201/" aria-label="Editar">'
        "<span>Editar</span></a></div>",
    )


def experience_form(lang: str, title: str, company: str, save: str) -> str:
    """The position form as measured: labels through aria-labelledby, a
    text-only Save button and a stated maximum in the description's label."""
    return page_html(
        lang,
        f'<dialog id="d"><h2>Editar cargo</h2><input type="checkbox" role="switch">'
        f'<span id="lt">{title}</span><input id="t" aria-labelledby="lt" placeholder="Cargo">'
        f'<span id="lc">{company}</span><input id="c" aria-labelledby="lc" value="INOVA">'
        '<div role="textbox" contenteditable="true" '
        'aria-label="Descrição, máximo de 2.000 caracteres"></div>'
        f'<button type="button" id="save">{save}</button></dialog>',
        """
        document.getElementById('t').value = S.title201;
        document.getElementById('d').show();
        document.getElementById('save').addEventListener('click', () => {
          S.title201 = document.getElementById('t').value; save();
          document.getElementById('d').close();
        });
        """,
    )


def skill_form_without_confirmation() -> str:
    """A skill form whose delete button deletes at once, with no confirmation:
    the case the editor must never click into blind."""
    return page_html(
        "pt",
        '<dialog id="d"><h2>Editar Frontend Developer</h2>'
        '<input type="checkbox" aria-label="Desenvolvedor na INOVA">'
        '<button type="button" id="del">Exclua a competência</button>'
        '<button type="button">Salvar</button></dialog>',
        """
        document.getElementById('d').show();
        document.getElementById('del').addEventListener('click', () => {
          S.skills = []; save(); document.getElementById('d').close();
        });
        """,
    )


PAGES: dict[str, str] = {
    "/in/joao/": page_html("pt", "<h1>Joao</h1>"),
    "/in/joao/details/experience/": experience_list("pt"),
    "/in/joao/details/experience/edit/forms/201/": experience_form(
        "pt", "Cargo*", "Empresa ou organização*", "Salvar"
    ),
    "/in/joao/details/skills/edit/forms/301/": skill_form_without_confirmation(),
    "/in/hans/": page_html("de", "<h1>Hans</h1>"),
    "/in/hans/details/experience/": experience_list("de"),
    "/in/hans/details/experience/edit/forms/201/": experience_form(
        "de", "Titel*", "Unternehmen oder Organisation*", "Speichern"
    ),
}


def _router(member: str):
    async def route(route: Route) -> None:
        url = route.request.url
        path = url.removeprefix(BASE).split("?")[0]
        if not url.startswith(BASE):
            await route.abort()  # hermetic: nothing leaves the test browser
        elif path == "/in/me/":
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<script>location.replace('/in/{member}/')</script>",
            )
        elif path in PAGES:
            await route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=PAGES[path]
            )
        else:
            await route.fulfill(status=404, body="")

    return route


class _Navigator(PageNavigator):
    """Plain navigation: the auth-barrier checks are covered by navigation's own tests."""

    async def _navigate_to_page(self, url: str) -> None:
        page = self._session.page
        await page.goto(url, wait_until="domcontentloaded")
        if url.endswith("/in/me/"):
            await page.wait_for_url(f"{BASE}/in/*/")


async def _browser_page(member: str) -> AsyncIterator[Page]:
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        context = await browser.new_context(locale="en-US")
        await context.route("**/*", _router(member))
        yield await context.new_page()
        await browser.close()


@pytest.fixture
async def page_pt() -> AsyncIterator[Page]:
    async for p in _browser_page("joao"):
        yield p


@pytest.fixture
async def page_de() -> AsyncIterator[Page]:
    async for p in _browser_page("hans"):
        yield p


class _FastSession(PageSession):
    """No navigation pacing, so the suite stays quick; pacing is a unit-test concern."""

    async def delay(self, seconds: float) -> None:
        return None


def _editor(page: Page) -> ProfileEditor:
    session = _FastSession(page)
    return ProfileEditor(session, _Navigator(session))


@pytest.mark.browser_dom
async def test_a_portuguese_page_selects_the_pt_labels(page_pt: Page) -> None:
    editor = _editor(page_pt)

    form = await editor.read_experience("201")

    assert form.title.value == "Desenvolvedor"
    assert form.company == "INOVA"
    assert form.description.max_length == 2000


@pytest.mark.browser_dom
async def test_a_portuguese_title_is_written_and_saved(page_pt: Page) -> None:
    editor = _editor(page_pt)
    await editor.list_experiences()

    await editor.write_experience(
        "201", field="title", expected="Desenvolvedor", value="Engenheiro de Software"
    )

    saved = await page_pt.evaluate("JSON.parse(localStorage.getItem('S')).title201")
    assert saved == "Engenheiro de Software"


@pytest.mark.browser_dom
async def test_a_language_without_a_table_stops_at_selector_not_found(
    page_de: Page,
) -> None:
    editor = _editor(page_de)

    with pytest.raises(ProfileEditError) as err:
        await editor.read_experience("201")

    assert err.value.code is ProfileEditErrorCode.SELECTOR_NOT_FOUND


@pytest.mark.browser_dom
async def test_skill_removal_is_refused_before_any_click_without_a_confirm_label(
    page_pt: Page,
) -> None:
    editor = _editor(page_pt)

    with pytest.raises(ProfileEditError) as err:
        await editor.remove_skill(
            Skill(name="Frontend Developer", position=1, ref="301")
        )

    assert err.value.code is ProfileEditErrorCode.SELECTOR_NOT_FOUND
    skills = await page_pt.evaluate(
        "JSON.parse(localStorage.getItem('S') || '{}').skills"
    )
    assert skills is None  # the delete button was never clicked, nothing was saved
