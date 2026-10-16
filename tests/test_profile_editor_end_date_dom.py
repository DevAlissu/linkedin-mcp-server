"""Browser-DOM tests for ending a position, or making it current again.

The synthetic form carries what was measured on a pt-BR account on
16 October 2026: a visually hidden, unlabelled "Trabalho atualmente neste
cargo" checkbox; while it is checked, one unlabelled "Encerrar cargo atual a
partir de agora - <title>" box per other role held now; while it is not, the
"Mês de término" and "Ano de término*" dropdowns; and labelled skill
checkboxes, which are none of these.
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
HIDDEN = 'style="position:absolute;width:0;height:0;opacity:0;margin:0"'


def page_html(body: str, script: str = "") -> str:
    return (
        '<!doctype html><html lang="pt"><head><meta charset="utf-8"><title>Joao | LinkedIn</title></head>'
        f"<body><main>{body}</main><script>"
        "const S = Object.assign({current: true, endMonth: 0, endYear: 'Year',"
        " others: ['Desenvolvedor Técnico Front End Pleno'], otherChecked: false},"
        " JSON.parse(localStorage.getItem('S') || '{}'));"
        "const save = () => localStorage.setItem('S', JSON.stringify(S));"
        f"{script}</script></body></html>"
    )


def edit_form() -> str:
    months = "".join(f"<option>{m}</option>" for m in MONTHS)
    years = "".join(f"<option>{y}</option>" for y in range(2026, 1999, -1))
    return page_html(
        '<dialog id="d"><h2>Editar cargo</h2><input type="checkbox" role="switch" id="notify">'
        '<span id="lt">Cargo*</span><input id="t" aria-labelledby="lt" value="Líder Técnico Frontend">'
        f'<div><div><input type="checkbox" id="cur" {HIDDEN}></div><span>Trabalho atualmente neste cargo</span></div>'
        '<div id="others"></div>'
        f'<label for="sm">Mês de início</label><select id="sm"><option>Month</option>{months}</select>'
        f'<label for="sy">Ano de início*</label><select id="sy"><option>Year</option>{years}</select>'
        '<div id="endbox">'
        f'<label for="em">Mês de término</label><select id="em"><option>Month</option>{months}</select>'
        f'<label for="ey">Ano de término*</label><select id="ey"><option>Year</option>{years}</select>'
        "</div>"
        '<div role="textbox" contenteditable="true" aria-label="Descrição, máximo de 2.000 caracteres"></div>'
        f'<input type="checkbox" id="sk1" {HIDDEN}><label for="sk1">Android</label>'
        '<button type="button" id="save">Salvar</button></dialog>',
        f"""
        const cur = document.getElementById('cur');
        const others = document.getElementById('others');
        const endbox = document.getElementById('endbox');
        const em = document.getElementById('em'), ey = document.getElementById('ey');
        const render = () => {{
          endbox.style.display = cur.checked ? 'none' : '';
          others.innerHTML = cur.checked ? S.others.map((t) =>
            `<div><div><input type="checkbox" class="end-other" {HIDDEN}></div>`
            + `<span>Encerrar cargo atual a partir de agora - ${{t}}</span></div>`).join('') : '';
          if (cur.checked && S.otherChecked) others.querySelectorAll('input').forEach((b) => b.checked = true);
        }};
        cur.checked = S.current;
        document.getElementById('sk1').checked = !!S.skillChecked;
        em.selectedIndex = S.endMonth; ey.value = S.endYear;
        document.getElementById('sm').selectedIndex = 10;
        document.getElementById('sy').value = '2025';
        cur.addEventListener('change', render);
        if (S.revertsEnd) em.addEventListener('change', () => {{ em.selectedIndex = S.endMonth; }});
        render();
        document.getElementById('d').show();
        document.getElementById('save').addEventListener('click', () => {{
          S.saved = {{
            current: cur.checked, endMonth: cur.checked ? null : em.selectedIndex,
            endYear: cur.checked ? null : ey.value, title: document.getElementById('t').value,
            othersChecked: [...others.querySelectorAll('input')].map((b) => b.checked),
            notify: document.getElementById('notify').checked,
          }};
          S.current = cur.checked; S.endMonth = em.selectedIndex; S.endYear = ey.value;
          save(); document.getElementById('d').close();
        }});
        """,
    )


PAGES = {
    "/in/joao/": page_html("<h1>Joao</h1>"),
    "/in/joao/details/experience/": page_html(
        '<div><a href="/in/joao/details/experience/edit/forms/301/"><div>Líder Técnico Frontend</div></a></div>'
    ),
    "/in/joao/details/experience/edit/forms/301/": edit_form(),
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


async def set_state(page: Page, **values: object) -> None:
    await page.goto(f"{BASE}/in/joao/")
    await page.evaluate("(v) => localStorage.setItem('S', JSON.stringify(v))", values)


async def saved(page: Page) -> dict | None:
    return json.loads(await page.evaluate("localStorage.getItem('S') || '{}'")).get(
        "saved"
    )


def editor(page: Page) -> ProfileEditor:
    session = _FastSession(page)
    ed = ProfileEditor(session, _Navigator(session))
    ed.set_network_notification(False)
    return ed


ENDED = {"current": False, "endMonth": 10, "endYear": "2026", "others": []}


async def test_a_role_held_now_reads_as_present(page: Page) -> None:
    await set_state(page)
    assert (await editor(page).read_experience("301")).end == "present"


async def test_an_ended_role_reads_its_end(page: Page) -> None:
    await set_state(page, **ENDED)
    assert (await editor(page).read_experience("301")).end == "10/2026"


async def test_a_role_is_ended_at_the_approved_month(page: Page) -> None:
    await set_state(page)

    await editor(page).write_experience_end("301", expected="present", end="01/2026")

    assert await saved(page) == {
        "current": False,
        "endMonth": 1,
        "endYear": "2026",
        "title": "Líder Técnico Frontend",
        "othersChecked": [],
        "notify": False,
    }


async def test_an_ended_role_is_made_current_without_ending_another(
    page: Page,
) -> None:
    await set_state(page, **{**ENDED, "others": ["Desenvolvedor full stack"]})

    await editor(page).write_experience_end("301", expected="10/2026", end="present")

    state = await saved(page)
    assert state is not None
    assert (state["current"], state["othersChecked"]) == (True, [False])


async def test_a_stale_end_changes_nothing(page: Page) -> None:
    await set_state(page)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).write_experience_end(
            "301", expected="12/2025", end="01/2026"
        )
    assert e.value.code is ProfileEditErrorCode.STALE_CHANGE_SET
    assert await saved(page) is None


async def test_a_form_that_would_end_another_role_is_never_saved(page: Page) -> None:
    await set_state(page, otherChecked=True)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).write_experience(
            "301",
            field="title",
            expected="Líder Técnico Frontend",
            value="Tech Lead Frontend",
        )
    assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
    assert e.value.details["wouldEnd"] == [
        "Encerrar cargo atual a partir de agora - Desenvolvedor Técnico Front End Pleno"
    ]
    assert await saved(page) is None


async def test_the_start_date_is_not_saved_either_if_it_would_end_another(
    page: Page,
) -> None:
    await set_state(page, otherChecked=True)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).write_experience_start(
            "301", expected="10/2025", month=9, year=2025
        )
    assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
    assert await saved(page) is None


async def test_a_checked_skill_does_not_count_as_ending_a_role(page: Page) -> None:
    await set_state(page, skillChecked=True)

    await editor(page).write_experience(
        "301",
        field="title",
        expected="Líder Técnico Frontend",
        value="Tech Lead Frontend",
    )

    assert (await saved(page) or {}).get("title") == "Tech Lead Frontend"


async def test_an_end_the_form_does_not_take_is_never_saved(page: Page) -> None:
    await set_state(page, revertsEnd=True)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).write_experience_end(
            "301", expected="present", end="01/2026"
        )
    assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
    assert await saved(page) is None
