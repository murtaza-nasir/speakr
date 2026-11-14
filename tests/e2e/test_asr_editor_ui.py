"""Browser test of the segment transcript editor, in a real browser.

The unit tests (static/js/modules/asr-editor/*.test.js) cover the editing
model; this test covers the editor a user sees: the transcript pane with its
speaker chips and times, typing, Enter to split and Backspace to merge,
speakers from the picker and from the number keys, undo and redo, find and
replace as one undo step, the question before unsaved changes are discarded,
the local draft, the phone layout, and saving. A second part loads a
transcript of 20,000 segments (about ten hours) and checks that only the
rows in view are in the DOM, that the end of the list can be reached, and
that an edit there is saved in reasonable time.

The recordings come from seed_recordings.py, which run_speakr.py runs when it
starts the instance; on an instance without them (the published image in
docker-publish.yml) the tests are skipped. Each test first puts the seeded
transcript back through the app's own save route, so the tests do not depend
on each other and a second run against the same instance works.

Every test fails on a JavaScript error, a same-origin response of 400 or
higher and template text left in the page (browser_checks.py).

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test. With
SPEAKR_E2E_SHOTS set, screenshots of the editor are saved in that directory.
"""

import json
import os
import re
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, en, login, open_page  # noqa: E402
from seed_recordings import TITLES, meeting_segments  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")

LONG_TITLE, MEETING_TITLE = TITLES
LARGE_COUNT = 20000
# Rows in the DOM whatever the length of the transcript: the rows in view
# plus a few above and below.
MAX_ROWS_IN_DOM = 80

ROW = ".asr-pane .asr-seg"
TEXT = ".asr-pane .asr-seg textarea"


class Editor:
    """The signed-in admin with one page, and the editor's parts on it."""

    def __init__(self, browser, viewport):
        self.context = browser.new_context(viewport=viewport, locale="en-US")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        # Every confirm dialog is recorded and answered with `agree`
        self.asked, self.agree = [], True
        self.page.on("dialog", self._dialog)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.problems.reset()
        listing = self.page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        self.ids = {r["title"]: r["id"] for r in items if r.get("title") in TITLES}

    def close(self):
        self.context.close()

    def _dialog(self, dialog):
        self.asked.append(dialog.message)
        if self.agree:
            dialog.accept()
        else:
            dialog.dismiss()

    # -- the stored transcript, through the app's own routes ---------------

    def csrf(self):
        return self.page.locator('meta[name="csrf-token"]').first.get_attribute("content")

    def store(self, recording_id, segments):
        """Save a transcript the way the editor does (needs an open app page for the token)."""
        if "/login" in self.page.url or self.page.url == "about:blank" or not self.page.url.startswith(BASE):
            self.page.goto(f"{BASE}/recordings/{recording_id}")
            self.page.wait_for_load_state("load")
        response = self.page.request.post(
            f"{BASE}/recording/{recording_id}/update_transcription",
            headers={"X-CSRFToken": self.csrf(), "Content-Type": "application/json"},
            data=json.dumps({"transcription": json.dumps(segments)}), timeout=120000)
        assert response.ok, f"could not store the transcript: HTTP {response.status}"

    def stored(self, recording_id):
        response = self.page.request.get(f"{BASE}/api/recordings/{recording_id}", timeout=120000)
        assert response.ok, f"could not read the recording: HTTP {response.status}"
        return json.loads(response.json()["transcription"])

    # -- the editor --------------------------------------------------------------

    def open(self, recording_id, settle_ms=1500):
        """Open the recording and its editor. Returns the seconds from the click to the first row."""
        path = f"/recordings/{recording_id}"
        open_page(self.page, self.problems, BASE, path, settle_ms=settle_ms)
        # A draft of an earlier test would be offered here
        self.page.evaluate("() => Object.keys(localStorage).filter(k => k.startsWith('speakr.asrDraft.')).forEach(k => localStorage.removeItem(k))")
        return self.click_edit()

    def click_edit(self):
        button = self.page.locator(f"button[title='{en('tooltips.editTranscript')}']:visible").first
        if button.count() == 0:
            # On a phone the transcript is a tab of the recording
            tab = re.compile(rf"^\s*{re.escape(en('transcription.title'))}\s*$")
            self.page.locator("button:visible").filter(has_text=tab).first.click()
        button.wait_for(state="visible", timeout=30000)
        started = time.monotonic()
        button.click()
        self.page.locator(ROW).first.wait_for(state="visible", timeout=60000)
        return time.monotonic() - started

    def rows(self):
        return self.page.locator(ROW)

    def texts(self):
        return self.page.eval_on_selector_all(TEXT, "els => els.map(e => e.value)")

    def speakers(self):
        return self.page.eval_on_selector_all(f"{ROW} .asr-seg__chip", "els => els.map(e => e.textContent.trim())")

    def clocks(self):
        return self.page.eval_on_selector_all(f"{ROW} .asr-seg__time", "els => els.map(e => e.textContent.trim())")

    def text_box(self, index):
        return self.page.locator(f"{ROW}[data-index='{index}'] textarea")

    def caret_in(self, index, position):
        """Put the text cursor of a row at a character position (None: the end)."""
        box = self.text_box(index)
        box.click()
        box.evaluate("(el, pos) => { const p = pos == null ? el.value.length : pos; el.setSelectionRange(p, p); }", position)

    def focused(self):
        """(row index, cursor position) of the text field that has focus, or None."""
        return self.page.evaluate("""() => {
            const el = document.activeElement;
            if (!el || el.tagName !== 'TEXTAREA' || !el.closest('.asr-seg')) return null;
            return [Number(el.closest('.asr-seg').dataset.index), el.selectionStart];
        }""")

    def select_row(self, index, modifiers=None):
        """Click a row beside its text: it is selected and keys go to the list."""
        self.page.locator(f"{ROW}[data-index='{index}'] .asr-seg__meta").click(position={"x": 2, "y": 2}, modifiers=modifiers or [])

    def selected(self):
        return self.page.eval_on_selector_all(f"{ROW}.asr-seg--selected", "els => els.map(e => Number(e.dataset.index))")

    def action(self, name):
        return self.page.locator(f"[data-asr-action='{name}']:visible").first

    def dirty(self):
        return self.page.locator("[data-asr-dirty]").count() == 1

    def save(self, timeout=60000):
        self.action("save").click()
        self.page.locator(".asr-editor-modal .pill-status--success").wait_for(state="visible", timeout=timeout)
        self.page.wait_for_function("() => !document.querySelector('[data-asr-dirty]')", timeout=timeout)

    def discard(self):
        """Close the editor, agreeing to discard when it asks."""
        self.agree = True
        self.page.locator(".asr-editor-modal .modal-close").click()
        self.page.locator(".asr-editor-modal").wait_for(state="detached", timeout=10000)

    def check(self, label):
        check_common(self.page, self.problems, label)

    def shot(self, name):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            self.page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def editor(browser):
    session = Editor(browser, {"width": 1400, "height": 900})
    if set(session.ids) != set(TITLES):
        session.close()
        pytest.skip("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    yield session
    session.store(session.ids[MEETING_TITLE], meeting_segments())
    session.store(session.ids[LONG_TITLE], meeting_segments())
    session.close()


@pytest.fixture
def meeting(editor):
    """The seeded meeting, as seeded, with its editor open."""
    recording_id = editor.ids[MEETING_TITLE]
    editor.store(recording_id, meeting_segments())
    editor.open(recording_id)
    return editor, recording_id


def test_the_editor_reads_like_the_transcript(meeting):
    editor, _ = meeting
    page = editor.page
    seeded = meeting_segments()
    assert editor.rows().count() == len(seeded)
    assert editor.texts() == [s["sentence"] for s in seeded]
    assert editor.speakers() == [s["speaker"] for s in seeded]
    assert editor.clocks()[:3] == ["0:00", "0:06", "0:12"]
    # The exact times are in the title of the compact time
    assert page.locator(f"{ROW}[data-index='1'] .asr-seg__time").get_attribute("title") == "0:06.000 – 0:11.500"
    assert page.locator(".asr-editor-modal table").count() == 0, "the editor still has a table"
    # Speakers keep the colours of the transcript view: one class per speaker
    colours = page.eval_on_selector_all(f"{ROW} .asr-seg__chip", "els => els.map(e => [...e.classList].find(c => c.startsWith('speaker-color-')))")
    assert colours[0] and colours[1] and colours[0] != colours[1] and colours[0] == colours[2]
    assert editor.action("undo").is_disabled() and editor.action("redo").is_disabled()
    assert not editor.dirty()
    assert en("asrEditor.noProblems") in page.locator(".asr-toolbar").inner_text()
    # The shortcut list opens from the toolbar and closes with Escape
    page.locator("[data-asr-help]").click()
    help_text = page.locator("[data-asr-popover='help']").inner_text()
    assert en("asrEditor.keys.split") in help_text and en("asrEditor.keys.undo") in help_text
    page.keyboard.press("Escape")
    page.locator("[data-asr-popover]").wait_for(state="detached", timeout=5000)
    editor.shot("asr-editor-test-desktop")
    editor.check("editor")
    editor.discard()


def test_edit_split_merge_speakers_undo_redo_and_save(meeting):
    editor, recording_id = meeting
    page = editor.page
    seeded = meeting_segments()
    expected = [dict(s) for s in seeded]

    # Type at the end of the first segment
    editor.caret_in(0, None)
    page.keyboard.type(" Welcome.")
    expected[0]["sentence"] += " Welcome."
    assert editor.texts()[0] == expected[0]["sentence"]
    assert editor.dirty()
    assert not editor.action("undo").is_disabled()

    # Enter at the cursor splits the segment there
    cut = len("Morning.")
    editor.caret_in(1, cut)
    page.keyboard.press("Enter")
    page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length === {len(seeded) + 1}")
    assert editor.texts()[1:3] == ["Morning.", "Shall we start with the budget?"]
    assert editor.focused() == [2, 0], "the cursor is not at the start of the new segment"
    assert editor.speakers()[2] == seeded[1]["speaker"]
    # 8 of 40 characters of 5.5 seconds after 0:06
    assert editor.clocks()[2] == "0:07"

    # Backspace at the very start merges it back into the previous segment
    page.keyboard.press("Backspace")
    page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length === {len(seeded)}")
    assert editor.texts()[1] == seeded[1]["sentence"]
    assert editor.focused() == [1, cut + 1], "the cursor is not where the two texts were joined"
    # Shift+Enter adds no line break
    page.keyboard.press("Shift+Enter")
    assert editor.texts()[1] == seeded[1]["sentence"]

    # A split that stays, further down
    cut = len("Yes,")
    editor.caret_in(2, cut)
    page.keyboard.press("Enter")
    page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length === {len(seeded) + 1}")
    first, second = dict(expected[2]), dict(expected[2])
    first["sentence"], second["sentence"] = "Yes,", "the first item is the budget for next year."
    expected[2:3] = [first, second]
    assert editor.texts() == [s["sentence"] for s in expected]
    page.keyboard.press("Escape")

    # Speaker from the picker: this recording's speakers are listed first
    page.locator(f"{ROW}[data-index='0'] .asr-seg__chip").click()
    picker = page.locator("[data-asr-popover='picker']")
    picker.wait_for(state="visible", timeout=5000)
    options = picker.locator("[data-asr-option]")
    assert [options.nth(i).get_attribute("data-asr-option") for i in range(2)] == ["SPEAKER_00", "SPEAKER_01"]
    picker.locator("[data-asr-option='SPEAKER_01']").click()
    picker.wait_for(state="detached", timeout=5000)
    expected[0]["speaker"] = "SPEAKER_01"
    assert editor.speakers()[0] == "SPEAKER_01"

    # A name typed into the picker
    page.locator(f"{ROW}[data-index='6'] .asr-seg__chip").click()
    picker.wait_for(state="visible", timeout=5000)
    page.keyboard.type("Dana Lee")
    page.keyboard.press("Enter")
    picker.wait_for(state="detached", timeout=5000)
    expected[6]["speaker"] = "Dana Lee"
    assert editor.speakers()[6] == "Dana Lee"

    # Number keys assign the recording's speakers to the selection: a range
    # chosen with Shift, then "1" (the first speaker in the recording is now SPEAKER_01)
    editor.select_row(8)
    editor.select_row(10, modifiers=["Shift"])
    assert editor.selected() == [8, 9, 10]
    assert en("asrEditor.selectedCount").replace("{{count}}", "3") in page.locator("[data-asr-selection]").inner_text()
    page.keyboard.press("1")
    for index in (8, 9, 10):
        expected[index]["speaker"] = "SPEAKER_01"
    assert editor.speakers() == [s["speaker"] for s in expected]

    # Undo and redo, by key and by button
    page.keyboard.press("Control+z")
    assert editor.speakers()[8:11] == [seeded[7]["speaker"], seeded[8]["speaker"], seeded[9]["speaker"]]
    page.keyboard.press("Control+Shift+z")
    assert editor.speakers() == [s["speaker"] for s in expected]
    editor.action("undo").click()
    editor.action("undo").click()
    assert editor.speakers()[6] == seeded[5]["speaker"]
    editor.action("redo").click()
    page.keyboard.press("Control+y")
    assert editor.speakers() == [s["speaker"] for s in expected]
    assert editor.texts() == [s["sentence"] for s in expected]
    editor.shot("asr-editor-test-desktop-editing")

    # Save without closing, reload, and the transcript is the edited one
    editor.save()
    stored = editor.stored(recording_id)
    assert [s["sentence"] for s in stored] == [s["sentence"] for s in expected]
    assert [s["speaker"] for s in stored] == [s["speaker"] for s in expected]
    assert stored[2]["start_time"] == 12.0 and stored[3]["end_time"] == 17.5
    assert stored[2]["end_time"] == stored[3]["start_time"]
    assert 12.0 < stored[3]["start_time"] < 13.0, "the split time is not the share of the characters"
    starts = [s["start_time"] for s in stored]
    assert starts == sorted(starts)
    editor.check("editor after save")

    editor.open(recording_id)
    assert editor.texts() == [s["sentence"] for s in expected]
    assert editor.speakers() == [s["speaker"] for s in expected]
    assert not editor.dirty()
    # The transcript view behind the editor shows the saved text as well
    page.locator(".asr-editor-modal .modal-close").click()
    page.locator(".asr-editor-modal").wait_for(state="detached", timeout=5000)
    assert "Welcome." in page.inner_text("body")
    editor.check("recording after save")


def test_merge_delete_and_times_from_the_selection(meeting):
    editor, recording_id = meeting
    page = editor.page
    seeded = meeting_segments()

    # M merges the selection into its first segment
    editor.select_row(3)
    editor.select_row(4, modifiers=["Control"])
    assert editor.selected() == [3, 4]
    page.keyboard.press("m")
    page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length === {len(seeded) - 1}")
    assert editor.texts()[3] == f"{seeded[3]['sentence']} {seeded[4]['sentence']}"
    assert editor.speakers()[3] == seeded[3]["speaker"]

    # Delete removes the selection
    editor.select_row(0)
    page.keyboard.press("Delete")
    page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length === {len(seeded) - 2}")
    assert editor.texts()[0] == seeded[1]["sentence"]

    # Exact times can be typed: the segment menu, Edit times
    page.locator(f"{ROW}[data-index='1'] .asr-seg__menu").click(force=True)
    page.locator("[data-asr-menu='times']").click()
    start = page.locator("[data-asr-time='start']")
    start.wait_for(state="visible", timeout=5000)
    assert start.input_value() == "0:12.000"
    start.fill("0:13.25")
    editor.action("apply-times").click()
    page.locator("[data-asr-popover]").wait_for(state="detached", timeout=5000)
    assert page.locator(f"{ROW}[data-index='1'] .asr-seg__time").get_attribute("title").startswith("0:13.250")

    # An end before the start is a problem, counted in the toolbar and marked on the row
    page.keyboard.press("t")
    end = page.locator("[data-asr-time='end']")
    end.wait_for(state="visible", timeout=5000)
    end.fill("0:05")
    end.press("Enter")
    page.locator("[data-asr-popover]").wait_for(state="detached", timeout=5000)
    problems = editor.action("next-problem")
    assert en("asrEditor.problemCount").replace("{{count}}", "1") in problems.inner_text()
    assert page.locator(f"{ROW}[data-index='1'] .asr-seg__problem").count() == 1
    editor.select_row(5)
    problems.click()
    assert editor.selected() == [1]

    # Four undos bring back the two times, the deleted and the merged segment
    for _ in range(4):
        page.keyboard.press("Control+z")
    assert editor.texts() == [s["sentence"] for s in seeded]
    assert page.locator("[data-asr-action='next-problem']").count() == 0
    assert not editor.dirty()
    assert editor.stored(recording_id) == seeded
    editor.check("editor, selection actions")
    editor.discard()


def test_find_and_replace_is_one_undo_step(meeting):
    editor, _ = meeting
    page = editor.page
    seeded = [s["sentence"] for s in meeting_segments()]
    editor.action("find").click()
    page.locator("[data-find-input]").fill("the")
    page.locator(".find-replace-bar__replace").fill("THE")
    count = sum(s.lower().count("the") for s in seeded)
    assert f"{count}" in page.locator(".find-replace-bar__count").inner_text()
    page.get_by_role("button", name=en("findReplace.replaceAll"), exact=True).click()
    changed = editor.texts()
    assert changed != seeded
    assert sum(s.count("THE") for s in changed) == count
    assert [s.lower() for s in changed] == [s.lower() for s in seeded]
    # One undo step for all of it
    editor.action("undo").click()
    assert editor.texts() == seeded
    assert editor.action("undo").is_disabled()
    editor.action("redo").click()
    assert editor.texts() == changed
    # Enter in the find field goes to a match and shows it selected in its text
    page.locator("[data-find-input]").fill("budget")
    page.locator("[data-find-input]").press("Enter")
    page.wait_for_function("() => document.activeElement && document.activeElement.tagName === 'TEXTAREA'")
    picked = page.evaluate("() => { const el = document.activeElement; return el.value.slice(el.selectionStart, el.selectionEnd); }")
    assert picked == "budget"
    # Enter there goes on to the next match and does not split the segment
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    assert editor.rows().count() == len(seeded)
    editor.check("editor, find and replace")
    editor.discard()


def test_closing_asks_before_unsaved_changes_are_discarded(meeting):
    editor, recording_id = meeting
    page = editor.page
    editor.caret_in(0, 0)
    page.keyboard.type("DRAFT ")
    assert editor.dirty()

    # Saying no keeps the editor and the edit
    editor.asked.clear()
    editor.agree = False
    page.locator(".asr-editor-footer").get_by_role("button", name=en("common.cancel"), exact=True).click()
    page.wait_for_timeout(300)
    editor.agree = True
    assert editor.asked == [en("asrEditor.discardConfirm")]
    assert editor.texts()[0].startswith("DRAFT ")

    # The edit is kept as a local draft: after a reload it is offered and restored
    page.wait_for_timeout(1300)
    open_page(page, editor.problems, BASE, f"/recordings/{recording_id}", settle_ms=1500)
    editor.click_edit()
    offer = page.locator("[data-asr-draft]")
    offer.wait_for(state="visible", timeout=5000)
    assert not editor.texts()[0].startswith("DRAFT ")
    editor.action("restore-draft").click()
    offer.wait_for(state="detached", timeout=5000)
    assert editor.texts()[0].startswith("DRAFT ")
    assert editor.dirty()

    # Saying yes closes it; nothing was saved and the draft is gone
    editor.discard()
    assert editor.stored(recording_id) == meeting_segments()
    editor.click_edit()
    assert page.locator("[data-asr-draft]").count() == 0
    assert not editor.texts()[0].startswith("DRAFT ")
    editor.check("editor, discard and draft")
    editor.discard()


def test_playback_keys_and_the_playing_segment(meeting):
    editor, _ = meeting
    page = editor.page
    audio = "document.querySelector('[data-asr-editor-audio]')"
    page.wait_for_function(f"{audio} && {audio}.readyState >= 1", timeout=15000)

    # A click on a segment's time plays from its start; the row is marked
    page.locator(f"{ROW}[data-index='3'] .asr-seg__time").click()
    page.wait_for_function(f"!{audio}.paused && {audio}.currentTime >= 18", timeout=10000)
    page.locator(f"{ROW}[data-index='3'].asr-seg--active").wait_for(state="visible", timeout=5000)
    # Space pauses when the focus is not in a text field
    page.keyboard.press("Space")
    page.wait_for_function(f"{audio}.paused", timeout=5000)

    # "[" sets the selected segment's start to the playhead
    page.evaluate(f"{audio}.currentTime = 20")
    editor.select_row(3)
    page.keyboard.press("[")
    assert page.locator(f"{ROW}[data-index='3'] .asr-seg__time").get_attribute("title").startswith("0:20.000")
    # Alt+Right moves it by 0.1 s
    page.keyboard.press("Alt+ArrowRight")
    assert page.locator(f"{ROW}[data-index='3'] .asr-seg__time").get_attribute("title").startswith("0:20.100")
    # S splits at the playhead
    page.evaluate(f"{audio}.currentTime = 22")
    page.keyboard.press("s")
    page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length === {len(meeting_segments()) + 1}")
    assert page.locator(f"{ROW}[data-index='4'] .asr-seg__time").get_attribute("title").startswith("0:22.000")
    page.keyboard.press("Escape")
    # L loops the selected segment
    editor.select_row(4)
    page.keyboard.press("l")
    assert editor.action("loop").get_attribute("aria-pressed") == "true"
    page.wait_for_function(f"!{audio}.paused", timeout=5000)
    page.keyboard.press("l")
    assert editor.action("loop").get_attribute("aria-pressed") == "false"
    page.keyboard.press("Space")
    editor.check("editor, playback")
    editor.discard()


def test_a_voice_match_is_accepted_and_saved_like_in_the_speaker_dialog(editor):
    """The suggestion comes from /speakers/suggestions/<id>, the source of the Identify
    Speakers dialog. This instance has no voice embeddings, so the answer of that route
    is supplied here; accepting and saving go through the real server."""
    page = editor.page
    recording_id = editor.ids[MEETING_TITLE]
    seeded = meeting_segments()
    editor.store(recording_id, seeded)
    answer = {"success": True, "recording_id": recording_id,
              "suggestions": {"SPEAKER_00": [{"speaker_id": 0, "name": "Dana Lee", "similarity": 87.2, "confidence": 0.9}]}}
    pattern = "**/speakers/suggestions/*"
    page.route(pattern, lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(answer)))
    try:
        editor.open(recording_id)
        # Offered once, where the unnamed speaker first talks, and marked on each of its chips
        offer = page.locator("[data-asr-suggestion]")
        offer.first.wait_for(state="visible", timeout=10000)
        assert offer.count() == 1
        assert en("asrEditor.suggestion").replace("{{name}}", "Dana Lee").replace("{{percent}}", "87%") in offer.inner_text()
        assert page.locator(f"{ROW}[data-index='0'] [data-asr-suggestion]").count() == 1
        assert page.locator(f"{ROW} .asr-seg__chip-match").count() == sum(1 for s in seeded if s["speaker"] == "SPEAKER_00")

        # The suggestion plays some of that speaker's speech
        audio = "document.querySelector('[data-asr-editor-audio]')"
        page.wait_for_function(f"{audio} && {audio}.readyState >= 1", timeout=15000)
        offer.locator(".asr-suggest__name").click()
        page.wait_for_function(f"!{audio}.paused", timeout=10000)
        page.evaluate(f"{audio}.pause()")

        # Accept renames the speaker in the whole recording, as one undo step
        editor.action("accept-suggestion").click()
        named = ["Dana Lee" if s["speaker"] == "SPEAKER_00" else s["speaker"] for s in seeded]
        assert editor.speakers() == named
        assert offer.count() == 0
        page.keyboard.press("Control+z")
        assert editor.speakers() == [s["speaker"] for s in seeded]
        assert editor.action("undo").is_disabled()
        assert offer.count() == 1
        editor.action("redo").click()
        assert editor.speakers() == named

        # Saving records the match the way the speaker dialog does: the
        # label and its name go to update_transcript, which renames the label
        with page.expect_request(lambda r: r.method == "POST" and r.url.endswith(f"/recording/{recording_id}/update_transcript")) as sent:
            editor.save()
        body = sent.value.post_data_json
        assert body["speaker_map"] == {"SPEAKER_00": {"name": "Dana Lee", "isMe": False}}
        assert [s["speaker"] for s in body["transcript_data"]] == [s["speaker"] for s in seeded]
        assert body["regenerate_summary"] is False
        stored = editor.stored(recording_id)
        assert [s["speaker"] for s in stored] == named
        assert [s["sentence"] for s in stored] == [s["sentence"] for s in seeded]
        assert editor.speakers() == named
        assert not editor.dirty()
        # A later save of the same session has no match left to record
        editor.caret_in(1, None)
        page.keyboard.type(" Yes.")
        with page.expect_request(lambda r: r.method == "POST" and "/update_transcri" in r.url) as sent:
            editor.save()
        assert sent.value.url.endswith("/update_transcription")
        editor.check("editor, voice match")
        editor.discard()
    finally:
        page.unroute(pattern)
        # The save created the saved speaker; this instance is left as it was
        saved = page.request.get(f"{BASE}/speakers")
        for speaker in (saved.json() if saved.ok else []):
            if speaker.get("name") == "Dana Lee":
                page.request.delete(f"{BASE}/speakers/{speaker['id']}", headers={"X-CSRFToken": editor.csrf()})


def test_phone_width(browser):
    phone = Editor(browser, {"width": 390, "height": 800})
    try:
        if set(phone.ids) != set(TITLES):
            pytest.skip("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
        recording_id = phone.ids[MEETING_TITLE]
        phone.store(recording_id, meeting_segments())
        phone.open(recording_id)
        page = phone.page
        seeded = meeting_segments()
        assert phone.texts()[:3] == [s["sentence"] for s in seeded[:3]]
        # Nothing is cut off sideways, and the row menu is there without hovering
        widths = page.evaluate("() => { const l = document.querySelector('.asr-list'); return [l.scrollWidth, l.clientWidth, document.documentElement.scrollWidth, window.innerWidth]; }")
        assert widths[0] <= widths[1] and widths[2] <= widths[3], f"the editor is wider than the phone: {widths}"
        menu = page.locator(f"{ROW}[data-index='1'] .asr-seg__menu")
        assert menu.evaluate("el => getComputedStyle(el).opacity") == "1"
        box = menu.bounding_box()
        assert box["width"] >= 39.5 and box["height"] >= 39.5, f"the row menu is a small touch target: {box}"
        phone.shot("asr-editor-test-phone")

        # Split at the cursor and merge with previous, from the menu
        phone.caret_in(1, len("Morning."))
        menu.click()
        sheet = page.locator("[data-asr-popover='menu']")
        sheet.wait_for(state="visible", timeout=5000)
        item = sheet.locator("[data-asr-menu='split']")
        assert item.bounding_box()["height"] >= 43.5
        phone.shot("asr-editor-test-phone-menu")
        item.click()
        # (On a phone not all twelve rows are in the DOM, so the rows are not counted here.)
        third = f"document.querySelector(\"{ROW}[data-index='2'] textarea\").value"
        page.wait_for_function(f"() => {third} === 'Shall we start with the budget?'")
        assert phone.texts()[1:3] == ["Morning.", "Shall we start with the budget?"]
        page.locator(f"{ROW}[data-index='2'] .asr-seg__menu").click()
        page.locator("[data-asr-menu='merge-prev']").click()
        page.wait_for_function(f"() => {third} === {json.dumps(seeded[2]['sentence'])}")
        assert phone.texts()[1] == seeded[1]["sentence"]
        check_common(page, phone.problems, "editor on a phone")
        phone.discard()
    finally:
        phone.close()


def test_a_ten_hour_transcript(editor):
    """20,000 segments: a bounded DOM, the end within reach, an edit and a save in reasonable time."""
    page = editor.page
    recording_id = editor.ids[LONG_TITLE]
    words = "this is a sentence of a long meeting with a few more words in it so that some rows wrap".split()
    segments = [
        {"speaker": f"SPEAKER_0{i % 4}", "sentence": f"Segment {i}: " + " ".join(words[: 6 + (i * 7) % 14]) + ".",
         "start_time": round(i * 1.8, 3), "end_time": round(i * 1.8 + 1.7, 3)}
        for i in range(LARGE_COUNT)
    ]
    editor.store(recording_id, segments)
    try:
        opened_in = editor.open(recording_id, settle_ms=4000)
        assert opened_in < 20, f"the editor took {opened_in:.1f} s to open"
        in_dom = editor.rows().count()
        assert 5 <= in_dom <= MAX_ROWS_IN_DOM, f"{in_dom} rows in the DOM"
        assert editor.texts()[0] == segments[0]["sentence"]

        # Typing in a row touches no other row: the other rows are the same DOM nodes with the same text
        page.evaluate(f"() => document.querySelectorAll('{ROW}').forEach((el, i) => {{ el.dataset.mark = String(i); }})")
        editor.caret_in(2, None)
        started = time.monotonic()
        page.keyboard.type(" typed in the third row")
        typed_in = time.monotonic() - started
        assert typed_in < 10, f"typing 23 characters took {typed_in:.1f} s"
        marks = page.eval_on_selector_all(ROW, "els => els.map(e => e.dataset.mark)")
        assert marks == [str(i) for i in range(len(marks))], "rows were replaced while typing"
        page.keyboard.press("Escape")

        # The end of the list
        started = time.monotonic()
        page.evaluate("() => { const l = document.querySelector('.asr-list'); l.scrollTop = l.scrollHeight; }")
        page.wait_for_function(f"() => !!document.querySelector(\"{ROW}[data-index='{LARGE_COUNT - 1}']\")", timeout=20000)
        # Rows are measured as they appear; the list settles at its real end
        page.wait_for_timeout(500)
        page.evaluate("() => { const l = document.querySelector('.asr-list'); l.scrollTop = l.scrollHeight; }")
        page.wait_for_timeout(500)
        scrolled_in = time.monotonic() - started
        last = page.locator(f"{ROW}[data-index='{LARGE_COUNT - 1}']")
        assert last.is_visible()
        at_end = page.evaluate("() => { const l = document.querySelector('.asr-list'); return l.scrollHeight - l.scrollTop - l.clientHeight; }")
        assert at_end <= 2, f"the list does not reach its end: {at_end}px left"
        in_dom = editor.rows().count()
        assert in_dom <= MAX_ROWS_IN_DOM, f"{in_dom} rows in the DOM at the end of the list"
        assert editor.texts()[-1] == segments[-1]["sentence"]
        assert scrolled_in < 20, f"reaching the end took {scrolled_in:.1f} s"

        # One edit there, and a save
        started = time.monotonic()
        editor.caret_in(LARGE_COUNT - 1, None)
        page.keyboard.type(" THE END")
        editor.save(timeout=120000)
        saved_in = time.monotonic() - started
        assert saved_in < 60, f"an edit and a save took {saved_in:.1f} s"
        print(f"\n[asr editor, {LARGE_COUNT} segments] open {opened_in:.2f} s, type 23 characters {typed_in:.2f} s, "
              f"reach the end {scrolled_in:.2f} s, edit and save {saved_in:.2f} s, rows in the DOM {in_dom}")

        stored = editor.stored(recording_id)
        assert len(stored) == LARGE_COUNT
        assert stored[-1]["sentence"] == segments[-1]["sentence"] + " THE END"
        assert stored[2]["sentence"] == segments[2]["sentence"] + " typed in the third row"
        assert stored[100] == segments[100]
        editor.shot("asr-editor-test-20000-segments-end")
        editor.check("editor, 20,000 segments")
    finally:
        editor.store(recording_id, meeting_segments())
