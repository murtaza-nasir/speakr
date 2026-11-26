"""Browser test of the merged Transcription models section.

One section replaces the old curation table and the separate ASR profiles
panel, and the schema-driven modal covers the connector fields, secrets,
discovery, the real connection test and the immediate visibility and default
toggles.

The steps run in file order against a running Speakr that has no transcription
profiles (tests/e2e/run_speakr.py creates such an instance; the suite also
removes every profile before it starts). Setup that needs a provider uses
``fake_asr_server.FakeAsrServer``, so discovery and the connection test make
real HTTP requests to a local endpoint and need no network.

Each step fails on a JavaScript error, an unexpected same-origin response of
400 or higher and template text left in the page
(tests/e2e/browser_checks.py). A step that expects a refusal registers it with
``problems.expect``.

It runs only with SPEAKR_BROWSER_SMOKE=1 against SPEAKR_BASE_URL.
"""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import ROOT, PageProblems, check_common, en, login, open_page  # noqa: E402
from fake_asr_server import FakeAsrServer  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))

PANEL = "#asr-models"
ROW = "[data-asr-row]"
ENV_ROW = "[data-asr-environment]"
MODAL = "[data-asr-modal]"
DIALOG = "[data-app-dialog]"
CONFIRM = f"{DIALOG} [data-app-dialog-confirm]"
CANCEL = f"{DIALOG} [data-app-dialog-cancel]"
STATUS = "[data-asr-status]"
LABEL = "[data-asr-label]"
CONNECTOR = "[data-asr-connector]"
ADVANCED = "[data-asr-advanced-toggle]"
DISCOVER = "[data-asr-discover]"
DISCOVERED = "[data-asr-discovered]"
TEST = "[data-asr-test]"
TEST_RESULT = "[data-asr-test-result]"
SAVE = "[data-asr-save]"

# The copy the section needs, plus the extras the merged panel added. Every
# locale carries all of it.
PANEL_KEYS = (
    "transcriptionModels", "transcriptionModelsDesc", "addModel", "addFirstModel",
    "label", "labelHelp", "connector", "connectorHelp", "modelName", "modelNameHelp",
    "baseUrl", "apiKey", "apiKeyPlaceholder", "apiKeyHelp", "apiKeyKept", "clearApiKey",
    "apiKeyClearedOnUrlChange", "visible", "visibleHelp", "default", "actions",
    "environment", "environmentFallback", "environmentNote", "useEnvironment",
    "importEnvironment", "noModels", "noModelsHelp", "modelLimitReached", "noKey",
    "always", "defaultVisibleHint", "discover", "discovering", "discoveryEmpty",
    "pickDiscoveredModel", "otherModel", "test", "testing", "testSuccess",
    "testNoModels", "testFailed", "testUnsupported", "advanced", "advancedHelp",
    "editModel", "saveModel", "saving", "saved", "saveFailed", "loadFailed",
    "visibleChanged", "visibleFailed", "defaultChanged", "defaultFailed",
    "deleteConfirm", "deleteFailed", "deleted", "connectorChangeConfirm",
)
API_ERROR_KEYS = (
    "invalid_asr_config", "asr_profile_not_found", "asr_profile_label_taken",
    "asr_profile_limit_reached", "asr_profile_not_visible",
    "asr_config_managed_elsewhere",
)


def eventually(check, message, timeout=15.0):
    """Poll ``check`` until it returns a truthy value."""
    import time

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"{message} (last: {last!r})")


class Admin:
    """The seeded admin: a browser context, its page and its problems."""

    def __init__(self, browser):
        self.context = browser.new_context(viewport={"width": 1400, "height": 1000}, locale="en-US")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.problems.reset()

    def api(self, method, path, data=None):
        token = self.page.locator("meta[name=csrf-token]").get_attribute("content")
        headers = {"X-CSRFToken": token or ""}
        # A default change re-probes the voice embedding backend, which
        # transcribes the canary on the configured provider; a real endpoint
        # takes tens of seconds.
        return self.page.request.fetch(f"{BASE}{path}", method=method, headers=headers,
                                       data=data, timeout=90000)

    def document(self):
        response = self.api("GET", "/admin/asr-config")
        assert response.ok, f"GET /admin/asr-config: {response.status} {response.text()}"
        return response.json()

    def environment_label(self):
        environment = self.document().get("environment")
        return environment["label"] if environment else ""

    def create(self, label, connector="asr_endpoint", config=None, visible=True):
        payload = {"label": label, "connector": connector, "config": config or {}, "visible": visible}
        response = self.api("POST", "/admin/asr-config/models", payload)
        assert response.status == 201, f"create {label}: {response.status} {response.text()}"
        return response.json()["model"]["id"]

    def delete(self, profile_id):
        response = self.api("DELETE", f"/admin/asr-config/models/{profile_id}")
        assert response.ok, f"delete {profile_id}: {response.status} {response.text()}"

    def clear_profiles(self):
        for model in self.document().get("models") or []:
            self.delete(model["id"])

    def open_panel(self):
        open_page(self.page, self.problems, BASE, "/admin")
        self.page.locator("nav.tabs button.tab", has_text=en("adminDashboard.systemSettings")).first.click()
        self.page.locator(PANEL).wait_for(state="visible", timeout=15000)
        self.page.wait_for_timeout(800)
        return self.page.locator(PANEL)

    def open_add(self):
        self.page.locator("[data-asr-add]").click()
        self.page.locator(MODAL).wait_for(state="visible", timeout=10000)
        return self.page.locator(MODAL)

    def open_edit(self, label):
        self.row(label).locator("[data-asr-edit]").click()
        self.page.locator(MODAL).wait_for(state="visible", timeout=10000)
        return self.page.locator(MODAL)

    def close_modal(self):
        if self.page.locator(MODAL).count():
            self.page.locator("[data-asr-cancel]").click()
            self.page.locator(MODAL).wait_for(state="detached", timeout=10000)

    def row(self, label):
        return self.page.locator(ROW, has_text=label).first

    def field(self, key):
        return self.page.locator(f'[data-asr-field="{key}"] input, [data-asr-field="{key}"] select').first

    def secret(self, key):
        return self.page.locator(f"#asr-field-{key}")

    def choose(self, connector):
        self.page.locator(CONNECTOR).select_option(connector)
        self.page.wait_for_timeout(200)

    def confirm(self, accept=True):
        self.page.locator(DIALOG).wait_for(state="visible", timeout=10000)
        self.page.locator(CONFIRM if accept else CANCEL).click()
        self.page.locator(DIALOG).wait_for(state="detached", timeout=10000)

    def save(self):
        self.page.locator(SAVE).click()
        self.page.locator(MODAL).wait_for(state="detached", timeout=15000)

    def check(self):
        check_common(self.page, self.problems, "/admin")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def admin(browser):
    session = Admin(browser)
    session.clear_profiles()
    session.open_panel()
    yield session
    session.clear_profiles()
    session.context.close()


# --------------------------------------------------------------- one section


def test_single_section_replaces_both(admin):
    admin.open_panel()
    page = admin.page
    assert page.locator(PANEL).count() == 1
    assert page.locator("[data-asr-panel]").count() == 1
    heading = page.locator(f"{PANEL} h4").first.inner_text()
    assert en("adminDashboard.transcriptionModels") in heading
    body = page.inner_text("body")
    assert "ASR (transcription) profiles" not in body
    assert page.locator("#asr-model-modal").count() == 0
    assert page.locator("[data-asr-visible-models]").count() == 0
    admin.check()


def test_fresh_state_shows_environment_row(admin):
    document = admin.document()
    assert document["models"] == []
    environment = document["environment"]
    assert environment is not None
    page = admin.page
    admin.open_panel()
    row = page.locator(ENV_ROW)
    assert row.count() == 1
    assert environment["label"] in row.inner_text()
    assert en("adminDashboard.always") in row.inner_text()
    admin.check()


def test_env_row_edit_snapshots_into_profile_with_default_label(admin):
    page = admin.page
    expected = admin.environment_label()
    admin.open_panel()
    page.locator(ENV_ROW).locator("[data-asr-edit]").click()
    page.locator(MODAL).wait_for(state="visible", timeout=10000)
    assert page.locator(LABEL).input_value() == expected
    # The snapshot is a new profile, not an edit of the environment.
    assert en("adminDashboard.addModel") in page.locator(f"{MODAL} h3").inner_text()
    # While the label is untouched, choosing a model completes the suggestion.
    page.locator("#asr-field-model").fill("large-v3")
    page.wait_for_timeout(300)
    assert page.locator(LABEL).input_value() == expected.rstrip(":") + ":large-v3"
    # The label is editable: once written by hand it stops following the model,
    # and "Save model" fills a dangling colon with the chosen model name.
    page.locator(LABEL).fill(expected.rstrip(":") + ":")
    assert page.locator(LABEL).input_value().endswith(":")
    page.locator("#asr-field-model").fill("small")
    page.wait_for_timeout(300)
    assert page.locator(LABEL).input_value() == expected.rstrip(":") + ":"
    admin.save()
    document = admin.document()
    assert len(document["models"]) == 1
    profile = document["models"][0]
    assert profile["id"].startswith("m_")
    assert profile["label"] == expected.rstrip(":") + ":small"
    assert profile["config"].get("model") == "small"
    assert page.locator(ENV_ROW).count() == 0
    assert page.locator("[data-asr-import]").count() == 1
    admin.delete(profile["id"])
    admin.open_panel()
    assert page.locator(ENV_ROW).count() == 1
    admin.check()


# ------------------------------------------------------------------- the modal


def test_add_model_label_defaults_to_connector_colon_model(admin):
    admin.open_panel()
    admin.open_add()
    assert admin.page.locator(LABEL).input_value() == "openai-whisper:whisper-1"
    admin.close_modal()
    admin.check()


def test_label_stops_autofilling_after_edit(admin):
    page = admin.page
    admin.open_panel()
    admin.open_add()
    page.locator(LABEL).fill("My provider")
    page.locator("#asr-field-model").fill("large-v3")
    page.wait_for_timeout(200)
    assert page.locator(LABEL).input_value() == "My provider"
    admin.close_modal()
    admin.check()


def test_connector_change_resets_fields_with_confirmation(admin):
    page = admin.page
    admin.open_panel()
    admin.open_add()
    page.locator("#asr-field-api_key").fill("sk-test")
    admin.choose("asr_endpoint")
    admin.confirm(accept=False)
    assert page.locator(CONNECTOR).input_value() == "openai_whisper"
    assert page.locator("#asr-field-api_key").input_value() == "sk-test"
    admin.choose("asr_endpoint")
    admin.confirm(accept=True)
    assert page.locator(CONNECTOR).input_value() == "asr_endpoint"
    assert page.locator("#asr-field-base_url").count() == 1
    # The WhisperX connector sends no key, so the form has no key field and
    # the key typed for the previous connector is gone with it.
    assert page.locator("#asr-field-api_key").count() == 0
    admin.close_modal()
    admin.check()


def test_common_fields_from_schema(admin):
    page = admin.page
    admin.open_panel()
    admin.open_add()
    assert page.locator("#asr-field-api_key").count() == 1
    assert page.locator("#asr-field-base_url").count() == 1
    assert page.locator("#asr-field-model").count() == 1
    assert page.locator(ADVANCED).count() == 0
    assert page.locator("#asr-field-api_key").get_attribute("type") == "password"
    assert en("adminDashboard.apiKey") in page.locator(f'[data-asr-field="api_key"] label').inner_text()
    admin.close_modal()
    admin.check()


def test_advanced_fields_azure_tencent_funasr(admin):
    page = admin.page
    admin.open_panel()
    admin.open_add()
    for connector, key in (("azure_openai_transcribe", "api_version"),
                           ("tencent_asr", "hotword_id"),
                           ("funasr", "timestamp_alignment_enabled")):
        admin.choose(connector)
        assert page.locator(ADVANCED).count() == 1, connector
        if not page.locator(f'[data-asr-field="{key}"]').count():
            page.locator(ADVANCED).click()
            page.wait_for_timeout(200)
        assert page.locator(f'[data-asr-field="{key}"]').count() == 1, connector
    admin.close_modal()
    admin.check()


def test_diarize_is_on_by_default_where_supported(admin):
    """Every connector with a "Diarize by default" field starts with it on."""
    page = admin.page
    admin.open_panel()
    admin.open_add()
    for connector, key in (("asr_endpoint", "diarize"),
                           ("funasr", "diarize"),
                           ("openasr", "diarize"),
                           ("tencent_asr", "diarize")):
        admin.choose(connector)
        if page.locator(DIALOG).is_visible():
            admin.confirm(accept=True)
        if not page.locator(f'[data-asr-field="{key}"]').count():
            page.locator(ADVANCED).click()
            page.wait_for_timeout(200)
        assert page.locator(f"#asr-field-{key}").is_checked(), connector
    admin.close_modal()
    admin.check()


def test_asr_model_hint_lists_whisperx_models(admin):
    page = admin.page
    admin.open_panel()
    admin.open_add()
    admin.choose("asr_endpoint")
    placeholder = page.locator("#asr-field-model").get_attribute("placeholder")
    assert "large-v3" in placeholder and "distil-large-v3" in placeholder
    admin.close_modal()
    admin.check()


# -------------------------------------------------------------------- secrets


def test_api_key_edit_never_shows_the_stored_key(admin):
    """The key is write-only: the edit form starts empty and says a key exists."""
    with FakeAsrServer() as fake:
        profile_id = admin.create("Key profile", connector="openai_whisper", config={"base_url": fake.url, "model": "small", "api_key": "sk-visible-key"})
        try:
            admin.open_panel()
            admin.open_edit("Key profile")
            field = admin.secret("api_key")
            assert field.input_value() == ""
            placeholder = field.get_attribute("placeholder") or ""
            assert placeholder and placeholder != en("adminDashboard.apiKeyPlaceholder")
            assert field.get_attribute("type") == "password"
            assert admin.page.locator("[data-asr-clear-secrets]").count() == 1
            admin.close_modal()
            admin.check()
        finally:
            admin.delete(profile_id)


def test_api_key_eye_toggle_and_select_all(admin):
    page = admin.page
    admin.open_panel()
    admin.open_add()
    page.locator("#asr-field-api_key").fill("sk-select-me")
    toggle = page.locator('[data-asr-secret-toggle="api_key"]')
    toggle.click()
    assert page.locator("#asr-field-api_key").get_attribute("type") == "text"
    toggle.click()
    assert page.locator("#asr-field-api_key").get_attribute("type") == "password"
    page.locator("#asr-field-api_key").click()
    selection = page.evaluate("""() => {
        const el = document.getElementById('asr-field-api_key');
        return [el.selectionStart, el.selectionEnd, el.value.length];
    }""")
    assert selection[0] == 0 and selection[1] == selection[2] == len("sk-select-me")
    admin.close_modal()
    admin.check()


def test_api_key_url_change_warning(admin):
    page = admin.page
    with FakeAsrServer() as fake:
        profile_id = admin.create("Url warning", connector="openai_whisper", config={"base_url": fake.url, "model": "small", "api_key": "sk-test"})
        try:
            admin.open_panel()
            admin.open_edit("Url warning")
            assert page.locator("[data-asr-url-warning]").count() == 0
            page.locator("#asr-field-base_url").fill(fake.url + "/other")
            page.wait_for_timeout(200)
            assert page.locator("[data-asr-url-warning]").count() == 1
            admin.close_modal()
            admin.check()
        finally:
            admin.delete(profile_id)


def test_api_key_untouched_keeps_key(admin):
    page = admin.page
    with FakeAsrServer() as fake:
        profile_id = admin.create("Keep key", connector="openai_whisper", config={"base_url": fake.url, "model": "small", "api_key": "sk-keep-me"})
        try:
            admin.open_panel()
            admin.open_edit("Keep key")
            page.locator("#asr-field-model").fill("large-v3")
            admin.save()
            body = admin.document()
            mask = body.get("masked_api_key")
            stored = body["models"][0]
            assert mask
            assert stored["config"]["api_key"] == mask
            # The profile still holds a secret: the edit form reports it.
            admin.open_edit("Keep key")
            assert page.locator("[data-asr-clear-secrets]").count() == 1
            admin.close_modal()
            admin.check()
        finally:
            admin.delete(profile_id)


def test_api_key_clear_checkbox(admin):
    page = admin.page
    with FakeAsrServer() as fake:
        profile_id = admin.create("Clear key", connector="openai_whisper", config={"base_url": fake.url, "model": "small", "api_key": "sk-clear-me"})
        try:
            admin.open_panel()
            admin.open_edit("Clear key")
            assert page.locator("[data-asr-clear-secrets]").count() == 1
            page.locator("[data-asr-clear-secrets]").check()
            admin.save()
            stored = admin.document()["models"][0]
            assert not stored["config"].get("api_key")
            assert en("adminDashboard.noKey") in admin.row("Clear key").inner_text()
            admin.check()
        finally:
            admin.delete(profile_id)


def test_api_key_emptied_field_keeps_the_stored_key(admin):
    """An empty field means "unchanged"; only the checkbox removes the key."""
    page = admin.page
    with FakeAsrServer() as fake:
        profile_id = admin.create("Empty key", connector="openai_whisper", config={"base_url": fake.url, "model": "small", "api_key": "sk-empty-me"})
        try:
            admin.open_panel()
            admin.open_edit("Empty key")
            page.locator("#asr-field-api_key").fill("")
            admin.save()
            body = admin.document()
            stored = body["models"][0]
            assert stored["config"]["api_key"] == body["masked_api_key"]
            admin.open_edit("Empty key")
            assert page.locator("[data-asr-clear-secrets]").count() == 1
            admin.close_modal()
            admin.check()
        finally:
            admin.delete(profile_id)


# ------------------------------------------------------------------ discovery


def test_discovery_populates_dropdown_and_other_option(admin):
    page = admin.page
    with FakeAsrServer(models=["small", "large-v3"]) as fake:
        admin.open_panel()
        admin.open_add()
        admin.choose("asr_endpoint")
        page.locator("#asr-field-base_url").fill(fake.url)
        page.locator(DISCOVER).click()
        page.locator(DISCOVERED).wait_for(state="visible", timeout=15000)
        options = page.locator(f"{DISCOVERED} option").all_inner_texts()
        assert any("small" in text for text in options)
        assert any(text == en("adminDashboard.otherModel") for text in options)
        page.locator(DISCOVERED).select_option("large-v3")
        page.wait_for_timeout(300)
        assert page.locator(LABEL).input_value() == "whisper-asr:large-v3"
        admin.close_modal()
        admin.check()


def test_discovery_empty_keeps_editable_field(admin):
    page = admin.page
    with FakeAsrServer(models=[]) as fake:
        admin.open_panel()
        admin.open_add()
        admin.choose("asr_endpoint")
        page.locator("#asr-field-base_url").fill(fake.url)
        page.locator(DISCOVER).click()
        eventually(lambda: en("adminDashboard.discoveryEmpty") in page.locator(MODAL).inner_text(),
                   "the empty discovery message did not appear")
        assert page.locator(DISCOVERED).count() == 0
        assert page.locator("#asr-field-model").count() == 1
        admin.close_modal()
        admin.check()


# --------------------------------------------------------------- connection


def test_test_button_success_and_error_and_unsupported(admin):
    page = admin.page
    with FakeAsrServer(models=["small", "large-v3"]) as fake:
        admin.open_panel()
        admin.open_add()
        admin.choose("asr_endpoint")
        page.locator("#asr-field-base_url").fill(fake.url)
        page.locator(TEST).click()
        eventually(lambda: page.locator(TEST_RESULT).count() == 1,
                   "the successful test result did not appear")
        assert en("adminDashboard.testSuccess").split("{{")[0].strip() in page.locator(TEST_RESULT).inner_text()
        # A provider that refuses the connection is a failure, not a success.
        page.locator("#asr-field-base_url").fill("http://127.0.0.1:1")
        admin.problems.expect(502, "/admin/asr-config/test")
        page.locator(TEST).click()
        eventually(lambda: en("adminDashboard.testFailed") in page.locator(TEST_RESULT).inner_text(),
                   "the failed test result did not appear")
        # FunASR has no cheap probe: a warning, not a success.
        admin.choose("funasr")
        admin.confirm(accept=True)
        page.locator("#asr-field-base_url").fill(fake.url)
        page.locator("#asr-field-api_key").fill("dummy")
        page.locator(TEST).click()
        eventually(lambda: en("adminDashboard.testUnsupported") in page.locator(TEST_RESULT).inner_text(),
                   "the unsupported warning did not appear")
        admin.close_modal()
        admin.check()


# ------------------------------------------------------- visibility and default


def test_visible_toggle_saves_immediately(admin):
    page = admin.page
    with FakeAsrServer() as fake:
        profile_id = admin.create("Visible toggle", config={"base_url": fake.url, "model": "small"})
        try:
            admin.open_panel()
            checkbox = admin.row("Visible toggle").locator("[data-asr-visible]")
            assert checkbox.is_checked()
            checkbox.uncheck()
            eventually(lambda: any(
                model["id"] == profile_id and model["visible"] is False
                for model in admin.document()["models"]),
                "the visibility change was not saved")
            admin.row("Visible toggle").locator("[data-asr-visible]").check()
            eventually(lambda: any(
                model["id"] == profile_id and model["visible"] is True
                for model in admin.document()["models"]),
                "the visibility change back was not saved")
            admin.check()
        finally:
            admin.delete(profile_id)


def test_default_radio_saves_immediately(admin):
    page = admin.page
    with FakeAsrServer() as fake:
        profile_id = admin.create("Default radio", config={"base_url": fake.url, "model": "small"})
        try:
            admin.open_panel()
            admin.row("Default radio").locator("[data-asr-default]").check()
            eventually(lambda: admin.document()["default"] == profile_id,
                       "the default was not saved", timeout=90.0)
            assert en("adminDashboard.defaultChanged").split('"')[0].strip() in page.locator(STATUS).inner_text()
            page.locator("[data-asr-use-env-default]").click()
            eventually(lambda: admin.document()["default"] is None,
                       "the environment default was not restored", timeout=90.0)
            admin.check()
        finally:
            admin.delete(profile_id)


def test_hiding_default_blocked(admin):
    page = admin.page
    with FakeAsrServer() as fake:
        profile_id = admin.create("Hidden default", config={"base_url": fake.url, "model": "small"})
        try:
            response = admin.api("POST", "/admin/asr-config/default", {"profile_id": profile_id})
            assert response.ok, response.text()
            admin.open_panel()
            checkbox = admin.row("Hidden default").locator("[data-asr-visible]")
            assert checkbox.is_disabled()
            assert en("adminDashboard.defaultVisibleHint") in (checkbox.get_attribute("title") or "")
            admin.api("POST", "/admin/asr-config/default", {"profile_id": None})
            admin.check()
        finally:
            admin.delete(profile_id)


def test_delete_confirm_and_environment_fallback(admin):
    page = admin.page
    with FakeAsrServer() as fake:
        keep = admin.create("Delete me", config={"base_url": fake.url, "model": "small"})
        admin.open_panel()
        admin.row("Delete me").locator("[data-asr-delete]").click()
        assert en("adminDashboard.deleteConfirm").split("{{")[0].strip() in page.locator(DIALOG).inner_text()
        admin.confirm(accept=False)
        assert admin.row("Delete me").count() == 1
        admin.row("Delete me").locator("[data-asr-delete]").click()
        admin.confirm(accept=True)
        eventually(lambda: all(model["id"] != keep for model in admin.document()["models"]),
                   "the profile was not deleted")
        # With every profile gone, the environment row is the table again.
        # The panel reloads after the delete, so wait for the row, not only
        # for the API.
        eventually(lambda: page.locator(ENV_ROW).count() == 1,
                   "the environment row did not come back")
        assert en("adminDashboard.environment") in page.locator(ENV_ROW).inner_text()
        assert page.locator(ROW).count() == 0
        admin.check()


def test_no_js_errors_no_4xx_5xx_no_raw_template_text(admin):
    admin.clear_profiles()
    admin.open_panel()
    admin.check()


# --------------------------------------------------------------------- locales


def test_all_locales_have_the_new_keys():
    locale_dir = os.path.join(ROOT, "static", "locales")
    missing = {}
    for name in sorted(os.listdir(locale_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(locale_dir, name), encoding="utf-8") as f:
            payload = json.load(f)
        admin_copy = payload.get("adminDashboard", {})
        api_errors = payload.get("server", {}).get("apiError", {})
        absent = [key for key in PANEL_KEYS if not str(admin_copy.get(key) or "").strip()]
        absent += [f"server.apiError.{key}" for key in API_ERROR_KEYS if not str(api_errors.get(key) or "").strip()]
        if absent:
            missing[name] = absent
    assert not missing, f"locale keys missing: {missing}"
