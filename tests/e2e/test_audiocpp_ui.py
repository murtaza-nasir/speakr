"""Browser test of the audio.cpp connector (#700) on the throwaway instance.

The admin adds an audio.cpp model in System settings > Transcription models
against fake_audiocpp_server.FakeAudioCppServer (discovery, test, save), marks
it as the default, uploads a recording, and the recording is transcribed
through the fake server's /v1/audio/transcriptions/details: the stored
transcript has the two speakers of the response, and the Identify speakers
dialog lists them.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL. With SPEAKR_E2E_SHOTS set, screenshots are saved there.
"""

import io
import json
import os
import sys
import tempfile
import time
import wave

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, en, login, open_page  # noqa: E402
from fake_audiocpp_server import FakeAudioCppServer  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")
MODAL = "[data-asr-modal]"
LABEL = "Review audio.cpp VibeVoice"


def tone_wav(seconds=6, rate=44100):
    """A stereo 44.1 kHz WAV, so the connector has to convert it."""
    import math
    path = os.path.join(tempfile.mkdtemp(prefix="speakr-audiocpp-"), "audiocpp-meeting.wav")
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for i in range(seconds * rate):
            value = int(8000 * math.sin(2 * math.pi * 440 * i / rate))
            sample = value.to_bytes(2, "little", signed=True)
            frames += sample + sample
        handle.writeframes(bytes(frames))
    return path


class Admin:
    def __init__(self, browser):
        self.context = browser.new_context(viewport={"width": 1400, "height": 1000}, locale="en-US")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.problems.reset()

    def csrf(self):
        return self.page.locator("meta[name=csrf-token]").first.get_attribute("content") or ""

    def api(self, method, path, data=None, multipart=None):
        headers = {"X-CSRFToken": self.csrf()}
        return self.page.request.fetch(f"{BASE}{path}", method=method, headers=headers,
                                       data=data, multipart=multipart, timeout=120000)

    def clear_profiles(self):
        self.api("POST", "/admin/asr-config/default", {"profile_id": None})
        for model in self.api("GET", "/admin/asr-config").json().get("models") or []:
            self.api("DELETE", f"/admin/asr-config/models/{model['id']}")

    def shot(self, name, locator=None):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            (locator or self.page).screenshot(path=os.path.join(SHOTS, f"audiocpp-{name}.png"))


@pytest.fixture(scope="module")
def admin():
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        session = Admin(browser)
        open_page(session.page, session.problems, BASE, "/admin")
        session.clear_profiles()
        yield session
        session.clear_profiles()
        browser.close()


def test_an_audiocpp_model_transcribes_with_speakers(admin):
    page = admin.page
    with FakeAudioCppServer() as fake:
        # Add the model in the admin section, as an admin does.
        open_page(page, admin.problems, BASE, "/admin")
        page.locator("nav.tabs button.tab", has_text=en("adminDashboard.systemSettings")).first.click()
        page.locator("#asr-models").wait_for(state="visible", timeout=15000)
        page.locator("[data-asr-add]").click()
        page.locator(MODAL).wait_for(state="visible", timeout=10000)
        page.locator("[data-asr-connector]").select_option("audiocpp")
        page.wait_for_timeout(300)
        assert page.locator("#asr-field-api_key").count() == 0
        page.locator("#asr-field-base_url").fill(fake.url)
        page.locator("[data-asr-discover]").click()
        discovered = page.locator("[data-asr-discovered]")
        discovered.wait_for(state="visible", timeout=15000)
        options = discovered.locator("option").all_inner_texts()
        assert any("vibevoice-asr" in text for text in options), options
        discovered.select_option("vibevoice-asr")
        page.wait_for_timeout(300)
        if not page.locator("#asr-field-model_diarizes").count():
            page.locator("[data-asr-advanced-toggle]").click()
            page.wait_for_timeout(200)
        page.locator("#asr-field-model_diarizes").check()
        page.locator("[data-asr-label]").fill(LABEL)
        page.locator("[data-asr-test]").click()
        page.locator("[data-asr-test-result]").wait_for(state="visible", timeout=15000)
        result = page.locator("[data-asr-test-result]").inner_text()
        assert "2" in result and en("adminDashboard.testFailed") not in result, result
        admin.shot("01-modal", page.locator(MODAL))
        page.locator("[data-asr-save]").click()
        page.locator(MODAL).wait_for(state="detached", timeout=15000)

        document = admin.api("GET", "/admin/asr-config").json()
        profile = next(m for m in document["models"] if m["label"] == LABEL)
        assert profile["connector"] == "audiocpp"
        assert profile["config"]["model"] == "vibevoice-asr"
        assert profile["config"]["model_diarizes"] is True
        assert profile["capabilities"]["diarization"] is True
        page.locator("[data-asr-row]", has_text=LABEL).first.locator("[data-asr-default]").check()
        page.wait_for_function(
            "() => [...document.querySelectorAll('[data-asr-status]')].some((el) => el.innerText.trim())",
            timeout=30000)
        assert admin.api("GET", "/admin/asr-config").json()["default"] == profile["id"]
        admin.shot("02-section", page.locator("#asr-models"))

        # Upload a recording; it goes to the default model.
        with open(tone_wav(), "rb") as handle:
            upload = admin.api("POST", "/upload", multipart={
                "file": {"name": "audiocpp-meeting.wav", "mimeType": "audio/wav", "buffer": handle.read()}})
        assert upload.status in (200, 202), upload.text()
        recording_id = upload.json()["id"]
        try:
            deadline = time.monotonic() + 180
            recording = {}
            while time.monotonic() < deadline:
                recording = admin.api("GET", f"/api/recordings/{recording_id}").json()
                if recording.get("status") == "FAILED" or "SPEAKER_0" in (recording.get("transcription") or ""):
                    break
                time.sleep(2)
            assert "SPEAKER_0" in (recording.get("transcription") or ""), \
                f"status {recording.get('status')}: {recording.get('error_message') or recording.get('transcription')}"
            segments = json.loads(recording["transcription"])
            assert [(s["speaker"], s["start_time"], s["end_time"]) for s in segments] == [
                ("SPEAKER_00", 0.0, 3.0), ("SPEAKER_01", 3.0, 6.0)]
            assert segments[0]["sentence"] == "Good morning, this is the first speaker."

            # What the server received: the model and a mono 16 kHz WAV.
            sent = fake.requests[-1]
            assert sent["model"] == b"vibevoice-asr"
            with wave.open(io.BytesIO(sent["file"]), "rb") as wav:
                assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (1, 16000, 2)

            # The Identify speakers dialog lists both speakers.
            page.goto(f"{BASE}/recordings/{recording_id}")
            page.wait_for_function(
                "() => !!document.querySelector(\"[data-tv-list=''] [data-segment-index='0']\")",
                timeout=60000)
            admin.problems.reset()
            button = page.locator(f"button[title='{en('buttons.identifySpeakers')}']:visible").first
            button.wait_for(state="visible", timeout=60000)
            button.click()
            page.locator(".spk-modal .spk-row").first.wait_for(state="visible", timeout=60000)
            labels = page.locator(".spk-modal .spk-row .spk-label").evaluate_all(
                "(els) => els.map((el) => el.getAttribute('title') || el.innerText.trim())")
            assert "SPEAKER_00" in labels and "SPEAKER_01" in labels, labels
            admin.shot("03-speakers")
            page.locator(".spk-modal .modal-close").click()
            check_common(page, admin.problems, f"/recordings/{recording_id}")
        finally:
            admin.api("DELETE", f"/recording/{recording_id}")
