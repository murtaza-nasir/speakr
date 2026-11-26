"""Browser test of the players when a recording's stored file is replaced (#707).

The convert job of an upload stores another file under the same recording id
while the recording is open, and so do a trim and the legacy reprocess. The
players address the audio as /audio/<id>?v=<audio_revision>, and they load
the new file under its new address at the position they had, playing on when
they were playing.

The seeded meeting recording is opened with a convert job of it in progress
in the job queue. Its audio plays from 0:20; the job then completes and the
recording is reported with another audio_revision, as after a conversion.
The test checks the new address, the position and that the audio plays on.
A media error then makes the page ask the status of the recording, which
reports a third revision: the players load it the same way.

The job queue and the recording are answered by the instance; only the
convert job and the revisions are added with page.route, so the stored file
of the seeded recording is not changed. The server side of the replacement
(the 410 for a range of an earlier file) is tested in tests/test_media_ranges.py.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test.
"""

import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, login, open_page, precondition_unmet  # noqa: E402
from seed_recordings import TITLES  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")
MEETING_TITLE = TITLES[1]
PLAYER = "#mainPlayerMedia"
FAKE_JOB_ID = 990707


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        yield instance
        instance.close()


def _player(page):
    return page.evaluate(f"""() => {{
        const m = document.querySelector('{PLAYER}');
        return m ? {{ src: m.getAttribute('src'), time: m.currentTime, paused: m.paused,
                      error: m.error ? m.error.code : null }} : null;
    }}""")


def _wait_playing_past(page, seconds, timeout=15000):
    page.wait_for_function(f"""() => {{
        const m = document.querySelector('{PLAYER}');
        return m && !m.paused && m.currentTime > {seconds};
    }}""", timeout=timeout)


def test_the_players_load_a_replaced_file_at_the_same_position(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US",
                                  timezone_id="UTC", service_workers="block")
    page = context.new_page()
    problems = PageProblems(page, BASE)
    try:
        login(page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        listing = page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        ids = [r["id"] for r in items if r.get("title") == MEETING_TITLE]
        if not ids:
            precondition_unmet(f"no seeded recording {MEETING_TITLE!r}")
        rid = ids[0]
        stored = page.request.get(f"{BASE}/api/recordings/{rid}").json()
        first = stored["audio_revision"]
        assert first, stored
        state = {"job": "processing", "revision": first, "status_revision": None}

        def queue(route):
            response = route.fetch()
            data = response.json()
            data["jobs"] = [j for j in data.get("jobs", []) if j.get("id") != FAKE_JOB_ID] + [{
                "id": FAKE_JOB_ID, "recording_id": rid, "recording_title": MEETING_TITLE,
                "job_status": state["job"], "job_type": "convert", "queue_type": "convert",
                "position": None, "is_new_upload": True, "error_message": None,
                "progress_done": None, "progress_total": None, "progress": None, "progress_kind": None,
                "stage": "converting", "stage_detail": None, "queue_position": None, "queue": None,
                "cancel_requested": False, "can_cancel": False,
                "created_at": data.get("server_time"), "started_at": data.get("server_time"),
                "completed_at": data.get("server_time") if state["job"] == "completed" else None,
                "finished_at": data.get("server_time") if state["job"] == "completed" else None,
            }]
            route.fulfill(response=response, json=data)

        def recording(route):
            response = route.fetch()
            data = response.json()
            data["audio_revision"] = state["revision"]
            route.fulfill(response=response, json=data)

        def status(route):
            response = route.fetch()
            data = response.json()
            if state["status_revision"]:
                data["audio_revision"] = state["status_revision"]
            route.fulfill(response=response, json=data)

        page.route(re.compile(r"/api/recordings/job-queue-status$"), queue)
        page.route(re.compile(rf"/api/recordings/{rid}$"), recording)
        page.route(re.compile(rf"/recording/{rid}/status$"), status)

        open_page(page, problems, BASE, f"/recordings/{rid}")
        page.locator(PLAYER).wait_for(state="attached", timeout=30000)

        # The address has the revision of the stored file from the start
        assert _player(page)["src"] == f"/audio/{rid}?v={first}"

        page.evaluate(f"""async () => {{
            const m = document.querySelector('{PLAYER}');
            if (m.readyState < 1) await new Promise(r => m.addEventListener('loadedmetadata', r, {{ once: true }}));
            m.currentTime = 20;
            await m.play();
        }}""")
        _wait_playing_past(page, 20.5)

        # The convert job completes: the recording has another stored file
        second = "e2e" + first[:9]
        state.update(job="completed", revision=second)
        page.wait_for_function(f"() => document.querySelector('{PLAYER}').getAttribute('src') === '/audio/{rid}?v={second}'",
                               timeout=20000)
        # The new file plays on from where the old one was
        _wait_playing_past(page, 20.5)
        after = _player(page)
        assert 20.5 < after["time"] < 40, after
        assert after["error"] is None, after
        page.wait_for_timeout(1500)
        assert _player(page)["time"] > after["time"] + 0.5
        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS, "audio-revision-after-convert.png"))

        # A media error: the page asks the status, which reports a newer file
        third = "e2f" + first[:9]
        state["status_revision"] = third
        before = _player(page)["time"]
        page.evaluate(f"() => document.querySelector('{PLAYER}').dispatchEvent(new Event('error'))")
        page.wait_for_function(f"() => document.querySelector('{PLAYER}').getAttribute('src') === '/audio/{rid}?v={third}'",
                               timeout=10000)
        _wait_playing_past(page, before - 0.5)
        assert _player(page)["time"] < before + 15

        assert not problems.errors, problems.errors
        assert not problems.failed, problems.failed
    finally:
        context.close()
