"""Browser test of the range of a queued file on the Upload files tab (#699).

A real ten-second WAV is put in the upload dialog. Its range control is
turned on, a start after the end is typed, and the file is not uploaded: no
request is sent, the file stays queued and the server's message shows under
it. The range is then set to 2 to 7 and the file uploaded. The instance's
own convert job cuts it, and the stored recording is five seconds long. No
transcription service runs on the test instance, so the transcription that
follows fails; the recording and its jobs are removed at the end.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test.
"""

import os
import re
import sys
import tempfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, clear_finished_jobs, en, login  # noqa: E402
from seed_recordings import write_tone  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")
WAIT = 30000


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


def _start_button(page):
    return page.locator(".modal-footer .btn--primary").filter(has_text=re.compile("1")).first


def test_a_queued_range_is_checked_in_the_browser_and_kept_by_the_server(browser):
    path = os.path.join(tempfile.mkdtemp(prefix="speakr-upload-trim-"), "range-test-upload.wav")
    write_tone(path, seconds=10)
    context = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US",
                                  timezone_id="UTC", service_workers="block")
    page = context.new_page()
    problems = PageProblems(page, BASE)
    sent = []
    page.on("request", lambda r: sent.append(r.url) if re.search(r"/upload(/session)?$", r.url) and r.method == "POST" else None)
    recording_id = None
    try:
        login(page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        page.wait_for_timeout(1500)
        page.locator("#app > header").get_by_role("button", name=en("nav.newRecording")).first.click()
        page.locator('input[type="file"]').first.set_input_files(path)

        # The control appears once the browser has read the file's length
        toggle = page.locator("[data-upload-trim-toggle]").first
        toggle.wait_for(state="visible", timeout=WAIT)
        assert toggle.inner_text().strip() == en("upload.trimToggle")
        toggle.click()
        start, end = page.locator("[data-upload-trim-start]"), page.locator("[data-upload-trim-end]")
        start.wait_for(state="visible", timeout=WAIT)
        assert end.input_value() == "10"

        # A start after the end: nothing is sent, the file stays queued
        start.fill("8")
        end.fill("3")
        problem = page.locator("[data-upload-trim-problem]")
        problem.wait_for(state="visible", timeout=WAIT)
        assert problem.inner_text().strip() == en("server.apiError.trim_invalid_range")
        button = _start_button(page)
        button.wait_for(state="visible", timeout=WAIT)
        button.click()
        page.wait_for_timeout(1500)
        assert sent == [], f"a range the server refuses was sent: {sent}"
        assert page.locator("[data-upload-trim-start]").count() == 1, "the file left the queue"
        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS, "upload-trim-refused.png"))

        # 2 to 7 keeps five seconds
        start.fill("2")
        end.fill("7")
        problem.wait_for(state="detached", timeout=WAIT)
        kept = page.locator("[data-upload-trim-kept]").inner_text()
        assert kept.strip() == en("upload.trimKept").replace("{{kept}}", "5s").replace("{{total}}", "10s"), kept
        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS, "upload-trim-range.png"))
        with page.expect_response(lambda r: r.url.endswith("/upload") and r.request.method == "POST", timeout=120000) as answer:
            _start_button(page).click()
        body = answer.value.json()
        assert answer.value.status in (200, 202), body
        recording_id = body["id"]
        assert len(sent) == 1

        # The upload answer already names the kept length; the stored file
        # is still whole until the convert job of the instance cuts it
        assert abs(float(body["audio_duration"]) - 5.0) < 0.3, body.get("audio_duration")
        record = {}
        convert = []
        deadline = time.time() + 120
        while time.time() < deadline:
            record = page.request.get(f"{BASE}/api/recordings/{recording_id}").json()
            jobs = page.request.get(f"{BASE}/api/recordings/job-queue-status").json().get("jobs", [])
            convert = [j.get("job_status") for j in jobs
                       if j.get("recording_id") == recording_id and j.get("job_type") == "convert"]
            if convert and all(status in ("completed", "failed") for status in convert):
                break
            time.sleep(1)
        assert convert == ["completed"], f"the convert job did not finish: {convert}"
        print(f"\nupload with range 2-7 of a 10 s file: recording {recording_id} is {record.get('audio_duration')!r} s, "
              f"{record.get('file_size')!r} bytes, {record.get('mime_type')!r}, status {record.get('status')!r}")
        assert abs(float(record["audio_duration"]) - 5.0) < 0.3, record.get("audio_duration")
        media = page.request.get(f"{BASE}/audio/{recording_id}")
        assert media.ok
        assert len(media.body()) == record["file_size"]
        assert record["file_size"] < os.path.getsize(path)
        assert not problems.errors, f"JavaScript errors: {problems.errors}"
    finally:
        if recording_id is not None:
            token = clear_finished_jobs(page.request, BASE)
            # The transcription may still be failing for want of a service
            deadline = time.time() + 60
            while time.time() < deadline:
                response = page.request.delete(f"{BASE}/recording/{recording_id}", headers={"X-CSRFToken": token})
                if response.ok or response.status == 404:
                    break
                time.sleep(2)
            clear_finished_jobs(page.request, BASE)
        context.close()
