"""Browser test of two fixes in the recording header.

- #686: the pen beside the title opens the title field with the cursor in
  it and the title selected (the field was looked up by a `ref` attribute
  that Vue does not render, so the focus never moved).
- #685: the share dialog offers public links only while public sharing is
  on: with enable_public_sharing off it says that no public link can be
  created and has no button for one; switched on again, the button is back.

It uses the seeded recordings of seed_recordings.py and puts the setting
back afterwards (an empty value: the .env value applies). It runs only with
SPEAKR_BROWSER_SMOKE=1 against a running Speakr at SPEAKR_BASE_URL, the
instance of the browser smoke test.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, en, login, open_page, precondition_unmet  # noqa: E402
from seed_recordings import TITLES  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")
MEETING_TITLE = TITLES[1]
WINDOW = ".modal-overlay:visible"


class Admin:
    def __init__(self, browser):
        self.context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.problems.reset()
        listing = self.page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        self.ids = {r["title"]: r["id"] for r in items}

    def api(self, method, path, data=None):
        token = self.page.locator("meta[name=csrf-token]").first.get_attribute("content")
        return self.page.request.fetch(f"{BASE}{path}", method=method, headers={"X-CSRFToken": token}, data=data)

    def shot(self, name):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            self.page.wait_for_timeout(300)
            self.page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))


@pytest.fixture(scope="module")
def admin():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        session = Admin(browser)
        if MEETING_TITLE not in session.ids:
            browser.close()
            precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
        yield session
        session.context.close()
        browser.close()


def test_the_pen_puts_the_cursor_in_the_title(admin):
    page = admin.page
    open_page(page, admin.problems, BASE, f"/recordings/{admin.ids[MEETING_TITLE]}")
    pen = page.locator(f"button[title='{en('tooltips.editTitle')}']:visible").first
    pen.wait_for(state="visible", timeout=30000)
    pen.click()
    field = page.locator("input[data-title-input]:visible")
    field.wait_for(state="visible", timeout=5000)
    page.wait_for_function("() => document.activeElement && document.activeElement.matches('input[data-title-input]')",
                           timeout=5000)
    focused = page.evaluate("() => { const el = document.activeElement; "
                            "return [el.getClientRects().length > 0, el.selectionStart, el.selectionEnd, el.value.length]; }")
    assert focused[0], "the focused title field is not the one on screen"
    assert focused[1] == 0 and focused[2] == focused[3] == len(MEETING_TITLE), f"the title is not selected: {focused}"
    admin.shot("header-test-title-focus")
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.querySelector('input[data-title-input]')", timeout=5000)
    check_common(page, admin.problems, "title edit")


def _share_window(admin):
    page = admin.page
    open_page(page, admin.problems, BASE, f"/recordings/{admin.ids[MEETING_TITLE]}")
    button = page.locator(f"button[title='{en('buttons.shareRecording')}']:visible").first
    button.wait_for(state="visible", timeout=30000)
    with page.expect_response(lambda r: r.url.endswith("/api/permissions/can-share-publicly")):
        button.click()
    window = page.locator(WINDOW).last
    window.wait_for(state="visible", timeout=5000)
    window.locator("[data-public-share-section]").wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(300)
    return window


def test_the_share_window_follows_the_public_sharing_switch(admin):
    page = admin.page
    open_page(page, admin.problems, BASE, "/admin")
    try:
        r = admin.api("POST", "/admin/settings", {"key": "enable_public_sharing", "value": "false", "setting_type": "boolean"})
        assert r.ok, f"public sharing off: {r.status}"
        window = _share_window(admin)
        assert window.locator("[data-create-public-link]").count() == 0
        off = window.locator("[data-public-share-off]")
        assert off.is_visible() and off.inner_text().strip() == en("unifiedShareModal.publicLinksOff")
        assert window.get_by_text(en("form.shareSummary"), exact=True).count() == 0
        # Internal sharing is off on this instance too: the dialog does not ask
        # for its list of people, so no error is shown
        assert page.locator("#toastContainer [role=alert]").count() == 0
        admin.shot("header-test-share-public-off")
        check_common(page, admin.problems, "share window, public sharing off")

        r = admin.api("POST", "/admin/settings", {"key": "enable_public_sharing", "value": "true", "setting_type": "boolean"})
        assert r.ok, f"public sharing on: {r.status}"
        window = _share_window(admin)
        assert window.locator("[data-create-public-link]").is_visible()
        assert window.locator("[data-public-share-off]").count() == 0
        admin.shot("header-test-share-public-on")
        check_common(page, admin.problems, "share window, public sharing on")
    finally:
        open_page(page, admin.problems, BASE, "/admin")
        admin.api("POST", "/admin/settings", {"key": "enable_public_sharing", "value": "", "setting_type": "boolean"})
