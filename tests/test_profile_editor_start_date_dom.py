"""Browser-DOM test for changing a position's start date in its edit form.

The synthetic form carries what was measured on a pt-BR account on 4 October
2026: "Mês de início" (a placeholder, then the twelve months) and "Ano de
início*" dropdowns, a notify-your-network switch and a text-only "Salvar".
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from patchright.async_api import Page, Route, async_playwright

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_editor import ProfileEditor
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

BASE = "https://www.linkedin.com"
MONTHS = [
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
]


def page_html(body: str, script: str = "") -> str:
    return (
        '<!doctype html><html lang="pt"><head><meta charset="utf-8"><title>Joao | LinkedIn</title></head>'
        f"<body><main>{body}</main><script>"
        "const S = Object.assign({month: 4, year: '2026', notify: true}, JSON.parse(localStorage.getItem('S') || '{}'));"
        "const save = () => localStorage.setItem('S', JSON.stringify(S));"
        f"{script}</script></body></html>"
    )


def edit_form(*, reverts_month: bool = False) -> str:
    """The edit form; with ``reverts_month`` the page puts the old month back."""
    months = "".join(f"<option>{m}</option>" for m in MONTHS)
    years = "".join(f"<option>{y}</option>" for y in range(2026, 1999, -1))
    return page_html(
        '<dialog id="d"><h2>Editar cargo</h2><input type="checkbox" role="switch" id="notify">'
        '<span id="lt">Cargo*</span><input id="t" aria-labelledby="lt" value="Líder Técnico Frontend">'
        f'<label for="sm">Mês de início</label><select id="sm"><option>Month</option>{months}</select>'
        f'<label for="sy">Ano de início*</label><select id="sy"><option>Year</option>{years}</select>'
        '<div role="textbox" contenteditable="true" aria-label="Descrição, máximo de 2.000 caracteres"></div>'
        '<button type="button" id="save">Salvar</button></dialog>',
        """
        const sm = document.getElementById('sm'), sy = document.getElementById('sy');
        sm.selectedIndex = S.month;
        sy.value = S.year;
        if (%s) sm.addEventListener('change', () => { sm.selectedIndex = S.month; });
        document.getElementById('notify').checked = S.notify;
        document.getElementById('d').show();
        document.getElementById('save').addEventListener('click', () => {
          S.month = sm.selectedIndex; S.year = sy.value;
          S.notifiedOnSave = document.getElementById('notify').checked;
          save(); document.getElementById('d').close();
        });
        """
        % ("true" if reverts_month else "false"),
    )


PAGES = {
    "/in/joao/": page_html("<h1>Joao</h1>"),
    "/in/joao/details/experience/": page_html(
        '<div><a href="/in/joao/details/experience/edit/forms/301/"><div>Líder Técnico Frontend</div></a></div>'
        '<div><a href="/in/joao/details/experience/edit/forms/302/"><div>Pesquisador</div></a></div>'
    ),
    "/in/joao/details/experience/edit/forms/301/": edit_form(),
    "/in/joao/details/experience/edit/forms/302/": edit_form(reverts_month=True),
}


async def _route(route: Route) -> None:
    url = route.request.url
    path = url.removeprefix(BASE).split("?")[0]
    if not url.startswith(BASE):
        await route.abort()
    elif path == "/in/me/":
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body="<script>location.replace('/in/joao/')</script>",
        )
    elif path in PAGES:
        await route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=PAGES[path]
        )
    else:
        await route.fulfill(status=404, body="")


class _Navigator(PageNavigator):
    async def _navigate_to_page(self, url: str) -> None:
        page = self._session.page
        await page.goto(url, wait_until="domcontentloaded")
        if url.endswith("/in/me/"):
            await page.wait_for_url(f"{BASE}/in/joao/")


class _FastSession(PageSession):
    async def delay(self, seconds: float) -> None:
        return None


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        context = await browser.new_context()
        await context.route("**/*", _route)
        yield await context.new_page()
        await browser.close()


def editor(page: Page) -> ProfileEditor:
    session = _FastSession(page)
    ed = ProfileEditor(session, _Navigator(session))
    ed.set_network_notification(False)
    return ed


async def stored(page: Page) -> dict:
    return json.loads(await page.evaluate("localStorage.getItem('S') || '{}'"))


async def test_the_start_date_is_read_from_the_form(page: Page) -> None:
    form = await editor(page).read_experience("301")
    assert (form.start_month, form.start_year, form.start) == (4, 2026, "04/2026")


async def test_the_start_date_is_changed_and_saved_without_notifying(
    page: Page,
) -> None:
    await editor(page).write_experience_start(
        "301", expected="04/2026", month=10, year=2025
    )

    s = await stored(page)
    assert (s["month"], s["year"], s["notifiedOnSave"]) == (10, "2025", False)


async def test_a_stale_start_date_changes_nothing(page: Page) -> None:
    with pytest.raises(ProfileEditError) as e:
        await editor(page).write_experience_start(
            "301", expected="03/2026", month=10, year=2025
        )
    assert e.value.code is ProfileEditErrorCode.STALE_CHANGE_SET
    assert "notifiedOnSave" not in await stored(page)


async def test_a_date_the_form_does_not_take_is_never_saved(page: Page) -> None:
    with pytest.raises(ProfileEditError) as e:
        await editor(page).write_experience_start(
            "302", expected="04/2026", month=10, year=2025
        )
    assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
    assert "notifiedOnSave" not in await stored(page)
