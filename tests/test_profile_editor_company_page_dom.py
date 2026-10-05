"""Browser-DOM tests for creating a company page through LinkedIn's setup form.

The synthetic page reproduces what was measured on a pt-BR account on
4 October 2026 at linkedin.com/company/setup/new/: a chooser of page kinds
whose buttons read "Empresa ..." and the like, then a form whose controls carry
ids ending in LinkedIn's form-item names, a message box "<id>-error" per field
(the logo's holds a size hint from the start), an industry typeahead that
reports "Selecione um setor." until a suggestion is chosen, an address check
that reports an address in use, the representative statement checkbox and a
"Criar página" button that stays disabled until the form is valid. Nothing
leaves the test browser.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from patchright.async_api import Page, Route, async_playwright

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_editor import ProfileEditor
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import NewCompanyPage

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

BASE = "https://www.linkedin.com"
P = "urn-li-fsu-pageCreationFormItem"
INDUSTRIES = [
    "Hospitais e atividades de atenção à saúde humana",
    "Hospitais",
    "Tecnologia, Informação e Internet",
]
SIZES = [
    "0–1 funcionários",
    "2–10 funcionários",
    "11–50 funcionários",
    "51–200 funcionários",
    "201–500 funcionários",
    "501–1.000 funcionários",
    "1.001–5.000 funcionários",
    "5.001–10.000 funcionários",
    "+ de 10.000 funcionários",
]
TYPES = [
    "Empresa de capital aberto",
    "Autônomo",
    "Órgão governamental",
    "ONG",
    "Firma individual",
    "Empresa privada",
    "Sociedade",
]

STATE_JS = """
const S = Object.assign({taken: ['bedoc'], captcha: false, chooser: true},
  JSON.parse(localStorage.getItem('S') || '{}'));
const save = () => localStorage.setItem('S', JSON.stringify(S));
"""


def page_html(body: str, script: str = "") -> str:
    return (
        '<!doctype html><html lang="pt"><head><meta charset="utf-8">'
        "<title>LinkedIn</title></head>"
        f"<body>{body}<script>{STATE_JS}{script}</script></body></html>"
    )


def field(kind: str, item: str, label: str, control: str) -> str:
    fid = f"{kind}-form-component-{P}-{item}"
    return (
        f'<div><label for="{fid}">{label}</label>'
        + control.format(id=fid)
        + f'<div id="{fid}-error"></div></div>'
    )


FORM = (
    '<div id="form" hidden>'
    + field("single-line-text", "NAME", "Nome", '<input id="{id}" type="text">')
    + field(
        "single-line-text",
        "UNIVERSAL-NAME",
        "linkedin.com/company/",
        '<input id="{id}" type="text">',
    )
    + field("single-line-text", "WEBSITE", "Site", '<input id="{id}" type="text">')
    + field(
        "single-typeahead-entity",
        "INDUSTRY",
        "Setor",
        '<input id="{id}" type="text" role="combobox"><div role="listbox" id="lb"></div>',
    )
    + field(
        "text-entity-list",
        "ORGANIZATION-SIZE",
        "Tamanho de organização",
        '<select id="{id}"><option>Selecionar tamanho</option>'
        + "".join(f"<option>{s}</option>" for s in SIZES)
        + "</select>",
    )
    + field(
        "text-entity-list",
        "ORGANIZATION-TYPE",
        "Tipo de organização",
        '<select id="{id}"><option>Selecionar tipo</option>'
        + "".join(f"<option>{t}</option>" for t in TYPES)
        + "</select>",
    )
    + field(
        "media-upload",
        "LOGO",
        "Logomarca",
        '<input id="{id}" type="file" accept=".jpg,.jpeg,.png">',
    )
    + field("multiline-text", "TAGLINE", "Slogan", '<textarea id="{id}"></textarea>')
    + '<input type="checkbox" id="urn:li:fsu_pageCreationFormItem:TERMS_AND_CONDITIONS-0"'
    ' name="urn:li:fsu_pageCreationFormItem:TERMS_AND_CONDITIONS">'
    "<span>Declaro que sou representante oficial desta organização</span>"
    '<button id="create" disabled>Criar página</button>'
    '<iframe id="challenge" hidden style="width:300px;height:300px"></iframe>'
    "</div>"
)

CHOOSER = (
    '<div id="chooser">'
    '<button type="button" id="kind-company">Empresa <span>Pequenas, médias e grandes empresas</span></button>'
    '<button type="button">Showcase Page <span>Páginas secundárias</span></button>'
    '<button type="button">Instituição de ensino <span>Escolas, faculdades e universidades</span></button>'
    "</div>"
)

SETUP = page_html(
    CHOOSER + FORM,
    f"""
    const P = {json.dumps(P)};
    const INDUSTRIES = {json.dumps(INDUSTRIES, ensure_ascii=False)};
    const el = (kind, item) => document.getElementById(`${{kind}}-form-component-${{P}}-${{item}}`);
    const err = (kind, item) => document.getElementById(`${{kind}}-form-component-${{P}}-${{item}}-error`);
    const name = el('single-line-text', 'NAME'), address = el('single-line-text', 'UNIVERSAL-NAME');
    const industry = el('single-typeahead-entity', 'INDUSTRY');
    const size = el('text-entity-list', 'ORGANIZATION-SIZE'), type = el('text-entity-list', 'ORGANIZATION-TYPE');
    const terms = document.querySelector('[name$="TERMS_AND_CONDITIONS"]');
    const create = document.getElementById('create');
    let chosenIndustry = '';
    const showForm = () => {{
      document.getElementById('chooser').remove();
      document.getElementById('form').hidden = false;
    }};
    if (S.chooser) document.getElementById('kind-company').addEventListener('click', showForm);
    else showForm();
    err('media-upload', 'LOGO').textContent = 'Recomendamos 300 x 300 px. Aceitamos JPGs, JPEGs e PNGs';
    const validate = () => {{
      const taken = S.taken.includes(address.value);
      err('single-line-text', 'UNIVERSAL-NAME').textContent = taken
        ? 'Esta URL pública já está em uso.' : '';
      err('single-typeahead-entity', 'INDUSTRY').textContent =
        industry.value && industry.value !== chosenIndustry ? 'Selecione um setor.' : '';
      create.disabled = S.stuck || !(name.value && address.value && !taken && chosenIndustry
        && industry.value === chosenIndustry && size.selectedIndex > 0
        && type.selectedIndex > 0 && terms.checked);
    }};
    name.addEventListener('input', () => {{
      address.value = name.value.toLowerCase().replace(/\\s+/g, ''); validate();
    }});
    for (const c of [address, size, type, terms]) {{
      c.addEventListener('input', validate); c.addEventListener('change', validate);
    }}
    industry.addEventListener('input', () => {{
      const q = industry.value.toLowerCase();
      document.getElementById('lb').innerHTML = INDUSTRIES
        .filter((i) => q && i.toLowerCase().startsWith(q.slice(0, 6)))
        .map((i) => `<div role="option">${{i}}</div>`).join('');
      for (const o of document.querySelectorAll('[role=option]')) o.addEventListener('click', () => {{
        industry.value = o.textContent; chosenIndustry = o.textContent;
        document.getElementById('lb').innerHTML = ''; validate();
      }});
      validate();
    }});
    create.addEventListener('click', () => {{
      if (S.captcha) {{
        const f = document.getElementById('challenge');
        f.src = 'https://www.google.com/recaptcha/api2/bframe?k=x'; f.hidden = false;
        return;
      }}
      const logo = el('media-upload', 'LOGO');
      S.created = {{
        name: name.value, address: address.value,
        website: el('single-line-text', 'WEBSITE').value, industry: industry.value,
        size: size.selectedIndex, type: type.selectedOptions[0].textContent,
        logo: logo.files.length ? logo.files[0].name : null,
        tagline: el('multiline-text', 'TAGLINE').value, terms: terms.checked,
      }};
      save();
      location.assign('/company/123/admin/dashboard/');
    }});
    """,
)

PUBLIC_PAGE = page_html(
    '<h1 id="h"></h1><p id="t"></p>',
    """
    const slug = location.pathname.split('/')[2];
    if (S.created && S.created.address === slug) {
      document.getElementById('h').textContent = S.created.name;
      document.getElementById('t').textContent = S.created.tagline;
    } else document.body.innerHTML = '<p>Página não encontrada</p>';
    """,
)


async def _route(route: Route) -> None:
    url = route.request.url
    path = url.removeprefix(BASE).split("?")[0]
    if not url.startswith(BASE):
        await route.abort()  # hermetic: nothing leaves the test browser
    elif path == "/company/setup/new/":
        await route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=SETUP
        )
    elif path.startswith("/company/123/admin/"):
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=page_html("<h1>Painel</h1>"),
        )
    elif path.startswith("/company/"):
        await route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=PUBLIC_PAGE
        )
    elif path == "/in/joao/":
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=page_html("<h1>Joao</h1>"),
        )
    else:
        await route.fulfill(status=404, body="")


class _Navigator(PageNavigator):
    async def _navigate_to_page(self, url: str) -> None:
        await self._session.page.goto(url, wait_until="domcontentloaded")


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


async def created(page: Page) -> dict | None:
    state = json.loads(await page.evaluate("localStorage.getItem('S') || '{}'"))
    return state.get("created")


def editor(page: Page) -> ProfileEditor:
    session = _FastSession(page)
    ed = ProfileEditor(session, _Navigator(session))
    ed.set_network_notification(False)
    return ed


@pytest.fixture
def logo(tmp_path: Path) -> Path:
    path = tmp_path / "bedoc-logo.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0 synthetic")
    return path


def bedoc(**overrides: object) -> NewCompanyPage:
    values: dict = {
        "name": "BeDoc",
        "public_url": "sejabedoc",
        "industry": "Hospitais e atividades de atenção à saúde humana",
        "size": "2-10",
        "organization_type": "privately_held",
        "website": "https://sejabedoc.com.br",
        "tagline": "Triagem por IA que leva o paciente ao especialista certo.",
        "representative_declared": True,
    }
    values.update(overrides)
    return NewCompanyPage(**values)


async def test_every_field_is_filled_as_approved_and_the_page_created(
    page: Page, logo: Path
) -> None:
    await set_state(page, taken=["bedoc"], captcha=False, chooser=True)
    ed = editor(page)

    landed = await ed.create_company_page(bedoc(logo_path=str(logo)))

    assert landed == f"{BASE}/company/123/admin/dashboard/"
    assert await created(page) == {
        "name": "BeDoc",
        "address": "sejabedoc",
        "website": "https://sejabedoc.com.br",
        "industry": "Hospitais e atividades de atenção à saúde humana",
        "size": 2,
        "type": "Empresa privada",
        "logo": "bedoc-logo.jpg",
        "tagline": "Triagem por IA que leva o paciente ao especialista certo.",
        "terms": True,
    }
    assert ed.last_network_notification() == "not_offered"
    shown = await ed.read_company_page("sejabedoc")
    assert (shown["name"], shown["url"]) == ("BeDoc", f"{BASE}/company/sejabedoc/")


async def test_the_form_is_used_directly_when_no_chooser_comes_first(
    page: Page,
) -> None:
    await set_state(page, taken=[], captcha=False, chooser=False)

    await editor(page).create_company_page(bedoc())

    assert (await created(page) or {}).get("name") == "BeDoc"


async def test_an_address_in_use_creates_nothing(page: Page) -> None:
    await set_state(page, taken=["sejabedoc"], captcha=False, chooser=True)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).create_company_page(bedoc())
    assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
    assert e.value.details["fieldMessages"] == {
        "UNIVERSAL-NAME": "Esta URL pública já está em uso."
    }
    assert await created(page) is None


async def test_an_industry_linkedin_does_not_offer_creates_nothing(page: Page) -> None:
    await set_state(page, taken=[], captcha=False, chooser=True)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).create_company_page(bedoc(industry="Hospitais e clínicas"))
    assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR
    assert await created(page) is None


async def test_a_security_check_is_handed_back_unsolved(page: Page) -> None:
    await set_state(page, taken=[], captcha=True, chooser=True)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).create_company_page(bedoc())
    assert e.value.code is ProfileEditErrorCode.AUTHENTICATION_REQUIRED
    assert await created(page) is None


async def test_nothing_is_created_without_the_users_statement(page: Page) -> None:
    await set_state(page, taken=[], captcha=False, chooser=True)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).create_company_page(bedoc(representative_declared=False))
    assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR
    assert await created(page) is None


async def test_a_create_button_left_disabled_creates_nothing(page: Page) -> None:
    await set_state(page, taken=[], captcha=False, chooser=True, stuck=True)
    with pytest.raises(ProfileEditError) as e:
        await editor(page).create_company_page(bedoc())
    assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
    assert e.value.details["createEnabled"] is False
    assert await created(page) is None
