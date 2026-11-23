"""Browser test of the Language models panel of the admin page.

The server tests (tests/test_llm_config.py and the route tests) cover the
storage and the routes; this test covers the part of the panel that only runs
in the browser:

- "Discover models" asks the provider for its model ids and a chosen id fills
  the model name field. The provider is the FakeLLMServer of this directory,
  which reports ``test-model``.
- The Extra Body field is checked before a save is attempted. Invalid JSON and
  a JSON value that is not an object keep the modal open with the validation
  message and send no POST to /admin/llm-config/models. A JSON object passes
  the check and is saved.
- API keys are write-only (#661): the page never receives a stored key. Editing
  a saved model shows an empty key field with the mask as its placeholder,
  saving with the field empty keeps the stored key, and the checkbox removes
  it. Changing the base URL shows a warning that the stored key is removed on
  save.

The second step creates one model profile, so the test deletes it through the
admin API in a finally block, whichever assertion failed. The discovery step
does not persist anything: it only probes the provider, and the modal is
closed.

Each step fails on a JavaScript error, an unexpected same-origin response of
400 or higher and template text left in the page (tests/e2e/browser_checks.py).

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test.
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
from fake_llm_server import FakeLLMServer  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
RUN = secrets.token_hex(3)

MODAL = "[data-llm-modal]"
DISCOVER = f"{MODAL} [data-llm-discover]"
DISCOVERED = f"{MODAL} [data-llm-discovered]"
SAVE = f"{MODAL} [data-llm-save]"
FORM_ERROR = f"{MODAL} [data-llm-form-error]"
SETTINGS_TAB = en("adminDashboard.systemSettings")
ADD_MODEL = en("adminDashboard.llmModels.addModel")
INVALID = en("adminDashboard.llmModels.extraBodyInvalid")
MODEL_URL = "/admin/llm-config/models"


def field(name):
    return f'{MODAL} [data-llm-field="{name}"]'


class Admin:
    """The seeded admin in one browser context."""

    def __init__(self, browser):
        self.context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.problems.reset()
        # POSTs of a model save, to prove that the client-side check came first.
        self.model_posts = []
        self.page.on("request", self._request)
        self.created = []

    def _request(self, request):
        path = request.url[len(BASE):].split("?")[0]
        if request.method == "POST" and path == MODEL_URL:
            self.model_posts.append(path)

    def csrf(self):
        return self.page.locator("meta[name=csrf-token]").get_attribute("content")

    def api(self, method, path, data=None):
        headers = {"X-CSRFToken": self.csrf()}
        if data is not None:
            headers["Content-Type"] = "application/json"
            return self.page.request.fetch(f"{BASE}{path}", method=method, headers=headers, data=data)
        return self.page.request.fetch(f"{BASE}{path}", method=method, headers=headers)

    def open_settings(self):
        open_page(self.page, self.problems, BASE, "/admin")
        self.page.locator("nav.tabs button.tab", has_text=SETTINGS_TAB).first.click()
        panel = self.page.locator("#llm-models")
        panel.wait_for(state="visible", timeout=10000)
        return panel

    def open_modal(self):
        self.page.locator("#llm-models button", has_text=ADD_MODEL).first.click()
        modal = self.page.locator(MODAL)
        modal.wait_for(state="visible", timeout=10000)
        return modal

    def close_modal(self):
        self.page.locator(f"{MODAL} .modal-close").click()
        self.page.locator(MODAL).wait_for(state="detached", timeout=10000)

    def save(self):
        """Submit the modal and return the response of the save request."""
        with self.page.expect_response(
                lambda r: r.url[len(BASE):].split("?")[0] == MODEL_URL and r.request.method == "POST",
                timeout=30000) as answer:
            self.page.locator(SAVE).click()
        return answer.value

    def check(self, step):
        check_common(self.page, self.problems, f"/admin ({step})")


@pytest.fixture(scope="module")
def llm():
    with FakeLLMServer() as server:
        yield server


@pytest.fixture(scope="module")
def admin():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        a = Admin(browser)
        try:
            yield a
        finally:
            for model_id in a.created:
                a.api("DELETE", f"{MODEL_URL}/{model_id}")
            a.context.close()
            browser.close()


def test_discovery_fills_the_model_name(admin, llm):
    page = admin.page
    admin.open_settings()
    admin.open_modal()
    page.locator(field("base_url")).fill(llm.url)
    with page.expect_response(lambda r: r.url.endswith("/admin/llm-config/test") and r.request.method == "POST",
                              timeout=30000) as probed:
        page.locator(DISCOVER).click()
    assert probed.value.ok, f"discovery: {probed.value.status} {probed.value.text()}"
    discovered = page.locator(DISCOVERED)
    discovered.wait_for(state="visible", timeout=10000)
    offered = discovered.locator("option").evaluate_all("options => options.map(o => o.value)")
    assert "test-model" in offered, f"the provider's models are {offered}"
    discovered.select_option("test-model")
    playwright_api.expect(page.locator(field("model_name"))).to_have_value("test-model", timeout=10000)
    admin.close_modal()
    assert not page.locator(MODAL).count(), "the modal is still open after Cancel"
    admin.check("discovery")


def test_extra_body_is_checked_before_saving(admin, llm):
    page = admin.page
    label = f"E2E browser {RUN}"
    admin.open_settings()
    admin.open_modal()
    page.locator(field("label")).fill(label)
    page.locator(field("base_url")).fill(llm.url)
    page.locator(field("model_name")).fill("test-model")

    # The eye button reveals only what is being typed; the saved key is never
    # sent to the browser. The field starts hidden and the toggle goes back to
    # hidden after the save.
    key_field = page.locator("[data-llm-api-key]")
    toggle = page.locator("[data-llm-api-key-toggle]")
    typed_key = f"e2e-typed-{RUN}"
    key_field.fill(typed_key)
    assert key_field.get_attribute("type") == "password", "the key is visible by default"
    toggle.click()
    assert key_field.get_attribute("type") == "text", "the eye button did not reveal the key"
    toggle.click()
    assert key_field.get_attribute("type") == "password", "the eye button did not hide the key again"
    admin.check("API key visibility")

    invalids = ("{bad json", "[1,2]")
    for raw in invalids:
        page.locator(field("extra_body")).fill(raw)
        before = len(admin.model_posts)
        page.locator(SAVE).click()
        error = page.locator(FORM_ERROR)
        error.wait_for(state="visible", timeout=10000)
        assert error.inner_text().strip() == INVALID, f"Extra Body {raw!r} was reported as {error.inner_text()!r}"
        page.wait_for_timeout(300)  # a save would have started by now
        assert len(admin.model_posts) == before, f"Extra Body {raw!r} was sent to {MODEL_URL}"
        assert page.locator(MODAL).count(), f"Extra Body {raw!r} closed the modal"
    admin.check("invalid Extra Body")

    # A JSON object passes the check and reaches the server.
    page.locator(field("extra_body")).fill('{"chat_template_kwargs": {"enable_thinking": false}}')
    response = admin.save()
    assert response.status == 201, f"save: {response.status} {response.text()}"
    model = response.json()["model"]
    admin.created.append(model["id"])
    assert model["api_key"] == "***", model
    assert typed_key not in response.text(), "the save answer repeats the key"
    assert model["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}, model
    page.locator(MODAL).wait_for(state="detached", timeout=10000)
    row = page.locator(f"#llm-models [data-llm-model='{model['id']}']")
    row.wait_for(state="visible", timeout=10000)
    assert label in row.inner_text(), f"the saved model row does not show {label!r}: {row.inner_text()!r}"
    admin.check("saved model")


def _edit(admin, model_id):
    page = admin.page
    admin.open_settings()
    row = page.locator(f"#llm-models [data-llm-model='{model_id}']")
    row.wait_for(state="visible", timeout=10000)
    row.locator(f"button[title='{en('adminDashboard.llmModels.editModel')}']").click()
    page.locator(MODAL).wait_for(state="visible", timeout=10000)
    return row


def _save_edit(admin, model_id):
    page = admin.page
    with page.expect_response(
            lambda r: r.url[len(BASE):].split("?")[0] == f"{MODEL_URL}/{model_id}" and r.request.method == "POST",
            timeout=30000) as answer:
        page.locator(SAVE).click()
    saved = answer.value
    assert saved.ok, f"save: {saved.status} {saved.text()}"
    page.locator(MODAL).wait_for(state="detached", timeout=10000)
    return saved


def test_a_stored_key_is_write_only(admin, llm):
    page = admin.page
    stored = f"e2e-stored-{RUN}"
    response = admin.api("POST", MODEL_URL, {
        "label": f"E2E edit {RUN}",
        "base_url": llm.url,
        "model_name": "test-model",
        "api_key": stored,
    })
    assert response.status == 201, f"seed: {response.status} {response.text()}"
    model = response.json()["model"]
    admin.created.append(model["id"])
    assert stored not in response.text(), "the create answer repeats the key"

    listing = admin.api("GET", "/admin/llm-config")
    assert stored not in listing.text(), "GET /admin/llm-config returns the stored key"
    listed = next(m for m in listing.json()["models"] if m["id"] == model["id"])
    assert listed["api_key"] == "***", listed

    row = _edit(admin, model["id"])
    assert stored not in row.inner_text(), "the configured models list shows the stored key"
    assert stored not in page.content(), "the page holds the stored key"

    # The field is empty, with the mask of a stored key as its placeholder.
    key_field = page.locator("[data-llm-api-key]")
    assert key_field.input_value() == "", "the key field is not empty"
    assert key_field.get_attribute("placeholder") == "********", key_field.get_attribute("placeholder")
    assert key_field.get_attribute("type") == "password", "a typed key is visible by default"
    clear = page.locator(f"{MODAL} label", has_text=en("adminDashboard.llmModels.clearApiKey"))
    assert clear.is_visible(), "the remove-key checkbox is missing for a stored key"
    admin.check("stored key")

    # Changing the base URL warns that the stored key goes on save; restoring
    # the URL removes the warning.
    warning = page.locator("[data-llm-url-changed]")
    assert not warning.count() or not warning.is_visible(), "the URL warning shows before any change"
    page.locator(field("base_url")).fill("http://127.0.0.1:1/v1")
    warning.wait_for(state="visible", timeout=5000)
    page.locator(field("base_url")).fill(llm.url)
    warning.wait_for(state="detached", timeout=5000)

    # Saving with the field empty keeps the key on the server.
    saved = _save_edit(admin, model["id"])
    assert saved.json()["model"]["api_key"] == "***", saved.text()
    assert stored not in saved.text(), "the save answer repeats the key"
    admin.check("saved masked key")

    # The checkbox removes the key.
    _edit(admin, model["id"])
    page.locator(f"{MODAL} label", has_text=en("adminDashboard.llmModels.clearApiKey")).click()
    saved = _save_edit(admin, model["id"])
    assert saved.json()["model"]["api_key"] == "", saved.text()
    row = page.locator(f"#llm-models [data-llm-model='{model['id']}']")
    playwright_api.expect(row).to_contain_text(en("adminDashboard.llmModels.noApiKey"), timeout=10000)
    admin.check("removed key")


def test_the_model_limit_hides_add_and_explains_it(admin, llm):
    page = admin.page
    config = admin.api("GET", "/admin/llm-config").json()
    limit = config["model_limit"]
    existing = len(config["models"])
    assert limit > 0

    for index in range(limit - existing):
        response = admin.api("POST", MODEL_URL, {
            "label": f"Limit {RUN} {index}",
            "base_url": llm.url,
            "model_name": "test-model",
        })
        assert response.status == 201, f"seed {index}: {response.status} {response.text()}"
        admin.created.append(response.json()["model"]["id"])

    admin.open_settings()
    page.locator("[data-llm-description]").wait_for(state="visible", timeout=10000)
    description = page.locator("[data-llm-description]").inner_text()
    expected = en("server.apiError.llm_model_limit_reached").replace("{{limit}}", str(limit))
    assert description == expected, f"limit description is {description!r}"
    assert page.locator("#llm-models button", has_text=ADD_MODEL).count() == 0, \
        "Add model is still offered at the limit"

    response = admin.api("POST", MODEL_URL, {
        "label": f"One more {RUN}",
        "base_url": llm.url,
        "model_name": "test-model",
    })
    assert response.status == 400, response.text()
    body = response.json()
    assert body["code"] == "llm_model_limit_reached", body
    assert str(limit) in body["error"], body
    admin.check("model limit")
