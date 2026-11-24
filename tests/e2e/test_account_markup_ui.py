"""Browser test of the account page's markup built in script (#692).

The cards and lists of the Speakers, Shares, Tags, Templates, API tokens and
Webhooks tabs are built with safeHtml (static/js/safe-html.js), which escapes
every value. The answers of their routes are replaced in the browser by
records whose names, titles, identifiers and dates hold markup and quotes. The
values must show as text, no attribute may gain a handler, and no script may
run, also in the dialogs built from those records (merge, delete, clear voice
profiles, change permissions, revoke). Each tab is then shown in the six UI
themes, light and dark.

With SPEAKR_E2E_SHOTS set, screenshots are saved there, and the markup of
each tab in the default theme (light) is saved as account-markup-<tab>.html,
for comparing the rendered markup with that of another version.

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

from browser_checks import PageProblems, check_common, login, open_page  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")
DESKTOP = {"width": 1400, "height": 900}
THEMES = ("default", "cards", "classic", "desk", "focus", "workspace")

# An image that fails to decode: an onerror handler that reached the DOM runs
XSS = '<img src="data:image/png;base64,AAAA" onerror="window.__xss692=(window.__xss692||0)+1">'
QUOTE = '" onmouseover="window.__xss692=1" x="'


def _data(hostile):
    """The records the routes answer with: hostile values, or plain ones."""
    v = (lambda plain, bad: bad) if hostile else (lambda plain, bad: plain)
    speakers = [
        {"id": v(901, f"901{QUOTE}"), "name": v("Dana Reyes", f"Dana {XSS}"), "use_count": 4,
         "created_at": "2026-03-01T10:00:00", "last_used": v("2026-03-05T10:00:00", XSS),
         "embedding_count": 2, "confidence_score": 0.85,
         "voice": {"sample_count": 2, "current_space_samples": 2, "variant_count": 1, "other_space_samples": 0}},
        {"id": 902, "name": v("Omar Haddad", f"Omar{QUOTE}"), "use_count": 1,
         "created_at": "2026-03-02T10:00:00", "last_used": "2026-03-02T11:00:00",
         "embedding_count": 0, "confidence_score": None},
    ]
    snippets = {"snippets": [
        {"recording_id": v(11, f"11{QUOTE}"), "recording_title": v("Budget review", XSS),
         "start_time": 1.5, "duration": 3},
    ]}
    shares = {
        "internal_sharing_enabled": True,
        "public_links": [{
            "id": v(31, f"31{QUOTE}"), "recording_id": v(12, f"12{QUOTE}"),
            "recording_title": v("Budget review", f"Budget {XSS}"), "public_id": v("abc123", f"abc{QUOTE}{XSS}"),
            "created_at": v("2026-03-04T09:00:00", XSS), "share_summary": True, "share_notes": False,
        }],
        "shared_by_me": [{
            "recording_id": v(13, f"13{QUOTE}"), "recording_title": v("Hiring plan", f"Hiring {XSS}"),
            "grantable": {"can_edit": True, "can_reshare": True},
            "recipients": [
                {"share_id": v(41, f"41{QUOTE}"), "username": v("lee", f"lee{XSS}"), "can_edit": False,
                 "can_reshare": False, "revocable": True, "permissions_editable": True,
                 "source": {"type": "manual"}},
                {"share_id": 42, "username": v("sam", f"sam{QUOTE}"), "can_edit": True, "can_reshare": False,
                 "revocable": False, "permissions_editable": False,
                 "source": {"type": "group_tag", "name": v("Finance", XSS)}},
            ],
        }],
        "shared_with_me": [{
            "recording_id": v(14, f"14{QUOTE}"), "recording_title": v("Roadmap", f"Roadmap {XSS}"),
            "owner_username": v("kim", f"kim{XSS}"), "created_at": v("2026-03-03T09:00:00", XSS),
            "can_edit": False, "can_reshare": False, "source": {"type": "manual"},
        }],
    }
    tokens = {"tokens": [
        {"id": v(51, f"51{QUOTE}"), "name": v("CI token", f"CI'); window.__xss692=1; ('{XSS}"),
         "created_at": "2026-03-01T10:00:00", "last_used_at": "2026-03-02T10:00:00",
         "expires_at": "2099-01-01T00:00:00", "revoked": False, "scopes": v(["full"], [XSS]),
         "sso_client_id": v(None, f"sso{XSS}"), "sso_client_name": v(None, f"Portal{XSS}")},
        {"id": 52, "name": "Laptop", "created_at": "2026-03-01T10:00:00", "last_used_at": None,
         "expires_at": None, "revoked": False, "scopes": ["full"], "sso_client_id": None},
    ]}
    tags = [{
        "id": v(61, f"61{QUOTE}"), "name": v("Finance", f"Fin {XSS}"), "color": v("#2563eb", f"#fff{QUOTE}"),
        "group_id": None, "can_edit": True, "protect_from_deletion": False, "retention_days": 30,
        "default_language": v("en", f"en{XSS}"), "default_min_speakers": 2, "default_max_speakers": 3,
        "naming_template_name": v("Meeting", XSS), "is_auto_process": True,
        "auto_process_folder_name": v("inbox", XSS), "recording_count": 3,
        "custom_prompt": v("Summarize decisions.", f"Prompt {XSS}"),
    }, {
        "id": 62, "name": "Team", "color": "#16a34a", "group_id": 5, "group_name": v("Ops", XSS),
        "can_edit": False, "auto_share_on_apply": True, "recording_count": 1, "custom_prompt": "",
    }]
    templates = [{"id": 71, "name": v("Minutes", f"Min {XSS}"), "description": v("Plain minutes", XSS),
                  "template": "{{text}}", "is_default": True}]
    webhooks = {"event_types": ["recording.completed", "recording.created"], "webhooks": [{
        "id": 81, "name": v("Zapier", f"Zap {XSS}"), "url": v("https://hooks.example.com/a", f"https://x.test/{XSS}"),
        "events": v(["recording.completed"], [XSS]), "enabled": True, "auto_paused": False,
        "consecutive_failures": 2, "last_delivery_at": "2026-03-05T10:00:00", "allow_http": False,
    }]}
    deliveries = {"deliveries": [{
        "id": 91, "event_type": v("recording.completed", XSS), "status": "failed", "response_status": 500,
        "error_message": v("HTTP 500", XSS), "created_at": "2026-03-05T10:00:00", "attempt_count": 2,
        "next_retry_at": "2026-03-05T10:10:00",
    }]}
    return {
        r"/speakers$": speakers,
        r"/speakers/[^/]+/snippets": snippets,
        r"/api/shares/overview$": shares,
        r"/api/tokens$": tokens,
        r"/api/tags$": tags,
        r"/api/transcript-templates$": templates,
        r"/api/v1/webhooks$": webhooks,
        r"/api/v1/webhooks/\d+/deliveries": deliveries,
    }


class Account:
    """The signed-in admin on /account, with the tab routes answered by the test."""

    def __init__(self, browser, hostile):
        self.context = browser.new_context(viewport=DESKTOP, locale="en-US", timezone_id="UTC",
                                           service_workers="block")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        for pattern, body in _data(hostile).items():
            self.page.route(re.compile(r"^[^?]*" + pattern.rstrip("$") + r"(\?.*)?$"), self._answer(body))

    @staticmethod
    def _answer(body):
        def handler(route):
            if route.request.method != "GET":
                return route.fulfill(status=200, content_type="application/json", body="{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        return handler

    def csrf(self):
        return self.page.locator("meta[name=csrf-token]").first.get_attribute("content")

    def api(self, method, path, data=None):
        headers = {"X-CSRFToken": self.csrf(), "Content-Type": "application/json"}
        return self.page.request.fetch(f"{BASE}{path}", method=method, headers=headers,
                                       data=json.dumps(data) if data is not None else None)

    def open(self):
        open_page(self.page, self.problems, BASE, "/account", settle_ms=800)

    def tab(self, name, ready):
        self.page.locator(f"#tab-{name}").click()
        self.page.locator(ready).first.wait_for(state="visible", timeout=15000)
        self.page.wait_for_timeout(300)

    def no_script_ran(self, step):
        page = self.page
        page.wait_for_timeout(300)
        assert page.evaluate("() => window.__xss692") is None, f"{step}: injected script ran"
        handlers = page.evaluate("""() => [...document.querySelectorAll('*')].filter(el =>
            [...el.attributes].some(a => /^on/i.test(a.name) && /__xss692/.test(a.value))).length""")
        assert handlers == 0, f"{step}: {handlers} elements with an injected handler"
        injected = page.evaluate("""() => [...document.querySelectorAll('*')].filter(el =>
            el.hasAttribute('onmouseover')).length""")
        assert injected == 0, f"{step}: an attribute value broke out of its quotes"

    def shows(self, selector, text):
        inner = self.page.locator(selector).first.inner_text()
        assert text in inner, f"{text!r} not shown as text in {selector}: {inner[:300]!r}"

    def shot(self, name):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            self.page.screenshot(path=os.path.join(SHOTS, f"{name}.png"), full_page=True)

    def dump(self, name, selector):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            html = self.page.locator(selector).first.evaluate("el => el.innerHTML")
            with open(os.path.join(SHOTS, f"account-markup-{name}.html"), "w", encoding="utf-8") as f:
                f.write(html)

    def close(self):
        self.context.close()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


def test_values_render_as_text_on_every_tab(browser):
    account = Account(browser, hostile=True)
    page = account.page
    try:
        account.open()
        bad = "onerror="

        account.tab("speakers", "#speakersTabGrid [data-speaker-id]")
        account.shows("#speakersTabGrid", "Dana <img")
        account.no_script_ran("speakers")
        page.locator("#speakersTabGrid .view-snippets-btn").first.click()
        page.locator("#speakersTabGrid .snippet-audio").first.wait_for(state="attached", timeout=10000)
        title = page.locator("#speakersTabGrid .snippets-content [title]").first.get_attribute("title")
        assert bad in title and title.startswith("<img")
        account.no_script_ran("snippets")
        for box in page.locator("#speakersTabGrid .speaker-select-checkbox").all():
            box.check()
        for button, dialog, listing in (("bulk-merge-btn", "#mergeSpeakersModal", "#mergeSpeakersList"),
                                         ("bulk-delete-btn", "#deleteSelectedSpeakersModal", "#deleteSelectedSpeakersList"),
                                         ("bulk-clear-btn", "#clearVoiceProfilesModal", "#clearVoiceProfilesList")):
            page.locator(f".{button}:visible").first.click()
            page.locator(dialog).wait_for(state="visible", timeout=5000)
            account.shows(listing, "Dana <img")
            account.shows(listing, "Omar\"")
            account.no_script_ran(button)
            page.evaluate("(id) => document.querySelector(id).classList.add('hidden')", dialog)

        account.tab("shares", "#shares-container section")
        container = "#shares-container"
        for text in ("Budget <img", "Hiring <img", "Roadmap <img", "lee<img", "kim<img"):
            account.shows(container, text)
        # The dates that are not dates show as the text they are (formatInstant)
        assert page.locator(container).inner_text().count("<img src=\"data:image/png") >= 2
        link = page.locator(f"{container} .share-link-input").input_value()
        assert link.endswith(f"share/abc{QUOTE}{XSS}")
        account.no_script_ran("shares")
        page.locator(f"{container} .change-permissions-btn").first.click()
        page.locator(f"{container} .permission-editor").wait_for(state="visible", timeout=5000)
        account.no_script_ran("permission editor")
        page.locator(f"{container} .revoke-internal-btn").first.click()
        account.no_script_ran("revoke confirmation")

        account.tab("organize", "#tagsGrid > *")
        account.shows("#tagsGrid", "Fin <img")
        account.shows("#tagsGrid", XSS + ": Team")
        account.no_script_ran("tags")
        page.locator("#tagsGrid .toggle-prompt-btn").first.click()
        account.shows("#tagsGrid .prompt-content", "Prompt <img")

        account.tab("templates", "#templatesList > *")
        account.shows("#templatesList", "Min <img")
        account.no_script_ran("templates")

        account.tab("tokens", "#activeTokensList > *")
        account.shows("#activeTokensList", "CI'); window.__xss692=1;")
        account.no_script_ran("tokens")
        # The revoke button passes the name as data: the app's dialog shows it as text
        page.locator("#activeTokensList .revoke-token-btn").first.click()
        message = page.locator(".app-dialog__message")
        message.wait_for(state="visible", timeout=5000)
        assert "CI'); window.__xss692=1; ('<img" in message.inner_text()
        page.keyboard.press("Escape")
        account.no_script_ran("token revoke")

        account.tab("webhooks", "#webhook-list [data-webhook-id]")
        account.shows("#webhook-list", "Zap <img")
        account.shows("#webhook-list", "https://x.test/<img")
        page.locator('#webhook-list [data-action="deliveries"]').first.click()
        page.locator("#webhook-list [data-replay]").first.wait_for(state="visible", timeout=5000)
        account.shows("#webhook-list [data-deliveries-list]", "<img")
        account.no_script_ran("webhooks")
        page.locator("#webhook-create-btn").click()
        account.no_script_ran("webhook dialog")

        check_common(page, account.problems, "/account (hostile values)")
    finally:
        account.close()


# The tab, the element that holds its markup, and what shows it has loaded
TABS = (
    ("speakers", "#speakersTabGrid", "#speakersTabGrid [data-speaker-id]"),
    ("shares", "#shares-container", "#shares-container section"),
    ("organize", "#tagsGrid", "#tagsGrid > *"),
    ("templates", "#templatesList", "#templatesList > *"),
    ("tokens", "#activeTokensList", "#activeTokensList > *"),
    ("webhooks", "#webhook-list", "#webhook-list [data-webhook-id]"),
)


@pytest.mark.parametrize("theme", THEMES)
def test_the_tabs_in_every_theme(browser, theme):
    account = Account(browser, hostile=False)
    page = account.page
    page.goto(f"{BASE}/account")
    saved = page.evaluate(
        "() => [document.documentElement.dataset.uiTheme || 'default', document.documentElement.classList.contains('dark')]")
    try:
        assert account.api("POST", "/api/user/preferences", {"ui_theme": theme}).ok
        for mode in ("light", "dark"):
            assert account.api("POST", "/api/user/preferences", {"dark_mode": mode == "dark"}).ok
            page.evaluate("(dark) => localStorage.setItem('darkMode', dark)", "true" if mode == "dark" else "false")
            account.open()
            assert page.locator("html").get_attribute("data-ui-theme") == theme
            for name, holder, ready in TABS:
                account.tab(name, ready)
                if name == "speakers":
                    page.locator("#speakersTabGrid .view-snippets-btn").first.click()
                    page.locator("#speakersTabGrid .snippet-audio").first.wait_for(state="attached", timeout=10000)
                if name == "webhooks":
                    page.locator('#webhook-list [data-action="deliveries"]').first.click()
                    page.locator("#webhook-list [data-replay]").first.wait_for(state="visible", timeout=5000)
                account.no_script_ran(f"{theme} {mode} {name}")
                if theme == "default" and mode == "light":
                    account.dump(name, holder)
                account.shot(f"account-markup-{theme}-{mode}-{name}")
            body = page.inner_text("body")
            assert "${" not in body, f"{theme} {mode}: template text left in the page"
            assert not account.problems.errors, f"{theme} {mode}: {account.problems.errors}"
    finally:
        account.api("POST", "/api/user/preferences", {"ui_theme": saved[0], "dark_mode": saved[1]})
        account.close()
