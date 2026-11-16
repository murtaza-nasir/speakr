"""Browser test of the Audit log tab of the admin page.

The server tests (tests/test_audit_log_routes.py) cover the routes; this test
covers the page: the tab and its table, the row that opens to its details,
the filters (applied on the server), paging by "Older" and "Newer", the two
export links, "View activity" on a user row, the notice that is shown until
it is dismissed, and the tab in each of the six UI themes, light and dark.

The events it reads are made by the test itself through the app: the admin's
own sign-in, one refused sign-in, and a series of setting changes. The steps
run in file order. Afterwards the settings it touched are restored.

Each step fails on a JavaScript error, a same-origin response of 400 or
higher that the step does not expect, and template text left in the page
(tests/e2e/browser_checks.py, shared with test_browser_smoke.py).

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test. With
SPEAKR_E2E_SHOTS set, screenshots are saved in that directory.
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

from browser_checks import PageProblems, check_common, en, login, open_page  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")

THEMES = ("default", "cards", "classic", "desk", "focus", "workspace")
TAB = "[data-audit-log]"
ROWS = f"{TAB} [data-audit-row]"
NOTICE_SETTING = "audit_log_notice_dismissed"
# Enough setting changes for a second page (the page holds 50 rows).
CHANGES = 55


class Admin:
    """The seeded admin in one browser context."""

    def __init__(self, browser):
        self.context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US",
                                           service_workers="block")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.problems.reset()

    def csrf(self):
        return self.page.locator("meta[name=csrf-token]").get_attribute("content")

    def api(self, method, path, data=None):
        headers = {"X-CSRFToken": self.csrf(), "Content-Type": "application/json"}
        return self.page.request.fetch(f"{BASE}{path}", method=method, headers=headers,
                                       data=json.dumps(data) if data is not None else None)

    def setting(self, key, value, setting_type="string"):
        response = self.api("POST", "/admin/settings", {"key": key, "value": value, "setting_type": setting_type})
        assert response.ok, f"could not save {key}: HTTP {response.status}"

    def open_admin(self):
        open_page(self.page, self.problems, BASE, "/admin")

    def open_tab(self):
        page = self.page
        page.locator("[data-audit-tab]").click()
        page.locator(f"{TAB} [data-audit-table]").wait_for(state="visible", timeout=10000)
        self.loaded()

    def loaded(self):
        """The request of the last filter change or page turn was answered."""
        page = self.page
        page.wait_for_timeout(450)  # the filters wait 300 ms after the last change
        page.wait_for_function(
            "() => !document.querySelector('[data-audit-log] .fa-spinner')", timeout=10000)

    def rows(self):
        """The rows on the page as (user, event, result)."""
        return self.page.evaluate(
            """(selector) => [...document.querySelectorAll(selector)].map(tr => {
                const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
                return {time: cells[0], user: cells[1], event: cells[2], recording: cells[3],
                        address: cells[4], result: cells[5]};
            })""", ROWS)

    def filter(self, name, value):
        control = self.page.locator(f'{TAB} [data-audit-filter="{name}"]')
        if control.evaluate("el => el.tagName") == "SELECT":
            control.select_option(value)
        else:
            control.fill(value)
        self.loaded()

    def check(self, step, utilities=True):
        """No failed request, script error or template text. The probe of the
        compiled utilities in check_common measures pixels, which differ in
        a theme with another root font size, so it is left out there."""
        path = f"/admin ({step})"
        if utilities:
            check_common(self.page, self.problems, path)
            return
        body = self.page.inner_text("body")
        leftovers = [m for m in ("${", "{{") if m in body]
        assert not leftovers, f"{path}: template text left in the page: {leftovers}"
        assert not self.problems.failed, f"{path}: failed requests: {self.problems.failed}"
        assert not self.problems.errors, f"{path}: JavaScript errors: {self.problems.errors}"

    def no_sideways_scroll(self, step):
        wide = self.page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert wide <= 1, f"{step}: the page scrolls sideways by {wide}px"

    def shot(self, name):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            self.page.wait_for_timeout(400)
            self.page.screenshot(path=os.path.join(SHOTS, f"{name}.png"), full_page=True)


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def admin(browser):
    session = Admin(browser)
    session.open_admin()
    session.setting(NOTICE_SETTING, "false", "boolean")
    # A refused sign-in from another browser, for the Result filter.
    other = browser.new_context(locale="en-US", service_workers="block")
    try:
        page = other.new_page()
        page.goto(f"{BASE}/login")
        page.fill("input[name=email]", ADMIN_EMAIL)
        page.fill("input[name=password]", "not-the-password")
        page.locator("form:has(input[name=password]) [type=submit]:visible").first.click()
        page.wait_for_load_state("load")
    finally:
        other.close()
    # Setting changes, one audit row each: the label is cleared every time.
    for _ in range(CHANGES):
        session.setting("help_center_label", "")
    # Rows of the queue are written within a second.
    session.page.wait_for_timeout(2500)
    session.open_admin()
    yield session
    session.setting(NOTICE_SETTING, "false", "boolean")
    session.context.close()


def test_the_tab_lists_events_newest_first(admin):
    page = admin.page
    tab = page.locator("[data-audit-tab]")
    assert tab.inner_text().strip() == en("auditLog.tab")
    admin.open_tab()
    heads = [text.strip().lower() for text in page.locator(f"{TAB} [data-audit-table] thead th").all_inner_texts()]
    assert heads == [en(f"auditLog.{key}").lower() for key in ("time", "user", "event", "recording", "address", "result")]
    rows = admin.rows()
    assert len(rows) == 50, f"{len(rows)} rows on the first page"
    assert all(row["event"] == "admin.setting.changed" for row in rows[:20]), rows[:3]
    assert all(row["result"] == en("auditLog.outcomes.success") for row in rows[:20])
    assert all(row["user"] and row["time"] and row["address"] for row in rows[:20]), rows[:3]
    # The time is relative, with the full date and time on hover.
    time = page.locator(f"{ROWS} time").first
    assert time.get_attribute("datetime") and time.get_attribute("title")
    assert page.locator(f"{TAB} [data-audit-older]").is_enabled()
    assert page.locator(f"{TAB} [data-audit-newer]").is_disabled()
    admin.check("audit tab")
    admin.no_sideways_scroll("audit tab")
    admin.shot("audit-log-01-tab")


def test_the_notice_names_what_is_recorded(admin):
    notice = admin.page.locator(f"{TAB} [data-audit-notice]")
    assert notice.is_visible()
    text = notice.locator("p").inner_text()
    assert en("auditLog.notice") in text and en("auditLog.noticeAddress") in text
    assert en("auditLog.noticeDays").replace("{{days}}", "365") in text
    assert admin.page.locator(f"{TAB} [data-audit-off]").count() == 0


def test_a_row_opens_to_its_details(admin):
    page = admin.page
    first = page.locator(ROWS).first
    assert first.get_attribute("aria-expanded") == "false"
    first.click()
    details = page.locator(f"{TAB} [data-audit-details]")
    details.wait_for(state="visible", timeout=5000)
    assert first.get_attribute("aria-expanded") == "true"
    text = details.inner_text()
    assert en("auditLog.accessBy") in text and en("auditLog.actors.session") in text
    assert "key" in text and "help_center_label" in text
    assert en("auditLog.userAgent") in text
    admin.shot("audit-log-02-details")
    first.click()
    details.wait_for(state="detached", timeout=5000)
    admin.check("details")


def test_filters_are_applied_on_the_server(admin):
    page = admin.page
    admin.filter("group", "auth")
    rows = admin.rows()
    assert rows and all(row["event"].split(".")[0] in ("auth", "token") for row in rows), rows[:5]
    assert any(row["event"] == "auth.login.succeeded" for row in rows)

    admin.filter("outcome", "failed")
    rows = admin.rows()
    assert rows and all(row["event"] == "auth.login.failed" for row in rows), rows
    assert all(row["result"] == en("auditLog.outcomes.failed") for row in rows)
    assert rows[0]["user"] == en("auditLog.actors.anonymous")
    admin.shot("audit-log-03-filtered")

    admin.filter("recording_id", "987654321")
    assert admin.rows() == []
    assert page.locator(f"{TAB} [data-audit-empty]").inner_text().strip() == en("auditLog.empty")

    clear = page.locator(f"{TAB} [data-audit-clear]")
    assert clear.is_visible()
    clear.click()
    admin.loaded()
    assert len(admin.rows()) == 50 and clear.count() == 0
    # A day in the future has no events; today has.
    admin.filter("from", "2099-01-01")
    assert admin.rows() == []
    admin.filter("from", "")
    admin.filter("actor_kind", "anonymous")
    rows = admin.rows()
    assert rows and all(row["user"] == en("auditLog.actors.anonymous") for row in rows)
    page.locator(f"{TAB} [data-audit-clear]").click()
    admin.loaded()
    admin.check("filters")


def test_older_and_newer_page_through_the_log(admin):
    page = admin.page
    admin.filter("group", "admin")
    first = admin.rows()
    assert len(first) == 50
    older = page.locator(f"{TAB} [data-audit-older]")
    newer = page.locator(f"{TAB} [data-audit-newer]")
    assert older.is_enabled() and newer.is_disabled()
    older.click()
    admin.loaded()
    second = admin.rows()
    assert 1 <= len(second) <= 50 and newer.is_enabled()
    assert len(first) + len(second) >= CHANGES
    newer.click()
    admin.loaded()
    assert admin.rows() == first and newer.is_disabled()
    page.locator(f"{TAB} [data-audit-clear]").click()
    admin.loaded()
    admin.check("paging")


def test_export_links_carry_the_filters(admin):
    page = admin.page
    admin.filter("group", "admin")
    admin.filter("outcome", "success")
    links = {kind: page.locator(f'{TAB} [data-audit-export="{kind}"]') for kind in ("csv", "jsonl")}
    assert links["csv"].inner_text().strip() == en("auditLog.exportCsv")
    assert links["jsonl"].inner_text().strip() == en("auditLog.exportJsonl")
    for kind, link in links.items():
        href = link.get_attribute("href")
        assert href.startswith("/admin/audit-log/export?") and f"format={kind}" in href
        assert "group=admin" in href and "outcome=success" in href
        response = page.request.get(f"{BASE}{href}")
        assert response.ok, f"{kind}: HTTP {response.status}"
        assert "attachment" in response.headers.get("content-disposition", "")
        body = response.text()
        lines = body.strip().splitlines()
        if kind == "csv":
            assert lines[0].startswith("id,created_at,event,event_group,actor_kind")
            assert len(lines) - 1 >= CHANGES
        else:
            rows = [json.loads(line) for line in lines]
            assert len(rows) >= CHANGES and all(row["event_group"] == "admin" for row in rows)
            assert all(row["outcome"] == "success" for row in rows)
    page.locator(f"{TAB} [data-audit-clear]").click()
    admin.loaded()
    admin.check("export")


def test_view_activity_opens_the_tab_for_one_user(admin):
    page = admin.page
    page.locator("nav.tabs button.tab").first.click()
    row = page.locator("tbody tr", has=page.locator("td", has_text=re.compile(rf"^{re.escape(ADMIN_EMAIL)}$"))).first
    row.wait_for(state="visible", timeout=10000)
    user_id = row.locator("td").first.inner_text().strip()
    username = row.locator("td").nth(1).inner_text().strip()
    action = row.locator("[data-audit-view-activity]")
    assert action.get_attribute("title") == en("auditLog.viewActivity")
    action.click()
    page.locator(f"{TAB} [data-audit-table]").wait_for(state="visible", timeout=10000)
    admin.loaded()
    assert page.locator(f'{TAB} [data-audit-filter="user"]').input_value() == user_id
    rows = admin.rows()
    # The account as actor, or as the target of an event without an account
    # (the refused sign-in).
    assert rows and all(row["user"] in (username, en("auditLog.actors.anonymous")) for row in rows), rows[:3]
    assert all(row["event"] == "auth.login.failed" for row in rows if row["user"] != username)
    admin.filter("outcome", "failed")
    assert {row["event"] for row in admin.rows()} == {"auth.login.failed"}
    admin.shot("audit-log-04-user-activity")
    page.locator(f"{TAB} [data-audit-clear]").click()
    admin.loaded()
    admin.check("view activity")


@pytest.mark.parametrize("theme", THEMES)
def test_the_tab_in_every_theme_light_and_dark(admin, theme):
    page = admin.page
    saved = page.locator("html").get_attribute("data-ui-theme") or "default"
    response = admin.api("POST", "/api/user/preferences", {"ui_theme": theme})
    assert response.ok, f"could not save the theme {theme}: HTTP {response.status}"
    try:
        for mode in ("light", "dark"):
            page.evaluate("(dark) => localStorage.setItem('darkMode', dark)", "true" if mode == "dark" else "false")
            admin.open_admin()
            assert page.locator("html").get_attribute("data-ui-theme") == theme
            assert ("dark" in (page.locator("html").get_attribute("class") or "").split()) == (mode == "dark")
            admin.open_tab()
            assert len(admin.rows()) == 50, f"{theme} {mode}"
            page.locator(ROWS).first.click()
            page.locator(f"{TAB} [data-audit-details]").wait_for(state="visible", timeout=5000)
            # The text of a row is readable against its background.
            contrast = page.evaluate(
                """(selector) => {
                    // Any CSS color (rgb, oklch, color-mix) as sRGB, through a canvas.
                    const canvas = document.createElement('canvas');
                    canvas.width = canvas.height = 1;
                    const ctx = canvas.getContext('2d', {willReadFrequently: true});
                    const rgba = (color) => {
                        ctx.clearRect(0, 0, 1, 1);
                        ctx.fillStyle = color;
                        ctx.fillRect(0, 0, 1, 1);
                        return [...ctx.getImageData(0, 0, 1, 1).data];
                    };
                    const lum = ([r, g, b]) => {
                        const [x, y, z] = [r, g, b].map(v => {
                            v /= 255;
                            return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
                        });
                        return 0.2126 * x + 0.7152 * y + 0.0722 * z;
                    };
                    // The second row: the first one is open and under the pointer.
                    const cell = document.querySelectorAll(selector)[1].querySelectorAll('td')[1];
                    let el = cell, bg = [0, 0, 0, 0];
                    while (el && bg[3] < 255) { bg = rgba(getComputedStyle(el).backgroundColor); el = el.parentElement; }
                    const a = lum(rgba(getComputedStyle(cell).color)), b = lum(bg);
                    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
                }""", ROWS)
            assert contrast >= 4.5, f"{theme} {mode}: contrast of a row {contrast:.2f}"
            admin.check(f"{theme} {mode}", utilities=False)
            admin.no_sideways_scroll(f"{theme} {mode}")
            admin.shot(f"audit-log-theme-{theme}-{mode}")
    finally:
        admin.api("POST", "/api/user/preferences", {"ui_theme": saved})
        page.evaluate("() => localStorage.removeItem('darkMode')")


def test_the_notice_is_dismissed_once(admin):
    page = admin.page
    admin.open_admin()
    admin.open_tab()
    notice = page.locator(f"{TAB} [data-audit-notice]")
    assert notice.is_visible()
    dismiss = notice.locator("button")
    assert dismiss.inner_text().strip() == en("auditLog.dismiss")
    with page.expect_response(lambda r: r.url.endswith("/admin/settings") and r.request.method == "POST") as saved:
        dismiss.click()
    assert saved.value.ok
    assert notice.count() == 0
    admin.open_admin()
    admin.open_tab()
    assert page.locator(f"{TAB} [data-audit-notice]").count() == 0
    admin.check("notice dismissed")
    admin.shot("audit-log-05-no-notice")
