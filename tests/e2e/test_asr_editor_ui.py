"""Browser test of the segment transcript editor, in a real browser.

The unit tests (static/js/modules/asr-editor/*.test.js) cover the editing
model; this test covers the editor a user sees: the transcript pane with its
speaker chips and times, typing, Enter to split and Backspace to merge,
speakers from the picker and from the number keys, undo and redo, find and
replace as one undo step, the question before unsaved changes are discarded,
the local draft, the phone layout, and saving. The timeline above the pane
(a canvas: waveform, one lane per speaker) is driven with the mouse: a click
seeks or selects, an edge is dragged to change a time, a block to another
lane to change its speaker, empty lane space to add a segment, each as one
undo step. The lane headers beside it are DOM: the name of a lane opens the
speaker picker for the whole speaker (rename or merge), a button in the header
cell of the overview adds a speaker, and a lane without segments has a button
that removes its speaker. A second part loads a transcript of 20,000 segments (about ten
hours) and checks that only the rows in view are in the DOM, that the end of
the list can be reached, that an edit there is saved in reasonable time, and
that the timeline redraws well inside a frame.

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

from browser_checks import PageProblems, check_common, en, login, open_page, precondition_unmet  # noqa: E402
from seed_recordings import SENTENCES, TITLES, meeting_segments  # noqa: E402
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
CANVAS = "[data-asr-timeline-canvas]"
DIALOG = "[data-app-dialog]"
# The timeline's handle for tests and the console (static/js/modules/asr-editor/timeline.js)
TL = f"document.querySelector('{CANVAS}').__tl"
# A full redraw of the timeline must fit a frame with room to spare. The
# machine of the CI job is slow and shared, so the bound is a frame (16 ms),
# not the 4 ms the timeline is built for; the measured time is printed.
FRAME_MS = 16


class Editor:
    """The signed-in admin with one page, and the editor's parts on it."""

    def __init__(self, browser, viewport):
        self.context = browser.new_context(viewport=viewport, locale="en-US")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        # The app asks in its own dialogs (static/js/app-dialog.js): a dialog
        # of the browser is recorded here and fails the test's check
        self.native = []
        self.page.on("dialog", self._dialog)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.problems.reset()
        listing = self.page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        self.ids = {r["title"]: r["id"] for r in items if r.get("title") in TITLES}

    def close(self):
        self.context.close()

    def _dialog(self, dialog):
        self.native.append(f"{dialog.message} (during {os.environ.get('PYTEST_CURRENT_TEST', '?')})")
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
        more = self.page.locator("[data-phone-more]")
        if more.count():
            # On a phone the editor is opened from the sheet behind More
            more.click()
            button = self.page.locator('[data-phone-action="edit-transcript"]')
        button.wait_for(state="visible", timeout=30000)
        started = time.monotonic()
        button.click()
        self.page.locator(ROW).first.wait_for(state="visible", timeout=60000)
        seconds = time.monotonic() - started
        self.opened()
        return seconds

    def opened(self):
        """Wait until the dialog has finished opening. It opens with a short scale and
        slide animation (a sheet that slides up on a phone); while it runs, nothing in it
        is where it will be: a point computed on the timeline then misses an edge by a
        few pixels, and a measured position is off by the rest of the slide."""
        self.page.wait_for_function(
            "() => { const m = document.querySelector('.asr-editor-modal'); if (!m) return false; "
            "const o = m.closest('.modal-overlay'); "
            "return [...m.getAnimations(), ...(o ? o.getAnimations() : [])].every(a => a.playState === 'finished'); }",
            timeout=10000)

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

    def question(self):
        """The app's own dialog: the question it asks before something is discarded or deleted."""
        return self.page.locator(DIALOG)

    def discard(self):
        """Close the editor, agreeing to discard when it asks."""
        self.page.locator(".asr-editor-modal .modal-close").click()
        self.page.wait_for_function(
            f"() => !document.querySelector('.asr-editor-modal') || !!document.querySelector('{DIALOG}')", timeout=10000)
        if self.question().count():
            self.question().locator("[data-app-dialog-confirm]").click()
        self.page.locator(".asr-editor-modal").wait_for(state="detached", timeout=10000)

    def check(self, label):
        check_common(self.page, self.problems, label)
        assert not self.native, f"{label}: a dialog of the browser was shown: {self.native}"

    # -- the timeline ------------------------------------------------------------

    def tl(self, expression):
        """A value of the timeline's handle, e.g. tl('view.pps')."""
        return self.page.evaluate(f"() => {TL}.{expression}")

    def tl_ready(self, waveform=True):
        self.page.locator(CANVAS).wait_for(state="visible", timeout=10000)
        self.page.wait_for_function(f"() => {TL} && {TL}.width > 0 && {TL}.stats.frames > 0", timeout=10000)
        self.opened()
        if waveform:
            self.page.wait_for_function(f"() => {TL}.waveform === 'ready'", timeout=30000)

    def tl_point(self, seconds, lane=None, dx=0):
        """Page coordinates of a time on the timeline: in a lane, or on the ruler (lane None)."""
        y = "h.laneY(lane)" if lane is not None else "10"
        return self.page.evaluate(
            f"([t, lane, dx]) => {{ const c = document.querySelector('{CANVAS}'); const r = c.getBoundingClientRect(); "
            f"const h = c.__tl; return [r.left + h.xOf(t) + dx, r.top + {y}]; }}", [seconds, lane, dx])

    def tl_drag(self, start, end, keys=(), steps=8, before_release=None):
        """Drag with the mouse from one page point to another, with modifier keys held while moving."""
        mouse = self.page.mouse
        mouse.move(*start)
        mouse.down()
        for key in keys:
            self.page.keyboard.down(key)
        mouse.move(*end, steps=steps)
        if before_release:
            before_release()
        mouse.up()
        for key in keys:
            self.page.keyboard.up(key)
        self.page.wait_for_timeout(150)

    def tl_lanes(self, count):
        """The lanes, once the timeline has drawn the model's `count` speakers (it rebuilds them with its next frame)."""
        self.page.wait_for_function(f"(n) => {TL}.lanes.length === n", arg=count, timeout=5000)
        return self.tl("lanes")

    def lane_offers(self, words):
        """Wait until the lane headers offer exactly these voice-match buttons (they follow the model with the next frame)."""
        try:
            self.page.wait_for_function(
                "(words) => JSON.stringify([...document.querySelectorAll(\"[data-asr-action='timeline-accept']\")].map(e => e.textContent.trim())) === JSON.stringify(words)",
                arg=words, timeout=5000)
        except Exception:
            found = self.page.eval_on_selector_all("[data-asr-action='timeline-accept']", "els => els.map(e => e.textContent.trim())")
            raise AssertionError(f"the lane headers offer {found}, not {words}")

    def tl_segments(self):
        return self.tl("segments")

    def lanes_are(self, expected):
        """Wait until the timeline has exactly these lanes, as (name, segments) in order, canvas and headers alike."""
        want = [[name, count] for name, count in expected]
        try:
            self.page.wait_for_function(
                f"(want) => JSON.stringify({TL}.lanes.map(l => [l.name, l.count])) === JSON.stringify(want) && "
                "JSON.stringify([...document.querySelectorAll('.asr-tl__lane .asr-tl__name-text')].map(e => e.textContent.trim())) === JSON.stringify(want.map(l => l[0]))",
                arg=want, timeout=5000)
        except Exception:
            heads = self.page.eval_on_selector_all(".asr-tl__lane .asr-tl__name-text", "els => els.map(e => e.textContent.trim())")
            raise AssertionError(f"the lanes are {self.tl('lanes')} with the headers {heads}, not {want}")

    def speakers_are(self, expected):
        """Wait until the rows show these speakers (the rows follow the model with the next render)."""
        try:
            self.page.wait_for_function(
                f"(want) => JSON.stringify([...document.querySelectorAll(\"{ROW} .asr-seg__chip\")].map(e => e.textContent.trim())) === JSON.stringify(want)",
                arg=list(expected), timeout=5000)
        except Exception:
            raise AssertionError(f"the rows show {self.speakers()}, not {list(expected)}")

    def undo_depth(self, depth):
        self.page.wait_for_function(f"(n) => {TL}.undoDepth === n", arg=depth, timeout=5000)

    def lane_name(self, index):
        """The name button of a lane header."""
        return self.page.locator(f".asr-tl__lane[data-asr-lane='{index}'] [data-asr-action='lane-rename']")

    def lane_picker(self, index, name):
        """Open the speaker picker of a whole speaker from its lane header. Returns the picker."""
        self.lane_name(index).click()
        picker = self.page.locator("[data-asr-popover='picker'][data-asr-picker-scope='speaker']")
        picker.wait_for(state="visible", timeout=5000)
        assert picker.locator("[data-asr-picker-heading]").inner_text().strip() == en("asrEditor.renameSpeaker").replace("{{name}}", name)
        return picker

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
        precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
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
    """The question is the app's own dialog, over the editor: Cancel and Esc keep the
    editor and the edit, Discard closes it."""
    editor, recording_id = meeting
    page = editor.page
    editor.caret_in(0, 0)
    page.keyboard.type("DRAFT ")
    assert editor.dirty()
    question = editor.question()
    cancel = question.locator("[data-app-dialog-cancel]")
    discard = question.locator("[data-app-dialog-confirm]")
    focused = "() => { const a = document.activeElement; return a.hasAttribute('data-app-dialog-confirm') ? 'confirm' : a.hasAttribute('data-app-dialog-cancel') ? 'cancel' : a.tagName; }"

    # Cancel in the footer asks; the dialog names the action on a danger button
    footer_cancel = page.locator(".asr-editor-footer").get_by_role("button", name=en("common.cancel"), exact=True)
    footer_cancel.click()
    question.wait_for(state="visible", timeout=5000)
    assert question.locator(".app-dialog__message").inner_text() == en("asrEditor.discardConfirm")
    assert discard.inner_text().strip() == en("common.discard") and "btn--danger" in discard.get_attribute("class")
    assert cancel.inner_text().strip() == en("common.cancel")
    assert question.locator("[role='alertdialog'][aria-modal='true']").count() == 1
    # It lies over the editor: the middle of its panel is the dialog, not the editor under it
    assert page.evaluate("""() => { const r = document.querySelector('.app-dialog__panel').getBoundingClientRect();
        const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !!top.closest('[data-app-dialog]'); }""")
    assert page.evaluate("() => Number(getComputedStyle(document.querySelector('[data-app-dialog]')).zIndex) > Number(getComputedStyle(document.querySelector('.asr-editor-modal').closest('.modal-overlay')).zIndex)")
    page.wait_for_timeout(300)      # the dialog fades in
    editor.shot("asr-editor-test-discard-dialog")
    # The focus is on the action and Tab stays in the dialog
    assert page.evaluate(focused) == "confirm"
    page.keyboard.press("Tab")
    assert page.evaluate(focused) == "cancel"
    page.keyboard.press("Tab")
    assert page.evaluate(focused) == "confirm"
    page.keyboard.press("Shift+Tab")
    assert page.evaluate(focused) == "cancel"
    # Keys do not reach the editor under it (Ctrl+Z would undo the edit)
    page.keyboard.press("Control+z")
    cancel.click()
    question.wait_for(state="detached", timeout=5000)
    assert page.locator(".asr-editor-modal").count() == 1
    assert editor.texts()[0].startswith("DRAFT ") and editor.dirty()
    # The focus is back where it was
    assert page.evaluate("() => document.activeElement.textContent.trim()") == en("common.cancel")

    # Esc cancels as well, and closes only the question
    page.locator(".asr-editor-modal .modal-close").click()
    question.wait_for(state="visible", timeout=5000)
    page.keyboard.press("Escape")
    question.wait_for(state="detached", timeout=5000)
    assert page.locator(".asr-editor-modal").count() == 1 and editor.texts()[0].startswith("DRAFT ")
    # So does a click beside the panel
    page.locator(".asr-editor-modal .modal-close").click()
    question.wait_for(state="visible", timeout=5000)
    page.mouse.click(5, 5)
    question.wait_for(state="detached", timeout=5000)
    assert page.locator(".asr-editor-modal").count() == 1 and editor.texts()[0].startswith("DRAFT ")

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

    # Discard closes it (Enter presses the focused action); nothing was saved and the draft is gone
    page.locator(".asr-editor-modal .modal-close").click()
    question.wait_for(state="visible", timeout=5000)
    page.keyboard.press("Enter")
    page.locator(".asr-editor-modal").wait_for(state="detached", timeout=10000)
    assert question.count() == 0
    assert editor.stored(recording_id) == meeting_segments()
    editor.click_edit()
    assert page.locator("[data-asr-draft]").count() == 0
    assert not editor.texts()[0].startswith("DRAFT ")
    editor.check("editor, discard and draft")
    editor.discard()


def test_the_summary_is_regenerated_after_a_save_when_asked(meeting):
    """The footer has the checkbox of the Identify Speakers dialog. The save that asks for
    the summary is intercepted here: it reaches the server without that request (this
    instance has no text model to ask), and the answer says a summary was queued."""
    editor, recording_id = meeting
    page = editor.page
    label = page.locator("[data-asr-regenerate-summary]")
    box = label.locator("input[type=checkbox]")
    assert label.inner_text().strip() == en("asrEditor.updateSummary")
    assert not box.is_checked(), "the checkbox is on when the editor opens"
    footer = page.locator(".asr-editor-footer").bounding_box()
    place = label.bounding_box()
    assert place["x"] >= footer["x"] and place["x"] + place["width"] <= footer["x"] + footer["width"]

    sent = []
    statuses = ["SUMMARIZING", "COMPLETED"]
    polled = []

    def save(route):
        body = route.request.post_data_json
        sent.append(body)
        response = route.fetch(post_data=json.dumps({**body, "regenerate_summary": False}))
        answer = response.json()
        answer["summary_queued"] = bool(body.get("regenerate_summary"))
        route.fulfill(status=response.status, content_type="application/json", body=json.dumps(answer))

    def status(route):
        polled.append(1)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"status": statuses.pop(0) if len(statuses) > 1 else statuses[0]}))

    save_url = f"**/recording/{recording_id}/update_transcript"
    status_url = f"**/recording/{recording_id}/status"
    page.route(save_url, save)
    page.route(status_url, status)
    any_save = lambda r: r.method == "POST" and "/update_transcri" in r.url      # noqa: E731
    started = page.locator("#toastContainer .toast-message").filter(has_text=en("help.summaryRegenerationStarted"))
    try:
        # On, but nothing was changed: nothing is asked for
        box.check()
        with page.expect_request(any_save) as plain:
            editor.action("save").click()
        assert plain.value.url.endswith("/update_transcription") and sent == []
        assert started.count() == 0
        page.wait_for_function("() => !document.querySelector(\"[data-asr-action='save']\").disabled", timeout=10000)

        # Off: an edit is saved by the plain route and the summary is left alone
        box.uncheck()
        editor.caret_in(0, 0)
        page.keyboard.type("One ")
        with page.expect_request(any_save) as plain:
            editor.save()
        assert plain.value.url.endswith("/update_transcription") and sent == []
        assert started.count() == 0

        # On, with a change, Save: the route of the speaker dialog, asking for the summary
        box.check()
        editor.caret_in(1, 0)
        page.keyboard.type("Two ")
        with page.expect_request(any_save) as asked:
            editor.save()
        assert asked.value.url.endswith("/update_transcript")
        assert sent[-1]["regenerate_summary"] is True and sent[-1]["speaker_map"] == {}
        assert [s["sentence"] for s in sent[-1]["transcript_data"]][:2] == ["One " + SENTENCES[0], "Two " + SENTENCES[1]]
        started.wait_for(state="visible", timeout=5000)
        editor.shot("asr-editor-test-regenerate-summary")
        # The editor stays open while the summary is waited for, and afterwards
        done = page.locator("#toastContainer .toast-message").filter(has_text=en("help.summaryUpdated"))
        done.wait_for(state="visible", timeout=20000)
        assert len(polled) == 2
        assert page.locator(".asr-editor-modal").count() == 1 and box.is_checked()
        assert editor.texts()[:2] == ["One " + SENTENCES[0], "Two " + SENTENCES[1]] and not editor.dirty()
        assert [s["sentence"] for s in editor.stored(recording_id)][:2] == ["One " + SENTENCES[0], "Two " + SENTENCES[1]]

        # Save and close asks for it as well
        statuses[:] = ["COMPLETED"]
        editor.caret_in(2, 0)
        page.keyboard.type("Three ")
        with page.expect_request(any_save) as asked:
            editor.action("save-close").click()
        assert asked.value.url.endswith("/update_transcript") and sent[-1]["regenerate_summary"] is True
        page.locator(".asr-editor-modal").wait_for(state="detached", timeout=10000)
        assert len(sent) == 2
        done.first.wait_for(state="visible", timeout=20000)
        assert editor.stored(recording_id)[2]["sentence"] == "Three " + SENTENCES[2]

        # Off again when the editor is opened the next time
        editor.click_edit()
        assert not box.is_checked()
        editor.check("editor, summary after saving")
        editor.discard()
    finally:
        page.unroute(save_url)
        page.unroute(status_url)


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

        # The lane header of the timeline offers the same match the same way:
        # the name and percent, which play the speaker, and a button with a
        # word on it. Nothing in a lane header is a button with only an icon.
        editor.tl_ready(waveform=False)
        lane_offer = page.locator("[data-asr-lane-suggestion]")
        assert lane_offer.count() == 1
        assert page.locator(".asr-tl__lane").first.locator("[data-asr-lane-suggestion]").count() == 1
        assert en("asrEditor.suggestion").replace("{{name}}", "Dana Lee").replace("{{percent}}", "87%") in lane_offer.inner_text()
        accept = editor.action("timeline-accept")
        assert accept.inner_text().strip() == en("asrEditor.suggestionAccept")
        wordless = page.eval_on_selector_all(".asr-tl__heads button", "els => els.filter(e => !e.textContent.trim()).length")
        assert wordless == 0, "a lane header has a button without a word on it"
        head = page.locator(".asr-tl__heads").bounding_box()
        pill = lane_offer.bounding_box()
        assert pill["x"] >= head["x"] and pill["x"] + pill["width"] <= head["x"] + head["width"] + 0.5, f"the match does not fit its lane header: {pill} in {head}"
        editor.shot("asr-editor-test-lane-match")
        lane_offer.locator(".asr-suggest__name").click()
        page.wait_for_function(f"!{audio}.paused", timeout=10000)
        page.evaluate(f"{audio}.pause()")
        # Accepting there renames the speaker and says so, with Undo in the message
        accept.click()
        assert editor.speakers() == ["Dana Lee" if s["speaker"] == "SPEAKER_00" else s["speaker"] for s in seeded]
        editor.lane_offers([])
        toast = page.locator("#toastContainer .toast-message").filter(has_text="Dana Lee")
        toast.wait_for(state="visible", timeout=5000)
        assert toast.locator(".toast__action").inner_text().strip() == en("asrEditor.undoAction")
        toast.locator(".toast__action").click()
        assert editor.speakers() == [s["speaker"] for s in seeded]
        assert editor.action("undo").is_disabled() and not editor.dirty()
        editor.lane_offers([en("asrEditor.suggestionAccept")])
        assert offer.count() == 1
        toast.wait_for(state="detached", timeout=5000)

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


def test_the_timeline_shows_the_recording_and_follows_the_selection(meeting):
    editor, _ = meeting
    page = editor.page
    editor.tl_ready()
    seeded = meeting_segments()

    # One lane per speaker, in the colours of the speaker chips, and the waveform of the audio
    assert editor.tl("lanes") == [{"name": "SPEAKER_00", "count": 6}, {"name": "SPEAKER_01", "count": 6}]
    heads = page.eval_on_selector_all(".asr-tl__lane .asr-tl__name", "els => els.map(e => e.textContent.trim())")
    assert heads == ["SPEAKER_00", "SPEAKER_01"]
    swatch = page.locator("[data-asr-lane-swatch]").first.evaluate("el => getComputedStyle(el).backgroundColor")
    chip = page.locator(f"{ROW} .asr-seg__chip").first.evaluate("el => getComputedStyle(el).backgroundColor")
    assert swatch == chip
    assert editor.tl("duration") == pytest.approx(90, abs=1)
    editor.shot("asr-editor-test-timeline")

    # A click on the ruler moves the playhead
    page.mouse.click(*editor.tl_point(9.0))
    page.wait_for_function("() => Math.abs(document.querySelector('[data-asr-editor-audio]').currentTime - 9) < 0.2")
    assert editor.tl("playhead") == pytest.approx(9.0, abs=0.2)

    # A click on a block selects its row; Ctrl adds another
    page.mouse.click(*editor.tl_point(14.0, lane=0))
    assert editor.selected() == [2]
    page.keyboard.down("Control")
    page.mouse.click(*editor.tl_point(20.0, lane=1))
    page.keyboard.up("Control")
    assert editor.selected() == [2, 3]

    # A row selected in the pane is brought into view on the timeline
    assert editor.tl("xOf(67)") > editor.tl("width"), "the last segment should start out of view"
    page.evaluate("() => { const l = document.querySelector('.asr-list'); l.scrollTop = l.scrollHeight; }")
    page.wait_for_timeout(300)
    editor.select_row(11)
    page.wait_for_function(f"() => {TL}.xOf(66) >= 0 && {TL}.xOf(71.5) <= {TL}.width", timeout=5000)

    # Zoom, and the whole recording at once
    pps = editor.tl("view.pps")
    editor.action("timeline-zoom-in").click()
    assert editor.tl("view.pps") > pps
    editor.action("timeline-zoom-fit").click()
    assert editor.tl("view.t0") == 0 and editor.tl("xOf(90)") == pytest.approx(editor.tl("width"), abs=1)
    # Ctrl+wheel zooms around the pointer: the time under it stays there
    x, y = editor.tl_point(30.0, lane=0)
    page.mouse.move(x, y)
    page.keyboard.down("Control")
    page.mouse.wheel(0, -300)
    page.keyboard.up("Control")
    page.wait_for_timeout(200)
    assert editor.tl("view.pps") > 90 / editor.tl("width") * 1.5
    assert editor.tl_point(30.0, lane=0)[0] == pytest.approx(x, abs=1.5)

    # Collapsed, it stays collapsed for the next recording; nothing was edited
    editor.action("timeline-toggle").click()
    assert not page.locator(CANVAS).is_visible()
    assert page.evaluate("() => localStorage.getItem('speakr.asrEditor.timeline')") == "0"
    assert not editor.dirty()
    editor.check("editor timeline")
    editor.discard()
    editor.click_edit()
    assert not page.locator(CANVAS).is_visible()
    editor.action("timeline-toggle").click()
    editor.tl_ready()
    assert page.evaluate("() => localStorage.getItem('speakr.asrEditor.timeline')") == "1"
    editor.check("editor timeline, reopened")
    editor.discard()


def test_two_labels_matched_to_one_person_are_merged(editor):
    """The diarization split one person into two labels and both match the same saved
    speaker. Accepting one keeps the offer for the other, as a merge."""
    page = editor.page
    recording_id = editor.ids[MEETING_TITLE]
    seeded = meeting_segments()
    labels = [s["speaker"] for s in seeded]
    editor.store(recording_id, seeded)
    answer = {"success": True, "recording_id": recording_id, "suggestions": {
        "SPEAKER_00": [{"speaker_id": 0, "name": "Dana Lee", "similarity": 87.2, "confidence": 0.9}],
        "SPEAKER_01": [{"speaker_id": 0, "name": "Dana Lee", "similarity": 83.4, "confidence": 0.9}]}}
    pattern = "**/speakers/suggestions/*"
    page.route(pattern, lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(answer)))
    accept_word, merge_word = en("asrEditor.suggestionAccept"), en("asrEditor.merge")
    merge_into = en("asrEditor.suggestionMergeInto").replace("{{name}}", "Dana Lee")
    try:
        editor.open(recording_id)
        editor.tl_ready(waveform=False)
        offers = page.locator("[data-asr-suggestion]")
        lane_offers = page.locator("[data-asr-lane-suggestion]")
        offers.first.wait_for(state="visible", timeout=10000)
        assert offers.count() == 2
        editor.lane_offers([accept_word, accept_word])

        # Accept the first: the second is still offered, now as a merge
        page.locator(f"{ROW}[data-index='0'] [data-asr-action='accept-suggestion']").click()
        one_named = ["Dana Lee" if label == "SPEAKER_00" else label for label in labels]
        assert editor.speakers() == one_named
        assert offers.count() == 1
        editor.lane_offers([merge_word])
        assert "83%" in lane_offers.inner_text() and "Dana Lee" in lane_offers.inner_text()
        merge = editor.action("timeline-accept")
        assert merge.inner_text().strip() == merge_word and merge.get_attribute("title") == merge_into
        in_pane = page.locator(f"{ROW}[data-index='1'] [data-asr-action='accept-suggestion']")
        assert in_pane.inner_text().strip() == merge_word and in_pane.get_attribute("title") == merge_into
        editor.shot("asr-editor-test-lane-merge")
        # The picker lists the person once
        page.locator(f"{ROW}[data-index='1'] .asr-seg__chip").click()
        page.locator("[data-asr-popover='picker']").wait_for(state="visible", timeout=5000)
        options = page.eval_on_selector_all("[data-asr-option]", "els => els.map(e => e.dataset.asrOption)")
        assert options.count("Dana Lee") == 1, options
        page.keyboard.press("Escape")
        page.locator("[data-asr-popover='picker']").wait_for(state="detached", timeout=5000)

        # Merge: one speaker, one lane, one undo step
        merge.click()
        assert editor.speakers() == ["Dana Lee"] * len(seeded)
        assert editor.tl_lanes(1) == [{"name": "Dana Lee", "count": len(seeded)}]
        assert offers.count() == 0
        editor.lane_offers([])
        toast = page.locator("#toastContainer .toast-message").filter(
            has_text=en("asrEditor.suggestionMerged").replace("{{label}}", "SPEAKER_01").replace("{{name}}", "Dana Lee"))
        toast.wait_for(state="visible", timeout=5000)
        assert editor.tl("undoDepth") == 2
        # Undo restores the label and the offer; once more, both labels
        editor.action("undo").click()
        assert editor.speakers() == one_named
        assert [lane["name"] for lane in editor.tl_lanes(2)] == ["Dana Lee", "SPEAKER_01"]
        editor.lane_offers([merge_word])
        editor.action("undo").click()
        assert editor.speakers() == labels
        editor.lane_offers([accept_word, accept_word])
        editor.action("redo").click()
        editor.action("redo").click()
        assert editor.speakers() == ["Dana Lee"] * len(seeded)

        # Saved: both labels go out under their own label, mapped to the one
        # name, so the server names both and learns the voice from both
        with page.expect_request(lambda r: r.method == "POST" and r.url.endswith(f"/recording/{recording_id}/update_transcript")) as sent:
            editor.save()
        body = sent.value.post_data_json
        assert body["speaker_map"] == {"SPEAKER_00": {"name": "Dana Lee", "isMe": False}, "SPEAKER_01": {"name": "Dana Lee", "isMe": False}}
        assert [s["speaker"] for s in body["transcript_data"]] == labels
        stored = editor.stored(recording_id)
        assert [s["speaker"] for s in stored] == ["Dana Lee"] * len(seeded)
        assert len({s.get("speaker_id") for s in stored}) == 1 and stored[0].get("speaker_id") is not None
        assert editor.speakers() == ["Dana Lee"] * len(seeded) and not editor.dirty()
        editor.check("editor, merged voice matches")
        editor.discard()

        # After a reload the recording has one speaker, in the transcript and in the editor
        page.unroute(pattern)
        open_page(page, editor.problems, BASE, f"/recordings/{recording_id}", settle_ms=1500)
        editor.click_edit()
        editor.tl_ready(waveform=False)
        assert editor.speakers() == ["Dana Lee"] * len(seeded)
        assert editor.tl_lanes(1) == [{"name": "Dana Lee", "count": len(seeded)}]
        assert page.locator("[data-asr-suggestion]").count() == 0
        editor.check("editor, one speaker after the merge")
        editor.discard()
    finally:
        page.unroute(pattern)
        saved = page.request.get(f"{BASE}/speakers")
        for speaker in (saved.json() if saved.ok else []):
            if speaker.get("name") == "Dana Lee":
                page.request.delete(f"{BASE}/speakers/{speaker['id']}", headers={"X-CSRFToken": editor.csrf()})


def test_dragging_an_edge_changes_the_times_and_undo_restores_them(editor):
    page = editor.page
    recording_id = editor.ids[MEETING_TITLE]
    # The first two segments of one speaker touch, the third follows after a gap
    segments = meeting_segments()
    segments[0] = {**segments[0], "start_time": 0.0, "end_time": 3.0}
    segments[1] = {**segments[1], "speaker": "SPEAKER_00", "start_time": 3.0, "end_time": 5.5}
    editor.store(recording_id, segments)
    editor.open(recording_id)
    editor.tl_ready()

    def times(index):
        seg = editor.tl_segments()[index]
        return seg["start_time"], seg["end_time"]

    # The edge two touching blocks share: both move, as one undo step (Ctrl: no snapping)
    grab = editor.tl_point(3.0, lane=0, dx=-2)
    editor.tl_drag(grab, editor.tl_point(4.0, lane=0, dx=-2), keys=["Control"],
                   before_release=lambda: editor.shot("asr-editor-test-timeline-drag"))
    assert times(0) == (0.0, pytest.approx(4.0, abs=0.05))
    assert times(1) == (pytest.approx(4.0, abs=0.05), 5.5)
    assert times(0)[1] == times(1)[0], "the neighbour's edge did not follow"
    assert editor.tl("undoDepth") == 1 and editor.dirty()
    assert editor.selected() == [0]

    # Saved as dragged
    editor.save()
    stored = editor.stored(recording_id)
    assert stored[0]["end_time"] == stored[1]["start_time"] == times(0)[1]
    assert [s["sentence"] for s in stored] == [s["sentence"] for s in segments]

    # One undo puts both back
    editor.action("undo").click()
    assert times(0) == (0.0, 3.0) and times(1) == (3.0, 5.5)
    assert editor.tl("undoDepth") == 0

    # With Alt the edge moves alone and may overlap its neighbour, which is then marked as a problem
    editor.tl_drag(editor.tl_point(3.0, lane=0, dx=-2), editor.tl_point(4.0, lane=0, dx=-2), keys=["Alt", "Control"])
    assert times(0) == (0.0, pytest.approx(4.0, abs=0.05)) and times(1) == (3.0, 5.5)
    assert editor.action("next-problem").is_visible()
    editor.action("undo").click()
    assert not editor.action("next-problem").is_visible()

    # Without a touching neighbour the edge stops at the next block of the lane
    editor.tl_drag(editor.tl_point(5.5, lane=0, dx=-2), editor.tl_point(15.0, lane=0), keys=["Control"])
    assert times(1) == (3.0, 12.0)
    editor.action("undo").click()

    # Snapping: to the playhead, and to the edge of a block in another lane
    page.mouse.click(*editor.tl_point(4.6))
    page.wait_for_function(f"() => Math.abs({TL}.playhead - 4.6) < 0.05")
    playhead = editor.tl("playhead")
    snaps = []
    editor.tl_drag(editor.tl_point(5.5, lane=0, dx=-2), editor.tl_point(playhead, lane=0, dx=1),
                   before_release=lambda: snaps.append(editor.tl("snap")))
    assert snaps == ["playhead"] and times(1)[1] == pytest.approx(playhead, abs=0.002)
    editor.action("undo").click()
    editor.tl_drag(editor.tl_point(18.0, lane=1, dx=2), editor.tl_point(17.52, lane=1),
                   before_release=lambda: snaps.append(editor.tl("snap")))
    assert snaps[-1] == "edge" and times(3)[0] == 17.5        # the end of the block above
    editor.action("undo").click()

    # Esc cancels a drag: nothing changes
    depth = editor.tl("undoDepth")
    page.mouse.move(*editor.tl_point(3.0, lane=0, dx=-2))
    page.mouse.down()
    page.mouse.move(*editor.tl_point(4.2, lane=0), steps=5)
    assert editor.tl("dragging")
    page.keyboard.press("Escape")
    assert not editor.tl("dragging")
    page.mouse.up()
    assert times(0) == (0.0, 3.0) and editor.tl("undoDepth") == depth
    assert page.locator(".asr-editor-modal").count() == 1, "Esc closed the editor instead of cancelling the drag"
    editor.check("editor timeline, edge drags")
    editor.discard()


def test_dragging_a_block_moves_it_or_changes_its_speaker(meeting):
    editor, recording_id = meeting
    page = editor.page
    editor.tl_ready()
    seeded = meeting_segments()

    # To the other lane: the speaker changes, the times stay, one undo step
    start = editor.tl_point(14.5, lane=0)
    target = editor.tl_point(14.5, lane=1)
    editor.tl_drag(start, target)
    assert editor.speakers()[:4] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_01", "SPEAKER_01"]
    moved = editor.tl_segments()[2]
    assert (moved["speaker"], moved["start_time"], moved["end_time"]) == ("SPEAKER_01", 12.0, 17.5)
    assert editor.tl("undoDepth") == 1 and editor.selected() == [2]
    assert editor.tl("lanes") == [{"name": "SPEAKER_00", "count": 5}, {"name": "SPEAKER_01", "count": 7}]
    editor.action("undo").click()
    assert editor.speakers()[:4] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_01"]
    assert editor.tl_segments() == seeded

    # Along its lane: the block keeps its length and stops at the next block of the lane
    editor.tl_drag(editor.tl_point(14.5, lane=0), editor.tl_point(16.5, lane=0), keys=["Control"])
    moved = editor.tl_segments()[2]
    assert moved["start_time"] == pytest.approx(14.0, abs=0.05)
    assert moved["end_time"] - moved["start_time"] == pytest.approx(5.5, abs=0.001)
    editor.tl_drag(editor.tl_point(16.0, lane=0), editor.tl_point(28.0, lane=0), keys=["Control"])
    # It now starts after the fourth segment (18 s), so it is the fourth itself
    moved = editor.tl_segments()[3]
    assert moved["sentence"] == seeded[2]["sentence"]
    assert (moved["start_time"], moved["end_time"]) == (18.5, 24.0)      # the next block of the lane starts at 24
    assert editor.texts()[2:4] == [seeded[3]["sentence"], seeded[2]["sentence"]]
    assert editor.clocks()[2:4] == ["0:18", "0:18"] and editor.selected() == [3]
    assert editor.tl("undoDepth") == 2

    # Saved, and undone after the save
    editor.save()
    assert editor.stored(recording_id)[3]["start_time"] == 18.5
    editor.action("undo").click()
    editor.action("undo").click()
    assert editor.tl_segments() == seeded
    editor.check("editor timeline, block drags")
    editor.discard()


def test_a_click_on_a_block_selects_it_and_changes_nothing(meeting):
    """A press that barely moves is a click. The speaker changes only when a block
    is dragged more than half a lane up or down."""
    editor, recording_id = meeting
    page = editor.page
    editor.tl_ready()
    seeded = meeting_segments()
    x, y = editor.tl_point(14.5, lane=0)
    lane = editor.tl("laneY(1)") - editor.tl("laneY(0)")

    def press(dx, dy):
        page.mouse.move(x, y)
        page.mouse.down()
        if dx or dy:
            page.mouse.move(x + dx, y + dy, steps=3)
        page.mouse.up()
        page.wait_for_timeout(100)

    def unchanged(what):
        assert editor.tl_segments() == seeded, f"{what} changed the transcript"
        assert editor.tl("undoDepth") == 0 and not editor.dirty(), f"{what} left an edit"
        assert editor.selected() == [2]

    # No movement, and movement under the threshold in either direction
    for dx, dy in [(0, 0), (3, 0), (0, 3), (-3, -3), (3, 3)]:
        press(dx, dy)
        unchanged(f"a click that moved by {dx}, {dy}")
    # A drag, but less than half a lane up or down and not sideways
    for dy in (round(lane * 0.45), -round(lane * 0.45)):
        press(0, dy)
        unchanged(f"a drag of {dy} px up or down")
    # A tap with an unsteady finger
    page.evaluate("""([sel, x, y]) => {
        const canvas = document.querySelector(sel);
        const fire = (type, dx, dy) => canvas.dispatchEvent(new PointerEvent(type, {
            pointerId: 9, pointerType: 'touch', isPrimary: true, bubbles: true, cancelable: true, clientX: x + dx, clientY: y + dy, button: 0, buttons: 1 }));
        fire('pointerdown', 0, 0);
        fire('pointermove', 4, 3);
        fire('pointermove', 7, 6);
        fire('pointerup', 7, 6);
    }""", [CANVAS, x, y])
    page.wait_for_timeout(100)
    unchanged("a tap that moved by 7, 6")

    # More than half a lane down: now the speaker changes, and only the speaker
    press(0, round(lane * 0.6))
    moved = editor.tl_segments()[2]
    assert (moved["speaker"], moved["start_time"], moved["end_time"]) == ("SPEAKER_01", 12.0, 17.5)
    assert editor.tl("undoDepth") == 1
    editor.action("undo").click()
    assert editor.tl_segments() == seeded
    editor.check("editor timeline, clicks")
    editor.discard()


def test_dragging_on_empty_lane_space_adds_a_segment(meeting):
    editor, recording_id = meeting
    page = editor.page
    editor.tl_ready()
    editor.action("timeline-zoom-fit").click()
    count = editor.rows().count()

    # After the last segment (71.5 s) the lanes are empty
    editor.tl_drag(editor.tl_point(76.0, lane=1), editor.tl_point(82.0, lane=1), keys=["Control"])
    page.wait_for_function(f"() => {TL}.segments.length === 13")
    added = editor.tl_segments()[-1]
    assert added["speaker"] == "SPEAKER_01" and added["sentence"] == ""
    assert added["start_time"] == pytest.approx(76.0, abs=0.2) and added["end_time"] == pytest.approx(82.0, abs=0.2)
    assert editor.tl("undoDepth") == 1
    # Its row is selected and its text has the cursor
    page.wait_for_function("() => document.activeElement && document.activeElement.tagName === 'TEXTAREA'")
    assert editor.focused() == (12, 0) or editor.focused() == [12, 0]
    page.keyboard.type("Thanks, see you next week.")
    editor.save()
    stored = editor.stored(recording_id)
    assert len(stored) == 13 and stored[12]["sentence"] == "Thanks, see you next week."
    assert stored[12]["speaker"] == "SPEAKER_01"

    # A drag shorter than a tenth of a second creates nothing, and a click on empty space moves the playhead
    page.keyboard.press("Escape")
    editor.tl("setView(84, 400)")
    editor.tl_drag(editor.tl_point(85.0, lane=0), editor.tl_point(85.0, lane=0, dx=12), keys=["Control"])
    assert len(editor.tl_segments()) == 13
    page.mouse.click(*editor.tl_point(85.5, lane=0))
    page.wait_for_function(f"() => Math.abs({TL}.playhead - 85.5) < 0.3")

    # Typing and the new segment are two undo steps
    editor.action("undo").click()
    editor.action("undo").click()
    assert len(editor.tl_segments()) == 12
    editor.check("editor timeline, new segment")
    editor.discard()


def test_a_lane_name_renames_or_merges_the_whole_speaker(editor):
    """The name in a lane header of the timeline is a button: it opens the speaker picker
    for the whole speaker. The voice-match control beside it stays a control of its own."""
    page = editor.page
    recording_id = editor.ids[MEETING_TITLE]
    seeded = meeting_segments()
    labels = [s["speaker"] for s in seeded]
    editor.store(recording_id, seeded)
    answer = {"success": True, "recording_id": recording_id,
              "suggestions": {"SPEAKER_00": [{"speaker_id": 0, "name": "Dana Lee", "similarity": 87.2, "confidence": 0.9}]}}
    pattern = "**/speakers/suggestions/*"
    page.route(pattern, lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(answer)))
    picker = page.locator("[data-asr-popover='picker']")
    merge_into = lambda name: en("asrEditor.suggestionMergeInto").replace("{{name}}", name)  # noqa: E731
    try:
        editor.open(recording_id)
        editor.tl_ready(waveform=False)
        editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6)])
        editor.lane_offers([en("asrEditor.suggestionAccept")])

        # The name and the voice match are two targets with space between them,
        # and the name button says what it does
        name = editor.lane_name(0)
        assert name.get_attribute("aria-label") == en("asrEditor.renameSpeaker").replace("{{name}}", "SPEAKER_00")
        assert name.evaluate("el => el.tagName") == "BUTTON"
        a, b = name.bounding_box(), page.locator("[data-asr-lane-suggestion]").bounding_box()
        assert a["x"] + a["width"] + 4 <= b["x"], f"the lane name and the voice match touch: {a} {b}"

        # The voice-match control still accepts, and a click on it opens no picker
        editor.action("timeline-accept").click()
        editor.speakers_are(["Dana Lee" if n == "SPEAKER_00" else n for n in labels])
        editor.lanes_are([("Dana Lee", 6), ("SPEAKER_01", 6)])
        assert picker.count() == 0, "accepting a voice match opened the speaker picker"
        page.keyboard.press("Control+z")
        editor.speakers_are(labels)
        editor.lane_offers([en("asrEditor.suggestionAccept")])
        # Its name part plays the speaker and opens no picker either
        audio = "document.querySelector('[data-asr-editor-audio]')"
        page.wait_for_function(f"{audio} && {audio}.readyState >= 1", timeout=15000)
        page.locator("[data-asr-lane-suggestion] .asr-suggest__name").click()
        page.wait_for_function(f"!{audio}.paused", timeout=10000)
        page.evaluate(f"{audio}.pause()")
        assert picker.count() == 0 and editor.tl("undoDepth") == 0

        # A click on the name: the picker for the whole speaker. Its heading
        # names the scope, there is no "All segments" checkbox, the speaker
        # itself is no choice, another speaker of the recording is a merge and
        # says so, and the voice match is the first of the saved speakers.
        sheet = editor.lane_picker(0, "SPEAKER_00")
        assert sheet.locator(".asr-pop__all").count() == 0
        assert sheet.locator(".asr-suggest--picker").count() == 1
        options = sheet.locator("[data-asr-option]")
        listed = [options.nth(i).get_attribute("data-asr-option") for i in range(options.count())]
        assert listed[:2] == ["SPEAKER_01", "Dana Lee"], f"the options of the lane picker: {listed}"
        other = sheet.locator("[data-asr-option='SPEAKER_01']")
        assert other.locator(".asr-pop__name").inner_text().strip() == merge_into("SPEAKER_01")
        assert other.get_attribute("data-asr-merge") is not None and sheet.locator("[data-asr-merge]").count() == 1
        assert sheet.locator(".asr-kbd").count() == 0, "a number key would merge two speakers"
        editor.shot("asr-editor-test-lane-picker")

        # A typed name renames the speaker in every segment, as one undo step,
        # and the message has Undo in it
        page.keyboard.type("Linus")
        page.wait_for_function("() => document.querySelectorAll('[data-asr-popover=\"picker\"] [data-asr-option]').length === 1")
        assert options.first.inner_text().strip() == en("asrEditor.pickerUseName").replace("{{name}}", "Linus")
        page.keyboard.press("Enter")
        picker.wait_for(state="detached", timeout=5000)
        editor.speakers_are(["Linus" if n == "SPEAKER_00" else n for n in labels])
        editor.lanes_are([("Linus", 6), ("SPEAKER_01", 6)])
        editor.undo_depth(1)
        editor.lane_offers([])
        toast = page.locator("#toastContainer .toast-message").filter(has_text="Linus")
        toast.wait_for(state="visible", timeout=5000)
        toast.locator(".toast__action").click()
        editor.speakers_are(labels)
        editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6)])
        editor.undo_depth(0)
        toast.wait_for(state="detached", timeout=5000)

        # By keyboard: Enter on the name opens the picker, Esc closes it and
        # the focus is back on the name, Space opens it again
        second = editor.lane_name(1)
        second.focus()
        page.keyboard.press("Enter")
        picker.wait_for(state="visible", timeout=5000)
        assert picker.get_attribute("data-asr-picker-scope") == "speaker"
        page.keyboard.press("Escape")
        picker.wait_for(state="detached", timeout=5000)
        assert second.evaluate("el => el === document.activeElement")
        assert editor.tl("undoDepth") == 0
        page.keyboard.press("Space")
        picker.wait_for(state="visible", timeout=5000)
        assert picker.locator("[data-asr-picker-heading]").inner_text().strip() == en("asrEditor.renameSpeaker").replace("{{name}}", "SPEAKER_01")
        # A number key chooses nothing here
        page.keyboard.press("1")
        assert picker.locator("input").input_value() == "1" and editor.tl("undoDepth") == 0
        page.keyboard.press("Backspace")

        # Choosing a speaker of the recording merges the two: one speaker and
        # one lane are left, and undo brings both back
        target = picker.locator("[data-asr-option='SPEAKER_00']")
        target.wait_for(state="visible", timeout=5000)
        assert target.locator(".asr-pop__name").inner_text().strip() == merge_into("SPEAKER_00")
        editor.shot("asr-editor-test-lane-picker-merge")
        target.click()
        picker.wait_for(state="detached", timeout=5000)
        editor.speakers_are(["SPEAKER_00"] * len(seeded))
        editor.lanes_are([("SPEAKER_00", 12)])
        editor.undo_depth(1)
        merged = page.locator("#toastContainer .toast-message").filter(
            has_text=en("asrEditor.suggestionMerged").replace("{{label}}", "SPEAKER_01").replace("{{name}}", "SPEAKER_00"))
        merged.wait_for(state="visible", timeout=5000)
        editor.action("undo").click()
        editor.speakers_are(labels)
        editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6)])
        editor.action("redo").click()
        editor.speakers_are(["SPEAKER_00"] * len(seeded))
        editor.action("undo").click()
        editor.speakers_are(labels)

        # The row picker is as it was: its checkbox, the number keys, no heading
        page.locator(f"{ROW}[data-index='0'] .asr-seg__chip").click()
        picker.wait_for(state="visible", timeout=5000)
        assert picker.get_attribute("data-asr-picker-scope") == "segments"
        assert picker.locator("[data-asr-picker-heading]").count() == 0 and picker.locator(".asr-pop__all").count() == 1
        assert picker.locator("[data-asr-merge]").count() == 0 and picker.locator(".asr-kbd").count() == 2
        page.keyboard.press("Escape")
        picker.wait_for(state="detached", timeout=5000)

        # The voice match chosen from the list of the lane picker is accepted
        # like with its Accept button: the same message, one undo step
        sheet = editor.lane_picker(0, "SPEAKER_00")
        sheet.locator("[data-asr-option='Dana Lee']").click()
        picker.wait_for(state="detached", timeout=5000)
        editor.speakers_are(["Dana Lee" if n == "SPEAKER_00" else n for n in labels])
        editor.lanes_are([("Dana Lee", 6), ("SPEAKER_01", 6)])
        editor.lane_offers([])
        accepted = page.locator("#toastContainer .toast-message").filter(
            has_text=en("asrEditor.suggestionAccepted").replace("{{label}}", "SPEAKER_00").replace("{{name}}", "Dana Lee"))
        accepted.wait_for(state="visible", timeout=5000)
        editor.action("undo").click()
        editor.speakers_are(labels)
        editor.lane_offers([en("asrEditor.suggestionAccept")])
        editor.undo_depth(0)

        # Renamed from the lane and saved: every segment of the speaker
        sheet = editor.lane_picker(1, "SPEAKER_01")
        page.keyboard.type("Linus")
        page.keyboard.press("Enter")
        picker.wait_for(state="detached", timeout=5000)
        named = ["Linus" if n == "SPEAKER_01" else n for n in labels]
        editor.speakers_are(named)
        editor.save()
        assert [s["speaker"] for s in editor.stored(recording_id)] == named
        editor.check("editor, lane name")
        editor.discard()
    finally:
        page.unroute(pattern)


def test_a_speaker_is_added_and_saved_only_with_segments(meeting):
    editor, recording_id = meeting
    page = editor.page
    editor.tl_ready()
    seeded = meeting_segments()
    labels = [s["speaker"] for s in seeded]
    picker = page.locator("[data-asr-popover='picker']")
    key_of = lambda lane: page.locator(f".asr-tl__lane[data-asr-lane='{lane}'] [data-asr-lane-swatch]").inner_text().strip()  # noqa: E731

    removers = page.locator("[data-asr-action='lane-remove']")
    remove_of = lambda lane: page.locator(f".asr-tl__lane[data-asr-lane='{lane}'] [data-asr-action='lane-remove']")  # noqa: E731

    # In the header cell of the overview, beside its label: a button with an
    # icon and words. The lane headers have no row of their own for it: they
    # are as tall as the canvas. The new speaker is the next label, has an
    # empty lane and a number key, and is no unsaved change.
    add = editor.action("add-speaker")
    assert add.inner_text().strip() == en("asrEditor.addSpeaker")
    assert add.get_attribute("aria-label") == en("asrEditor.addSpeaker")
    cell = page.locator(".asr-tl__mini-label")
    assert en("asrEditor.timeline.overview") in cell.inner_text()
    inside = lambda box, outer: (box["x"] >= outer["x"] - 0.5 and box["x"] + box["width"] <= outer["x"] + outer["width"] + 0.5  # noqa: E731
                                 and box["y"] >= outer["y"] - 0.5 and box["y"] + box["height"] <= outer["y"] + outer["height"] + 0.5)
    assert inside(add.bounding_box(), cell.bounding_box()), f"the Add speaker button does not fit the overview cell: {add.bounding_box()} in {cell.bounding_box()}"
    label = page.locator(".asr-tl__mini-text").bounding_box()
    assert label["x"] + label["width"] <= add.bounding_box()["x"], "the Add speaker button covers the Overview label"
    assert abs(cell.bounding_box()["height"] - page.locator(".asr-tl__mini-box").bounding_box()["height"]) <= 1
    heads, canvas = page.locator(".asr-tl__heads").bounding_box(), page.locator(CANVAS).bounding_box()
    assert abs(heads["height"] - canvas["height"]) <= 1, f"the lane headers have a row the canvas does not: {heads} {canvas}"
    assert removers.count() == 0, "a lane with segments offers to remove its speaker"
    add.click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0)])
    assert key_of(2) == "3"
    assert not editor.dirty() and editor.tl("undoDepth") == 1
    assert editor.tl_segments() == seeded
    editor.shot("asr-editor-test-added-speaker")

    # Undo and redo
    add.click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0), ("SPEAKER_03", 0)])
    editor.action("undo").click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0)])
    editor.action("undo").click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6)])
    assert editor.action("undo").is_disabled()
    editor.action("redo").click()
    editor.action("redo").click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0), ("SPEAKER_03", 0)])
    assert not editor.dirty()

    # A lane without segments has a button that removes its speaker; a lane
    # with segments has none. It is a target of its own, beside the name.
    assert removers.count() == 2 and remove_of(0).count() == 0 and remove_of(1).count() == 0
    remove = remove_of(2)
    assert remove.get_attribute("aria-label") == en("asrEditor.removeSpeaker") and remove.get_attribute("title") == en("asrEditor.removeSpeaker")
    a, b = editor.lane_name(2).bounding_box(), remove.bounding_box()
    assert a["x"] + a["width"] + 4 <= b["x"], f"the lane name and the remove button touch: {a} {b}"
    assert inside(b, page.locator(".asr-tl__lane[data-asr-lane='2']").bounding_box())
    editor.shot("asr-editor-test-remove-speaker")
    # Removed without a question, as one undo step; the next speaker takes its number key
    depth = editor.tl("undoDepth")
    remove.click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_03", 0)])
    editor.undo_depth(depth + 1)
    assert editor.question().count() == 0 and page.locator("[data-asr-popover]").count() == 0
    assert key_of(2) == "3" and not editor.dirty()
    assert editor.tl_segments() == seeded
    editor.action("undo").click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0), ("SPEAKER_03", 0)])
    assert [key_of(2), key_of(3)] == ["3", "4"]
    editor.action("redo").click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_03", 0)])
    page.keyboard.press("Control+z")
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0), ("SPEAKER_03", 0)])
    # By keyboard, and the label of a removed speaker is handed out again
    remove_of(3).focus()
    page.keyboard.press("Enter")
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0)])
    add.click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0), ("SPEAKER_03", 0)])
    assert not editor.dirty()

    # The row picker lists the added speakers with the recording's, and assigns one
    page.locator(f"{ROW}[data-index='0'] .asr-seg__chip").click()
    picker.wait_for(state="visible", timeout=5000)
    options = picker.locator("[data-asr-option]")
    assert [options.nth(i).get_attribute("data-asr-option") for i in range(4)] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03"]
    picker.locator("[data-asr-option='SPEAKER_02']").click()
    picker.wait_for(state="detached", timeout=5000)
    expected = list(labels)
    expected[0] = "SPEAKER_02"
    editor.speakers_are(expected)
    editor.lanes_are([("SPEAKER_00", 5), ("SPEAKER_01", 6), ("SPEAKER_02", 1), ("SPEAKER_03", 0)])
    page.wait_for_function("() => !!document.querySelector('[data-asr-dirty]')", timeout=5000)
    # The added speakers keep their number keys when they get a segment
    assert [key_of(2), key_of(3)] == ["3", "4"]
    # With a segment the speaker cannot be removed
    assert removers.count() == 1 and remove_of(2).count() == 0 and remove_of(3).count() == 1

    # Its number key assigns the selection
    editor.select_row(1)
    page.keyboard.press("4")
    editor.speakers_are([expected[0], "SPEAKER_03"] + expected[2:])
    editor.lanes_are([("SPEAKER_00", 5), ("SPEAKER_01", 5), ("SPEAKER_02", 1), ("SPEAKER_03", 1)])
    assert removers.count() == 0
    page.keyboard.press("Control+z")
    editor.speakers_are(expected)
    editor.lanes_are([("SPEAKER_00", 5), ("SPEAKER_01", 6), ("SPEAKER_02", 1), ("SPEAKER_03", 0)])

    # An added speaker that lost its segments again keeps its lane and can be
    # removed (the first two number keys are SPEAKER_01 and SPEAKER_00 now)
    editor.select_row(0)
    page.keyboard.press("2")
    editor.speakers_are(labels)
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0), ("SPEAKER_03", 0)])
    assert removers.count() == 2
    remove_of(2).click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_03", 0)])
    editor.action("undo").click()
    editor.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6), ("SPEAKER_02", 0), ("SPEAKER_03", 0)])
    editor.action("undo").click()
    editor.speakers_are(expected)
    editor.lanes_are([("SPEAKER_00", 5), ("SPEAKER_01", 6), ("SPEAKER_02", 1), ("SPEAKER_03", 0)])

    # A block dragged to the empty lane gets that speaker
    editor.tl_drag(editor.tl_point(14.5, lane=0), editor.tl_point(14.5, lane=3))
    editor.speakers_are(expected[:2] + ["SPEAKER_03"] + expected[3:])
    editor.lanes_are([("SPEAKER_00", 4), ("SPEAKER_01", 6), ("SPEAKER_02", 1), ("SPEAKER_03", 1)])
    editor.action("undo").click()
    editor.speakers_are(expected)
    editor.lanes_are([("SPEAKER_00", 5), ("SPEAKER_01", 6), ("SPEAKER_02", 1), ("SPEAKER_03", 0)])

    # A drag on the empty lane adds a segment of that speaker
    editor.action("timeline-zoom-fit").click()
    editor.tl_drag(editor.tl_point(76.0, lane=3), editor.tl_point(82.0, lane=3), keys=["Control"])
    page.wait_for_function(f"() => {TL}.segments.length === 13")
    assert editor.tl_segments()[-1]["speaker"] == "SPEAKER_03"
    page.wait_for_function("() => document.activeElement && document.activeElement.tagName === 'TEXTAREA'")
    page.keyboard.press("Escape")
    editor.action("undo").click()
    page.wait_for_function(f"() => {TL}.segments.length === 12")
    editor.lanes_are([("SPEAKER_00", 5), ("SPEAKER_01", 6), ("SPEAKER_02", 1), ("SPEAKER_03", 0)])

    # The empty speaker is renamed through its name
    editor.lane_picker(3, "SPEAKER_03")
    page.keyboard.type("Kim")
    page.keyboard.press("Enter")
    picker.wait_for(state="detached", timeout=5000)
    editor.lanes_are([("SPEAKER_00", 5), ("SPEAKER_01", 6), ("SPEAKER_02", 1), ("Kim", 0)])
    editor.speakers_are(expected)

    # Saved: the speaker with a segment is kept, the one without is not written
    editor.save()
    stored = editor.stored(recording_id)
    assert [s["speaker"] for s in stored] == expected
    assert [s["sentence"] for s in stored] == [s["sentence"] for s in seeded]
    assert all(set(s) == {"speaker", "sentence", "start_time", "end_time"} for s in stored), "the stored segments have another shape"
    # The session keeps the lane until the editor closes
    editor.lanes_are([("SPEAKER_00", 5), ("SPEAKER_01", 6), ("SPEAKER_02", 1), ("Kim", 0)])
    editor.check("editor, added speaker")
    editor.discard()

    # Reopened: the unused speaker is gone, the used one is a speaker like the others
    editor.open(recording_id)
    editor.tl_ready(waveform=False)
    editor.speakers_are(expected)
    editor.lanes_are([("SPEAKER_02", 1), ("SPEAKER_01", 6), ("SPEAKER_00", 5)])
    page.locator(f"{ROW}[data-index='1'] .asr-seg__chip").click()
    picker.wait_for(state="visible", timeout=5000)
    assert picker.locator("[data-asr-option='Kim']").count() == 0 and picker.locator("[data-asr-option='SPEAKER_03']").count() == 0
    page.keyboard.press("Escape")
    picker.wait_for(state="detached", timeout=5000)

    # A speaker that was only added is nothing to save: the editor closes without asking
    editor.action("add-speaker").click()
    editor.lanes_are([("SPEAKER_02", 1), ("SPEAKER_01", 6), ("SPEAKER_00", 5), ("SPEAKER_03", 0)])
    assert not editor.dirty()

    # With many speakers the lanes scroll: the button stays in view after each
    # speaker it adds, and the last lane can be removed
    lanes = [("SPEAKER_02", 1), ("SPEAKER_01", 6), ("SPEAKER_00", 5), ("SPEAKER_03", 0)]
    for number in range(4, 18):
        editor.action("add-speaker").click()
        lanes.append((f"SPEAKER_{number:02d}", 0))
        editor.tl_lanes(len(lanes))
    editor.lanes_are(lanes)
    body = page.locator(".asr-tl__body")
    sizes = body.evaluate("el => [el.scrollHeight, el.clientHeight]")
    assert sizes[0] > sizes[1], f"the lanes do not scroll with {len(lanes)} speakers: {sizes}"
    page.wait_for_function(
        "() => { const b = document.querySelector('.asr-tl__body').getBoundingClientRect(); "
        "const a = document.querySelector(\"[data-asr-action='add-speaker']\").getBoundingClientRect(); "
        "return a.top >= b.top - 0.5 && a.bottom <= b.bottom + 0.5; }", timeout=5000)
    remove_of(len(lanes) - 1).click()
    editor.lanes_are(lanes[:-1])
    assert not editor.dirty()
    page.locator(".asr-editor-modal .modal-close").click()
    page.locator(".asr-editor-modal").wait_for(state="detached", timeout=10000)
    assert editor.question().count() == 0
    assert [s["speaker"] for s in editor.stored(recording_id)] == expected
    editor.check("editor, added speaker, reopened")


def test_lane_names_and_add_speaker_on_a_phone(browser):
    phone = Editor(browser, {"width": 390, "height": 800})
    try:
        if set(phone.ids) != set(TITLES):
            precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
        recording_id = phone.ids[MEETING_TITLE]
        seeded = meeting_segments()
        phone.store(recording_id, seeded)
        phone.open(recording_id)
        page = phone.page
        phone.tl_ready(waveform=False)
        phone.lanes_are([("SPEAKER_00", 6), ("SPEAKER_01", 6)])
        head = page.locator(".asr-tl__heads").bounding_box()
        box = phone.lane_name(0).bounding_box()
        assert box["x"] >= head["x"] - 0.5 and box["x"] + box["width"] <= head["x"] + head["width"] + 0.5, f"does not fit the lane header: {box} in {head}"
        assert box["height"] >= 27.5, f"a small touch target: {box}"
        # Add speaker is in the header cell of the overview: the plus alone
        # beside "Overview" (no room for its words), named for assistive technology
        add = phone.action("add-speaker")
        cell = page.locator(".asr-tl__mini-label").bounding_box()
        box = add.bounding_box()
        assert box["x"] >= cell["x"] - 0.5 and box["x"] + box["width"] <= cell["x"] + cell["width"] + 0.5, f"does not fit the overview cell: {box} in {cell}"
        assert box["width"] >= 35.5 and box["height"] >= 25.5, f"a small touch target: {box}"
        assert add.inner_text().strip() == "" and add.get_attribute("aria-label") == en("asrEditor.addSpeaker")
        label = page.locator(".asr-tl__mini-text")
        assert label.inner_text().strip() == en("asrEditor.timeline.overview")
        assert label.evaluate("el => el.scrollWidth <= el.clientWidth"), "the Overview label is cut off"
        assert abs(head["height"] - page.locator(CANVAS).bounding_box()["height"]) <= 1

        # The picker of the whole speaker is a sheet at the foot of the screen
        sheet = phone.lane_picker(1, "SPEAKER_01")
        assert "asr-pop--sheet" in sheet.get_attribute("class")
        phone.shot("asr-editor-test-phone-lane-picker")
        sheet.locator("input").click()
        page.keyboard.type("Linus")
        page.keyboard.press("Enter")
        sheet.wait_for(state="detached", timeout=5000)
        phone.lanes_are([("SPEAKER_00", 6), ("Linus", 6)])

        phone.action("add-speaker").click()
        phone.lanes_are([("SPEAKER_00", 6), ("Linus", 6), ("SPEAKER_02", 0)])
        # The empty lane has the remove button, within its header
        remove = page.locator("[data-asr-action='lane-remove']")
        assert remove.count() == 1
        # (the header column is wider while a lane has the button, so that the label beside it is whole)
        head = page.locator(".asr-tl__heads").bounding_box()
        shown = phone.lane_name(2).locator(".asr-tl__name-text")
        assert shown.evaluate("el => el.scrollWidth <= el.clientWidth"), "the label of the added speaker is cut off"
        canvas = page.locator(CANVAS).bounding_box()
        assert canvas["width"] >= 220 and canvas["x"] + canvas["width"] <= 390.5, f"the timeline does not fit: {canvas}"
        box = remove.bounding_box()
        assert box["x"] + box["width"] <= head["x"] + head["width"] + 0.5 and box["width"] >= 27.5 and box["height"] >= 27.5, f"the remove button: {box}"
        name = phone.lane_name(2).bounding_box()
        assert name["x"] + name["width"] <= box["x"], f"the lane name and the remove button overlap: {name} {box}"
        remove.click()
        phone.lanes_are([("SPEAKER_00", 6), ("Linus", 6)])
        phone.action("undo").click()
        phone.lanes_are([("SPEAKER_00", 6), ("Linus", 6), ("SPEAKER_02", 0)])
        widths = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
        assert widths[0] <= widths[1], f"the editor is wider than the phone: {widths}"
        phone.shot("asr-editor-test-phone-added-speaker")
        check_common(page, phone.problems, "editor on a phone, lane names")
        phone.discard()
    finally:
        phone.close()


def test_phone_width(browser):
    phone = Editor(browser, {"width": 390, "height": 800})
    try:
        if set(phone.ids) != set(TITLES):
            precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
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
        # The footer: the summary checkbox on its own line, the three buttons under it, all within the screen
        summary = page.locator("[data-asr-regenerate-summary]").bounding_box()
        buttons = [b.bounding_box() for b in page.locator(".asr-editor-footer__actions button").all()]
        assert len(buttons) == 3 and all(b["x"] >= 0 and b["x"] + b["width"] <= 390.5 for b in buttons), f"the footer buttons do not fit: {buttons}"
        assert summary["x"] >= 0 and summary["x"] + summary["width"] <= 390.5 and summary["y"] + summary["height"] <= buttons[0]["y"] + 1, f"the summary checkbox: {summary}"
        phone.shot("asr-editor-test-phone")

        # The timeline fits the phone; its lanes are dragged with one finger
        phone.tl_ready()
        box = page.locator(CANVAS).bounding_box()
        assert box["width"] >= 240 and box["x"] + box["width"] <= 390.5, f"the timeline does not fit: {box}"
        toggle = page.locator("[data-asr-action='timeline-toggle']").bounding_box()
        assert toggle["height"] >= 35.5, f"the timeline toggle is a small touch target: {toggle}"
        zoom = page.locator("[data-asr-action='timeline-zoom-in']").bounding_box()
        assert zoom["width"] >= 39.5 and zoom["height"] >= 39.5, f"the zoom button is a small touch target: {zoom}"
        phone.action("timeline-zoom-fit").click()
        a, b = phone.tl_point(76.0, lane=0), phone.tl_point(84.0, lane=0)
        page.evaluate("""([sel, a, b]) => {
            const canvas = document.querySelector(sel);
            const fire = (type, [x, y]) => canvas.dispatchEvent(new PointerEvent(type, {
                pointerId: 7, pointerType: 'touch', isPrimary: true, bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, buttons: 1 }));
            fire('pointerdown', a);
            for (let i = 1; i <= 6; i++) fire('pointermove', [a[0] + (b[0] - a[0]) * i / 6, a[1]]);
            fire('pointerup', b);
        }""", [CANVAS, a, b])
        page.wait_for_function(f"() => {TL}.segments.length === 13")
        added = phone.tl_segments()[-1]
        assert added["speaker"] == "SPEAKER_00" and 5 < added["end_time"] - added["start_time"] < 11
        phone.action("undo").click()
        assert len(phone.tl_segments()) == 12
        page.keyboard.press("Escape")
        phone.action("timeline-toggle").click()
        assert not page.locator(CANVAS).is_visible()
        phone.shot("asr-editor-test-phone-timeline-closed")
        phone.action("timeline-toggle").click()

        # Split at the cursor and merge with previous, from the menu. (The list
        # went to the added segment at the end; back to the top.)
        page.evaluate("() => { document.querySelector('.asr-list').scrollTop = 0; }")
        page.locator(f"{ROW}[data-index='1']").wait_for(state="visible", timeout=5000)
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

        # The timeline: a full redraw fits a frame, zoomed in (blocks with text)
        # and zoomed all the way out (20,000 blocks as merged runs)
        editor.tl_ready(waveform=False)
        assert sum(lane["count"] for lane in editor.tl("lanes")) == LARGE_COUNT
        near = editor.tl("benchmark(120)")
        editor.action("timeline-zoom-fit").click()
        page.wait_for_timeout(200)
        far = page.evaluate(f"""() => {{
            const h = {TL};
            const pps = h.view.pps;
            const times = [];
            for (let i = 0; i < 60; i++) {{
                const started = performance.now();
                h.setView(0, pps * (1 + (i % 5) * 0.02));
                document.querySelector('{CANVAS}').getContext('2d').getImageData(0, 0, 1, 1);
                times.push(performance.now() - started);
            }}
            h.setView(0, pps);
            times.sort((a, b) => a - b);
            return {{ median: times[30], p95: times[57], max: times[59], drawn: h.stats.drawn, visible: h.stats.visible }};
        }}""")
        assert far["visible"] == LARGE_COUNT, f"zoomed out, {far['visible']} of {LARGE_COUNT} blocks were looked at"
        assert far["drawn"] < 4000, f"zoomed out, {far['drawn']} rectangles were drawn"
        editor.shot("asr-editor-test-20000-segments-timeline")
        # Frames the browser really presented while the view scrolls through the recording
        presented = page.evaluate(f"""() => new Promise((resolve) => {{
            const h = {TL};
            const times = [];
            let last = 0, n = 0;
            const step = (now) => {{
                if (last) times.push(now - last);
                last = now;
                h.setView((h.duration * n) / 90, 30);
                if (++n <= 90) requestAnimationFrame(step); else {{ times.sort((a, b) => a - b); resolve({{ median: times[45], p95: times[85] }}); }}
            }};
            requestAnimationFrame(step);
        }})""")
        print(f"\n[asr timeline, {LARGE_COUNT} segments] redraw while scrolling and zooming: median {near['median']:.2f} ms, "
              f"p95 {near['p95']:.2f} ms, max {near['max']:.2f} ms ({near['visible']} blocks in view); zoomed out on all of it: "
              f"median {far['median']:.2f} ms, p95 {far['p95']:.2f} ms ({far['drawn']} rectangles for {far['visible']} blocks); "
              f"presented frames while scrolling: median {presented['median']:.1f} ms, p95 {presented['p95']:.1f} ms apart")
        assert near["median"] < FRAME_MS, f"a timeline frame took {near['median']:.1f} ms (median)"
        assert presented["median"] < 100, f"frames were presented {presented['median']:.0f} ms apart while the timeline scrolled"
        assert far["median"] < FRAME_MS, f"a zoomed-out timeline frame took {far['median']:.1f} ms (median)"

        # A drag at this size: one edge, one undo step
        editor.tl("setView(0, 30)")
        before = editor.tl("segments.slice(0, 2)")
        started = time.monotonic()
        editor.tl_drag(editor.tl_point(1.7, lane=0, dx=-2), editor.tl_point(1.2, lane=0, dx=-2), keys=["Control"])
        dragged_in = time.monotonic() - started
        after = editor.tl("segments.slice(0, 2)")
        assert after[0]["end_time"] == pytest.approx(1.2, abs=0.05) and after[1] == before[1]
        assert editor.tl("undoDepth") == 1 and dragged_in < 5, f"an edge drag took {dragged_in:.1f} s"
        editor.action("undo").click()
        assert editor.tl("segments.slice(0, 2)") == before

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
