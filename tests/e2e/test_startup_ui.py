"""Browser test of the start of the signed-in app (#704).

The full-page "Loading…" overlay (templates/includes/loading_overlay.html)
covers the page until its script hides it. The main app shows itself when
it mounts, with the theme in place, and the startup requests continue
underneath. This test checks:

- with GET /api/recordings never answered, the app is shown well before the
  8 second fallback of the overlay, in each of the six UI themes, light and
  dark, on the desktop and on a phone; the recording list shows its own
  loading state, and the theme, mode and background when the overlay goes
  are those of the page once it has settled;
- without loading.js the app removes the overlay itself;
- a page whose script never hides the overlay (/account without
  loading.js) is shown by the 8 second fallback;
- when app.modular.js does not load, the app's template stays hidden and
  the overlay says so and offers a reload, which loads the app;
- a link to a recording opens that recording and not, first, the one that
  was open last.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test.
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

from browser_checks import PageProblems, en, login, precondition_unmet  # noqa: E402
from seed_recordings import TITLES  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))

THEMES = ("default", "cards", "classic", "desk", "focus", "workspace")
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}
# The app must be on screen well before the overlay's own 8 s fallback.
SHOWN_WITHIN = 6000
FALLBACK_WITHIN = 14000
LIST = re.compile(r"/api/recordings(\?.*)?$")

# What the page looks like at the moment the overlay goes: recorded by an
# observer on <html> that is installed before any script of the page runs.
AT_REVEAL = """
(() => {
    const snap = () => {
        const html = document.documentElement;
        return {
            classes: [...html.classList].filter((c) => c !== 'app-loading').sort(),
            uiTheme: html.dataset.uiTheme || null,
            bg: document.body ? getComputedStyle(document.body).backgroundColor : null,
            cloak: !!document.querySelector('[v-cloak]'),
            at: Math.round(performance.now()),
        };
    };
    window.__snap = snap;
    let seen = false;
    // <html> may not exist yet when this runs, so the document is observed.
    const observer = new MutationObserver(() => {
        const html = document.documentElement;
        if (window.__atReveal || !html) return;
        if (html.classList.contains('app-loading')) seen = true;
        else if (seen) { window.__atReveal = snap(); observer.disconnect(); }
    });
    observer.observe(document, {attributes: true, attributeFilter: ['class'], childList: true, subtree: true});
})();
"""

SHOWN = """() => !document.documentElement.classList.contains('app-loading')
    && !document.querySelector('.app-loading-overlay')
    && !!document.querySelector('#app') && getComputedStyle(document.querySelector('#app')).opacity === '1'"""


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def account(browser):
    """The admin's session, the seeded recordings and the theme to put back."""
    context = browser.new_context(viewport=DESKTOP, locale="en-US", timezone_id="UTC")
    page = context.new_page()
    login(page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
    listing = page.request.get(f"{BASE}/api/recordings?per_page=50")
    items = listing.json().get("recordings", []) if listing.ok else []
    ids = {r["title"]: r["id"] for r in items if r.get("title") in TITLES}
    if set(ids) != set(TITLES):
        context.close()
        precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    page.goto(f"{BASE}/")
    page.wait_for_function(SHOWN, timeout=30000)
    saved = page.locator("html").get_attribute("data-ui-theme") or "default"
    state = context.storage_state()
    yield {"state": state, "ids": ids, "page": page}
    set_theme(page, saved)
    context.close()


def csrf(page):
    return page.request.get(f"{BASE}/api/csrf-token").json()["csrf_token"]


def set_theme(page, theme):
    response = page.request.post(f"{BASE}/api/user/preferences",
                                 headers={"X-CSRFToken": csrf(page), "Content-Type": "application/json"},
                                 data=json.dumps({"ui_theme": theme}))
    assert response.ok, f"could not save the theme {theme}: HTTP {response.status}"


def session(browser, account, viewport=DESKTOP, dark=None):
    phone = viewport["width"] < 768
    context = browser.new_context(viewport=viewport, locale="en-US", timezone_id="UTC",
                                  is_mobile=phone, has_touch=phone, storage_state=account["state"])
    script = AT_REVEAL
    if dark is not None:
        script = f"try {{ localStorage.setItem('darkMode', '{str(dark).lower()}'); }} catch (e) {{}}\n" + script
    context.add_init_script(script)
    page = context.new_page()
    return context, page, PageProblems(page, BASE)


def hold_forever(held):
    """A route handler that never answers: the request stays pending."""
    def handler(route):
        if route.request.method == "GET":
            held.append(route)
        else:
            route.continue_()
    return handler


CASES = [(t, "desktop", False) for t in THEMES] + [(t, "desktop", True) for t in THEMES] \
    + [(t, "phone", False) for t in THEMES]


@pytest.mark.parametrize("theme,where,dark", CASES, ids=[f"{t}-{w}-{'dark' if d else 'light'}" for t, w, d in CASES])
def test_the_app_is_shown_while_the_list_never_answers(browser, account, theme, where, dark):
    set_theme(account["page"], theme)
    context, page, problems = session(browser, account, PHONE if where == "phone" else DESKTOP, dark)
    held = []
    try:
        page.route(LIST, hold_forever(held))
        page.goto(f"{BASE}/")
        page.wait_for_function(SHOWN, timeout=SHOWN_WITHIN)
        reveal = page.evaluate("() => window.__atReveal")
        assert reveal, "the overlay was never shown and removed"
        assert reveal["at"] < SHOWN_WITHIN, f"the app was shown after {reveal['at']} ms"
        assert not reveal["cloak"], "the overlay went before the app had mounted"
        assert held, "GET /api/recordings was not requested"
        # The list's own loading state, not an empty list
        loading = page.get_by_text(en("help.loadingRecordings"))
        loading.wait_for(state="visible", timeout=5000)
        assert not page.get_by_text(en("sidebar.noRecordings")).first.is_visible(), "the list says there are no recordings"
        body = page.inner_text("body")
        assert "{{" not in body and "${" not in body, "template text in the page"
        # The theme was in place when the overlay went
        page.wait_for_timeout(1000)
        now = page.evaluate("() => window.__snap()")
        assert now["uiTheme"] == theme, f"the page has the theme {now['uiTheme']}, not {theme}"
        assert reveal["uiTheme"] == now["uiTheme"], f"theme at reveal {reveal['uiTheme']}, later {now['uiTheme']}"
        assert reveal["classes"] == now["classes"], f"classes of <html> at reveal {reveal['classes']}, later {now['classes']}"
        assert reveal["bg"] == now["bg"], f"background at reveal {reveal['bg']}, later {now['bg']}"
        assert ("dark" in now["classes"]) == dark
        if where == "phone":
            assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth"), "sideways scrolling"
        assert not problems.errors, f"JavaScript errors: {problems.errors}"
        assert not problems.failed, f"failed requests: {problems.failed}"
    finally:
        context.close()


def test_without_loading_js_the_app_removes_the_overlay(browser, account):
    set_theme(account["page"], "default")
    context, page, problems = session(browser, account)
    try:
        page.route(re.compile(r"/static/js/loading\.js"), lambda route: route.abort())
        page.goto(f"{BASE}/")
        page.wait_for_function(SHOWN, timeout=SHOWN_WITHIN)
        assert page.evaluate("() => window.__atReveal.at") < SHOWN_WITHIN
        assert not problems.errors, f"JavaScript errors: {problems.errors}"
    finally:
        context.close()


def test_a_page_that_never_hides_the_overlay_is_shown_after_8_seconds(browser, account):
    context, page, problems = session(browser, account)
    try:
        page.route(re.compile(r"/static/js/loading\.js"), lambda route: route.abort())
        page.goto(f"{BASE}/account")
        page.wait_for_timeout(2000)
        assert page.evaluate("() => document.documentElement.classList.contains('app-loading')"), \
            "the overlay went without the fallback; the test no longer covers it"
        page.wait_for_function("() => !document.documentElement.classList.contains('app-loading')"
                               " && !document.querySelector('.app-loading-overlay')", timeout=FALLBACK_WITHIN)
        at = page.evaluate("() => window.__atReveal.at")
        assert 7500 <= at <= FALLBACK_WITHIN, f"the fallback showed the page after {at} ms"
    finally:
        context.close()


def test_an_app_that_does_not_start_offers_a_reload(browser, account):
    context, page, problems = session(browser, account)
    try:
        page.route(re.compile(r"/static/js/app\.modular\.js"), lambda route: route.abort())
        page.goto(f"{BASE}/")
        stalled = page.locator(".app-loading-overlay.app-loading-stalled")
        stalled.wait_for(state="visible", timeout=FALLBACK_WITHIN)
        assert en("server.loadingOverlay.stalled") in stalled.inner_text()
        # The template of the app stays hidden behind the overlay
        state = page.evaluate("""() => ({loading: document.documentElement.classList.contains('app-loading'),
            cloak: document.querySelector('#app').hasAttribute('v-cloak'),
            opacity: getComputedStyle(document.querySelector('#app')).opacity})""")
        assert state == {"loading": True, "cloak": True, "opacity": "0"}, state
        page.unroute(re.compile(r"/static/js/app\.modular\.js"))
        stalled.get_by_role("button", name=en("server.loadingOverlay.reload")).click()
        page.wait_for_function(SHOWN, timeout=30000)
        assert page.locator(".app-loading-stalled").count() == 0
    finally:
        context.close()


def test_a_link_to_a_recording_does_not_open_the_last_one_first(browser, account):
    last, linked = account["ids"][TITLES[0]], account["ids"][TITLES[1]]
    context, page, problems = session(browser, account)
    opened = []
    try:
        page.add_init_script(f"try {{ localStorage.setItem('lastSelectedRecordingId', '{last}'); }} catch (e) {{}}")
        page.on("request", lambda r: opened.append(int(m.group(1))) if (m := re.search(r"/api/recordings/(\d+)$", r.url.split("?")[0])) else None)
        page.goto(f"{BASE}/recordings/{linked}")
        page.wait_for_function(SHOWN, timeout=SHOWN_WITHIN)
        page.wait_for_function(f"() => location.pathname === '/recordings/{linked}' && document.body.innerText.includes({json.dumps(TITLES[1])})",
                               timeout=30000)
        page.wait_for_timeout(1000)
        assert linked in opened, f"the linked recording was not loaded: {opened}"
        assert last not in opened, f"the last viewed recording was opened first: {opened}"
        assert not problems.errors, f"JavaScript errors: {problems.errors}"
    finally:
        context.close()
