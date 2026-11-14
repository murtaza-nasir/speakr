"""Browser smoke test: every page loads and its header works, in a real browser.

The API-level tests never render a page, so two header regressions reached a
release: a Vue template error (an element with both v-text and fallback text)
that left the header blank, and a request for vendor/js/vue.global.prod.js, a
file the image does not contain, which left the settings-page headers empty.
This test logs in through the login form, opens each page in headless
Chromium, and fails on:

- any same-origin response with status 400 or higher;
- any JavaScript error (uncaught exception or console error);
- template text left in the page (``${`` or ``{{``);
- a header that is not mounted, or a user menu that does not open with its
  entries;
- the Tailwind Play CDN build (vendor/js/tailwind.min.js or
  cdn.tailwindcss.com) requested, or its "should not be used in production"
  console warning; the stylesheet is compiled at build time instead;
- Tailwind utilities from static/css/tailwind.css not applied;
- translation keys (for example ``nav.account``) shown as text in the header.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL (the test.yml browser-smoke job starts one from source with
tests/e2e/run_speakr.py; docker-publish.yml runs the built image before it is
pushed). SPEAKR_SMOKE_USER / SPEAKR_SMOKE_PASSWORD default to the admin in
config/env.whisperx.example.

The response, JavaScript and template checks, with their console allowlist,
are in browser_checks.py, shared with test_group_permission_ui.py.
"""

import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser smoke test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, login, open_page  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
USER = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))

MENU_ENTRIES = ("Notifications", "Settings", "Shared recordings", "Sign out")
# A translation key: two or more dot-separated identifiers, the first one
# lower case (nav.account, inquire.title, tokenBudget.percentage).
TRANSLATION_KEY = re.compile(r"\b[a-z][a-zA-Z]*(?:\.[a-zA-Z]+)+\b")


@pytest.fixture(scope="module")
def browser_page():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        page = context.new_page()
        problems = PageProblems(page, BASE)
        login(page, BASE, USER, PASSWORD)
        problems.reset()
        yield page, problems
        context.close()
        browser.close()


def _open(page, problems, path):
    return open_page(page, problems, BASE, path)


def _check_common(page, problems, path):
    check_common(page, problems, path)


def _check_header_translated(page, path, selector):
    header = page.locator(selector).first
    assert header.count() == 1, f"{path}: no header ({selector})"
    keys = TRANSLATION_KEY.findall(header.inner_text())
    assert not keys, f"{path}: translation keys shown in the header: {keys}"


def _check_user_menu(page, path):
    toggle = page.locator("[data-user-menu-toggle]:visible").first
    assert toggle.count() == 1, f"{path}: no user menu button in the header"
    toggle.click()
    menu = page.locator("[data-user-menu-dropdown]:visible").first
    menu.wait_for(state="visible", timeout=5000)
    text = menu.inner_text()
    missing = [entry for entry in MENU_ENTRIES if entry not in text]
    assert not missing, f"{path}: user menu is missing {missing}"
    page.keyboard.press("Escape")
    page.mouse.click(5, 400)


def test_main_view(browser_page):
    page, problems = browser_page
    _open(page, problems, "/")
    assert page.locator("[data-v-app]").count() >= 1, "/: the app did not mount"
    _check_header_translated(page, "/", "header")
    _check_user_menu(page, "/")
    _check_common(page, problems, "/")


@pytest.mark.parametrize("path", ["/account", "/admin"])
def test_settings_pages(browser_page, path):
    page, problems = browser_page
    response = _open(page, problems, path)
    assert response is not None and response.status == 200, f"{path}: HTTP {response and response.status}"
    header = page.locator("#global-header")
    assert header.count() == 1, f"{path}: no shared page header"
    assert header.get_attribute("data-v-app") is not None, f"{path}: the page header did not mount"
    label = header.locator(".t-meta").first.inner_text().strip()
    assert label, f"{path}: the page label in the header is empty"
    _check_header_translated(page, path, "#global-header")
    _check_user_menu(page, path)
    if path == "/account":
        tabs = page.locator(".tabs button:visible, .tabs a:visible")
        assert tabs.count() >= 3, "/account: the tab strip did not render"
    _check_common(page, problems, path)


def test_inquire_page(browser_page):
    page, problems = browser_page
    response = _open(page, problems, "/inquire")
    if response is None or response.status != 200 or "/inquire" not in page.url:
        pytest.skip("Inquire mode is not enabled")
    problems.failed[:] = [f for f in problems.failed if "/inquire " not in f]
    assert page.locator("#inquire-app[data-v-app]").count() == 1, "/inquire: the app did not mount"
    _check_header_translated(page, "/inquire", "#inquire-app header")
    _check_user_menu(page, "/inquire")
    _check_common(page, problems, "/inquire")


def test_a_recording_detail_view(browser_page):
    page, problems = browser_page
    listing = page.request.get(f"{BASE}/api/recordings?per_page=1")
    items = listing.json().get("recordings", []) if listing.ok else []
    if not items:
        pytest.skip("no recordings on this instance")
    path = f"/recordings/{items[0]['id']}"
    _open(page, problems, path)
    _check_user_menu(page, path)
    _check_common(page, problems, path)


_CHAT_GAP = """() => {
    const p = document.querySelector('.floating-chat-panel');
    const c = document.getElementById('mainContentColumns');
    if (!p || !c) return null;
    const r = p.getBoundingClientRect(), b = c.getBoundingClientRect();
    return {dx: Math.round(b.right - r.right), dy: Math.round(b.bottom - r.bottom),
            inside: r.top >= b.top && r.left >= b.left && r.right <= b.right && r.bottom <= b.bottom};
}"""


def test_the_chat_panel_layout_is_shared_and_anchored(browser_page):
    """One chat layout for every recording, kept as a gap to the content area's corner.

    It used to be saved per recording with absolute coordinates: the panel
    opened on some recordings only, and after a window or sidebar change it
    floated in the middle of the page.
    """
    page, _ = browser_page
    listing = page.request.get(f"{BASE}/api/recordings?per_page=2")
    items = listing.json().get("recordings", []) if listing.ok else []
    if len(items) < 2:
        pytest.skip("needs two recordings")
    first, second = (f"/recordings/{r['id']}" for r in items[:2])
    page.goto(f"{BASE}{first}")
    page.wait_for_timeout(2500)
    page.evaluate("() => localStorage.removeItem('chat_panel_layout')")
    page.click(".floating-chat-fab")
    page.wait_for_timeout(300)
    head = page.locator(".floating-chat-header-title").bounding_box()
    page.mouse.move(head["x"] + 20, head["y"] + 10)
    page.mouse.down()
    page.mouse.move(head["x"] - 80, head["y"] - 40, steps=8)
    page.mouse.up()
    page.wait_for_timeout(300)
    moved = page.evaluate(_CHAT_GAP)
    assert moved and moved["inside"]

    page.goto(f"{BASE}{second}")
    page.wait_for_timeout(2500)
    there = page.evaluate(_CHAT_GAP)
    assert there is not None, "the chat panel did not open on the second recording"
    assert there["inside"] and abs(there["dx"] - moved["dx"]) <= 1

    page.set_viewport_size({"width": 1200, "height": 900})
    page.wait_for_timeout(600)
    narrower = page.evaluate(_CHAT_GAP)
    assert narrower["inside"] and abs(narrower["dx"] - moved["dx"]) <= 1
    keys = page.evaluate("() => Object.keys(localStorage).filter(k => k.startsWith('chat_panel'))")
    assert keys == ["chat_panel_layout"]
    page.set_viewport_size({"width": 1400, "height": 900})
