"""Browser-DOM tests for adding a current position through the new-position form.

The synthetic page reproduces the form measured on a pt-BR account on
4 October 2026: a notify-your-network switch, "Cargo*" labelled through
aria-labelledby, an "Empresa/organização" typeahead, "Tipo de localidade" and
"Tipo de emprego" dropdowns, one plain checkbox for "I currently work here"
(checked by default), start month and year dropdowns, a rich-text description
and a text-only "Salvar" button. Nothing leaves the test browser.
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
from linkedin_mcp_server.profile_edit.model import NewPosition, normalize_text

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

BASE = "https://www.linkedin.com"
INOVA = "INOVA - Polo de Inovação IFAM"

HEADLINE = "Software Engineer | Mobile & Web Developer"

STATE_JS = """
const S = Object.assign({notify: false, current: true, extraCheckbox: false,
  headline: 'Software Engineer | Mobile & Web Developer'},
  JSON.parse(localStorage.getItem('S') || '{}'));
const save = () => localStorage.setItem('S', JSON.stringify(S));
"""

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
COMPANIES = ["Inova", "INOVA+", INOVA, "Polo de Inovação IFCE"]


def page_html(body: str, script: str = "") -> str:
    return (
        '<!doctype html><html lang="pt"><head><meta charset="utf-8">'
        "<title>Joao | LinkedIn</title></head>"
        f"<body><main>{body}</main><script>{STATE_JS}{script}</script></body></html>"
    )


def new_position_form() -> str:
    months = "".join(f"<option>{m}</option>" for m in MONTHS)
    years = "".join(f"<option>{y}</option>" for y in range(2026, 1999, -1))
    return page_html(
        '<dialog id="d"><h2>Adicione um cargo ao seu perfil</h2>'
        '<input type="checkbox" role="switch" id="notify">'
        '<span id="lt">Cargo*</span><input id="t" aria-labelledby="lt">'
        '<label for="c">Empresa/organização</label><input id="c"><div role="listbox" id="lb"></div>'
        '<label for="lt2">Tipo de localidade</label><select id="lt2"><option>Selecione</option>'
        "<option>Presencial</option><option>Híbrido</option><option>Remoto</option></select>"
        '<label for="et">Tipo de emprego</label><select id="et"><option>Selecione</option>'
        "<option>Tempo integral</option><option>Meio período</option><option>Autônomo</option></select>"
        '<input type="checkbox" id="cur"><span id="extra"></span>'
        f'<label for="sm">Mês de início</label><select id="sm"><option>Month</option>{months}</select>'
        f'<label for="sy">Ano de início*</label><select id="sy"><option>Year</option>{years}</select>'
        '<div role="textbox" contenteditable="true" aria-label="Descrição, máximo de 2.000 caracteres"></div>'
        # "Atualizar título do perfil", as measured: the new title preselected,
        # the current headline second with its "(atual)" marker in an <em>.
        '<fieldset role="radiogroup">'
        '<div role="radio" tabindex="0" aria-checked="true" id="hnew"><div><input type="radio" checked name="h">'
        "<label></label></div><p>Novo cargo da empresa</p></div>"
        '<div role="radio" tabindex="0" aria-checked="false" id="hcur"><div><input type="radio" name="h">'
        '<label></label></div><p><span id="hl"></span><span> </span><em>(atual)</em></p></div>'
        "</fieldset>"
        '<button type="button" id="save">Salvar</button></dialog>',
        f"""
        const COMPANIES = {json.dumps(COMPANIES, ensure_ascii=False)};
        document.getElementById('notify').checked = S.notify;
        document.getElementById('cur').checked = S.current;
        if (S.extraCheckbox) document.getElementById('extra').outerHTML = '<input type="checkbox" id="x">';
        const box = document.getElementById('c');
        box.addEventListener('input', () => {{
          const q = box.value.toLowerCase();
          document.getElementById('lb').innerHTML = q.length < 3 ? '' :
            COMPANIES.filter((c) => c.toLowerCase().includes(q.slice(0, 5))).map((c) => `<div role="option">${{c}}</div>`).join('');
          for (const o of document.querySelectorAll('[role=option]')) o.addEventListener('click', () => {{
            box.value = o.textContent; document.getElementById('lb').innerHTML = '';
          }});
        }});
        document.getElementById('hl').textContent = S.headline;
        for (const r of document.querySelectorAll('[role=radio]')) r.addEventListener('click', () => {{
          for (const o of document.querySelectorAll('[role=radio]')) o.setAttribute('aria-checked', String(o === r));
        }});
        document.getElementById('d').show();
        document.getElementById('save').addEventListener('click', () => {{
          const sel = (id) => document.getElementById(id);
          S.saved = {{
            headline: sel('hcur').getAttribute('aria-checked') === 'true' ? 'kept' : 'replaced',
            title: sel('t').value, company: sel('c').value,
            locationType: sel('lt2').selectedOptions[0].textContent,
            employmentType: sel('et').selectedOptions[0].textContent,
            current: sel('cur').checked, month: sel('sm').selectedIndex,
            year: sel('sy').selectedOptions[0].textContent,
            description: document.querySelector('[role=textbox]').innerText.replace(/\\n$/, ''),
            notify: sel('notify').checked,
          }};
          save(); sel('d').close();
        }});
        """,
    )


PAGES = {
    "/in/joao/": page_html("<h1>Joao</h1>"),
    "/in/joao/edit/forms/position/new/": new_position_form(),
}


async def _route(route: Route) -> None:
    url = route.request.url
    path = url.removeprefix(BASE).split("?")[0]
    if not url.startswith(BASE):
        await route.abort()  # hermetic: nothing leaves the test browser
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
    state = json.loads(await page.evaluate("localStorage.getItem('S') || '{}'"))
    return state.get("saved")


async def saved_form(page: Page) -> dict:
    """What the form saved; fails the test if it saved nothing."""
    state = await saved(page)
    assert state is not None, "the form was never saved"
    return state


def editor(page: Page, notify: bool | None = False) -> ProfileEditor:
    session = _FastSession(page)
    ed = ProfileEditor(session, _Navigator(session))
    ed.set_network_notification(notify)
    return ed


POSITION = NewPosition(
    title="Líder Técnico Frontend",
    company=INOVA,
    start_month=4,
    start_year=2026,
    employment_type="part_time",
    location_type="remote",
    description="Plataforma que monitora queimadas.\n\nDo protótipo à produção.",
)


async def test_every_field_is_filled_as_approved_and_saved(page: Page) -> None:
    await set_state(page)
    ed = editor(page)

    await ed.add_experience(POSITION, keep_headline=HEADLINE)

    stored = await saved_form(page)
    # An editor stores a paragraph break as an empty paragraph (see normalize_text).
    stored["description"] = normalize_text(stored["description"])
    assert stored == {
        "headline": "kept",
        "title": "Líder Técnico Frontend",
        "company": INOVA,
        "locationType": "Remoto",
        "employmentType": "Meio período",
        "current": True,
        "month": 4,
        "year": "2026",
        "description": "Plataforma que monitora queimadas.\n\nDo protótipo à produção.",
        "notify": False,
    }
    assert ed.last_network_notification() == "off"


async def test_a_notify_switch_left_on_is_turned_off_before_saving(page: Page) -> None:
    await set_state(page, notify=True)

    await editor(page, notify=False).add_experience(POSITION, keep_headline=HEADLINE)

    assert (await saved_form(page))["notify"] is False


async def test_the_current_role_box_is_checked_when_it_was_not(page: Page) -> None:
    await set_state(page, current=False)

    await editor(page).add_experience(POSITION, keep_headline=HEADLINE)

    assert (await saved_form(page))["current"] is True


async def test_a_company_without_an_exact_suggestion_saves_nothing(page: Page) -> None:
    await set_state(page)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).add_experience(
            NewPosition(
                title="Dev", company="INOVA Polo", start_month=1, start_year=2026
            ),
            keep_headline=HEADLINE,
        )
    assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR
    assert await saved(page) is None


async def test_an_unexpected_second_checkbox_stops_before_saving(page: Page) -> None:
    await set_state(page, extraCheckbox=True)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).add_experience(POSITION, keep_headline=HEADLINE)
    assert e.value.code is ProfileEditErrorCode.SELECTOR_NOT_FOUND
    assert await saved(page) is None


async def test_the_current_headline_is_kept_over_the_preselected_one(
    page: Page,
) -> None:
    await set_state(page)

    await editor(page).add_experience(POSITION, keep_headline=HEADLINE)

    assert (await saved_form(page))["headline"] == "kept"


async def test_choices_without_the_current_headline_save_nothing(page: Page) -> None:
    await set_state(page, headline="Another headline")
    with pytest.raises(ProfileEditError) as e:
        await editor(page).add_experience(POSITION, keep_headline=HEADLINE)
    assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
    assert e.value.details["offered"] == ["Novo cargo da empresa", "Another headline"]
    assert await saved(page) is None
