"""Browser test of the open recording when a job of another recording finishes (#702).

When a job completes for a recording that is not in the list of the page (an
upload through the API, another tab), the job-queue watcher reloads page 1 of
the list. The open recording was then cleared whenever it was not on page 1,
and the Identify speakers dialog, still open on it, failed to render: the page
went blank and the names typed in it were lost.

The queue's answers come from the test: a job of a recording the page does not
have is processing and then completed. The list of the page is answered
without the open recording, as if it were further down. The open recording is
kept while the server has it, and the dialog closes cleanly when it is gone.

The recordings come from seed_recordings.py, which run_speakr.py runs when it
starts the instance. Every test fails on a JavaScript error and a same-origin
response of 400 or higher that the test did not expect (browser_checks.py).

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL.
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

DIALOG = ".spk-modal"
# A recording the page does not have, as one uploaded through the API
OTHER_ID = 990001


def queue_row(status):
    return {
        "id": 880001, "recording_id": OTHER_ID, "recording_title": "Uploaded through the API",
        "job_status": status, "job_type": "transcribe", "queue_type": "transcription", "is_new_upload": True,
        "error_message": None, "stage": "completed" if status == "completed" else None, "stage_detail": None,
        "progress": None, "progress_kind": None, "progress_done": None, "progress_total": None,
        "position": None, "queue_position": None, "queue": None, "cancel_requested": False,
        "can_cancel": status == "processing", "created_at": None, "started_at": None,
        "completed_at": None, "finished_at": None,
    }


class Session:
    def __init__(self, browser):
        self.context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US",
                                           service_workers="block")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        listing = self.page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        self.ids = {r["title"]: r["id"] for r in items if r.get("title") in TITLES}
        self.jobs = [queue_row("processing")]
        self.hidden = None      # the recording left out of the list
        self.gone = False       # the server answers 404 for the hidden recording
        self.page.route(re.compile(r"/api/recordings/job-queue-status$"), self._status)
        self.page.route(re.compile(r"/api/recordings(\?.*)?$"), self._listing)
        self.page.route(re.compile(r"/api/recordings/\d+$"), self._recording)

    def _status(self, route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"server_time": None, "jobs": self.jobs}))

    def _listing(self, route):
        response = route.fetch()
        body = response.json()
        if self.hidden is not None and isinstance(body, dict):
            body["recordings"] = [r for r in body.get("recordings", []) if r.get("id") != self.hidden]
        route.fulfill(response=response, body=json.dumps(body))

    def _recording(self, route):
        recording_id = int(route.request.url.rsplit("/", 1)[1])
        if route.request.method != "GET":
            return route.continue_()
        if recording_id == OTHER_ID:
            response = self.page.request.get(f"{BASE}/api/recordings/{self.hidden}")
            body = response.json()
            body.update(id=OTHER_ID, title="Uploaded through the API")
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        if recording_id == self.hidden and self.gone:
            return route.fulfill(status=404, content_type="application/json",
                                 body=json.dumps({"error": "Recording not found"}))
        return route.continue_()

    def open_dialog(self, recording_id):
        page = self.page
        self.hidden = recording_id
        page.goto(f"{BASE}/recordings/{recording_id}")
        button = page.locator(f"button[title='{en('buttons.identifySpeakers')}']:visible").first
        button.wait_for(state="visible", timeout=60000)
        page.wait_for_timeout(1000)
        button.click()
        page.locator(f"{DIALOG} .spk-name input[type=text]").first.wait_for(state="visible", timeout=60000)

    def finish_the_other_job(self):
        """The job of the recording the page does not have completes, and the watcher reconciles."""
        self.jobs = [queue_row("completed")]
        # The poll runs every few seconds while a job is active; the reconcile follows
        # the request for the completed recording and the reload of page 1.
        with self.page.expect_request(re.compile(r"/api/recordings\?"), timeout=30000):
            pass
        self.page.wait_for_timeout(1500)

    def detail_shown(self):
        """Whether the detail view of a recording is shown (its Identify speakers button)."""
        return self.page.locator(f"button[title='{en('buttons.identifySpeakers')}']:visible").count() > 0

    def close(self):
        self.context.close()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def session(browser):
    s = Session(browser)
    if set(s.ids) != set(TITLES):
        s.close()
        precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    yield s
    s.close()


def test_the_dialog_stays_open_when_the_open_recording_is_not_on_page_one(session):
    recording_id = session.ids[TITLES[1]]
    session.open_dialog(recording_id)
    field = session.page.locator(f"{DIALOG} .spk-name input[type=text]").first
    field.fill("Dana")

    session.finish_the_other_job()

    # A render of the dialog after the reconcile, as the reporter's choice of a name
    field.fill("Dana Reyes")
    session.page.wait_for_timeout(500)
    assert session.page.locator(DIALOG).is_visible(), "the Identify speakers dialog closed"
    assert field.input_value() == "Dana Reyes", "the typed name was lost"
    assert session.detail_shown(), "the open recording was cleared"
    assert not session.problems.errors, session.problems.errors
    assert not session.problems.failed, session.problems.failed


def test_the_dialog_closes_cleanly_when_the_open_recording_was_deleted(session):
    recording_id = session.ids[TITLES[1]]
    session.open_dialog(recording_id)
    session.gone = True
    session.problems.expect(404, f"/api/recordings/{recording_id}")

    session.finish_the_other_job()

    session.page.locator(DIALOG).wait_for(state="detached", timeout=10000)
    assert not session.detail_shown(), "the deleted recording is still shown"
    assert session.page.locator("#app").inner_text().strip(), "the page is blank"
    assert not session.problems.errors, session.problems.errors
    assert not session.problems.failed, session.problems.failed
