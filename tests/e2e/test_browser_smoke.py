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
  entries.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL (the test.yml browser-smoke job starts one from source with
tests/e2e/run_speakr.py; docker-publish.yml runs the built image before it is
pushed). SPEAKR_SMOKE_USER / SPEAKR_SMOKE_PASSWORD default to the admin in
config/env.whisperx.example.

Console messages that are not failures (documented allowlist):
- messages from browser extensions (chrome-extension://);
- "Failed to load resource": the browser's echo of a failed response, which
  is already reported with its URL by the response check.
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

from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
USER = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))

MENU_ENTRIES = ("Notifications", "Settings", "Shared Transcripts", "Sign Out")
IGNORED_CONSOLE = (re.compile(r"chrome-extension://"), re.compile(r"Failed to load resource"))


class PageProblems:
    """Collects failed responses and JavaScript errors for the current page."""

    def __init__(self, page):
        self.failed, self.errors = [], []
        page.on("response", self._response)
        page.on("pageerror", lambda exc: self.errors.append(f"uncaught: {exc}"))
        page.on("console", self._console)

    def _response(self, response):
        if response.url.startswith(BASE) and response.status >= 400:
            self.failed.append(f"{response.status} {response.url}")

    def _console(self, msg):
        if msg.type != "error":
            return
        text = msg.text
        location = (msg.location or {}).get("url", "")
        if any(p.search(text) or p.search(location) for p in IGNORED_CONSOLE):
            return
        self.errors.append(f"console: {text}")

    def reset(self):
        self.failed.clear()
        self.errors.clear()


@pytest.fixture(scope="module")
def browser_page():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        page = context.new_page()
        problems = PageProblems(page)
        page.goto(f"{BASE}/login")
        page.fill("input[name=email]", USER)
        page.fill("input[name=password]", PASSWORD)
        page.locator("form:has(input[name=password]) [type=submit]:visible").first.click()
        page.wait_for_load_state("load")
        assert "/login" not in page.url, f"could not log in as {USER}"
        problems.reset()
        yield page, problems
        context.close()
        browser.close()


def _open(page, problems, path):
    # Leave the previous page first: its requests still in flight are
    # aborted on unload and logged as "Failed to fetch", which belongs to
    # that page, not the one being checked.
    page.goto("about:blank")
    page.wait_for_timeout(300)
    problems.reset()
    response = page.goto(f"{BASE}{path}")
    page.wait_for_load_state("load")
    # Headers mount after the page's scripts run; give polling apps a moment.
    page.wait_for_timeout(2500)
    return response


def _check_common(page, problems, path):
    body = page.inner_text("body")
    leftovers = [m for m in ("${", "{{") if m in body]
    assert not leftovers, f"{path}: template text left in the page: {leftovers}"
    assert not problems.failed, f"{path}: failed requests: {problems.failed}"
    assert not problems.errors, f"{path}: JavaScript errors: {problems.errors}"


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
