"""Browser test of the User defaults panel (#584), in a real browser.

The server tests (tests/test_user_defaults.py) cover the rules; this test
covers the panel in System Settings: every default is listed once, with a
select or a switch, and the settings behind the panel are left out of the
list below it. A changed default asks whether the accounts that still have
the previous default get the new one, with their number and the previous
value, and both answers are followed to the server. Cancel, Esc and a click
beside the dialog leave the default as it was. Without such accounts the
default is saved without a question.

The steps run in file order and build on each other. Setup, through the
app's own HTTP API as the seeded admin: the default color scheme set to
rose, two users created with it and one user created with amber. Afterwards
the users are removed and the changed settings cleared. The test needs no
recordings.

Each step fails on a JavaScript error, an unexpected same-origin response of
400 or higher and template text left in the page (tests/e2e/browser_checks.py).

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test. With
SPEAKR_E2E_SHOTS set, screenshots of the panel are saved in that directory,
in every interface theme, light and dark.
"""

import os
import secrets
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
RUN = secrets.token_hex(3)

SCHEME = "default_color_scheme"
EVENTS = "default_extract_events"
KEYS = ("default_ui_theme", "default_color_scheme", "default_audio_player_position", "default_ui_language",
        "default_transcription_language", "default_output_language", "default_timezone_mode",
        "default_timezone", "default_auto_summarization", "default_auto_speaker_labelling",
        "default_extract_events")
DIALOG = "[data-app-dialog]"
CONFIRM = f"{DIALOG} [data-app-dialog-confirm]"
CANCEL = f"{DIALOG} [data-app-dialog-cancel]"
NEW_ONLY = f"{DIALOG} [data-app-dialog-alternate]"
STATUS = "#user-defaults-status"


class Admin:
    """The seeded admin: a browser context, its page and its problems."""

    def __init__(self, browser):
        self.context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.problems.reset()
        self.users = []

    def api(self, method, path, data=None):
        headers = {"X-CSRFToken": self.page.locator("meta[name=csrf-token]").get_attribute("content")}
        return self.page.request.fetch(f"{BASE}{path}", method=method, headers=headers, data=data)

    def save(self, key, value, apply_to_existing=False):
        """A default saved through the API, as a client without the dialog does."""
        r = self.api("POST", "/admin/settings", {"key": key, "value": value, "apply_to_existing": apply_to_existing})
        assert r.ok, f"setting {key}: {r.status} {r.text()}"
        return r.json()

    def count(self, key):
        r = self.api("GET", f"/admin/user-defaults/{key}/accounts")
        assert r.ok, f"count {key}: {r.status} {r.text()}"
        return r.json()["count"]

    def accounts_with(self, key, value):
        """The number of accounts with a value: it is made the default for the
        count, without a change of any account, and the default is put back."""
        current = self.api("GET", f"/admin/user-defaults/{key}/accounts").json()["value"]
        self.save(key, value)
        number = self.count(key)
        self.save(key, current)
        return number

    def last_setting_change(self):
        """The id of the newest admin.setting.changed row of the audit log, or 0."""
        r = self.api("GET", "/admin/audit-log?event=admin.setting.changed&limit=1")
        assert r.ok, f"audit log: {r.status} {r.text()}"
        events = r.json()["events"]
        return events[0]["id"] if events else 0

    def add_user(self):
        name = f"ud{len(self.users)}_{RUN}"
        r = self.api("POST", "/admin/users", {"username": name, "email": f"{name}@example.com",
                                             "password": secrets.token_urlsafe(16)})
        assert r.status == 201, f"user {name}: {r.status} {r.text()}"
        self.users.append(r.json()["id"])

    def open_settings(self):
        open_page(self.page, self.problems, BASE, "/admin")
        self.page.locator("nav.tabs button.tab", has_text=en("adminDashboard.systemSettings")).first.click()
        panel = self.page.locator("#user-defaults")
        panel.wait_for(state="visible", timeout=10000)
        return panel

    def check(self):
        check_common(self.page, self.problems, "/admin")


@pytest.fixture(scope="module")
def admin():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        a = Admin(browser)
        open_page(a.page, a.problems, BASE, "/admin")
        try:
            a.save(SCHEME, "amber")
            a.add_user()
            a.save(SCHEME, "rose")
            a.add_user()
            a.add_user()
            yield a
        finally:
            open_page(a.page, a.problems, BASE, "/admin")
            for user_id in a.users:
                a.api("DELETE", f"/admin/users/{user_id}")
            # An empty value lets the .env value apply again
            for key in (SCHEME, EVENTS):
                a.api("POST", "/admin/settings", {"key": key, "value": ""})
            a.context.close()
            browser.close()


def control(panel, key):
    return panel.locator(f'[data-user-default="{key}"]').locator("select, input[type=checkbox]")


def wait_status(page, text):
    status = page.locator(STATUS)
    status.wait_for(state="visible", timeout=10000)
    playwright_api.expect(status).to_have_text(text, timeout=10000)


def test_every_default_is_listed_once(admin):
    panel = admin.open_settings()
    page = admin.page
    assert panel.locator("h4").inner_text().strip() == en("adminDashboard.userDefaults.title")
    listed = panel.locator("[data-user-default]").evaluate_all("rows => rows.map(r => r.dataset.userDefault)")
    assert listed == list(KEYS), f"/admin: User defaults lists {listed}"
    for key in KEYS:
        row = panel.locator(f'[data-user-default="{key}"]')
        label = row.locator("label").first.inner_text().strip()
        assert label and not label.startswith(("default_", "adminDashboard.")), f"/admin: {key} is labeled {label!r}"
        field = control(panel, key)
        assert field.count() == 1, f"/admin: {key} has {field.count()} controls"
        if field.evaluate("el => el.tagName") == "SELECT":
            options = field.locator("option").all_inner_texts()
            assert len(options) >= 2 and all(o.strip() for o in options), f"/admin: {key} options {options[:5]}"
            assert field.input_value(), f"/admin: {key} shows no value"
    # The names of the Account page, from the shared lists
    scheme = control(panel, SCHEME)
    assert scheme.input_value() == "rose"
    assert scheme.locator("option:checked").inner_text().strip() == en("colorScheme.themes.light.rose.name")
    languages = control(panel, "default_ui_language").locator("option").all_inner_texts()
    assert "Deutsch (German)" in [text.strip() for text in languages]
    assert control(panel, "default_transcription_language").locator("option").count() > 50
    assert control(panel, "default_output_language").locator("option").count() > 50
    assert control(panel, "default_timezone").locator("option").count() > 100
    # The settings of the panel are not in the list of settings below it
    keys_below = page.locator("div.shadow-sm > div > div > h4").all_inner_texts()
    assert keys_below, "/admin: the list of settings is empty"
    assert not set(KEYS) & {text.strip() for text in keys_below}, "/admin: a user default is listed twice"
    if SHOTS:
        _shots(admin, panel)
    admin.check()


def _shots(admin, panel):
    """The panel in every interface theme, light and dark."""
    os.makedirs(SHOTS, exist_ok=True)
    page = admin.page
    themes = page.request.get(f"{BASE}/admin/user-defaults").json()["defaults"][0]["choices"]
    for theme in themes:
        for dark in (False, True):
            admin.api("POST", "/api/user/preferences", {"ui_theme": theme, "dark_mode": dark})
            page.evaluate("dark => localStorage.setItem('darkMode', dark ? 'true' : 'false')", dark)
            panel = admin.open_settings()
            page.wait_for_timeout(300)
            panel.screenshot(path=os.path.join(SHOTS, f"user-defaults-{theme}-{'dark' if dark else 'light'}.png"))
    admin.api("POST", "/api/user/preferences", {"ui_theme": "cards", "dark_mode": False})
    page.evaluate("localStorage.setItem('darkMode', 'false')")
    admin.open_settings()


@pytest.mark.parametrize("dismiss", ["cancel", "escape", "beside"])
def test_cancelling_the_question_changes_nothing(admin, dismiss):
    panel = admin.open_settings()
    page = admin.page
    on_rose = admin.count(SCHEME)
    last_change = admin.last_setting_change()
    saves = []
    page.on("request", lambda r: saves.append(r.url) if r.method == "POST" and r.url.endswith("/admin/settings") else None)
    control(panel, SCHEME).select_option("teal")
    page.locator(DIALOG).wait_for(state="visible", timeout=5000)
    if dismiss == "cancel":
        page.locator(CANCEL).click()
    elif dismiss == "escape":
        page.keyboard.press("Escape")
    else:
        page.mouse.click(5, 5)
    page.locator(DIALOG).wait_for(state="detached", timeout=5000)
    page.wait_for_timeout(500)
    assert not saves, f"/admin: {dismiss} saved the default"
    assert control(panel, SCHEME).input_value() == "rose", f"/admin: after {dismiss} the select does not show the default"
    assert page.locator(STATUS).count() == 0
    assert not control(panel, SCHEME).is_disabled()
    current = admin.api("GET", f"/admin/user-defaults/{SCHEME}/accounts").json()
    assert (current["value"], current["count"]) == ("rose", on_rose)
    assert admin.last_setting_change() == last_change, f"/admin: {dismiss} wrote an audit row"
    admin.check()


def test_a_changed_default_asks_and_updates_the_accounts_on_the_previous_one(admin):
    panel = admin.open_settings()
    page = admin.page
    on_rose = admin.count(SCHEME)
    on_amber = admin.accounts_with(SCHEME, "amber")
    assert on_rose >= 2 and on_amber >= 1
    control(panel, SCHEME).select_option("teal")
    question = page.locator(DIALOG)
    question.wait_for(state="visible", timeout=5000)
    assert question.locator(".app-dialog__message").inner_text() == (
        en("adminDashboard.userDefaults.confirmMessagePlural")
        .replace("{{value}}", en("colorScheme.themes.light.rose.name")).replace("{{count}}", str(on_rose)))
    assert en("adminDashboard.userDefaults.confirmTitle") in question.inner_text()
    assert page.locator(CONFIRM).inner_text().strip() == (
        en("adminDashboard.userDefaults.applyToCountPlural").replace("{{count}}", str(on_rose)))
    assert page.locator(NEW_ONLY).inner_text().strip() == en("adminDashboard.userDefaults.newAccountsOnly")
    assert page.locator(CANCEL).inner_text().strip() == en("common.cancel")
    if SHOTS:
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(SHOTS, "user-defaults-question.png"))
    with page.expect_response(lambda r: r.url.endswith("/admin/settings") and r.request.method == "POST") as saved:
        page.locator(CONFIRM).click()
    body = saved.value.json()
    assert body["value"] == "teal" and body["users_updated"] == on_rose
    assert saved.value.request.post_data_json["apply_to_existing"] is True
    wait_status(page, en("adminDashboard.userDefaults.savedUpdated").replace("{{count}}", str(on_rose)))
    assert control(panel, SCHEME).input_value() == "teal"
    # The accounts on rose have teal; the account with amber kept it
    assert admin.accounts_with(SCHEME, "rose") == 0
    assert admin.accounts_with(SCHEME, "amber") == on_amber
    assert admin.count(SCHEME) >= on_rose
    admin.check()


def test_new_accounts_only_changes_no_account(admin):
    panel = admin.open_settings()
    page = admin.page
    on_teal = admin.count(SCHEME)
    control(panel, SCHEME).select_option("purple")
    page.locator(DIALOG).wait_for(state="visible", timeout=5000)
    with page.expect_response(lambda r: r.url.endswith("/admin/settings") and r.request.method == "POST") as saved:
        page.locator(NEW_ONLY).click()
    assert saved.value.json()["users_updated"] == 0
    assert saved.value.request.post_data_json["apply_to_existing"] is False
    wait_status(page, en("adminDashboard.userDefaults.saved"))
    assert control(panel, SCHEME).input_value() == "purple"
    assert admin.accounts_with(SCHEME, "teal") == on_teal
    admin.check()


def test_a_default_that_no_account_has_is_saved_without_a_question(admin):
    assert admin.accounts_with(SCHEME, "rose") == 0
    admin.save(SCHEME, "rose")
    panel = admin.open_settings()
    page = admin.page
    assert control(panel, SCHEME).input_value() == "rose"
    with page.expect_response(lambda r: r.url.endswith("/admin/settings") and r.request.method == "POST") as saved:
        control(panel, SCHEME).select_option("blue")
    assert saved.value.json()["users_updated"] == 0
    assert page.locator(DIALOG).count() == 0, "/admin: a question was asked although no account had the default"
    wait_status(page, en("adminDashboard.userDefaults.saved"))
    admin.check()


def test_a_switch_asks_with_its_previous_state(admin):
    panel = admin.open_settings()
    page = admin.page
    switch = control(panel, EVENTS)
    assert not switch.is_checked()
    off = admin.count(EVENTS)
    assert off >= 3
    # The input of the switch is visually hidden; its track is clicked
    panel.locator(f'[data-user-default="{EVENTS}"] label').last.click()
    question = page.locator(DIALOG)
    question.wait_for(state="visible", timeout=5000)
    assert question.locator(".app-dialog__message").inner_text() == (
        en("adminDashboard.userDefaults.confirmMessagePlural")
        .replace("{{value}}", en("adminDashboard.disabled")).replace("{{count}}", str(off)))
    with page.expect_response(lambda r: r.url.endswith("/admin/settings") and r.request.method == "POST") as saved:
        page.locator(NEW_ONLY).click()
    assert saved.value.json()["value"] == "true" and saved.value.json()["users_updated"] == 0
    wait_status(page, en("adminDashboard.userDefaults.saved"))
    assert control(panel, EVENTS).is_checked()
    assert admin.accounts_with(EVENTS, "false") == off
    admin.check()
