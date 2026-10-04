"""A profile at LinkedIn's 100-skill maximum is read in full.

LinkedIn allows up to 100 skills (Help answer a549047). Its skills view loads
about ten per scroll, so a full profile needs more scrolls than an endless view
is allowed before the reader gives up. A pt-BR account with 100 skills was once
refused as INCOMPLETE_READ after 80; this browser-DOM test reproduces that list
with synthetic pages routed under www.linkedin.com.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from patchright.async_api import Page, Route, async_playwright

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_editor import ProfileEditor
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

BASE = "https://www.linkedin.com"
TOTAL = 100
BATCH = 10

# Like LinkedIn: the list scrolls inside its own container, so scrolling the
# window loads nothing, and each scroll of the container loads one batch.
SKILLS_PAGE = f"""<!doctype html><html lang="pt"><head><meta charset="utf-8">
<title>Joao | LinkedIn</title></head><body><main>
<div id="skills" style="height:600px;overflow-y:auto"></div></main>
<script>
const box = document.getElementById('skills');
let n = 0, loading = false;
const more = () => {{
  const html = [];
  for (let i = 0; i < {BATCH} && n < {TOTAL}; i += 1) {{
    n += 1;
    html.push(`<div style="height:120px"><div>Competência ${{n}}</div>`
      + `<a href="/in/joao/details/skills/edit/forms/${{7000 + n}}/" aria-label="Editar">`
      + `<span>Editar</span></a></div>`);
  }}
  box.insertAdjacentHTML('beforeend', html.join(''));
}};
more();
box.addEventListener('scroll', () => {{
  if (loading) return;
  loading = true;
  setTimeout(() => {{ more(); loading = false; }}, 300);
}});
</script></body></html>"""


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
    elif path == "/in/joao/":
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body='<!doctype html><html lang="pt"><body><main><h1>Joao</h1></main></body></html>',
        )
    elif path == "/in/joao/details/skills/":
        await route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=SKILLS_PAGE
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


async def test_a_profile_at_the_100_skill_maximum_is_read_in_full(page: Page) -> None:
    session = _FastSession(page)
    editor = ProfileEditor(session, _Navigator(session))

    skills = await editor.list_skills()

    assert len(skills) == TOTAL
    assert [s.name for s in skills[:2]] == ["Competência 1", "Competência 2"]
    assert skills[-1].name == f"Competência {TOTAL}"
    assert [s.position for s in skills] == list(range(1, TOTAL + 1))
