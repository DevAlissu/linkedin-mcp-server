"""Browser-DOM test for reading a rich-text description line by line.

The synthetic editor behaves like the one measured on a pt-BR position form on
4 October 2026: every line is its own <p>, a blank line is an empty <p>, and
both Enter and Shift+Enter start a new <p>. innerText separates those <p> with
a blank line, so a description written as consecutive lines would read back as
paragraphs and fail verification after a good save.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from patchright.async_api import Page, Route, async_playwright

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_editor import ProfileEditor
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.profile_edit.model import normalize_text

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

BASE = "https://www.linkedin.com"
BEFORE_HTML = "<p>Plataforma de P&amp;D.</p><p><br></p><p>Lidero o front-end.</p>"
BEFORE = "Plataforma de P&D.\n\nLidero o front-end."
SECTIONED = (
    "Abertura.\n\nLiderança Técnica:\n• Defini a arquitetura\n• Revisei os PRs\n\nFim."
)


def page_html(body: str, script: str = "") -> str:
    return (
        '<!doctype html><html lang="pt"><head><meta charset="utf-8"><title>Joao | LinkedIn</title></head>'
        f"<body><main>{body}</main><script>"
        f"const S = Object.assign({{html: {json.dumps(BEFORE_HTML)}}}, JSON.parse(localStorage.getItem('S') || '{{}}'));"
        "const save = () => localStorage.setItem('S', JSON.stringify(S));"
        f"{script}</script></body></html>"
    )


EDIT_FORM = page_html(
    '<dialog id="d"><h2>Editar cargo</h2><input type="checkbox" role="switch" id="notify">'
    '<span id="lt">Cargo*</span><input id="t" aria-labelledby="lt" value="Líder Técnico Frontend">'
    '<div role="textbox" contenteditable="true" aria-label="Descrição, máximo de 2.000 caracteres"></div>'
    '<button type="button" id="save">Salvar</button></dialog>',
    """
    const box = document.querySelector('[role=textbox]');
    box.innerHTML = S.html;
    // Enter and Shift+Enter both open a new paragraph, as LinkedIn's editor does.
    box.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const sel = getSelection();
      let p = sel.anchorNode;
      while (p && p !== box && p.nodeName !== 'P') p = p.parentNode;
      const next = document.createElement('p');
      next.appendChild(document.createElement('br'));
      if (p && p !== box) p.after(next); else box.appendChild(next);
      const r = document.createRange();
      r.setStart(next, 0);
      r.collapse(true);
      sel.removeAllRanges();
      sel.addRange(r);
    });
    document.getElementById('d').show();
    document.getElementById('save').addEventListener('click', () => {
      S.html = box.innerHTML; save(); document.getElementById('d').close();
    });
    """,
)

PAGES = {
    "/in/joao/": page_html("<h1>Joao</h1>"),
    "/in/joao/details/experience/": page_html(
        '<div><a href="/in/joao/details/experience/edit/forms/301/"><div>Líder Técnico Frontend</div></a></div>'
    ),
    "/in/joao/details/experience/edit/forms/301/": EDIT_FORM,
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


async def test_paragraphs_and_blank_lines_read_as_they_display(page: Page) -> None:
    form = await editor(page).read_experience("301")
    assert normalize_text(form.description.value) == BEFORE


async def test_consecutive_lines_survive_a_save_as_lines(page: Page) -> None:
    await editor(page).write_experience(
        "301", field="description", expected=BEFORE, value=SECTIONED
    )

    stored = json.loads(await page.evaluate("localStorage.getItem('S')"))["html"]
    assert stored.count("<p>") == 7  # six lines and one blank, each its own <p>
    form = await editor(page).read_experience("301")
    assert normalize_text(form.description.value) == SECTIONED
