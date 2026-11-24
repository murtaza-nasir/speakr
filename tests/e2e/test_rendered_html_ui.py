"""Browser test: summary_html and notes_html are sanitized at the sink (#695).

The answer of the recording route is rewritten so that its summary_html and
notes_html hold the markup a summary legitimately has (headings, lists, a
table, code, an https link, text direction) next to markup that would run
script (an image with onerror, details with ontoggle, a javascript: link). The
summary and notes tabs must show the first and none of the second, and no
script may run.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL. With SPEAKR_E2E_SHOTS set, screenshots are saved there.
"""

import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, en, login, precondition_unmet  # noqa: E402
from seed_recordings import TITLES  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")
MEETING_TITLE = TITLES[1]

# An image that fails to decode, so an onerror handler that survived would run
BROKEN_IMAGE = "data:image/png;base64,AAAA"
LEGITIMATE = (
    '<h2 dir="auto">Decisions</h2>'
    '<ul dir="auto"><li dir="auto">Budget <strong>approved</strong></li></ul>'
    '<table><thead><tr><th dir="auto">Owner</th></tr></thead><tbody><tr><td dir="auto">Dana</td></tr></tbody></table>'
    '<div class="codehilite"><pre><span></span><code>x = 1\n</code></pre></div>'
    '<p dir="auto"><a href="https://example.com/doc">doc link</a></p>'
)
HOSTILE = (
    f'<img src="{BROKEN_IMAGE}" onerror="window.__xss695=1">'
    '<details open ontoggle="window.__xss695=2"><summary>more</summary>x</details>'
    '<p><a id="bad-link" href="javascript:window.__xss695=3">bad link</a></p>'
)


def _hostile(route):
    response = route.fetch()
    try:
        body = response.json()
    except Exception:
        route.fulfill(response=response)
        return
    if isinstance(body, dict) and route.request.method == "GET":
        body.update({
            "summary": "Decisions", "summary_html": LEGITIMATE + HOSTILE,
            "notes": "Notes", "notes_html": '<p dir="auto">Call <em>Dana</em></p>' + HOSTILE,
        })
    route.fulfill(response=response, body=json.dumps(body), headers={**response.headers, "content-type": "application/json"})


def _check_sink(page, region):
    assert region.locator("[onerror], [ontoggle]").count() == 0
    image = region.locator("img")
    assert image.count() == 1 and image.get_attribute("onerror") is None
    bad = region.get_by_text("bad link")
    href = bad.get_attribute("href")
    assert not href or not href.lower().startswith("javascript:")
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.__xss695") is None


def test_server_html_is_sanitized_in_summary_and_notes():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        page = context.new_page()
        problems = PageProblems(page, BASE)
        login(page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        listing = page.request.get(f"{BASE}/api/recordings?per_page=50")
        ids = {r["title"]: r["id"] for r in listing.json().get("recordings", [])} if listing.ok else {}
        if MEETING_TITLE not in ids:
            precondition_unmet("the seeded recordings are not on this instance (tests/e2e/seed_recordings.py)")
        rid = ids[MEETING_TITLE]
        page.route(re.compile(rf".*/api/recordings/{rid}(\?.*)?$"), _hostile)
        problems.reset()
        page.goto(f"{BASE}/recordings/{rid}")

        summary_tab = re.compile(rf"^\s*{re.escape(en('summary.title'))}\s*\d*\s*$")
        page.locator("button.tab:visible").filter(has_text=summary_tab).first.click()
        summary = page.locator(f'[role="region"][aria-label="{en("summary.title")}"]')
        summary.locator("h2").wait_for(state="visible", timeout=30000)
        assert summary.locator("h2").inner_text() == "Decisions"
        assert summary.locator("li strong").inner_text() == "approved"
        assert summary.locator("table td").inner_text() == "Dana"
        assert summary.locator("pre code").inner_text().strip() == "x = 1"
        assert summary.locator("div.codehilite").count() == 1
        assert summary.locator('a[href="https://example.com/doc"]').inner_text() == "doc link"
        assert summary.locator('p[dir="auto"]').count() >= 1
        _check_sink(page, summary)
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            page.screenshot(path=os.path.join(SHOTS, "rendered-html-summary.png"))

        notes_tab = re.compile(rf"^\s*{re.escape(en('notes.title'))}\s*\d*\s*$")
        page.locator("button.tab:visible").filter(has_text=notes_tab).first.click()
        notes = page.locator(f'[role="region"][aria-label="{en("notes.title")}"]')
        notes.locator("em").wait_for(state="visible", timeout=10000)
        assert notes.locator("em").inner_text() == "Dana"
        _check_sink(page, notes)
        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS, "rendered-html-notes.png"))

        check_common(page, problems, "rendered summary and notes")
        context.close()
        browser.close()
