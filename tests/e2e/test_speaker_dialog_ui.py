"""Browser test of the Identify speakers dialog with a long transcript.

The dialog shows the speakers beside the transcript. Its transcript pane
renders only the rows near its viewport (templates/modals/speaker-modal.html,
composables/speaker-modal.js, with the window of the recording page's
transcript). This test stores a transcript of 20,000 segments and checks
that the dialog opens quickly and stays small, and that everything that
used to rely on every row being in the DOM still works: the rows of the
selected speaker alone and among all rows, the previous and next turn, the
name typed in a field shown on that speaker's rows (cheaply: the style and
layout time of a keystroke is read from the browser), a click on a row
playing the audio from there, the pane's find bar (Ctrl+F) going to a match
far away, a voice-match suggestion, a line given to another speaker, the
"Update the summary with the new names" checkbox and saving. A phone-width
layout and a short transcript, which is rendered whole, are checked too.

The recordings come from seed_recordings.py, which run_speakr.py runs when it
starts the instance; on an instance without them the tests are skipped. The
long transcript is stored through the app's own save route and replaced by
the seeded one afterwards.

Every test fails on a JavaScript error, a same-origin response of 400 or
higher and template text left in the page (browser_checks.py).

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test. With
SPEAKR_E2E_SHOTS set, screenshots are saved in that directory.
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

from browser_checks import PageProblems, check_common, en, login, precondition_unmet  # noqa: E402
from seed_recordings import AUDIO_SECONDS, TITLES, meeting_segments  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")

LONG_TITLE, MEETING_TITLE = TITLES
LARGE_COUNT = 20000
SPEAKERS = 4
STEP = AUDIO_SECONDS / (LARGE_COUNT + 2000)
# Whatever the length of the transcript (every row in the DOM was 180,000 elements)
MAX_ROWS_IN_DOM = 150
MAX_DIALOG_ELEMENTS = 2500
# Seconds until the dialog shows its rows. The target on a desktop is 1 s
# (measured: about 0.2 s; 2.9 s with every row rendered); a loaded CI runner gets room.
MAX_OPEN_SECONDS = 4
# Style and layout per keystroke in a name field, milliseconds (measured: about 1.5)
MAX_KEYSTROKE_MS = 30

DIALOG = ".spk-modal"
PANE = f"{DIALOG} .speaker-modal-transcript"
ROW = f"{PANE} .spk-seg"
FIND = "[data-transcript-find='speakers']"
MEDIA = f"document.querySelector('{DIALOG} audio, {DIALOG} video')"

WORDS = "this is a sentence of a long meeting with a few more words in it so that some rows wrap".split()


def long_segments():
    return [
        {"speaker": f"SPEAKER_0{i % SPEAKERS}", "sentence": f"Segment {i}: " + " ".join(WORDS[: 6 + (i * 7) % 14]) + ".",
         "start_time": round(i * STEP, 4), "end_time": round((i + 1) * STEP, 4)}
        for i in range(LARGE_COUNT)
    ]


STATE = """() => {
    const pane = document.querySelector('.spk-modal .speaker-modal-transcript');
    if (!pane) return null;
    const frame = pane.getBoundingClientRect();
    const rows = [...pane.querySelectorAll('.spk-seg')];
    const inView = rows.filter((el) => { const r = el.getBoundingClientRect(); return r.bottom > frame.top + 1 && r.top < frame.bottom - 1; });
    return {
        rows: rows.length,
        elements: document.querySelector('.spk-modal').getElementsByTagName('*').length,
        visible: inView.map((el) => Number(el.dataset.segmentIndex)),
        speakers: inView.map((el) => el.querySelector('.spk-seg-speaker').textContent.trim()),
        dimmed: inView.map((el) => el.classList.contains('is-dimmed')),
        toEnd: Math.round(pane.scrollHeight - pane.scrollTop - pane.clientHeight),
        scrollTop: pane.scrollTop,
    };
}"""


class Dialog:
    """The signed-in admin with a recording open, and the Identify speakers dialog on it."""

    def __init__(self, browser, viewport):
        self.phone = viewport["width"] < 1024
        self.context = browser.new_context(viewport=viewport, locale="en-US")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.problems.reset()
        listing = self.page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        self.ids = {r["title"]: r["id"] for r in items if r.get("title") in TITLES}
        self.cdp = self.context.new_cdp_session(self.page)
        self.cdp.send("Performance.enable")

    def close(self):
        self.context.close()

    def csrf(self):
        return self.page.locator('meta[name="csrf-token"]').first.get_attribute("content")

    def store(self, recording_id, segments):
        if not self.page.url.startswith(BASE) or "/login" in self.page.url:
            self.page.goto(f"{BASE}/recordings/{recording_id}")
            self.page.wait_for_load_state("load")
        response = self.page.request.post(
            f"{BASE}/recording/{recording_id}/update_transcription",
            headers={"X-CSRFToken": self.csrf(), "Content-Type": "application/json"},
            data=json.dumps({"transcription": json.dumps(segments)}), timeout=120000)
        assert response.ok, f"could not store the transcript: HTTP {response.status}"

    def stored(self, recording_id):
        response = self.page.request.get(f"{BASE}/api/recordings/{recording_id}")
        assert response.ok, f"could not read the recording: HTTP {response.status}"
        return json.loads(response.json()["transcription"])

    def open(self, recording_id, suggestions=None, first_sentence="Segment 0: this is a sentence of a."):
        """Open the recording and the dialog. Returns the seconds from the click until the rows are there."""
        page = self.page
        page.goto("about:blank")
        page.wait_for_timeout(300)
        self.problems.reset()
        page.unroute("**/speakers/suggestions/*")
        if suggestions is not None:
            page.route("**/speakers/suggestions/*", lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps({"success": True, "suggestions": suggestions})))
        page.goto(f"{BASE}/recordings/{recording_id}")
        if self.phone:
            # On a phone the recording's actions are in the sheet behind More
            page.locator("[data-phone-more]").click(timeout=60000)
            page.locator('[data-phone-sheet="actions"] .ph-sheet').wait_for(state="visible", timeout=60000)
        else:
            # The recording of the address is the one shown, with its transcript
            page.wait_for_function(
                "(text) => { const el = document.querySelector(\"[data-tv-list=''] [data-segment-index='0']\"); return !!el && el.innerText.trim() === text; }",
                arg=first_sentence, timeout=60000)
        button = (page.locator('[data-phone-action="speakers"]') if self.phone
                  else page.locator(f"button[title='{en('buttons.identifySpeakers')}']:visible").first)
        button.wait_for(state="visible", timeout=60000)
        page.wait_for_timeout(1000)
        started = time.monotonic()
        button.click()
        if self.phone:
            page.locator(f"{DIALOG} .spk-row").first.wait_for(state="visible", timeout=60000)
        else:
            page.locator(ROW).first.wait_for(state="visible", timeout=60000)
        page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
        opened_in = time.monotonic() - started
        page.wait_for_timeout(600)
        return opened_in

    def state(self):
        return self.page.evaluate(STATE)

    def scroll_to(self, fraction):
        self.page.evaluate("(f) => { const pane = document.querySelector('.spk-modal .speaker-modal-transcript'); pane.scrollTop = (pane.scrollHeight - pane.clientHeight) * f; }", fraction)
        self.page.wait_for_timeout(400)

    def row(self, index):
        return self.page.locator(f"{ROW}[data-segment-index='{index}']")

    def wait_in_view(self, index, timeout=10000):
        self.page.wait_for_function(f"(index) => {{ const s = ({STATE})(); return !!s && s.visible.includes(index); }}", arg=index, timeout=timeout)

    def speaker_row(self, speaker_id):
        return self.page.locator(f"{DIALOG} .spk-row", has=self.page.locator(f".spk-label[title='{speaker_id}']"))

    def name_field(self, speaker_id):
        return self.speaker_row(speaker_id).locator(".spk-name input[type=text]")

    def render_cost(self, action):
        """Milliseconds of style and layout work the browser did for `action`."""
        def read():
            values = {m["name"]: m["value"] for m in self.cdp.send("Performance.getMetrics")["metrics"]}
            return (values["RecalcStyleDuration"] + values["LayoutDuration"]) * 1000
        before = read()
        action()
        self.page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
        return read() - before

    def cancel(self):
        self.page.locator(f"{DIALOG} .modal-close").click()
        self.page.locator(DIALOG).wait_for(state="detached", timeout=10000)

    def check(self, label):
        check_common(self.page, self.problems, label)

    def shot(self, name):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            self.page.wait_for_timeout(300)
            self.page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def segments():
    return long_segments()


@pytest.fixture(scope="module")
def dialog(browser, segments):
    """The admin, with the long transcript stored for the whole module."""
    session = Dialog(browser, {"width": 1400, "height": 900})
    if set(session.ids) != set(TITLES):
        session.close()
        precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    session.store(session.ids[LONG_TITLE], segments)
    yield session
    session.store(session.ids[LONG_TITLE], meeting_segments())
    session.close()


def assert_small(state, where):
    assert 5 <= state["rows"] <= MAX_ROWS_IN_DOM, f"{where}: {state['rows']} rows in the DOM"
    assert state["elements"] <= MAX_DIALOG_ELEMENTS, f"{where}: {state['elements']} elements in the dialog"


def test_the_dialog_opens_quickly_and_renders_only_the_rows_in_view(dialog, segments):
    page = dialog.page
    opened_in = dialog.open(dialog.ids[LONG_TITLE])
    assert opened_in < MAX_OPEN_SECONDS, f"the dialog took {opened_in:.1f} s to open"
    top = dialog.state()
    assert_small(top, "all rows, at the top")
    assert top["visible"][:4] == [0, 1, 2, 3] and top["scrollTop"] == 0
    assert dialog.row(0).locator(".spk-seg-text").inner_text().strip() == segments[0]["sentence"]
    assert page.locator(f"{DIALOG} .spk-row").count() == SPEAKERS
    # The speakers' totals are of the whole transcript, not of the rows rendered
    assert f"{100 // SPEAKERS}%" in dialog.speaker_row("SPEAKER_01").locator(".spk-row-stats").inner_text()

    dialog.scroll_to(0.5)
    middle = dialog.state()
    assert_small(middle, "all rows, in the middle")
    assert middle["visible"] == list(range(middle["visible"][0], middle["visible"][-1] + 1))
    assert LARGE_COUNT * 0.4 < middle["visible"][0] < LARGE_COUNT * 0.6
    dialog.scroll_to(1)
    dialog.scroll_to(1)
    end = dialog.state()
    assert_small(end, "all rows, at the end")
    assert end["visible"][-1] == LARGE_COUNT - 1 and end["toEnd"] <= 2
    assert dialog.row(LARGE_COUNT - 1).locator(".spk-seg-text").inner_text().strip() == segments[-1]["sentence"]
    print(f"\n[speaker dialog, {LARGE_COUNT} segments] open after {opened_in:.2f} s, {top['rows']} rows in the DOM, {top['elements']} elements in the dialog")
    dialog.cancel()
    dialog.check("speaker dialog, 20,000 segments")


def test_a_speakers_rows_alone_among_all_and_turn_by_turn(dialog, segments):
    page = dialog.page
    dialog.open(dialog.ids[LONG_TITLE])
    dialog.scroll_to(0.3)

    # A click on a speaker shows only their rows, from the top
    dialog.speaker_row("SPEAKER_02").click()
    page.wait_for_function(f"() => {{ const s = ({STATE})(); return s.scrollTop === 0 && s.visible[0] === 2; }}", timeout=10000)
    only = dialog.state()
    assert_small(only, "one speaker's rows")
    assert only["visible"] == [2 + SPEAKERS * i for i in range(len(only["visible"]))]
    assert set(only["speakers"]) == {"SPEAKER_02"} and not any(only["dimmed"])
    assert "is-selected" in dialog.speaker_row("SPEAKER_02").get_attribute("class")
    # ... to their last one
    dialog.scroll_to(1)
    dialog.scroll_to(1)
    end = dialog.state()
    assert end["visible"][-1] == LARGE_COUNT - 2 and end["toEnd"] <= 2
    assert_small(end, "one speaker's rows, at the end")

    # All rows, the speaker's lit and the others dimmed, at the speaker's first turn
    page.locator(f"{DIALOG} .spk-filter button", has_text=en("speakerModal.showAll")).click()
    dialog.wait_in_view(2)
    everyone = dialog.state()
    assert_small(everyone, "all rows with one speaker lit")
    assert [index % SPEAKERS != 2 for index in everyone["visible"]] == everyone["dimmed"]
    counter = page.locator(f"{DIALOG} .spk-filter .pill-status")
    turns = LARGE_COUNT // SPEAKERS
    assert counter.inner_text().strip() == f"1 / {turns}"
    dialog.shot("speaker-dialog-test-turns")

    # Next and previous turn; the previous one from the first is the last, far away
    page.locator(f"{DIALOG} .spk-filter button[title='{en('speakerModal.nextTurn')}']").click()
    assert counter.inner_text().strip() == f"2 / {turns}"
    dialog.wait_in_view(6)
    previous = page.locator(f"{DIALOG} .spk-filter button[title='{en('speakerModal.previousTurn')}']")
    previous.click()
    previous.click()
    assert counter.inner_text().strip() == f"{turns} / {turns}"
    dialog.wait_in_view(LARGE_COUNT - 2)
    assert_small(dialog.state(), "at the speaker's last turn")

    # Back to that speaker alone, and no speaker at all
    page.locator(f"{DIALOG} .spk-filter button", has_text=en("speakerModal.onlyThisSpeaker")).click()
    page.wait_for_function(f"() => {{ const s = ({STATE})(); return s.scrollTop === 0 && s.visible[0] === 2 && s.visible[1] === 6; }}", timeout=10000)
    page.locator(f"{DIALOG} .spk-filter button[title='{en('speakerModal.clearSelection')}']").click()
    page.wait_for_function(f"() => {{ const s = ({STATE})(); return s.visible[0] === 0 && s.visible[1] === 1; }}", timeout=10000)
    assert page.locator(f"{DIALOG} .spk-filter").inner_text().strip() == en("speakerModal.selectHint")
    dialog.cancel()
    dialog.check("speaker dialog, one speaker's rows")


def test_a_typed_name_is_shown_on_the_rows_and_typing_is_cheap(dialog):
    page = dialog.page
    dialog.open(dialog.ids[LONG_TITLE])
    field = dialog.name_field("SPEAKER_01")
    # The field's focus selects its speaker
    field.click()
    page.wait_for_function(f"() => {{ const s = ({STATE})(); return s.visible[0] === 1 && s.visible[1] === 5; }}", timeout=10000)
    costs = [dialog.render_cost(lambda ch=ch: page.keyboard.type(ch)) for ch in "Alexandra"]
    assert field.input_value() == "Alexandra"
    assert set(dialog.state()["speakers"]) == {"Alexandra"}
    assert max(costs) < MAX_KEYSTROKE_MS, f"a keystroke cost {max(costs):.0f} ms of style and layout"
    # ... also with every row shown, and on rows that come into view later
    page.locator(f"{DIALOG} .spk-filter button", has_text=en("speakerModal.showAll")).click()
    dialog.wait_in_view(1)
    field.focus()
    field.press("End")
    more = [dialog.render_cost(lambda ch=ch: page.keyboard.type(ch)) for ch in " B"]
    assert field.input_value() == "Alexandra B"
    dialog.scroll_to(0.6)
    far = dialog.state()
    assert {name for index, name in zip(far["visible"], far["speakers"]) if index % SPEAKERS == 1} == {"Alexandra B"}
    assert {name for index, name in zip(far["visible"], far["speakers"]) if index % SPEAKERS == 0} == {"SPEAKER_00"}
    assert max(more) < MAX_KEYSTROKE_MS, f"a keystroke cost {max(more):.0f} ms of style and layout"
    print(f"\n[speaker dialog, {LARGE_COUNT} segments] style and layout per keystroke: "
          f"{sum(costs + more) / len(costs + more):.1f} ms on average, {max(costs + more):.1f} ms at most")
    dialog.shot("speaker-dialog-test-name")
    dialog.cancel()
    dialog.check("speaker dialog, typing a name")


def test_a_click_on_a_row_plays_from_there(dialog, segments):
    page = dialog.page
    dialog.open(dialog.ids[LONG_TITLE])
    page.wait_for_function(f"() => {{ const m = {MEDIA}; return !!m && m.readyState >= 1; }}", timeout=20000)
    dialog.scroll_to(0.7)
    visible = dialog.state()["visible"]
    target = visible[len(visible) // 2]
    assert target > LARGE_COUNT * 0.6
    dialog.row(target).locator(".spk-seg-text").click()
    page.wait_for_function(f"(time) => Math.abs(({MEDIA}).currentTime - time) < 0.5 && !({MEDIA}).paused", arg=segments[target]["start_time"], timeout=10000)
    page.evaluate(f"() => ({MEDIA}).pause()")
    # The time of a row plays it too, and the playing row is marked
    other = visible[len(visible) // 2 - 2]
    dialog.row(other).locator(".spk-seg-time").click()
    page.wait_for_function(f"(time) => Math.abs(({MEDIA}).currentTime - time) < 0.5", arg=segments[other]["start_time"], timeout=10000)
    page.evaluate(f"() => ({MEDIA}).pause()")
    page.evaluate(f"(time) => {{ ({MEDIA}).currentTime = time; }}", segments[other]["start_time"] + STEP / 2)
    page.wait_for_function("(index) => { const el = document.querySelector('.spk-modal .spk-seg.is-playing'); return !!el && Number(el.dataset.segmentIndex) === index; }", arg=other, timeout=10000)
    # A speaker's sample plays their longest segment
    dialog.speaker_row("SPEAKER_03").locator(f"button[title='{en('speakerModal.playSample')}']").click()
    page.wait_for_function(f"() => !({MEDIA}).paused || ({MEDIA}).currentTime > 0", timeout=10000)
    page.evaluate(f"() => ({MEDIA}).pause()")
    dialog.cancel()
    dialog.check("speaker dialog, playing a row")


def test_find_in_the_dialog_goes_to_a_match_far_away(dialog, segments):
    page = dialog.page
    dialog.open(dialog.ids[LONG_TITLE])
    target = 15001
    assert dialog.row(target).count() == 0
    # Ctrl+F opens the pane's find bar, not the one of the page behind the dialog
    page.locator(PANE).click(position={"x": 5, "y": 5})
    page.keyboard.press("Control+f")
    field = page.locator(f"{FIND} input")
    field.wait_for(state="visible", timeout=5000)
    assert field.evaluate("el => el === document.activeElement")
    assert page.locator("[data-transcript-find='']").count() == 0
    field.fill(f"segment {target}:")
    count = page.locator(f"{FIND} .find-replace-bar__count")
    page.wait_for_function("(el) => el.textContent.trim() === '1'", arg=count.element_handle(), timeout=5000)
    page.keyboard.press("Enter")
    dialog.wait_in_view(target)
    assert count.inner_text().strip() == "1 / 1"
    assert_small(dialog.state(), "at the match")
    if page.evaluate("() => !!(window.CSS && CSS.highlights && window.Highlight)"):
        page.wait_for_function(
            "(text) => [...(CSS.highlights.get('transcript-find-speakers-current') || [])].map((range) => range.toString()).join('|') === text",
            arg=f"Segment {target}:", timeout=5000)
    dialog.shot("speaker-dialog-test-find")

    # With one speaker's rows shown, the search is among those rows
    dialog.speaker_row("SPEAKER_02").click()
    page.wait_for_function(f"() => {{ const s = ({STATE})(); return s.visible[0] === 2; }}", timeout=10000)
    page.wait_for_function("(el) => el.textContent.trim() === '" + en("findReplace.noMatches") + "'", arg=count.element_handle(), timeout=5000)
    field.fill("segment 15002:")
    page.wait_for_function("(el) => el.textContent.trim() === '1'", arg=count.element_handle(), timeout=5000)
    field.press("Enter")
    dialog.wait_in_view(15002)
    assert set(dialog.state()["speakers"]) == {"SPEAKER_02"}

    # Escape closes the bar and leaves the dialog open
    field.press("Escape")
    page.locator(FIND).wait_for(state="detached", timeout=5000)
    assert page.locator(DIALOG).is_visible()
    find_button = page.locator(f"{DIALOG} .spk-find")
    assert find_button.get_attribute("title") == en("findReplace.find")
    find_button.click()
    page.locator(f"{FIND} input").wait_for(state="visible", timeout=5000)
    find_button.click()
    page.locator(FIND).wait_for(state="detached", timeout=5000)
    dialog.cancel()
    dialog.check("speaker dialog, find")


def test_a_voice_match_a_changed_line_and_the_save(dialog, segments):
    """A suggested name is taken, one line far down goes to another speaker, and the save
    carries both. The summary checkbox is on; the save that asks for the summary is
    intercepted and reaches the server without that request (this instance has no text model)."""
    page = dialog.page
    recording_id = dialog.ids[LONG_TITLE]
    suggestions = {"SPEAKER_00": [{"speaker_id": 1, "name": "Morgan Reed", "similarity": 91.4}]}
    dialog.open(recording_id, suggestions=suggestions)

    # The voice match is offered in the field of its speaker and fills it
    pill = dialog.speaker_row("SPEAKER_00").locator(".spk-voice-pill")
    pill.wait_for(state="visible", timeout=10000)
    assert "Morgan Reed" in pill.inner_text()
    assert dialog.speaker_row("SPEAKER_01").locator(".spk-voice-pill").count() == 0
    pill.click()
    assert dialog.name_field("SPEAKER_00").input_value() == "Morgan Reed"
    assert dialog.state()["speakers"][0] == "Morgan Reed"

    # A line far down is given to another speaker
    dialog.scroll_to(0.5)
    visible = dialog.state()["visible"]
    line = next(index for index in visible[2:] if index % SPEAKERS == 1)
    row = dialog.row(line)
    row.hover()
    row.locator(f"button[title='{en('speakerModal.changeSpeaker')}']").click()
    row.locator(".spk-seg-reassign select").select_option("SPEAKER_03")
    page.wait_for_function("(index) => document.querySelector(\".spk-modal .spk-seg[data-segment-index='\" + index + \"'] .spk-seg-speaker\").textContent.trim() === 'SPEAKER_03'", arg=line, timeout=10000)
    assert line in dialog.state()["visible"], "the pane moved when a line changed its speaker"

    # The checkbox, with its own text
    label = page.locator(f"{DIALOG} label", has_text=en("help.regenerateSummaryAfterNames"))
    assert label.locator("input[type=checkbox]").is_checked()

    sent = []

    def save(route):
        body = route.request.post_data_json
        sent.append(body)
        route.continue_(post_data=json.dumps({**body, "regenerate_summary": False}))

    page.route(re.compile(rf"/recording/{recording_id}/update_(speakers|transcript)$"), save)
    try:
        page.locator(f"{DIALOG} .modal-footer button", has_text=en("buttons.saveNames")).click()
        page.locator(DIALOG).wait_for(state="detached", timeout=60000)
    finally:
        page.unroute(re.compile(rf"/recording/{recording_id}/update_(speakers|transcript)$"))
    assert len(sent) == 1 and sent[0]["regenerate_summary"] is True
    stored = dialog.stored(recording_id)
    assert len(stored) == LARGE_COUNT
    assert stored[0]["speaker"] == "Morgan Reed" and stored[4]["speaker"] == "Morgan Reed"
    assert stored[line]["speaker"] == "SPEAKER_03" and stored[line]["sentence"] == segments[line]["sentence"]
    assert stored[line + SPEAKERS]["speaker"] == "SPEAKER_01"
    # The page behind the dialog shows the name
    page.wait_for_function("() => { const el = document.querySelector(\"[data-tv-list=''] .speaker-run\"); return !!el && el.innerText.includes('Morgan Reed'); }", timeout=20000)
    dialog.check("speaker dialog, voice match and save")
    dialog.store(recording_id, segments)


def test_phone_width(browser, dialog, segments):
    phone = Dialog(browser, {"width": 390, "height": 844})
    try:
        page = phone.page
        phone.open(dialog.ids[LONG_TITLE])
        # The speakers first; a tap on one opens their rows on the other tab
        assert page.locator(f"{DIALOG} .spk-row").count() == SPEAKERS
        phone.speaker_row("SPEAKER_03").locator(".spk-row-head").click()
        page.locator(ROW).first.wait_for(state="visible", timeout=10000)
        page.wait_for_function(f"() => {{ const s = ({STATE})(); return !!s && s.visible[0] === 3 && s.visible[1] === 7; }}", timeout=10000)
        top = phone.state()
        assert_small(top, "phone, one speaker's rows")
        phone.scroll_to(1)
        phone.scroll_to(1)
        end = phone.state()
        assert end["visible"][-1] == LARGE_COUNT - 1 and end["toEnd"] <= 2
        assert_small(end, "phone, at the end")
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        phone.shot("speaker-dialog-test-phone")
        phone.cancel()
        phone.check("speaker dialog on a phone")
    finally:
        phone.close()


def test_a_short_transcript_is_rendered_whole(dialog):
    page = dialog.page
    expected = meeting_segments()
    dialog.open(dialog.ids[MEETING_TITLE], first_sentence=expected[0]["sentence"])
    texts = page.eval_on_selector_all(f"{ROW} .spk-seg-text", "els => els.map(e => e.innerText.trim())")
    assert texts == [s["sentence"] for s in expected]
    assert page.eval_on_selector_all(f"{PANE} .tv-spacer", "els => els.map(e => e.offsetHeight)") == [0, 0]
    # The browser's own find sees all of it: no find button, and Ctrl+F is left alone
    assert page.locator(f"{DIALOG} .spk-find").count() == 0
    page.locator(PANE).click(position={"x": 5, "y": 5})
    page.keyboard.press("Control+f")
    page.wait_for_timeout(300)
    assert page.locator("[data-transcript-find]").count() == 0
    dialog.shot("speaker-dialog-test-short")
    dialog.cancel()
    dialog.check("speaker dialog, short transcript")
