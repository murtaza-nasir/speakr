"""Browser test of the public share page (/share/<public_id>).

The page a person without an account opens from a public link shows the
transcript the way the recording page does: only the part of a long
transcript near the viewport is rendered (templates/share.html,
static/js/share-page.js, composables/transcript-view.js). This test shares a
recording with a transcript of 20,000 segments and opens the link in a
browser that is not signed in. It checks that the page stays small at the
top, in the middle and at the end, that the find bar (Ctrl+F and its button)
goes to a match far away, that a click on a line plays the audio from there,
that the playing line is followed until the reader scrolls and again after
"Follow", the single view, copying, printing the whole transcript, a phone
width in light and dark, and that the page receives nothing beyond what it
shows. A short transcript is rendered whole. Text in shared notes that looks
like a template expression stays text.

The recordings come from seed_recordings.py, which run_speakr.py runs when it
starts the instance; on an instance without them the tests are skipped. The
link is created through the app's own route as the seeded admin and removed
afterwards, and the long transcript is replaced by the seeded one. Its
20,000 segments are spread over the seeded audio file's 90 seconds, so a
segment far down the list has a time the player can seek to.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test. With
SPEAKR_E2E_SHOTS set, screenshots are saved in that directory.
"""

import json
import os
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
# Seconds per segment: the whole transcript fits in the seeded audio file
STEP = AUDIO_SECONDS / (LARGE_COUNT + 2000)
# Whatever the length of the transcript (every segment in the DOM was 60,000
# elements for the transcript alone)
MAX_SEGMENTS_IN_DOM = 150
MAX_TRANSCRIPT_ELEMENTS = 2000
MAX_ELEMENTS_IN_PAGE = 3000
# Seconds until the first row is there. The target on a desktop is 1.5 s
# (measured: about 0.4 s); a loaded CI runner gets room.
MAX_OPEN_SECONDS = 5

LIST = "[data-tv-list]"
SEGMENT = f"{LIST} [data-segment-index]"
FIND = "[data-transcript-find]"
BOX = ".transcription-box"
MEDIA = "document.querySelector('audio, video')"
# What the page may receive (src/api/shares.py, view_shared_recording)
DATA_KEYS = {"public_id", "title", "transcript", "summary", "summary_raw", "notes", "notes_raw",
             "meeting_date", "mime_type", "audio_deleted_at", "audio_duration"}
NOTES = "Shared notes. A probe: ${ 7 * 7 } and ${ recording.public_id }."

WORDS = "this is a sentence of a long meeting with a few more words in it so that some rows wrap".split()


def long_segments():
    return [
        {"speaker": f"SPEAKER_0{i % 4}", "sentence": f"Segment {i}: " + " ".join(WORDS[: 6 + (i * 7) % 14]) + ".",
         "start_time": round(i * STEP, 4), "end_time": round((i + 1) * STEP, 4)}
        for i in range(LARGE_COUNT)
    ]


STATE = """() => {
    const list = [...document.querySelectorAll('[data-tv-list]')].find((el) => el.getClientRects().length);
    const box = document.querySelector('.transcription-box');
    if (!list || !box) return null;
    const frame = box.getBoundingClientRect();
    const segments = [...list.querySelectorAll('[data-segment-index]')];
    const inView = segments.filter((el) => { const r = el.getBoundingClientRect(); return r.bottom > frame.top + 1 && r.top < frame.bottom - 1; });
    return {
        segments: segments.length,
        transcript: box.getElementsByTagName('*').length,
        elements: document.getElementsByTagName('*').length,
        visible: inView.map((el) => Number(el.dataset.segmentIndex)),
        toEnd: Math.round(box.scrollHeight - box.scrollTop - box.clientHeight),
        scrollTop: box.scrollTop,
    };
}"""


class Owner:
    """The signed-in admin: stores transcripts and makes the public links."""

    def __init__(self, browser):
        self.context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        self.page = self.context.new_page()
        login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        listing = self.page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        self.ids = {r["title"]: r["id"] for r in items if r.get("title") in TITLES}
        self.shares = []
        if self.ids:
            self.page.goto(f"{BASE}/recordings/{next(iter(self.ids.values()))}")
            self.page.wait_for_load_state("load")

    def headers(self):
        token = self.page.locator('meta[name="csrf-token"]').first.get_attribute("content")
        return {"X-CSRFToken": token, "Content-Type": "application/json"}

    def post(self, path, body, headers=None, timeout=120000):
        return self.page.request.post(f"{BASE}{path}", headers={**self.headers(), **(headers or {})},
                                      data=json.dumps(body), timeout=timeout)

    def store(self, recording_id, segments):
        response = self.post(f"/recording/{recording_id}/update_transcription", {"transcription": json.dumps(segments)})
        assert response.ok, f"could not store the transcript: HTTP {response.status}"

    def notes(self, recording_id, text):
        response = self.post("/save", {"id": recording_id, "notes": text})
        assert response.ok, f"could not store the notes: HTTP {response.status}"

    def share(self, recording_id, notes=True):
        """A new public link; returns its path. A link is made only over https,
        which a proxy reports this way."""
        https = {"X-Forwarded-Proto": "https", "Referer": BASE.replace("http://", "https://") + "/"}
        response = self.post(f"/api/recording/{recording_id}/share",
                             {"share_summary": True, "share_notes": notes, "force_new": True}, headers=https)
        if response.status == 403:
            precondition_unmet(f"public links cannot be made on this instance: {response.text()[:200]}")
        assert response.ok, f"could not share: HTTP {response.status} {response.text()[:200]}"
        body = response.json()
        self.shares.append(body["share"]["id"])
        return "/share/" + body["share_url"].rsplit("/share/", 1)[1]

    def close(self):
        for share_id in self.shares:
            self.page.request.delete(f"{BASE}/api/share/{share_id}", headers=self.headers())
        self.context.close()


class Visitor:
    """A browser that is not signed in, with the shared page."""

    def __init__(self, browser, viewport=None, scheme="light", phone=False):
        self.context = browser.new_context(viewport=viewport or {"width": 1400, "height": 900}, locale="en-US",
                                           color_scheme=scheme, is_mobile=phone, has_touch=phone,
                                           permissions=["clipboard-read", "clipboard-write"])
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        self.requests = []
        self.page.on("request", lambda request: self.requests.append(request.url))
        self.cdp = self.context.new_cdp_session(self.page)
        self.cdp.send("Performance.enable")

    def close(self):
        self.context.close()

    def open(self, path, wait_for=SEGMENT):
        """Open the link. Returns the response and the seconds until the first segment is there."""
        self.page.goto("about:blank")
        self.problems.reset()
        del self.requests[:]
        started = time.monotonic()
        response = self.page.goto(f"{BASE}{path}", wait_until="commit")
        self.page.locator(wait_for).first.wait_for(state="visible", timeout=60000)
        opened_in = time.monotonic() - started
        self.page.wait_for_load_state("load")
        self.page.wait_for_timeout(800)
        return response, opened_in

    def state(self):
        return self.page.evaluate(STATE)

    def scroll_to(self, fraction):
        self.page.evaluate("(f) => { const box = document.querySelector('.transcription-box'); box.scrollTop = (box.scrollHeight - box.clientHeight) * f; }", fraction)
        self.page.wait_for_timeout(400)

    def segment(self, index):
        return self.page.locator(f"{SEGMENT}[data-segment-index='{index}']")

    def wait_in_view(self, index, timeout=10000):
        self.page.wait_for_function(f"(index) => {{ const s = ({STATE})(); return !!s && s.visible.includes(index); }}", arg=index, timeout=timeout)

    def playing(self):
        """Index of the line marked as playing, or None."""
        return self.page.evaluate("""() => { const el = document.querySelector('[data-tv-list] .active-playing-segment');
            return el ? Number(el.dataset.segmentIndex) : null; }""")

    def seek(self, seconds):
        self.page.evaluate(f"(time) => {{ ({MEDIA}).currentTime = time; }}", seconds)

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
def owner(browser):
    session = Owner(browser)
    if set(session.ids) != set(TITLES):
        session.close()
        precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    yield session
    session.close()


@pytest.fixture(scope="module")
def long_link(owner, segments):
    """The public link of the recording with 20,000 segments."""
    recording_id = owner.ids[LONG_TITLE]
    owner.store(recording_id, segments)
    yield owner.share(recording_id)
    owner.store(recording_id, meeting_segments())


@pytest.fixture(scope="module")
def visitor(browser):
    session = Visitor(browser)
    yield session
    session.close()


def assert_small(state, where):
    assert 5 <= state["segments"] <= MAX_SEGMENTS_IN_DOM, f"{where}: {state['segments']} segments in the DOM"
    assert state["transcript"] <= MAX_TRANSCRIPT_ELEMENTS, f"{where}: {state['transcript']} elements in the transcript"
    assert state["elements"] <= MAX_ELEMENTS_IN_PAGE, f"{where}: {state['elements']} elements in the page"


def test_only_the_part_near_the_viewport_is_rendered(visitor, long_link, segments):
    page = visitor.page
    response, opened_in = visitor.open(long_link)
    assert response.status == 200
    assert opened_in < MAX_OPEN_SECONDS, f"the transcript took {opened_in:.1f} s to appear"
    top = visitor.state()
    assert_small(top, "at the top")
    assert top["visible"][0] == 0 and top["scrollTop"] == 0
    assert visitor.segment(0).inner_text().strip() == segments[0]["sentence"]
    # The speaker of a turn is shown with it, in the colors of the recording page
    turn = page.locator(f"{LIST} .speaker-run").first
    assert "SPEAKER_00" in turn.inner_text()
    assert "speaker-color-1" in turn.locator(".speaker-tablet").get_attribute("class")
    visitor.shot("share-page-test-20000-top")

    # The middle: the rows there, in order, and nothing blank
    visitor.scroll_to(0.5)
    middle = visitor.state()
    assert_small(middle, "in the middle")
    assert middle["visible"] == list(range(middle["visible"][0], middle["visible"][-1] + 1))
    assert LARGE_COUNT * 0.4 < middle["visible"][0] < LARGE_COUNT * 0.6, f"the middle shows segment {middle['visible'][0]}"

    # Scrolling by the wheel moves the text by that much, and costs little
    box = page.locator(BOX).bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(8):
        probe = page.evaluate(f"""() => {{
            const s = ({STATE})(); const index = s.visible[Math.floor(s.visible.length / 2)];
            const el = document.querySelector("[data-tv-list] [data-segment-index='" + index + "']");
            window.__probe = el; return el.getBoundingClientRect().top; }}""")
        page.mouse.wheel(0, -200)
        page.wait_for_timeout(150)
        moved = page.evaluate("() => (window.__probe.isConnected ? window.__probe.getBoundingClientRect().top : null)")
        assert moved is not None, "a row in view was removed while scrolling"
        assert abs((moved - probe) - 200) <= 3, f"the text moved {moved - probe:.0f} px for a wheel step of 200"
    scrolled = visitor.render_cost(lambda: page.evaluate("() => { document.querySelector('.transcription-box').scrollTop += 3000; }"))
    assert scrolled < 100, f"scrolling cost {scrolled:.0f} ms of style and layout"

    # The end
    visitor.scroll_to(1)
    visitor.scroll_to(1)
    end = visitor.state()
    assert_small(end, "at the end")
    assert end["visible"][-1] == LARGE_COUNT - 1
    assert end["toEnd"] <= 2, f"the transcript does not reach its end: {end['toEnd']} px left"
    assert visitor.segment(LARGE_COUNT - 1).inner_text().strip() == segments[-1]["sentence"]
    print(f"\n[share page, {LARGE_COUNT} segments] first segment after {opened_in:.2f} s, {top['elements']} elements in the page, "
          f"{top['transcript']} in the transcript, {top['segments']} segments in the DOM, a scroll of 3000 px: {scrolled:.1f} ms of style and layout")
    visitor.check("share page, 20,000 segments")


def test_the_page_needs_no_account_and_receives_only_what_it_shows(visitor, long_link, segments):
    page = visitor.page
    response, _ = visitor.open(long_link)
    headers = response.headers
    # Not for search engines, and under the app's content security policy
    assert "noindex" in headers.get("x-robots-tag", "")
    assert "noindex" in page.locator('meta[name="robots"]').get_attribute("content")
    assert "default-src 'self'" in headers.get("content-security-policy", "")
    # Nothing was asked of the app but the page, its files and the audio
    paths = sorted({url[len(BASE):].split("?")[0] for url in visitor.requests if url.startswith(BASE)})
    other = [p for p in paths if not (p.startswith("/static/") or p == long_link or p.startswith("/share/audio/"))]
    assert not other, f"requests beyond the page, its files and the audio: {other}"
    assert all(url.startswith(BASE) or url.startswith("data:") or url.startswith("blob:") for url in visitor.requests)
    assert "/login" not in page.url
    assert page.evaluate("() => navigator.serviceWorker.getRegistrations().then((r) => r.length)") == 0

    data = json.loads(page.locator("#share-data").inner_text())
    assert set(data) == DATA_KEYS, f"the page received other fields: {sorted(set(data) ^ DATA_KEYS)}"
    transcript = data["transcript"]
    assert set(transcript) == {"speakers", "segments"}
    assert transcript["speakers"] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03"]
    assert len(transcript["segments"]) == LARGE_COUNT
    assert transcript["segments"][5] == [1, segments[5]["start_time"], segments[5]["end_time"], segments[5]["sentence"]]
    assert all(len(row) == 4 for row in transcript["segments"])
    html = page.request.get(f"{BASE}{long_link}").text()
    for private in (ADMIN_EMAIL, "csrf", "user_id", "owner"):
        assert private not in html.split('id="share-data"', 1)[1].split("</script>", 1)[0], f"{private!r} in the page's data"
    size = len(html.encode())
    assert size < 2_500_000, f"the page is {size} bytes for {LARGE_COUNT} segments"
    print(f"\n[share page, {LARGE_COUNT} segments] the page is {size} bytes")
    visitor.check("share page without an account")


def test_find_goes_to_a_match_far_away(visitor, long_link, segments):
    page = visitor.page
    visitor.open(long_link)
    target = 15000
    assert visitor.segment(target).count() == 0

    # Ctrl+F opens the transcript's own find bar: the browser's sees only the rendered rows
    page.locator(BOX).click(position={"x": 5, "y": 5})
    page.keyboard.press("Control+f")
    field = page.locator(f"{FIND} input")
    field.wait_for(state="visible", timeout=5000)
    assert field.evaluate("el => el === document.activeElement")
    field.fill(f"segment {target}:")
    count = page.locator(f"{FIND} .find-replace-bar__count")
    page.wait_for_function("(el) => el.textContent.trim() === '1'", arg=count.element_handle(), timeout=5000)
    page.keyboard.press("Enter")
    visitor.wait_in_view(target)
    assert count.inner_text().strip() == "1 / 1"
    assert visitor.segment(target).inner_text().strip() == segments[target]["sentence"]
    assert_small(visitor.state(), "at the match")
    # The match is marked in the text (CSS highlight API, where the browser has it)
    if page.evaluate("() => !!(window.CSS && CSS.highlights && window.Highlight)"):
        page.wait_for_function(
            "(text) => [...(CSS.highlights.get('transcript-find-current') || [])].map((range) => range.toString()).join('|') === text",
            arg=f"Segment {target}:", timeout=5000)
    visitor.shot("share-page-test-find")

    # A text in many segments: the matches are gone through in order
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

    # Escape closes the bar; the button opens it again
    page.keyboard.press("Escape")
    page.locator(FIND).wait_for(state="detached", timeout=5000)
    button = page.locator("[data-share-find]")
    assert button.is_visible() and button.get_attribute("title") == en("findReplace.find")
    button.click()
    page.locator(f"{FIND} input").wait_for(state="visible", timeout=5000)
    button.click()
    page.locator(FIND).wait_for(state="detached", timeout=5000)
    visitor.check("share page find")


def test_a_click_plays_from_a_line_and_the_playing_line_is_followed(visitor, long_link, segments):
    page = visitor.page
    visitor.open(long_link)
    page.wait_for_function(f"() => {{ const m = {MEDIA}; return !!m && m.readyState >= 1; }}", timeout=20000)
    assert visitor.playing() is None, "a line is marked before anything was played"
    follow = page.locator("[data-share-follow]")
    assert follow.count() == 0

    # A row three quarters down the transcript: a click plays the audio from there
    visitor.scroll_to(0.75)
    visible = visitor.state()["visible"]
    target = visible[len(visible) // 2]
    assert target > LARGE_COUNT * 0.6
    visitor.segment(target).click()
    page.wait_for_function(f"(time) => Math.abs(({MEDIA}).currentTime - time) < 0.5 && !({MEDIA}).paused", arg=segments[target]["start_time"], timeout=10000)
    page.evaluate(f"() => ({MEDIA}).pause()")
    assert follow.count() == 0, "the page stopped following after a click on a line"

    # The playing line is brought into view, wherever it is, and stays followed
    far = 4000
    assert visitor.segment(far).count() == 0
    visitor.seek(segments[far]["start_time"] + STEP / 2)
    visitor.wait_in_view(far)
    page.wait_for_function("(index) => { const el = document.querySelector('[data-tv-list] .active-playing-segment'); return !!el && Number(el.dataset.segmentIndex) === index; }", arg=far, timeout=10000)
    visitor.seek(segments[far + 3]["start_time"] + STEP / 2)
    visitor.wait_in_view(far + 3)
    assert visitor.playing() == far + 3
    assert_small(visitor.state(), "following the player")
    visitor.shot("share-page-test-follow")
    page.wait_for_timeout(800)   # the smooth scroll to that line has ended

    # The reader scrolls away: the page stops following and offers "Follow"
    box = page.locator(BOX).bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 4000)
    follow.wait_for(state="visible", timeout=5000)
    assert follow.inner_text().strip() == en("sharePage.follow")
    page.wait_for_timeout(500)
    reading = visitor.state()
    assert far + 3 not in reading["visible"]
    later = 9000
    visitor.seek(segments[later]["start_time"] + STEP / 2)
    page.wait_for_function(f"(time) => Math.abs(({MEDIA}).currentTime - time) < 0.5", arg=segments[later]["start_time"], timeout=10000)
    page.wait_for_timeout(700)
    stayed = visitor.state()
    assert abs(stayed["scrollTop"] - reading["scrollTop"]) <= 2, "the transcript moved under a reader who had scrolled away"
    assert later not in stayed["visible"]
    visitor.shot("share-page-test-follow-paused")

    # "Follow" goes to the playing line and follows again
    follow.click()
    visitor.wait_in_view(later)
    assert visitor.playing() == later
    assert follow.count() == 0
    visitor.seek(segments[later + 2]["start_time"] + STEP / 2)
    visitor.wait_in_view(later + 2)

    # A key that moves the transcript stops it as well
    page.locator(BOX).focus()
    page.keyboard.press("PageDown")
    follow.wait_for(state="visible", timeout=5000)
    visitor.check("share page seek and follow")


def test_one_view_copy_and_summary(visitor, long_link, segments):
    page = visitor.page
    visitor.open(long_link)

    # The speaker turns are the only view: no view switch, no bubbles
    assert page.locator(".view-mode-toggle").count() == 0
    assert page.locator(".speaker-bubble, .speaker-legend").count() == 0

    # Every button of the controls row has one height, with find closed and open
    heights = """() => [...document.querySelectorAll('.share-controls button')]
        .filter(b => b.offsetParent).map(b => Math.round(b.getBoundingClientRect().height))"""
    closed = page.evaluate(heights)
    assert closed and len(set(closed)) == 1, closed
    page.locator("[data-share-find]").click()
    page.locator(f"{FIND} input").wait_for(state="visible", timeout=5000)
    opened = page.evaluate(heights)
    assert len(set(opened)) == 1 and opened[0] == closed[0], opened
    # It opens in the controls row, not in a row of its own
    assert page.locator(f".share-controls {FIND}").count() == 1
    visitor.shot("share-page-test-controls-find")
    page.keyboard.press("Escape")
    page.locator(FIND).wait_for(state="detached", timeout=5000)

    # Copy puts the whole transcript on the clipboard
    page.locator("[data-share-copy]").click()
    page.locator("#toastContainer .toast-message").first.wait_for(state="visible", timeout=5000)
    copied = page.evaluate("() => navigator.clipboard.readText()")
    lines = copied.split("\n")
    assert len(lines) == LARGE_COUNT
    assert lines[0] == f"[SPEAKER_00]: {segments[0]['sentence']}" and lines[-1] == f"[SPEAKER_03]: {segments[-1]['sentence']}"

    # The summary is on its tab
    page.locator("button", has_text=en("summary.title")).first.click()
    summary = page.locator(".summary-box")
    summary.wait_for(state="visible", timeout=5000)
    assert "seeded for the browser tests" in summary.inner_text()
    assert not page.locator(BOX).is_visible()
    page.locator("button", has_text=en("transcription.title")).first.click()
    page.locator(SEGMENT).first.wait_for(state="visible", timeout=5000)
    assert_small(visitor.state(), "back on the transcript tab")
    visitor.check("share page view, copy and summary")


def test_printing_prints_the_whole_transcript(visitor, long_link, segments):
    page = visitor.page
    visitor.open(long_link)
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
    assert visitor.segment(0).is_visible()
    visitor.check("printing the shared transcript")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_phone_width(browser, long_link, segments, scheme):
    phone = Visitor(browser, viewport={"width": 390, "height": 844}, scheme=scheme, phone=True)
    try:
        page = phone.page
        phone.open(long_link)
        top = phone.state()
        assert_small(top, "phone, at the top")
        assert top["visible"][0] == 0
        # The mode follows the system; nothing is wider than the screen
        assert page.evaluate("() => document.documentElement.classList.contains('dark')") == (scheme == "dark")
        # ... and the page is dark or light with it (the color read back from a canvas, whatever its notation)
        channels = page.evaluate("""() => { const c = document.createElement('canvas'); c.width = c.height = 1;
            const g = c.getContext('2d'); g.fillStyle = getComputedStyle(document.body).backgroundColor; g.fillRect(0, 0, 1, 1);
            return [...g.getImageData(0, 0, 1, 1).data].slice(0, 3); }""")
        assert (sum(channels) < 200) == (scheme == "dark"), f"{scheme}: the page background is rgb{tuple(channels)}"
        text = page.evaluate("""() => { const c = document.createElement('canvas'); c.width = c.height = 1;
            const g = c.getContext('2d'); g.fillStyle = getComputedStyle(document.querySelector('[data-tv-list] .speaker-text')).color; g.fillRect(0, 0, 1, 1);
            return [...g.getImageData(0, 0, 1, 1).data].slice(0, 3); }""")
        assert abs(sum(text) - sum(channels)) > 250, f"{scheme}: text rgb{tuple(text)} on rgb{tuple(channels)}"
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        overflowing = page.evaluate("""() => [...document.querySelectorAll('#app *')].filter((el) => el.getClientRects().length
            && el.getBoundingClientRect().right > window.innerWidth + 1 && !el.closest('.tv-spacer')).map((el) => el.className).slice(0, 5)""")
        assert not overflowing, f"wider than the screen: {overflowing}"
        phone.shot(f"share-page-test-phone-{scheme}")

        phone.scroll_to(1)
        phone.scroll_to(1)
        end = phone.state()
        assert_small(end, "phone, at the end")
        assert end["visible"][-1] == LARGE_COUNT - 1
        assert phone.segment(LARGE_COUNT - 1).inner_text().strip() == segments[-1]["sentence"]
        # The find bar, from its button
        page.locator("[data-share-find]").click()
        field = page.locator(f"{FIND} input")
        field.wait_for(state="visible", timeout=5000)
        field.fill("segment 7000:")
        field.press("Enter")
        phone.wait_in_view(7000)
        # A tap on a line plays from there
        phone.segment(7000).tap()
        page.wait_for_function(f"(time) => Math.abs(({MEDIA}).currentTime - time) < 0.5", arg=segments[7000]["start_time"], timeout=10000)
        page.evaluate(f"() => ({MEDIA}).pause()")
        phone.shot(f"share-page-test-phone-{scheme}-find")
        phone.check(f"share page on a phone, {scheme}")
    finally:
        phone.close()


def test_the_dark_mode_button(browser, long_link):
    session = Visitor(browser, scheme="dark")
    try:
        page = session.page
        session.open(long_link)
        assert page.evaluate("() => document.documentElement.classList.contains('dark')")
        toggle = page.locator(f"button[title='{en('buttons.toggleTheme')}']")
        assert "fa-sun" in toggle.locator("i").get_attribute("class"), "the button does not know the page is dark"
        session.shot("share-page-test-dark")
        toggle.click()
        assert not page.evaluate("() => document.documentElement.classList.contains('dark')")
        assert page.evaluate("() => localStorage.getItem('darkMode')") == "false"
        assert_small(session.state(), "after the mode was switched")
        session.check("share page dark mode button")
    finally:
        session.close()


@pytest.mark.parametrize("theme", ["default", "desk", "cards"])
def test_the_speaker_turns_are_those_of_the_ui_theme(browser, long_link, segments, theme):
    """The turns are the recording page's, in the UI theme of the browser (a visitor's is
    the look for new accounts; here one saved in the browser). Each theme's turn works on
    this page: it is shown, a click plays from the line, and nothing fails."""
    session = Visitor(browser)
    try:
        page = session.page
        page.add_init_script(f"try {{ localStorage.setItem('uiTheme', '{theme}'); }} catch (e) {{}}")
        session.open(long_link)
        assert page.evaluate("() => document.documentElement.getAttribute('data-ui-theme')") == theme
        turn = page.locator(f"{LIST} .speaker-run").first
        assert "SPEAKER_00" in turn.inner_text() and segments[0]["sentence"] in turn.inner_text()
        assert ("desk-run" in turn.get_attribute("class")) == (theme == "desk")
        assert turn.locator(".spk-av").count() == (1 if theme == "cards" else 0)
        # Nothing of the owner's page: no hint to edit, and a double click opens nothing
        line = session.segment(2)
        assert line.get_attribute("title") is None
        line.dblclick()
        page.wait_for_function(f"(time) => Math.abs(({MEDIA}).currentTime - time) < 0.5", arg=segments[2]["start_time"], timeout=10000)
        page.evaluate(f"() => ({MEDIA}).pause()")
        assert page.locator(".modal-overlay").count() == 0
        assert session.playing() == 2
        session.scroll_to(1)
        session.scroll_to(1)
        end = session.state()
        assert end["visible"][-1] == LARGE_COUNT - 1 and end["toEnd"] <= 2
        assert_small(end, f"{theme} theme, at the end")
        session.shot(f"share-page-test-theme-{theme}")
        # (check_common also measures Tailwind utilities, which a theme may resize)
        body = page.inner_text("body")
        assert "${" not in body and "{{" not in body
        assert not session.problems.failed and not session.problems.errors, (session.problems.failed, session.problems.errors)
    finally:
        session.close()


def test_a_short_transcript_is_rendered_whole_and_notes_stay_text(owner, visitor):
    """A recording of ordinary length: everything is on the page, the browser's own find
    is left alone, and the print still has the whole transcript (the page does not scroll,
    its transcript does). Text in the notes that looks like a template expression is text."""
    page = visitor.page
    recording_id = owner.ids[MEETING_TITLE]
    expected = meeting_segments()
    owner.notes(recording_id, NOTES)
    try:
        visitor.open(owner.share(recording_id))
        texts = page.eval_on_selector_all(SEGMENT, "els => els.map(e => e.innerText.trim())")
        assert texts == [s["sentence"] for s in expected]
        assert page.eval_on_selector_all(f"{LIST} > .tv-spacer", "els => els.map(e => e.offsetHeight)") == [0, 0]
        assert page.locator("[data-share-find]").count() == 0
        page.locator(BOX).click(position={"x": 5, "y": 5})
        page.keyboard.press("Control+f")
        page.wait_for_timeout(300)
        assert page.locator(FIND).count() == 0
        visitor.check("short shared transcript")

        try:
            page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
            page.emulate_media(media="print")
            printed = page.eval_on_selector_all("#transcript-print p", "els => els.map(e => e.textContent)")
            assert printed == [s["sentence"] for s in expected]
        finally:
            page.emulate_media(media="screen")
            page.evaluate("() => window.dispatchEvent(new Event('afterprint'))")

        # The notes: shown as written, also in a print of their tab
        page.locator("button", has_text=en("notes.title")).first.click()
        notes = page.locator(".notes-box")
        notes.wait_for(state="visible", timeout=5000)
        assert notes.inner_text().strip() == NOTES
        assert not visitor.problems.errors and not visitor.problems.failed
        try:
            page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
            page.emulate_media(media="print")
            assert page.locator("#transcript-print").count() == 0
            assert notes.is_visible() and not page.locator("#app > header").is_visible()
        finally:
            page.emulate_media(media="screen")
            page.evaluate("() => window.dispatchEvent(new Event('afterprint'))")

        # A link without the notes: no tab, and the page does not receive them
        visitor.open(owner.share(recording_id, notes=False))
        assert page.locator("button", has_text=en("notes.title")).count() == 0
        data = json.loads(page.locator("#share-data").inner_text())
        assert data["notes"] is None and data["notes_raw"] is None
        assert "Shared notes" not in page.content()
        visitor.check("shared recording without notes")
    finally:
        owner.notes(recording_id, "")


def test_transcripts_without_speakers(owner, visitor):
    """Segments without speakers are plain lines that still play from a click; a transcript
    that is one text is shown and copied as it is."""
    page = visitor.page
    recording_id = owner.ids[MEETING_TITLE]
    plain = [{"sentence": s["sentence"], "start_time": s["start_time"], "end_time": s["end_time"]} for s in meeting_segments()]
    text = "First line of a plain transcript.\nSecond line, with <b>markup</b> and ${ 7 * 7 }."
    try:
        owner.store(recording_id, plain)
        visitor.open(owner.share(recording_id))
        lines = page.locator(f"{LIST} .transcript-segment")
        assert lines.all_inner_texts() == [s["sentence"] for s in plain]
        assert page.locator(".view-mode-toggle").count() == 0 and page.locator(f"{LIST} .speaker-run").count() == 0
        lines.nth(3).click()
        page.wait_for_function(f"(time) => Math.abs(({MEDIA}).currentTime - time) < 0.5", arg=plain[3]["start_time"], timeout=10000)
        page.evaluate(f"() => ({MEDIA}).pause()")
        assert visitor.playing() == 3
        page.locator("[data-share-copy]").click()
        page.locator("#toastContainer .toast-message").first.wait_for(state="visible", timeout=5000)
        assert page.evaluate("() => navigator.clipboard.readText()") == "\n".join(s["sentence"] for s in plain)
        visitor.check("shared transcript without speakers")

        response = owner.post(f"/recording/{recording_id}/update_transcription", {"transcription": text})
        assert response.ok
        visitor.open(owner.share(recording_id), wait_for=f"{BOX} .whitespace-pre-wrap")
        assert page.locator(f"{BOX} .whitespace-pre-wrap").inner_text() == text
        assert page.locator(LIST).count() == 0 and page.locator("[data-share-find]").count() == 0
        page.locator("[data-share-copy]").click()
        page.wait_for_function("(text) => navigator.clipboard.readText().then((copied) => copied === text)", arg=text, timeout=5000)
        assert not visitor.problems.errors and not visitor.problems.failed
    finally:
        owner.store(recording_id, meeting_segments())
