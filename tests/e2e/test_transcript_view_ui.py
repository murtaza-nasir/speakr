"""Browser test of the transcript on the recording page, with a long transcript.

The recording page renders only the part of a long transcript that is near
the viewport (static/js/modules/composables/transcript-view.js). This test
stores a transcript of 20,000 segments and checks, in a real browser, that
the page stays small and that everything that used to rely on every segment
being in the DOM still works: reaching the middle and the end, the find bar
(Ctrl+F) going to a match far away, a click on a far row seeking the player,
the playing segment being followed, the bubble view, a phone-width layout,
and the segment editor opening over a page that is still rendered. A short
transcript is rendered whole, as before.

The recordings come from seed_recordings.py, which run_speakr.py runs when it
starts the instance; on an instance without them the tests are skipped. The
long transcript is stored through the app's own save route and replaced by
the seeded one afterwards. Its 20,000 segments are spread over the seeded
audio file's 90 seconds, so that a segment far down the list has a time the
player can seek to.

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

from browser_checks import PageProblems, check_common, en, login, open_page, precondition_unmet  # noqa: E402
from seed_recordings import AUDIO_SECONDS, TITLES, meeting_segments  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")

LONG_TITLE, MEETING_TITLE = TITLES
LARGE_COUNT = 20000
# Seconds per segment: the whole transcript fits in the seeded audio file
STEP = AUDIO_SECONDS / (LARGE_COUNT + 2000)
# Whatever the length of the transcript: the segments near the viewport, and
# a page of this size (it was 120,000 elements with every segment rendered).
MAX_SEGMENTS_IN_DOM = 150
MAX_ELEMENTS_IN_PAGE = 4000

LIST = "[data-tv-list]"
SEGMENT = f"{LIST} [data-segment-index]"
FIND = "[data-transcript-find]"

WORDS = "this is a sentence of a long meeting with a few more words in it so that some rows wrap".split()


def long_segments():
    return [
        {"speaker": f"SPEAKER_0{i % 4}", "sentence": f"Segment {i}: " + " ".join(WORDS[: 6 + (i * 7) % 14]) + ".",
         "start_time": round(i * STEP, 4), "end_time": round((i + 1) * STEP, 4)}
        for i in range(LARGE_COUNT)
    ]


# The list's scrolling element, and what of the list is in it
STATE = """() => {
    const list = [...document.querySelectorAll('[data-tv-list]')].find((el) => el.getClientRects().length);
    if (!list) return null;
    let scroller = list.parentElement;
    while (scroller && !/auto|scroll/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement;
    const frame = scroller.getBoundingClientRect();
    const segments = [...list.querySelectorAll('[data-segment-index]')];
    const inView = segments.filter((el) => { const r = el.getBoundingClientRect(); return r.bottom > frame.top + 1 && r.top < frame.bottom - 1; });
    return {
        segments: segments.length,
        elements: document.getElementsByTagName('*').length,
        visible: inView.map((el) => Number(el.dataset.segmentIndex)),
        toEnd: Math.round(scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight),
        scrollTop: scroller.scrollTop,
    };
}"""

SCROLL_TO = """(fraction) => {
    const list = [...document.querySelectorAll('[data-tv-list]')].find((el) => el.getClientRects().length);
    let scroller = list.parentElement;
    while (scroller && !/auto|scroll/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement;
    scroller.scrollTop = (scroller.scrollHeight - scroller.clientHeight) * fraction;
}"""

MEDIA = "document.getElementById('mainPlayerMedia') || document.querySelector('audio, video')"


class Viewer:
    """The signed-in admin with one page, and the transcript on it."""

    def __init__(self, browser, viewport):
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
        """Save a transcript the way the editor does (needs an open app page for the token)."""
        if "/login" in self.page.url or self.page.url == "about:blank" or not self.page.url.startswith(BASE):
            self.page.goto(f"{BASE}/recordings/{recording_id}")
            self.page.wait_for_load_state("load")
        response = self.page.request.post(
            f"{BASE}/recording/{recording_id}/update_transcription",
            headers={"X-CSRFToken": self.csrf(), "Content-Type": "application/json"},
            data=json.dumps({"transcription": json.dumps(segments)}), timeout=120000)
        assert response.ok, f"could not store the transcript: HTTP {response.status}"

    def open(self, recording_id, view="simple", phone=False):
        """Open the recording with its transcript shown. Returns the seconds until the first segment is there."""
        if self.page.url.startswith(BASE):
            self.page.evaluate("(view) => { localStorage.setItem('transcriptionViewMode', view); localStorage.removeItem('followPlayerMode'); }", view)
        self.page.goto("about:blank")
        self.page.wait_for_timeout(300)
        self.problems.reset()
        started = time.monotonic()
        self.page.goto(f"{BASE}/recordings/{recording_id}")
        if phone:
            # On a phone the transcript is a tab of the recording
            tab = re.compile(rf"^\s*{re.escape(en('transcription.title'))}\s*$")
            self.page.locator("button:visible").filter(has_text=tab).first.click()
        self.page.locator(SEGMENT).first.wait_for(state="visible", timeout=60000)
        opened_in = time.monotonic() - started
        self.page.wait_for_timeout(1000)
        return opened_in

    def state(self):
        return self.page.evaluate(STATE)

    def scroll_to(self, fraction):
        self.page.evaluate(SCROLL_TO, fraction)
        self.page.wait_for_timeout(400)

    def segment(self, index):
        return self.page.locator(f"{SEGMENT}[data-segment-index='{index}']")

    def wait_in_view(self, index, timeout=10000):
        """Wait until segment `index` is rendered inside the list's scrolling element."""
        self.page.wait_for_function(f"(index) => {{ const s = ({STATE})(); return !!s && s.visible.includes(index); }}", arg=index, timeout=timeout)

    def render_cost(self, action):
        """Milliseconds of style and layout work the browser did for `action`."""
        def read():
            values = {m["name"]: m["value"] for m in self.cdp.send("Performance.getMetrics")["metrics"]}
            return (values["RecalcStyleDuration"] + values["LayoutDuration"]) * 1000
        before = read()
        action()
        self.page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
        return read() - before

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
def segments():
    return long_segments()


@pytest.fixture(scope="module")
def viewer(browser, segments):
    """The admin, with the long transcript stored for the whole module."""
    session = Viewer(browser, {"width": 1400, "height": 900})
    if set(session.ids) != set(TITLES):
        session.close()
        precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    session.store(session.ids[LONG_TITLE], segments)
    yield session
    session.store(session.ids[LONG_TITLE], meeting_segments())
    session.close()


def assert_small(state, where):
    assert 5 <= state["segments"] <= MAX_SEGMENTS_IN_DOM, f"{where}: {state['segments']} segments in the DOM"
    assert state["elements"] <= MAX_ELEMENTS_IN_PAGE, f"{where}: {state['elements']} elements in the page"


def test_only_the_part_near_the_viewport_is_rendered(viewer, segments):
    page = viewer.page
    opened_in = viewer.open(viewer.ids[LONG_TITLE])
    assert opened_in < 15, f"the transcript took {opened_in:.1f} s to appear"
    top = viewer.state()
    assert_small(top, "at the top")
    assert top["visible"][0] == 0
    assert viewer.segment(0).inner_text().strip() == segments[0]["sentence"]
    # The speaker of a turn is shown with it
    assert "SPEAKER_00" in page.locator(f"{LIST} .speaker-run").first.inner_text()

    # The middle: the rows there, in order, and nothing blank
    viewer.scroll_to(0.5)
    middle = viewer.state()
    assert_small(middle, "in the middle")
    assert middle["visible"] == list(range(middle["visible"][0], middle["visible"][-1] + 1))
    assert LARGE_COUNT * 0.4 < middle["visible"][0] < LARGE_COUNT * 0.6, f"the middle shows segment {middle['visible'][0]}"
    assert len(middle["visible"]) >= 5
    viewer.shot("transcript-view-test-20000-middle")

    # Scrolling by the wheel moves the text by that much: rows that come in
    # above the view, measured only then, do not push it around
    box = page.locator(LIST).first.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, 450)
    for _ in range(12):
        probe = page.evaluate(f"""() => {{
            const s = ({STATE})(); const index = s.visible[Math.floor(s.visible.length / 2)];
            const el = document.querySelector("[data-tv-list] [data-segment-index='" + index + "']");
            window.__probe = el; return el.getBoundingClientRect().top; }}""")
        page.mouse.wheel(0, -200)
        page.wait_for_timeout(150)
        moved = page.evaluate("() => (window.__probe.isConnected ? window.__probe.getBoundingClientRect().top : null)")
        assert moved is not None, "a row in view was removed while scrolling"
        assert abs((moved - probe) - 200) <= 3, f"the text moved {moved - probe:.0f} px for a wheel step of 200"

    # The end
    viewer.scroll_to(1)
    viewer.scroll_to(1)
    end = viewer.state()
    assert_small(end, "at the end")
    assert end["visible"][-1] == LARGE_COUNT - 1
    assert end["toEnd"] <= 2, f"the transcript does not reach its end: {end['toEnd']} px left"
    assert viewer.segment(LARGE_COUNT - 1).inner_text().strip() == segments[-1]["sentence"]
    print(f"\n[transcript view, {LARGE_COUNT} segments] first segment after {opened_in:.2f} s, "
          f"{top['elements']} elements in the page, {top['segments']} segments in the DOM")
    viewer.check("transcript, 20,000 segments")


def test_find_goes_to_a_match_far_away(viewer, segments):
    page = viewer.page
    viewer.open(viewer.ids[LONG_TITLE])
    target = 15000
    assert viewer.segment(target).count() == 0

    # Ctrl+F opens the transcript's own find bar: the browser's sees only the rendered rows
    page.locator(LIST).first.click(position={"x": 5, "y": 5})
    page.keyboard.press("Control+f")
    field = page.locator(f"{FIND} input")
    field.wait_for(state="visible", timeout=5000)
    assert field.evaluate("el => el === document.activeElement")
    field.fill(f"segment {target}:")
    count = page.locator(f"{FIND} .find-replace-bar__count")
    page.wait_for_function("(el) => el.textContent.trim() === '1'", arg=count.element_handle(), timeout=5000)
    page.keyboard.press("Enter")
    viewer.wait_in_view(target)
    assert count.inner_text().strip() == "1 / 1"
    assert viewer.segment(target).inner_text().strip() == segments[target]["sentence"]
    assert_small(viewer.state(), "at the match")
    # The match is marked in the text (CSS highlight API, where the browser has it)
    read_marked = """() => (window.CSS && CSS.highlights && window.Highlight)
        ? [...(CSS.highlights.get('transcript-find-current') || [])].map((range) => range.toString()) : null"""
    if page.evaluate(read_marked) is not None:
        # The mark is set a frame after the row is rendered
        page.wait_for_function(f"() => ({read_marked})().length > 0", timeout=5000)
        marked = page.evaluate(read_marked)
        assert marked == [f"Segment {target}:"], f"marked: {marked}"
    viewer.shot("transcript-view-test-find")

    # A text in many segments: the matches are gone through in order, around the end
    field.fill("a long meeting with a few more words")
    page.wait_for_function("(el) => /^\\d+$/.test(el.textContent.trim()) && Number(el.textContent.trim()) > 1000", arg=count.element_handle(), timeout=5000)
    total = int(count.inner_text().strip())
    page.keyboard.press("Enter")
    page.wait_for_function("(el) => /^\\d+ \\/ \\d+$/.test(el.textContent.trim())", arg=count.element_handle(), timeout=5000)
    first = int(count.inner_text().split("/")[0])
    page.locator(f"{FIND} button[title='{en('findReplace.next')}']").click()
    assert count.inner_text().strip() == f"{first % total + 1} / {total}"
    page.locator(f"{FIND} button[title='{en('findReplace.previous')}']").click()
    assert count.inner_text().strip() == f"{first} / {total}"

    # Nothing found, and Escape closes the bar
    field.fill("no such words anywhere")
    page.wait_for_function(f"() => document.querySelector(\"{FIND} .find-replace-bar__count\").textContent.trim() === {json.dumps(en('findReplace.noMatches'))}", timeout=5000)
    page.keyboard.press("Escape")
    page.locator(FIND).wait_for(state="detached", timeout=5000)
    viewer.check("transcript find")


def test_a_click_on_a_far_row_seeks_and_the_playing_segment_is_followed(viewer, segments):
    page = viewer.page
    viewer.open(viewer.ids[LONG_TITLE])
    page.wait_for_function(f"() => {{ const m = {MEDIA}; return !!m && m.readyState >= 1; }}", timeout=20000)

    # A row three quarters down the transcript
    viewer.scroll_to(0.75)
    visible = viewer.state()["visible"]
    target = visible[len(visible) // 2]
    assert target > LARGE_COUNT * 0.6
    viewer.segment(target).click()
    page.wait_for_function(f"(time) => Math.abs(({MEDIA}).currentTime - time) < 0.05", arg=segments[target]["start_time"], timeout=10000)
    page.evaluate(f"() => ({MEDIA}).pause()")
    # ... is the playing segment
    page.wait_for_function(
        "(index) => [...document.querySelectorAll('[data-tv-list] .active-playing-segment')].some((el) => Number(el.dataset.segmentIndex) === index)",
        arg=target, timeout=10000)

    # Follow the player: the playing segment is brought into view, wherever it is
    page.locator(f"button[title='{en('tooltips.followPlayerDisabled')}']:visible").first.click()
    far = 4000
    assert viewer.segment(far).count() == 0
    page.evaluate(f"(time) => {{ ({MEDIA}).currentTime = time; }}", segments[far]["start_time"] + STEP / 2)
    viewer.wait_in_view(far)
    page.wait_for_function(
        "(index) => { const el = document.querySelector('[data-tv-list] .active-playing-segment'); return !!el && Number(el.dataset.segmentIndex) === index; }",
        arg=far, timeout=10000)
    # ... and stays followed as the player moves on
    page.evaluate(f"(time) => {{ ({MEDIA}).currentTime = time; }}", segments[far + 3]["start_time"] + STEP / 2)
    viewer.wait_in_view(far + 3)
    assert "active-playing-segment" in viewer.segment(far + 3).get_attribute("class")
    assert_small(viewer.state(), "following the player")
    viewer.shot("transcript-view-test-follow")
    page.locator(f"button[title='{en('tooltips.followPlayerEnabled')}']:visible").first.click()
    viewer.check("transcript seek and follow")


def test_the_bubble_view_and_the_way_back(viewer, segments):
    page = viewer.page
    viewer.open(viewer.ids[LONG_TITLE])
    viewer.scroll_to(0.5)
    reading = viewer.state()["visible"][0]

    # The other view shows the place that was being read
    page.locator(f"button[title='{en('tooltips.switchToBubbleView')}']:visible").first.click()
    page.locator(f"{LIST} .speaker-bubble").first.wait_for(state="visible", timeout=10000)
    page.wait_for_timeout(600)
    bubbles = viewer.state()
    assert_small(bubbles, "bubble view")
    assert abs(bubbles["visible"][0] - reading) <= 3, f"the bubble view shows segment {bubbles['visible'][0]}, the simple view showed {reading}"
    index = bubbles["visible"][2]
    assert viewer.segment(index).inner_text().strip() == segments[index]["sentence"]
    viewer.shot("transcript-view-test-bubbles")

    viewer.scroll_to(1)
    viewer.scroll_to(1)
    end = viewer.state()
    assert end["visible"][-1] == LARGE_COUNT - 1
    assert_small(end, "end of the bubble view")

    page.locator(f"button[title='{en('tooltips.switchToSimpleView')}']:visible").first.click()
    page.locator(f"{LIST} .speaker-segment").first.wait_for(state="visible", timeout=10000)
    page.wait_for_timeout(600)
    back = viewer.state()
    assert_small(back, "back in the simple view")
    assert back["visible"][-1] >= LARGE_COUNT - 30
    viewer.check("transcript bubble view")


def test_the_editor_opens_over_a_page_that_is_still_there(viewer, segments):
    """With the transcript windowed, the page stays rendered behind the segment editor and editing stays cheap."""
    page = viewer.page
    viewer.open(viewer.ids[LONG_TITLE])
    page.evaluate("() => Object.keys(localStorage).filter(k => k.startsWith('speakr.asrDraft.')).forEach(k => localStorage.removeItem(k))")
    page.locator(f"button[title='{en('tooltips.editTranscript')}']:visible").first.click()
    row = page.locator(".asr-pane .asr-seg").first
    row.wait_for(state="visible", timeout=60000)
    page.wait_for_timeout(1000)
    assert page.evaluate("() => getComputedStyle(document.querySelector('.main-content')).contentVisibility") == "visible"
    assert viewer.segment(0).count() == 1, "the transcript behind the editor is gone"

    # An edit that adds a row (Enter splits the segment), and scrolling the editor's list
    box = page.locator(".asr-pane .asr-seg[data-index='1'] textarea")
    box.click()
    box.evaluate("el => el.setSelectionRange(10, 10)")
    split = viewer.render_cost(lambda: page.keyboard.press("Enter"))
    scrolled = viewer.render_cost(lambda: page.evaluate("() => { document.querySelector('.asr-list').scrollTop += 2500; }"))
    print(f"\n[asr editor over the transcript, {LARGE_COUNT} segments] style and layout: split {split:.1f} ms, scroll {scrolled:.1f} ms")
    assert split < 100, f"splitting a segment cost {split:.0f} ms of style and layout"
    assert scrolled < 100, f"scrolling the editor cost {scrolled:.0f} ms of style and layout"

    # The edit is not saved: the app's own dialog asks before it is discarded
    page.locator(".asr-editor-modal .modal-close").click()
    page.locator("[data-app-dialog] [data-app-dialog-confirm]").click()
    page.locator(".asr-editor-modal").wait_for(state="detached", timeout=10000)
    viewer.check("editor over the long transcript")


def test_printing_prints_the_whole_transcript(viewer, segments):
    """Only part of the transcript is rendered; for a print the whole text is put on the page."""
    page = viewer.page
    viewer.open(viewer.ids[LONG_TITLE])
    try:
        page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
        page.emulate_media(media="print")
        printed = page.evaluate("""() => {
            const el = document.getElementById('transcript-print');
            const texts = [...el.querySelectorAll('p')].map((p) => p.textContent);
            return { shown: getComputedStyle(el).display, app: getComputedStyle(document.getElementById('app')).display,
                     count: texts.length, first: texts[0], last: texts[texts.length - 1],
                     speakers: el.querySelectorAll('h2').length, title: el.querySelector('h1').textContent,
                     height: document.documentElement.scrollHeight }; }""")
        assert printed["shown"] == "block" and printed["app"] == "none"
        assert printed["count"] == LARGE_COUNT
        assert printed["first"] == segments[0]["sentence"] and printed["last"] == segments[-1]["sentence"]
        assert printed["speakers"] == LARGE_COUNT, "every turn has its speaker"
        assert printed["title"] == LONG_TITLE
        assert printed["height"] > 100000, f"the printed page is {printed['height']} px tall"
    finally:
        page.emulate_media(media="screen")
        page.evaluate("() => window.dispatchEvent(new Event('afterprint'))")
    assert page.locator("#transcript-print").count() == 0
    assert viewer.segment(0).is_visible()
    viewer.check("printing the transcript")


def test_phone_width(browser, viewer, segments):
    phone = Viewer(browser, {"width": 390, "height": 844})
    try:
        phone.open(viewer.ids[LONG_TITLE], phone=True)
        top = phone.state()
        assert_small(top, "phone, at the top")
        assert top["visible"][0] == 0
        phone.scroll_to(1)
        phone.scroll_to(1)
        end = phone.state()
        assert_small(end, "phone, at the end")
        assert end["visible"][-1] == LARGE_COUNT - 1
        assert phone.segment(LARGE_COUNT - 1).inner_text().strip() == segments[-1]["sentence"]
        # The find bar, from its button
        phone.page.locator(f"button[title='{en('findReplace.find')}']:visible").first.click()
        field = phone.page.locator(f"{FIND} input")
        field.wait_for(state="visible", timeout=5000)
        field.fill("segment 7000:")
        field.press("Enter")
        phone.wait_in_view(7000)
        phone.shot("transcript-view-test-phone")
        phone.check("transcript on a phone")
    finally:
        phone.close()


def test_a_short_transcript_is_rendered_whole(viewer):
    page = viewer.page
    viewer.open(viewer.ids[MEETING_TITLE])
    expected = meeting_segments()
    texts = page.eval_on_selector_all(SEGMENT, "els => els.map(e => e.innerText.trim())")
    assert texts == [s["sentence"] for s in expected]
    spacers = page.eval_on_selector_all(f"{LIST} > .tv-spacer", "els => els.map(e => e.offsetHeight)")
    assert spacers == [0, 0]
    # The browser's own find sees all of it: no find button, and Ctrl+F is left alone
    assert page.locator(f"button[title='{en('findReplace.find')}']:visible").count() == 0
    page.locator(LIST).first.click(position={"x": 5, "y": 5})
    page.keyboard.press("Control+f")
    page.wait_for_timeout(300)
    assert page.locator(FIND).count() == 0
    viewer.check("short transcript")


def test_a_plain_text_transcript_with_speaker_lines_is_shown_in_both_views(viewer):
    """Recordings from before the JSON transcript store "[Speaker]: text" lines."""
    page = viewer.page
    recording_id = viewer.ids[MEETING_TITLE]
    lines = ["[Ana]: Good morning.", "Shall we start?", "[Ben]: Yes, with the budget.", "[Ana]: Fine."]
    spoken = ["Good morning. Shall we start?", "Yes, with the budget.", "Fine."]
    try:
        response = page.request.post(
            f"{BASE}/recording/{recording_id}/update_transcription",
            headers={"X-CSRFToken": viewer.csrf(), "Content-Type": "application/json"},
            data=json.dumps({"transcription": "\n".join(lines)}))
        assert response.ok
        viewer.open(recording_id, view="simple")
        assert page.eval_on_selector_all(SEGMENT, "els => els.map(e => e.innerText.trim())") == spoken
        turns = page.eval_on_selector_all(f"{LIST} .speaker-run", "els => els.map(e => e.innerText)")
        assert [name for turn, name in zip(turns, ["Ana", "Ben", "Ana"]) if name in turn] == ["Ana", "Ben", "Ana"]
        viewer.check("plain-text transcript, simple view")
        viewer.open(recording_id, view="bubble")
        assert page.eval_on_selector_all(SEGMENT, "els => els.map(e => e.innerText.trim())") == spoken
        viewer.check("plain-text transcript, bubble view")
    finally:
        viewer.store(recording_id, meeting_segments())
