"""Browser test of the app's own dialogs on a standalone page.

confirm(), alert() and prompt() of the browser are not used anywhere in the
app: static/js/app-dialog.js shows the question in the app's own dialog, on
the Vue app and on the pages that are not part of it. This test covers one of
those pages, the account page: a saved speaker is deleted from the Speakers
tab. Cancel, Esc and a click beside the panel keep the speaker; the button
that names the action deletes it. The message and input dialogs are shown
once each as well. The same question over the segment editor is covered in
test_asr_editor_ui.py.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test. With
SPEAKR_E2E_SHOTS set, a screenshot of the dialog is saved in that directory.
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

from browser_checks import PageProblems, check_common, en, login, open_page  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")

DIALOG = "[data-app-dialog]"
CONFIRM = f"{DIALOG} [data-app-dialog-confirm]"
CANCEL = f"{DIALOG} [data-app-dialog-cancel]"
NAME = "Dialog Test Person"


@pytest.fixture(scope="module")
def page():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        page = context.new_page()
        page.problems = PageProblems(page, BASE)
        # A dialog of the browser fails the test
        page.native = []
        page.on("dialog", lambda dialog: (page.native.append(dialog.message), dialog.dismiss()))
        login(page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        yield page
        browser.close()


def csrf(page):
    return page.locator('meta[name="csrf-token"]').first.get_attribute("content")


def saved_speakers(page):
    response = page.request.get(f"{BASE}/speakers")
    assert response.ok
    return {s["name"]: s["id"] for s in response.json()}


def test_deleting_a_speaker_on_the_account_page_asks_in_the_apps_dialog(page):
    open_page(page, page.problems, BASE, "/account")
    created = page.request.post(f"{BASE}/speakers", headers={"X-CSRFToken": csrf(page), "Content-Type": "application/json"},
                                data=json.dumps({"name": NAME}))
    assert created.ok
    try:
        open_page(page, page.problems, BASE, "/account")
        page.locator("#tab-speakers").click()
        card = page.locator("#content-speakers").locator(f"[data-speaker-name='{NAME.lower()}']").first
        card.wait_for(state="visible", timeout=10000)
        delete = card.locator(".delete-speaker-tab-btn")
        question = page.locator(DIALOG)

        # The question names the speaker, and the action is on a danger button
        delete.click()
        question.wait_for(state="visible", timeout=5000)
        assert question.locator(".app-dialog__message").inner_text() == en("account.confirmDeleteNamed").replace("{{name}}", NAME)
        confirm = page.locator(CONFIRM)
        assert confirm.inner_text().strip() == en("common.delete") and "btn--danger" in confirm.get_attribute("class")
        assert page.locator(CANCEL).inner_text().strip() == en("common.cancel")
        assert page.evaluate("() => document.activeElement.hasAttribute('data-app-dialog-confirm')")
        if SHOTS:
            page.wait_for_timeout(300)      # the dialog fades in
            os.makedirs(SHOTS, exist_ok=True)
            page.screenshot(path=os.path.join(SHOTS, "app-dialog-test-account.png"))
        # Tab stays inside
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.hasAttribute('data-app-dialog-cancel')")
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.hasAttribute('data-app-dialog-confirm')")

        # Cancel, Esc and a click beside the panel keep the speaker; the focus returns to the button
        page.locator(CANCEL).click()
        question.wait_for(state="detached", timeout=5000)
        assert page.evaluate("() => document.activeElement.classList.contains('delete-speaker-tab-btn')")
        delete.click()
        question.wait_for(state="visible", timeout=5000)
        page.keyboard.press("Escape")
        question.wait_for(state="detached", timeout=5000)
        delete.click()
        question.wait_for(state="visible", timeout=5000)
        page.mouse.click(4, 4)
        question.wait_for(state="detached", timeout=5000)
        assert NAME in saved_speakers(page) and card.count() == 1

        # Delete deletes
        delete.click()
        question.wait_for(state="visible", timeout=5000)
        with page.expect_response(lambda r: r.request.method == "DELETE" and "/speakers/" in r.url) as deleted:
            confirm.click()
        assert deleted.value.ok
        question.wait_for(state="detached", timeout=5000)
        card.wait_for(state="hidden", timeout=10000)     # the last speaker: the list is hidden, not emptied
        assert NAME not in saved_speakers(page)

        # A message and an input, in the same dialog
        page.evaluate("() => { window.__told = false; window.alertDialog('Saved.').then(() => { window.__told = true; }); }")
        question.wait_for(state="visible", timeout=5000)
        assert page.locator(CANCEL).count() == 0 and page.locator(CONFIRM).inner_text().strip() == en("common.ok")
        page.keyboard.press("Enter")
        question.wait_for(state="detached", timeout=5000)
        assert page.evaluate("() => window.__told") is True
        page.evaluate("() => { window.__typed = undefined; window.promptDialog({ message: 'Model id', value: 'whisper-1', confirmLabel: 'Add' }).then((v) => { window.__typed = v; }); }")
        field = page.locator(f"{DIALOG} [data-app-dialog-input]")
        field.wait_for(state="visible", timeout=5000)
        assert field.input_value() == "whisper-1"
        field.fill("large-v3")
        page.keyboard.press("Enter")
        question.wait_for(state="detached", timeout=5000)
        assert page.evaluate("() => window.__typed") == "large-v3"
        page.evaluate("() => { window.__typed = undefined; window.promptDialog('Model id').then((v) => { window.__typed = v; }); }")
        field.wait_for(state="visible", timeout=5000)
        field.fill("x")
        page.keyboard.press("Escape")
        question.wait_for(state="detached", timeout=5000)
        assert page.evaluate("() => window.__typed") is None

        check_common(page, page.problems, "account, app dialog")
        assert not page.native, f"a dialog of the browser was shown: {page.native}"
    finally:
        left = saved_speakers(page).get(NAME)
        if left is not None:
            page.request.delete(f"{BASE}/speakers/{left}", headers={"X-CSRFToken": csrf(page)})
