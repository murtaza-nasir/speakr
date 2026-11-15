"""Browser test of the phone layout of the recording page and the recording list.

Below 768px a recording has one top bar (back, title, star, More), a details
block that scrolls away, four fixed tabs, a slim player strip and sheets; the
recording list is a screen of its own (templates/components/phone/,
static/css/phone.css, static/js/modules/composables/phone-layout.js). This
test opens the seeded recordings at phone widths and checks:

- the top bar and what left it (the account menu, New recording, rename);
- the details line with both dates, and with one date when the meeting and
  the upload fall on the same day, and the Info sheet;
- the actions in the sheet behind More for a recording of one's own, one
  shared without rights, a failed one and an incognito one;
- the four tabs, with Events and Speaker stats as sheets;
- the pinned bar of the transcript: it stays under the top bar while a
  transcript of 20,000 segments scrolls, the page stays small, the switch
  shows the translation, and the find bar goes to a match far away;
- the player strip and the player sheet (seek, skip, speed, no volume);
- the composer, on the assistant's tab only, one line high and growing;
- the list: search, filter chips, the menu, selection mode, New recording;
- no sideways scrolling at 360 and 430 px, and touch targets of 44 px;
- the same structure in each of the six UI themes;
- on the desktop: the Transcription / Translation switch stays in view while
  the transcript scrolls, and an incognito recording has no download control.

A recording's state (another meeting date, shared without rights, failed, a
translation) is given to the page by changing the response of
GET /api/recordings/<id> in the browser, so the instance's data stays as it
is. The incognito recording is put in sessionStorage, where the app keeps
one, with incognito mode switched on in the response of GET /api/config.
The long transcript is stored through the app's own save route and replaced
by the seeded one afterwards.

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

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, en, login  # noqa: E402
from seed_recordings import AUDIO_SECONDS, TITLES, meeting_segments  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")

LONG_TITLE, MEETING_TITLE = TITLES
LARGE_COUNT = 20000
STEP = AUDIO_SECONDS / (LARGE_COUNT + 2000)
MAX_SEGMENTS_IN_DOM = 150
MAX_ELEMENTS_IN_PAGE = 4000
THEMES = ("default", "cards", "classic", "desk", "focus", "workspace")
PHONE = {"width": 390, "height": 844}
# Loose limits: a slow machine must not fail the test
WAIT = 30000

WORDS = "this is a sentence of a long meeting with a few more words in it so that some rows wrap".split()

BAR = '[data-phone-bar="recording"]'
LIST_BAR = '[data-phone-bar="list"]'
SCROLL = "[data-phone-scroll]"
SHEET = "[data-phone-sheet]"
TBAR = "[data-phone-transcript-bar]"
SEGMENT = "[data-tv-list] [data-segment-index]"
FIND = "[data-transcript-find]"
ROWS = "aside.sidebar .recording-group > *"
MEDIA = "document.querySelector('[data-mobile-player] audio, [data-mobile-player] video')"


def long_segments(word="Segment"):
    return [
        {"speaker": f"SPEAKER_0{i % 4}", "sentence": f"{word} {i}: " + " ".join(WORDS[: 6 + (i * 7) % 14]) + ".",
         "start_time": round(i * STEP, 4), "end_time": round((i + 1) * STEP, 4)}
        for i in range(LARGE_COUNT)
    ]


# What of the transcript is in the recording's scrolling area, under the pinned bar
STATE = """() => {
    const list = [...document.querySelectorAll('[data-tv-list]')].find((el) => el.getClientRects().length);
    const scroller = document.querySelector('[data-phone-scroll]');
    const bar = document.querySelector('[data-phone-transcript-bar]');
    if (!list || !scroller || !bar) return null;
    const frame = scroller.getBoundingClientRect();
    const top = bar.getBoundingClientRect().bottom;
    const segments = [...list.querySelectorAll('[data-segment-index]')];
    const inView = segments.filter((el) => { const r = el.getBoundingClientRect(); return r.bottom > top + 1 && r.top < frame.bottom - 1; });
    return {
        segments: segments.length,
        elements: document.getElementsByTagName('*').length,
        visible: inView.map((el) => Number(el.dataset.segmentIndex)),
        scrollTop: scroller.scrollTop,
        toEnd: Math.round(scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight),
    };
}"""

OVERFLOW = "() => [document.documentElement.scrollWidth, window.innerWidth]"


class Phone:
    """The signed-in admin on a phone-sized page."""

    def __init__(self, browser, viewport=None, touch=True):
        viewport = viewport or PHONE
        phone = viewport["width"] < 768
        # One sign-in for the module: the later sessions start from its cookies
        self.context = browser.new_context(viewport=viewport, locale="en-US", timezone_id="UTC",
                                           is_mobile=phone and touch, has_touch=phone and touch,
                                           storage_state=Phone.signed_in)
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        self.patches = {}
        self.config = {}
        self._sign_in()
        self.problems.reset()
        listing = self.page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        self.ids = {r["title"]: r["id"] for r in items if r.get("title") in TITLES}
        self.page.route(re.compile(r"/api/recordings/\d+$"), self._recording)
        self.page.route(re.compile(r"/api/config$"), self._config)

    signed_in = None

    def _sign_in(self):
        if Phone.signed_in is None:
            login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
            Phone.signed_in = self.context.storage_state()
        else:
            self.page.goto(f"{BASE}/")
            self.page.wait_for_load_state("load")
            assert "/login" not in self.page.url, "the sign-in of the module is no longer valid"

    # ---- what the page is told ------------------------------------------

    def _recording(self, route):
        recording_id = int(route.request.url.rsplit("/", 1)[1])
        patch = self.patches.get(recording_id)
        if not patch or route.request.method != "GET":
            return route.continue_()
        response = route.fetch()
        body = response.json()
        body.update(patch(body) if callable(patch) else patch)
        route.fulfill(response=response, body=json.dumps(body), headers={**response.headers, "content-type": "application/json"})

    def _config(self, route):
        if not self.config:
            return route.continue_()
        response = route.fetch()
        body = response.json()
        body.update(self.config)
        route.fulfill(response=response, body=json.dumps(body), headers={**response.headers, "content-type": "application/json"})

    # ---- the page ----------------------------------------------------------

    def close(self):
        self.context.close()

    def csrf(self):
        return self.page.locator('meta[name="csrf-token"]').first.get_attribute("content")

    def store(self, recording_id, segments):
        """Save a transcript the way the editor does (needs an open app page for the token)."""
        if not self.page.url.startswith(BASE) or "/login" in self.page.url:
            self.page.goto(f"{BASE}/recordings/{recording_id}")
            self.page.wait_for_load_state("load")
        response = self.page.request.post(
            f"{BASE}/recording/{recording_id}/update_transcription",
            headers={"X-CSRFToken": self.csrf(), "Content-Type": "application/json"},
            data=json.dumps({"transcription": json.dumps(segments)}), timeout=120000)
        assert response.ok, f"could not store the transcript: HTTP {response.status}"

    def leave(self):
        self.page.goto("about:blank")
        self.page.wait_for_timeout(300)
        self.problems.reset()

    def open(self, recording_id, tab=None):
        """Open a recording; its top bar and its details are there when this returns."""
        self.leave()
        self.page.goto(f"{BASE}/recordings/{recording_id}")
        self.page.locator(BAR).wait_for(state="visible", timeout=60000)
        self.page.locator("[data-phone-details]").wait_for(state="visible", timeout=WAIT)
        # The record itself has arrived (the list's row has no participants field)
        self.page.wait_for_function("() => !!document.querySelector('[data-phone-duration], [data-phone-status]')", timeout=WAIT)
        if tab:
            self.tab(tab)

    def open_list(self):
        """Open the app without a recording to return to: the list is the screen."""
        if self.page.url.startswith(BASE):
            # The app reopens the recording that was open last
            self.page.evaluate("() => localStorage.removeItem('lastSelectedRecordingId')")
        self.leave()
        self.page.goto(f"{BASE}/")
        self.page.locator(LIST_BAR).wait_for(state="visible", timeout=60000)
        self.page.locator(ROWS).first.wait_for(state="visible", timeout=60000)
        self.list_in_place()

    def list_in_place(self):
        """The list has finished sliding in."""
        self.wait("() => Math.abs(document.querySelector('aside.sidebar').getBoundingClientRect().left) < 0.5")

    def set_theme(self, theme):
        """Save a UI theme in the account (the page takes the account's theme on every load)."""
        response = self.page.request.post(f"{BASE}/api/user/preferences", headers={"X-CSRFToken": self.csrf(), "Content-Type": "application/json"},
                                          data=json.dumps({"ui_theme": theme}))
        assert response.ok, f"could not save the theme {theme}: HTTP {response.status}"

    def tab(self, name):
        self.page.locator(f'[data-phone-tab="{name}"]').click()
        self.page.locator(f'[data-phone-pane="{name}"]').wait_for(state="visible", timeout=WAIT)
        self.wait("(name) => document.querySelector(`[data-phone-tab=\"${name}\"]`).getAttribute('aria-current') === 'true'", name)

    def wait(self, script, arg=None, timeout=WAIT):
        self.page.wait_for_function(script, arg=arg, timeout=timeout)

    def sheet(self, name):
        return self.page.locator(f'[data-phone-sheet="{name}"]')

    def open_sheet(self, opener, name):
        self.page.locator(opener).first.click()
        self.sheet(name).locator(".ph-sheet").wait_for(state="visible", timeout=WAIT)
        # The sheet has finished sliding up
        self.wait("() => document.querySelector('[data-phone-sheet] .ph-sheet').getAnimations().every((a) => a.playState === 'finished')")
        return self.sheet(name)

    def close_sheet(self):
        self.page.locator(f"{SHEET} [data-phone-sheet-close]").first.click()
        self.page.locator(SHEET).wait_for(state="detached", timeout=WAIT)

    def actions(self):
        """The actions in the sheet behind More, by their names."""
        sheet = self.open_sheet("[data-phone-more]", "actions")
        names = sheet.locator("[data-phone-action]").evaluate_all("(els) => els.map((el) => el.dataset.phoneAction)")
        self.close_sheet()
        return set(names)

    def state(self):
        return self.page.evaluate(STATE)

    def segment(self, index):
        return self.page.locator(f"{SEGMENT}[data-segment-index='{index}']")

    def wait_in_view(self, index, timeout=WAIT):
        self.wait(f"(index) => {{ const s = ({STATE})(); return !!s && s.visible.includes(index); }}", index, timeout)

    def no_sideways_scroll(self, where):
        wide, window = self.page.evaluate(OVERFLOW)
        assert wide <= window, f"{where}: the page is {wide}px wide in a {window}px window"

    def check(self, label, utilities=True):
        """The shared checks. `utilities=False` leaves out the probe of the Tailwind
        utilities, whose rem values are other pixels in a theme with another root font size."""
        if utilities:
            return check_common(self.page, self.problems, label)
        body = self.page.inner_text("body")
        leftovers = [m for m in ("${", "{{") if m in body]
        assert not leftovers, f"{label}: template text left in the page: {leftovers}"
        assert not self.problems.failed, f"{label}: failed requests: {self.problems.failed}"
        assert not self.problems.errors, f"{label}: JavaScript errors: {self.problems.errors}"
        assert not self.problems.play_cdn, f"{label}: Tailwind Play CDN in use: {self.problems.play_cdn}"

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
def phone(browser):
    Phone.signed_in = None
    session = Phone(browser)
    if set(session.ids) != set(TITLES):
        session.close()
        pytest.skip("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    session.store(session.ids[MEETING_TITLE], meeting_segments())
    yield session
    session.close()


@pytest.fixture(autouse=True)
def plain_state(request):
    """Each test starts without changed responses and without an incognito recording."""
    session = request.getfixturevalue("phone") if "phone" in request.fixturenames else None
    yield
    if session:
        session.patches.clear()
        session.config.clear()
        if session.page.url.startswith(BASE):
            session.page.evaluate("() => { sessionStorage.removeItem('speakr_incognito_recording'); localStorage.removeItem('transcriptionViewMode'); }")


@pytest.fixture(scope="module")
def long_transcript(phone):
    """The long recording with 20,000 segments, for the tests that ask for it."""
    segments = long_segments()
    phone.store(phone.ids[LONG_TITLE], segments)
    yield segments
    phone.store(phone.ids[LONG_TITLE], meeting_segments())


# ---- the top bar ---------------------------------------------------------------

def test_one_top_bar_with_back_title_star_and_more(phone):
    page = phone.page
    phone.open(phone.ids[MEETING_TITLE])
    bar = page.locator(BAR)
    # Exactly four things, in this order
    parts = bar.evaluate("""(el) => [...el.children].map((c) =>
        c.dataset.phoneBack !== undefined ? 'back' : c.dataset.phoneTitle !== undefined ? 'title'
        : c.dataset.phoneStar !== undefined ? 'star' : c.dataset.phoneMore !== undefined ? 'more' : c.tagName)""")
    assert parts == ["back", "title", "star", "more"], parts
    assert bar.locator("[data-phone-title]").inner_text().strip() == MEETING_TITLE
    # One bar: the page header with the assistant, New recording and the account menu is not drawn
    assert page.locator("#app > header").count() == 0
    assert page.locator("[data-user-menu-toggle]:visible").count() == 0
    assert page.locator(f"button[title='{en('tooltips.editTitle')}']:visible").count() == 0
    # The title may take two lines, never more
    lines = bar.locator("[data-phone-title]").evaluate("(el) => getComputedStyle(el).webkitLineClamp")
    assert lines == "2", lines
    # Touch targets
    for name in ("back", "star", "more"):
        box = bar.locator(f"[data-phone-{name}]").bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44, f"{name}: {box}"

    # The star: a toggle, also shown in Actions
    star = bar.locator("[data-phone-star]")
    before = star.get_attribute("aria-pressed")
    after = "false" if before == "true" else "true"
    star.click()
    phone.wait("(state) => document.querySelector('[data-phone-star]').getAttribute('aria-pressed') === state", after)
    sheet = phone.open_sheet("[data-phone-more]", "actions")
    assert sheet.locator('[data-phone-action="star"]').get_attribute("aria-pressed") == after
    sheet.locator('[data-phone-action="star"]').click()
    phone.wait("(state) => document.querySelector('[data-phone-star]').getAttribute('aria-pressed') === state", before)
    phone.close_sheet()

    # Back shows the list, which has the menu with the account entries
    bar.locator("[data-phone-back]").click()
    page.locator(LIST_BAR).wait_for(state="visible", timeout=WAIT)
    phone.list_in_place()
    menu = phone.open_sheet("[data-phone-menu]", "menu")
    entries = menu.locator("[data-phone-menu-item]").evaluate_all("(els) => els.map((el) => el.dataset.phoneMenuItem)")
    for entry in ("notifications", "shares", "settings", "scheme", "dark", "signout"):
        assert entry in entries, entries
    assert menu.locator('a[href="/account"]').count() == 1
    phone.shot("phone-layout-test-menu")
    phone.close_sheet()
    # A row opens the recording again
    page.locator(ROWS).filter(has_text=MEETING_TITLE).first.click()
    page.locator(BAR).wait_for(state="visible", timeout=WAIT)
    phone.wait("() => document.querySelector('aside.sidebar').classList.contains('collapsed')")
    phone.check("the top bar of a recording")


def test_rename_from_actions(phone):
    page = phone.page
    recording_id = phone.ids[MEETING_TITLE]
    phone.open(recording_id)
    sheet = phone.open_sheet("[data-phone-more]", "actions")
    sheet.locator('[data-phone-action="rename"]').click()
    field = page.locator("[data-phone-title-input]")
    field.wait_for(state="visible", timeout=WAIT)
    assert page.locator(SHEET).count() == 0
    assert field.input_value() == MEETING_TITLE
    # Esc leaves the title as it was
    field.fill("Another title")
    field.press("Escape")
    page.locator("[data-phone-title]").wait_for(state="visible", timeout=WAIT)
    assert page.locator("[data-phone-title]").inner_text().strip() == MEETING_TITLE
    phone.check("rename")


# ---- the details block -------------------------------------------------------

def test_both_dates_and_one_date_on_the_same_day(phone):
    page = phone.page
    recording_id = phone.ids[MEETING_TITLE]

    # As seeded: the meeting and the upload are the same moment
    phone.open(recording_id)
    assert page.locator("[data-phone-meeting-date]").count() == 1
    assert page.locator("[data-phone-uploaded-date]").count() == 0
    one_date = page.locator("[data-phone-meeting-date]").inner_text()
    assert re.search(r"\d{1,2}:\d{2}", one_date), one_date
    assert page.locator("[data-phone-duration]").inner_text().strip() == "1m 30s"
    assert page.locator("[data-phone-size]").inner_text().strip() != ""

    # A meeting three days before the upload: both, the second one labeled
    phone.patches[recording_id] = {"meeting_date": "2026-03-02T10:02:00", "created_at": "2026-03-05T09:15:00"}
    phone.open(recording_id)
    meeting = page.locator("[data-phone-meeting-date]").inner_text()
    uploaded = page.locator("[data-phone-uploaded-date]").inner_text().strip()
    assert "Mar 2" in meeting and "10:02" in meeting, meeting
    assert page.locator("[data-phone-meeting-date]").get_attribute("title") == en("metadata.meetingDate")
    assert uploaded.startswith("uploaded Mar 5"), uploaded
    # One line: the facts sit beside each other
    tops = page.locator(".ph-details__fact").evaluate_all("(els) => els.map((el) => Math.round(el.getBoundingClientRect().top))")
    assert max(tops) - min(tops) <= 4, tops
    phone.shot("phone-layout-test-details")

    # The same day at another hour is still one date
    phone.patches[recording_id] = {"meeting_date": "2026-03-05T08:00:00", "created_at": "2026-03-05T21:15:00"}
    phone.open(recording_id)
    assert "Mar 5" in page.locator("[data-phone-meeting-date]").inner_text()
    assert page.locator("[data-phone-uploaded-date]").count() == 0

    # No meeting date: the upload date alone, labeled
    phone.patches[recording_id] = {"meeting_date": None, "created_at": "2026-03-05T21:15:00"}
    phone.open(recording_id)
    assert page.locator("[data-phone-meeting-date]").count() == 0
    assert page.locator("[data-phone-uploaded-date]").inner_text().strip().startswith("Uploaded Mar 5")
    phone.check("the dates of the details line")


def test_details_fold_and_the_info_sheet(phone):
    page = phone.page
    recording_id = phone.ids[MEETING_TITLE]
    phone.patches[recording_id] = {"meeting_date": "2026-03-02T10:02:00", "created_at": "2026-03-05T09:15:00",
                                   "participants": "Dana Whitlock, Marco Ibarra"}
    phone.open(recording_id)
    assert page.locator("[data-phone-participants]").inner_text().strip() == "Dana Whitlock, Marco Ibarra"
    toggle = page.locator("[data-phone-details-toggle]")
    toggle.click()
    page.locator("[data-phone-participants]").wait_for(state="detached", timeout=WAIT)
    assert toggle.get_attribute("aria-expanded") == "false"
    assert page.locator("[data-phone-meeting-date]").is_visible()
    toggle.click()
    page.locator("[data-phone-participants]").wait_for(state="visible", timeout=WAIT)

    # The block scrolls away with the text
    phone.tab("transcript")
    page.evaluate("() => { document.querySelector('[data-phone-scroll]').scrollTop = 400; }")
    phone.wait("() => document.querySelector('[data-phone-details]').getBoundingClientRect().bottom < 60")
    phone.tab("summary")

    info = phone.open_sheet("[data-phone-info]", "info")
    text = info.inner_text()
    for part in (MEETING_TITLE, en("metadata.meetingDate"), "Mar 2, 2026", en("phone.uploadedLabel"), "Mar 5, 2026",
                 en("metadata.fileName"), "browser-test-", en("metadata.duration"), "1m 30s", en("metadata.fileSize"),
                 "Dana Whitlock, Marco Ibarra"):
        assert part in text, f"Info lacks {part!r}: {text}"
    assert info.locator('[data-phone-action="info-meeting-date"]').count() == 1
    assert info.locator('[data-phone-action="info-participants"]').count() == 1
    phone.shot("phone-layout-test-info")
    phone.close_sheet()
    phone.check("details and Info")


# ---- the actions -------------------------------------------------------------

OWN = {"inbox", "star", "archive", "tags", "rename", "regenerate-title", "speakers", "edit-transcript", "bubbles",
       "download-transcript", "translate", "translation-preferences", "reprocess-transcription", "regenerate-summary",
       "stats", "info", "share", "download-audio", "delete-audio", "delete"}
# Present on some instances only: folders switched on, a recording with events
OPTIONAL = {"folder", "events"}


def test_actions_of_a_recording_of_ones_own(phone):
    phone.open(phone.ids[MEETING_TITLE])
    names = phone.actions()
    assert OWN <= names, sorted(OWN - names)
    assert names - OWN <= OPTIONAL, sorted(names - OWN)

    sheet = phone.open_sheet("[data-phone-more]", "actions")
    # Labeled and grouped
    groups = sheet.locator(".ph-group").all_inner_texts()
    assert groups == [en("phone.organize"), en("transcription.title"), en("headerActions.reprocess"), en("phone.about"),
                      en("phone.shareExport"), en("common.delete")], groups
    labels = sheet.locator("[data-phone-action]").evaluate_all("(els) => els.filter((el) => !el.innerText.trim()).length")
    assert labels == 0, "an action without a label"
    heights = sheet.locator(".ph-row").evaluate_all("(els) => els.map((el) => el.getBoundingClientRect().height)")
    assert min(heights) >= 44, heights
    phone.no_sideways_scroll("Actions")
    phone.shot("phone-layout-test-actions")
    # The sheet scrolls to its last action
    last = sheet.locator('[data-phone-action="delete"]')
    last.scroll_into_view_if_needed()
    assert last.is_visible()
    # A row closes the sheet and opens what it names
    last.click()
    phone.page.locator(SHEET).wait_for(state="detached", timeout=WAIT)
    dialog = phone.page.locator(".modal-overlay:visible")
    dialog.first.wait_for(state="visible", timeout=WAIT)
    phone.page.keyboard.press("Escape")
    if dialog.count():
        dialog.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(en('buttons.cancel'))}\s*$")).first.click()
    dialog.first.wait_for(state="hidden", timeout=WAIT)
    # The scrim closes a sheet too
    phone.open_sheet("[data-phone-more]", "actions")
    phone.page.locator(f"{SHEET} .ph-scrim").click(position={"x": 195, "y": 20})
    phone.page.locator(SHEET).wait_for(state="detached", timeout=WAIT)
    phone.check("actions of one's own recording")


def test_actions_of_a_recording_shared_without_rights(phone):
    recording_id = phone.ids[MEETING_TITLE]
    phone.patches[recording_id] = {"is_shared": True, "can_edit": False, "can_delete": False,
                                   "share_info": {"can_reshare": False}, "owner_username": "someone"}
    phone.open(recording_id)
    assert phone.page.locator("[data-phone-shared-in]").is_visible()
    names = phone.actions()
    kept = {"inbox", "star", "archive", "tags", "speakers", "edit-transcript", "bubbles", "download-transcript",
            "translate", "translation-preferences", "stats", "info", "download-audio"}
    assert kept <= names, sorted(kept - names)
    gone = {"rename", "regenerate-title", "reprocess-transcription", "regenerate-summary", "reset", "share",
            "delete-audio", "delete", "folder"}
    assert not (names & gone), sorted(names & gone)
    assert names - kept <= OPTIONAL, sorted(names - kept)
    phone.check("actions of a shared recording")


def test_actions_of_a_failed_recording(phone):
    recording_id = phone.ids[MEETING_TITLE]
    phone.patches[recording_id] = {"status": "FAILED"}
    phone.open(recording_id)
    assert phone.page.locator("[data-phone-status]").is_visible()
    names = phone.actions()
    for name in ("reset", "reprocess-transcription", "regenerate-summary", "delete", "info"):
        assert name in names, (name, sorted(names))
    assert "regenerate-title" not in names
    phone.check("actions of a failed recording")


def incognito_recording():
    return {
        "id": "incognito", "incognito": True, "title": "Incognito check", "status": "COMPLETED",
        "transcription": json.dumps(meeting_segments()), "summary": "A summary kept in this tab only.",
        "notes": "Notes kept in this tab only.", "participants": "", "audio_duration_seconds": 72,
        "created_at": "2026-03-05T09:15:00", "meeting_date": "2026-03-05T09:15:00", "tags": [],
    }


def open_incognito(session):
    """Open the incognito recording the tab keeps: its card is first in the list."""
    session.config.update({"enable_incognito_mode": True})
    session.leave()
    session.page.goto(f"{BASE}/")
    session.page.evaluate("""(data) => { localStorage.removeItem('lastSelectedRecordingId');
        sessionStorage.setItem('speakr_incognito_recording', JSON.stringify(data)); }""", incognito_recording())
    session.leave()
    session.page.goto(f"{BASE}/")
    card = session.page.locator("aside.sidebar").get_by_text("Incognito check", exact=True)
    card.first.wait_for(state="attached", timeout=60000)
    # The app may show the kept recording by itself; else its card opens it
    opened = "() => !!document.querySelector('[data-phone-incognito]') || /incognito/i.test((document.querySelector('#app main') || document.body).innerText)"
    in_place = "Math.abs(document.querySelector('aside.sidebar').getBoundingClientRect().left) < 0.5"
    session.wait(f"() => ({opened})() || {in_place}")
    if not session.page.evaluate(opened):
        card.first.click()


def test_an_incognito_recording_on_a_phone(phone):
    page = phone.page
    open_incognito(phone)
    page.locator(BAR).wait_for(state="visible", timeout=WAIT)
    page.locator("[data-phone-incognito]").wait_for(state="visible", timeout=WAIT)
    assert page.locator("[data-phone-star]").count() == 0
    names = phone.actions()
    assert "discard-incognito" in names and "info" in names and "edit-transcript" in names, sorted(names)
    downloads = {n for n in names if "download" in n}
    assert not downloads, sorted(downloads)
    for name in ("inbox", "star", "archive", "tags", "share", "delete", "delete-audio", "translate"):
        assert name not in names, name
    # Summary: copy stays, download is gone
    phone.tab("summary")
    acts = page.locator('[data-phone-acts="summary"]')
    assert acts.get_by_text(en("buttons.copy"), exact=True).is_visible()
    assert page.locator('[data-phone-download="summary"]').count() == 0
    phone.tab("notes")
    assert page.locator('[data-phone-acts="notes"]').get_by_text(en("buttons.copy"), exact=True).is_visible()
    assert page.locator('[data-phone-download="notes"]').count() == 0
    phone.tab("transcript")
    assert page.locator("[data-phone-copy]").is_visible()
    assert page.locator("[data-phone-switch]").count() == 0
    # No audio is kept: the strip says so
    assert en("incognito.audioNotStored") in page.locator("[data-mobile-player]").inner_text()
    assert page.locator("[data-mobile-player] a[download]").count() == 0
    phone.shot("phone-layout-test-incognito")
    phone.check("an incognito recording on a phone")


# ---- the tabs ----------------------------------------------------------------

def test_four_fixed_tabs_and_the_sheets_of_the_summary(phone):
    page = phone.page
    phone.open(phone.ids[MEETING_TITLE])
    tabs = page.locator("[data-mobile-bottom-nav] [data-phone-tab]")
    assert tabs.evaluate_all("(els) => els.map((el) => el.dataset.phoneTab)") == ["summary", "transcript", "notes", "chat"]
    labels = [text.strip().split("\n")[0] for text in tabs.all_inner_texts()]
    assert labels[:3] == [en("summary.title"), en("transcription.title"), en("notes.title")], labels
    assert en("detail.more") not in labels
    heights = tabs.evaluate_all("(els) => els.map((el) => el.getBoundingClientRect().height)")
    assert min(heights) >= 44, heights
    for name in ("transcript", "notes", "chat", "summary"):
        phone.tab(name)
        assert page.locator("[data-phone-pane]:visible").count() == 1
        phone.no_sideways_scroll(f"tab {name}")
    # Speaker stats: a row at the end of the summary opens a sheet
    row = page.locator('[data-phone-open="stats"]')
    row.scroll_into_view_if_needed()
    stats = phone.open_sheet('[data-phone-open="stats"]', "stats")
    assert stats.locator("[data-phone-stats]").is_visible()
    assert en("stats.perSpeaker") in stats.inner_text()
    phone.no_sideways_scroll("Speaker stats")
    phone.close_sheet()
    assert page.locator('[data-phone-tab="summary"]').get_attribute("aria-current") == "true"
    # Events: with events, a row and a sheet as well
    recording_id = phone.ids[MEETING_TITLE]
    phone.patches[recording_id] = {"events": [{"id": 901, "title": "Design review", "description": "Screens",
                                               "start_datetime": "2026-03-10T14:00:00", "end_datetime": "2026-03-10T14:30:00",
                                               "location": "Room 2", "attendees": ["Dana", "Marco"]}]}
    phone.open(recording_id)
    page.locator('[data-phone-open="events"]').scroll_into_view_if_needed()
    events = phone.open_sheet('[data-phone-open="events"]', "events")
    assert "Design review" in events.inner_text() and "Room 2" in events.inner_text()
    assert events.locator('[data-phone-action="export-events"]').is_visible()
    phone.close_sheet()
    assert "events" in phone.actions()
    phone.check("tabs and the sheets of the summary")


# ---- the transcript ----------------------------------------------------------

def assert_small(state, where):
    assert 5 <= state["segments"] <= MAX_SEGMENTS_IN_DOM, f"{where}: {state['segments']} segments in the DOM"
    assert state["elements"] <= MAX_ELEMENTS_IN_PAGE, f"{where}: {state['elements']} elements in the page"


def bar_place(page):
    return page.evaluate("""() => {
        const bar = document.querySelector('[data-phone-transcript-bar]').getBoundingClientRect();
        const top = document.querySelector('[data-phone-bar="recording"]').getBoundingClientRect();
        const strip = document.querySelector('[data-mobile-player]').getBoundingClientRect();
        const option = document.querySelector('[data-phone-switch-option="translation"]');
        const at = option ? option.getBoundingClientRect() : null;
        const hit = at ? document.elementFromPoint(at.left + at.width / 2, at.top + at.height / 2) : null;
        return { barTop: bar.top, barBottom: bar.bottom, topBarBottom: top.bottom, stripTop: strip.top,
                 switchOnTop: !!hit && !!option && option.contains(hit) };
    }""")


def test_the_switch_stays_pinned_over_a_long_transcript(phone, long_transcript):
    page = phone.page
    recording_id = phone.ids[LONG_TITLE]
    phone.patches[recording_id] = lambda body: {
        "transcription_translation": json.dumps(long_segments("Segmento")), "transcription_translation_language": "es"}
    phone.open(recording_id, tab="transcript")
    page.locator(SEGMENT).first.wait_for(state="visible", timeout=60000)
    top = phone.state()
    assert_small(top, "at the top")
    assert top["visible"][0] == 0
    # At the top the bar is below the details block
    start = bar_place(page)
    assert start["barTop"] > start["topBarBottom"] + 40, start

    def pinned(where):
        place = bar_place(page)
        assert abs(place["barTop"] - place["topBarBottom"]) <= 1, f"{where}: the bar is not under the top bar: {place}"
        assert place["switchOnTop"], f"{where}: the switch is covered: {place}"
        return place

    # Scrolled by the wheel, to the middle and to the end: the bar stays under the top bar
    page.locator(SCROLL).hover()
    page.mouse.wheel(0, 1500)
    phone.wait("() => document.querySelector('[data-phone-scroll]').scrollTop >= 1400")
    pinned("after the wheel")
    for fraction in (0.5, 1):
        page.evaluate("(f) => { const s = document.querySelector('[data-phone-scroll]'); s.scrollTop = (s.scrollHeight - s.clientHeight) * f; }", fraction)
        phone.wait(f"() => {{ const s = ({STATE})(); return !!s && s.visible.length > 0 && s.visible[0] > 100; }}")
        pinned(f"at {fraction}")
        assert_small(phone.state(), f"at {fraction}")
    page.evaluate("() => { const s = document.querySelector('[data-phone-scroll]'); s.scrollTop = s.scrollHeight; }")
    phone.wait_in_view(LARGE_COUNT - 1)
    assert phone.segment(LARGE_COUNT - 1).inner_text().strip() == long_transcript[-1]["sentence"]
    phone.shot("phone-layout-test-pinned-bar")

    # The switch, used where the reader is: the translation of the same place
    page.locator('[data-phone-switch-option="translation"]').click()
    phone.wait("() => document.querySelector('[data-phone-switch-option=\"translation\"]').getAttribute('aria-pressed') === 'true'")
    phone.wait("() => [...document.querySelectorAll('[data-tv-list] [data-segment-index]')].some((el) => el.innerText.includes('Segmento'))")
    pinned("showing the translation")
    assert_small(phone.state(), "showing the translation")
    assert not page.locator(SEGMENT).filter(has_text="Segment 1").count()
    page.locator('[data-phone-switch-option="transcription"]').click()
    phone.wait("() => [...document.querySelectorAll('[data-tv-list] [data-segment-index]')].some((el) => /Segment \\d/.test(el.innerText))")
    pinned("back on the transcription")
    phone.check("the pinned bar over a long transcript")


def test_find_from_the_pinned_bar(phone, long_transcript):
    page = phone.page
    phone.open(phone.ids[LONG_TITLE], tab="transcript")
    page.locator(SEGMENT).first.wait_for(state="visible", timeout=60000)
    page.locator("[data-phone-find]").click()
    field = page.locator(f"{TBAR} {FIND} input")
    field.wait_for(state="visible", timeout=WAIT)
    assert page.locator("[data-phone-find]").get_attribute("aria-pressed") == "true"
    field.fill("segment 7000:")
    phone.wait("() => { const el = document.querySelector('[data-transcript-find] .find-replace-bar__count'); return !!el && el.textContent.trim() === '1'; }")
    field.press("Enter")
    phone.wait_in_view(7000)
    # The match is below the bar with its find field, and marked
    place = bar_place(page)
    box = phone.segment(7000).bounding_box()
    assert box["y"] >= place["barBottom"] - 1 and box["y"] + box["height"] <= place["stripTop"] + 1, (box, place)
    assert abs(place["barTop"] - place["topBarBottom"]) <= 1, place
    phone.wait("() => typeof CSS === 'undefined' || !CSS.highlights || CSS.highlights.has('transcript-find-current')")
    phone.no_sideways_scroll("the find bar")
    phone.shot("phone-layout-test-find")
    # A second word, with several matches: next and previous
    field.fill("segment 1234")
    phone.wait("() => document.querySelector('[data-transcript-find] .find-replace-bar__count').textContent.trim() === '11'")
    page.locator(f"{FIND} button[title='{en('findReplace.next')}']").click()
    phone.wait("() => /^\\d+ \\/ 11$/.test(document.querySelector('[data-transcript-find] .find-replace-bar__count').textContent.trim())")
    # Closing the bar from its button
    page.locator("[data-phone-find]").click()
    field.wait_for(state="detached", timeout=WAIT)
    assert page.locator("[data-phone-find]").get_attribute("aria-pressed") == "false"
    phone.check("find on a phone")


def test_find_and_copy_on_a_short_transcript(phone):
    page = phone.page
    phone.open(phone.ids[MEETING_TITLE], tab="transcript")
    page.locator(SEGMENT).first.wait_for(state="visible", timeout=60000)
    assert page.locator(SEGMENT).count() == 12
    assert page.locator("[data-phone-copy]").is_visible() and page.locator("[data-phone-follow]").is_visible()
    page.locator("[data-phone-find]").click()
    field = page.locator(f"{TBAR} {FIND} input")
    field.wait_for(state="visible", timeout=WAIT)
    field.fill("budget")
    phone.wait("() => document.querySelector('[data-transcript-find] .find-replace-bar__count').textContent.trim() === '2'")
    field.press("Enter")
    phone.wait("() => /^1 \\/ 2$/.test(document.querySelector('[data-transcript-find] .find-replace-bar__count').textContent.trim())")
    page.locator("[data-phone-find]").click()
    field.wait_for(state="detached", timeout=WAIT)
    # Follow playback is a toggle in the bar
    follow = page.locator("[data-phone-follow]")
    before = follow.get_attribute("aria-pressed")
    follow.click()
    phone.wait("(was) => document.querySelector('[data-phone-follow]').getAttribute('aria-pressed') !== was", before)
    follow.click()
    phone.wait("(was) => document.querySelector('[data-phone-follow]').getAttribute('aria-pressed') === was", before)
    # Bubble view from Actions
    sheet = phone.open_sheet("[data-phone-more]", "actions")
    sheet.locator('[data-phone-action="bubbles"]').click()
    phone.wait("() => document.querySelector('[data-phone-action=\"bubbles\"]').getAttribute('aria-checked') === 'true'")
    phone.close_sheet()
    page.locator(".speaker-bubble").first.wait_for(state="visible", timeout=WAIT)
    sheet = phone.open_sheet("[data-phone-more]", "actions")
    sheet.locator('[data-phone-action="bubbles"]').click()
    phone.wait("() => document.querySelector('[data-phone-action=\"bubbles\"]').getAttribute('aria-checked') === 'false'")
    phone.close_sheet()
    phone.check("a short transcript on a phone")


# ---- the player --------------------------------------------------------------

def test_player_strip_and_player_sheet(phone):
    page = phone.page
    phone.open(phone.ids[MEETING_TITLE])
    strip = page.locator("[data-phone-player-strip]")
    strip.wait_for(state="visible", timeout=WAIT)
    # One slim strip above the tab bar
    box = strip.bounding_box()
    nav = page.locator("[data-mobile-bottom-nav]").bounding_box()
    assert box["height"] <= 56, box
    assert abs(box["y"] + box["height"] - nav["y"]) <= 2, (box, nav)
    phone.wait("() => /\\/ 1:30$/.test(document.querySelector('[data-phone-time]').textContent.trim())")
    # No volume control, no speed menu, no download in the strip
    assert page.locator("[data-mobile-player] input[type=range]").count() == 0
    assert page.locator("[data-mobile-player] .fa-volume-up, [data-mobile-player] .fa-volume-mute, [data-mobile-player] .fa-volume-down").count() == 0
    assert strip.locator("a[download]").count() == 0

    # Play from the strip: the time moves, the speaker is named, the line grows
    page.locator("[data-phone-play]").click()
    phone.wait(f"() => {{ const m = {MEDIA}; return !!m && !m.paused && m.currentTime > 0.5; }}")
    phone.wait("() => document.querySelector('[data-phone-speaker]').textContent.trim().startsWith('SPEAKER_0')")
    phone.wait("() => parseFloat(document.querySelector('[data-phone-progress]').style.width) > 0")
    page.locator("[data-phone-play]").click()
    phone.wait(f"() => {MEDIA}.paused")

    # The sheet: skip, speed, download, and no volume
    sheet = phone.open_sheet("[data-phone-player-open]", "player")
    page.evaluate(f"() => {{ {MEDIA}.currentTime = 30; }}")
    phone.wait(f"() => Math.abs({MEDIA}.currentTime - 30) < 0.5")
    sheet.locator('[data-phone-skip="15"]').click()
    phone.wait(f"() => Math.abs({MEDIA}.currentTime - 45) < 1")
    sheet.locator('[data-phone-skip="-15"]').click()
    phone.wait(f"() => Math.abs({MEDIA}.currentTime - 30) < 1")
    phone.wait("() => document.querySelector('[data-phone-sheet-time]').textContent.trim() === '0:30'")
    sheet.locator('[data-phone-speed="1.5"]').click()
    phone.wait(f"() => {MEDIA}.playbackRate === 1.5")
    assert sheet.locator('[data-phone-speed="1.5"]').get_attribute("aria-pressed") == "true"
    phone.wait("() => document.querySelector('.ph-mp__rate').textContent.trim() === '1.5x'")
    sheet.locator('[data-phone-speed="1"]').click()
    phone.wait(f"() => {MEDIA}.playbackRate === 1")
    # The scrubber: a tap near its end seeks there
    scrub = sheet.locator("[data-phone-scrubber]").bounding_box()
    page.mouse.click(scrub["x"] + scrub["width"] * 0.8, scrub["y"] + scrub["height"] / 2)
    phone.wait(f"() => {MEDIA}.currentTime > 60")
    sizes = sheet.locator("[data-phone-skip], [data-phone-speed], [data-phone-sheet-play]").evaluate_all(
        "(els) => els.map((el) => Math.min(el.getBoundingClientRect().width, el.getBoundingClientRect().height))")
    assert min(sizes) >= 44, sizes
    assert scrub["height"] >= 44, scrub
    download = sheet.locator('[data-phone-action="player-download"]')
    assert download.get_attribute("href") == f"/audio/{phone.ids[MEETING_TITLE]}?download=true"
    assert sheet.locator("input[type=range]").count() == 0
    assert "volume" not in sheet.inner_html().lower()
    phone.no_sideways_scroll("the player sheet")
    phone.shot("phone-layout-test-player")
    # Play and pause from the sheet; the media element is the strip's own
    sheet.locator("[data-phone-sheet-play]").click()
    phone.wait(f"() => !{MEDIA}.paused")
    phone.close_sheet()
    phone.wait(f"() => !{MEDIA}.paused")
    page.locator("[data-phone-play]").click()
    phone.wait(f"() => {MEDIA}.paused")
    phone.check("the player on a phone")


# ---- the composer --------------------------------------------------------------

def test_the_composer_is_on_the_assistant_tab_only_and_grows(phone):
    page = phone.page
    phone.open(phone.ids[MEETING_TITLE])
    for name in ("summary", "transcript", "notes"):
        phone.tab(name)
        assert page.locator("[data-phone-composer]").count() == 0, name
        assert page.locator("[data-phone-detail] textarea:visible").count() == 0, name
    phone.tab("chat")
    composer = page.locator("[data-phone-composer]")
    field = composer.locator("textarea")
    field.wait_for(state="visible", timeout=WAIT)
    one_line = field.bounding_box()["height"]
    assert 40 <= one_line <= 48, one_line
    # Between the chat and the player strip
    strip = page.locator("[data-mobile-player]").bounding_box()
    box = composer.bounding_box()
    assert abs(box["y"] + box["height"] - strip["y"]) <= 2, (box, strip)
    # It grows with its text, to about five lines, and shrinks again
    field.fill("one\ntwo\nthree")
    phone.wait("(h) => document.querySelector('[data-phone-composer] textarea').getBoundingClientRect().height > h + 30", one_line)
    three = field.bounding_box()["height"]
    field.fill("\n".join(str(n) for n in range(1, 13)))
    phone.wait("(h) => document.querySelector('[data-phone-composer] textarea').getBoundingClientRect().height > h", three)
    tall = field.bounding_box()["height"]
    assert tall <= 126, tall
    phone.no_sideways_scroll("the composer")
    phone.shot("phone-layout-test-composer")
    field.fill("")
    phone.wait("(h) => document.querySelector('[data-phone-composer] textarea').getBoundingClientRect().height <= h + 1", one_line)
    send = composer.locator("button").last.bounding_box()
    assert send["width"] >= 44 and send["height"] >= 44, send

    phone.check("the composer")


def test_the_composer_stays_above_the_keyboard(browser, phone):
    """The keyboard shrinks the visible height; the page follows it (--app-height), hides
    the player strip and the tabs, and keeps the composer on screen."""
    session = Phone(browser)
    try:
        page = session.page
        session.open(session.ids[MEETING_TITLE], tab="chat")
        field = page.locator("[data-phone-composer] textarea")
        field.wait_for(state="visible", timeout=WAIT)
        field.click()
        page.set_viewport_size({"width": PHONE["width"], "height": 440})
        session.wait("() => document.documentElement.classList.contains('keyboard-open')")
        session.wait("() => { const b = document.querySelector('[data-phone-composer]').getBoundingClientRect(); return b.height > 0 && b.bottom <= window.innerHeight + 1; }")
        assert not page.locator("[data-mobile-bottom-nav]").is_visible()
        assert not page.locator("[data-phone-player-strip]").is_visible()
        assert field.is_visible() and page.locator(BAR).is_visible()
        # The details block makes room for the chat
        assert not page.locator("[data-phone-details]").is_visible()
        session.shot("phone-layout-test-keyboard")
        page.set_viewport_size(PHONE)
        session.wait("() => !document.documentElement.classList.contains('keyboard-open')")
        page.locator("[data-mobile-bottom-nav]").wait_for(state="visible", timeout=WAIT)
        session.check("the composer with the keyboard up")
    finally:
        session.close()


# ---- the list ------------------------------------------------------------------

def test_the_list_is_a_screen_with_search_filters_and_new_recording(phone):
    page = phone.page
    phone.open_list()
    # No page header over the list, no empty state: the list is the screen
    assert page.locator("#app > header").count() == 0
    side = page.locator("aside.sidebar").bounding_box()
    assert round(side["x"]) == 0 and round(side["width"]) == PHONE["width"], side
    bar = page.locator(LIST_BAR)
    assert bar.locator("[data-phone-menu]").is_visible() and bar.locator("[data-phone-search]").is_visible()
    assert bar.locator("[data-phone-more-filters]").is_visible() and bar.locator("[data-phone-select]").is_visible()
    chips = page.locator("[data-phone-filters] [data-phone-filter]").evaluate_all("(els) => els.map((el) => el.dataset.phoneFilter)")
    for chip in ("sort", "inbox", "starred", "archived"):
        assert chip in chips, chips
    assert set(chips) <= {"sort", "folder", "inbox", "starred", "archived", "shared", "clear"}, chips
    # The chips wrap; nothing scrolls sideways
    row = page.locator("[data-phone-filters]")
    assert row.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1 && getComputedStyle(el).flexWrap === 'wrap'")
    phone.no_sideways_scroll("the list")
    titles = page.locator(ROWS).all_inner_texts()
    assert any(MEETING_TITLE in t for t in titles) and any(LONG_TITLE in t for t in titles), titles
    phone.shot("phone-layout-test-list")

    # With a recording open behind the list, a search that finds it keeps the list on screen
    page.locator(ROWS).filter(has_text=LONG_TITLE).first.click()
    page.locator(BAR).wait_for(state="visible", timeout=WAIT)
    phone.wait("() => document.querySelector('aside.sidebar').classList.contains('collapsed')")
    page.locator("[data-phone-back]").click()
    phone.list_in_place()
    bar.locator("[data-phone-search]").fill("long transcript")
    phone.wait("(title) => { const rows = [...document.querySelectorAll('aside.sidebar .recording-group > *')]; return rows.length === 1 && rows[0].innerText.includes(title); }", LONG_TITLE)
    page.wait_for_timeout(1500)
    assert not page.locator("aside.sidebar").evaluate("(el) => el.classList.contains('collapsed')"), "the search closed the list"
    phone.list_in_place()
    bar.locator("[data-phone-search]").fill("")
    phone.wait("() => document.querySelectorAll('aside.sidebar .recording-group > *').length >= 2")

    # Search
    search = bar.locator("[data-phone-search]")
    search.fill("long transcript")
    phone.wait("(title) => { const rows = [...document.querySelectorAll('aside.sidebar .recording-group > *')]; return rows.length === 1 && rows[0].innerText.includes(title); }", LONG_TITLE)
    # A filter chip
    search.fill("")
    phone.wait("() => document.querySelectorAll('aside.sidebar .recording-group > *').length >= 2")
    inbox = page.locator('[data-phone-filter="inbox"]')
    inbox.click()
    phone.wait("() => document.querySelector('[data-phone-filter=\"inbox\"]').getAttribute('aria-pressed') === 'true'")
    phone.wait("() => document.querySelectorAll('aside.sidebar .recording-group > *').length === 0")
    page.locator('[data-phone-filter="clear"]').click()
    phone.wait("() => document.querySelectorAll('aside.sidebar .recording-group > *').length >= 2")
    assert inbox.get_attribute("aria-pressed") == "false"
    # The sort chip names the order and changes it
    sort = page.locator('[data-phone-filter="sort"]')
    first = sort.inner_text().strip()
    sort.click()
    phone.wait("(was) => document.querySelector('[data-phone-filter=\"sort\"]').innerText.trim() !== was", first)
    sort.click()
    phone.wait("(was) => document.querySelector('[data-phone-filter=\"sort\"]').innerText.trim() === was", first)
    phone.wait("() => document.querySelectorAll('aside.sidebar .recording-group > *').length >= 2")

    # The further filters, behind the button in the search field
    filters = phone.open_sheet("[data-phone-more-filters]", "filters")
    text = filters.inner_text()
    for part in (en("sidebar.needsTranscription"), en("sidebar.needsSummary"), en("sidebar.needsSpeakers"),
                 en("tags.filterByTag"), en("sidebar.dateRange")):
        assert part in text, part
    filters.locator('[data-phone-needs="summary"]').click()
    phone.wait("() => document.querySelector('[data-phone-needs=\"summary\"]').getAttribute('aria-pressed') === 'true'")
    phone.no_sideways_scroll("the filters sheet")
    filters.locator("[data-phone-filters-done]").click()
    page.locator(SHEET).wait_for(state="detached", timeout=WAIT)
    phone.wait("() => document.querySelector('[data-phone-more-filters]').getAttribute('aria-pressed') === 'true'")
    page.locator('[data-phone-filter="clear"]').click()
    phone.wait("() => document.querySelector('[data-phone-more-filters]').getAttribute('aria-pressed') === 'false'")
    phone.wait("() => document.querySelectorAll('aside.sidebar .recording-group > *').length >= 2")

    # Selection mode
    bar.locator("[data-phone-select]").click()
    done = bar.locator("[data-phone-select-done]")
    done.wait_for(state="visible", timeout=WAIT)
    assert page.locator("[data-phone-new]").count() == 0
    page.locator(ROWS).filter(has_text=MEETING_TITLE).first.click()
    bulk = page.locator("[data-bulk-bar]")
    bulk.wait_for(state="visible", timeout=WAIT)
    box = bulk.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"] + 0.5 and box["y"] > PHONE["height"] / 2, box
    assert page.locator(BAR).count() == 0 or not page.locator(BAR).is_visible() or page.locator("aside.sidebar.collapsed").count() == 0
    phone.no_sideways_scroll("selection mode")
    done.click()
    bulk.wait_for(state="detached", timeout=WAIT)

    # New recording: a button over the list that opens the upload and record choices
    new = page.locator("[data-phone-new]")
    new.wait_for(state="visible", timeout=WAIT)
    box = new.bounding_box()
    assert box["height"] >= 44 and box["x"] + box["width"] <= PHONE["width"] and box["y"] + box["height"] <= PHONE["height"], box
    assert en("nav.newRecording") in new.inner_text()
    new.click()
    dialog = page.locator(".modal-overlay:visible").filter(has_text=en("upload.title"))
    dialog.first.wait_for(state="visible", timeout=WAIT)
    # The dialog is over the list, not under it
    top = dialog.first.locator(".modal-title").first
    assert top.evaluate("(el) => { const r = el.getBoundingClientRect(); const hit = document.elementFromPoint(r.left + 4, r.top + r.height / 2); return !!hit && !!hit.closest('.modal-overlay'); }")
    dialog.first.locator(".modal-close, button[aria-label='Close'], .modal-header button").last.click()
    dialog.first.wait_for(state="hidden", timeout=WAIT)
    phone.check("the list on a phone")


# ---- widths ----------------------------------------------------------------------

@pytest.mark.parametrize("width", [360, 430])
def test_no_sideways_scrolling(browser, phone, width):
    session = Phone(browser, {"width": width, "height": 800})
    try:
        page = session.page
        recording_id = session.ids[MEETING_TITLE]
        session.patches[recording_id] = {
            "meeting_date": "2026-03-02T10:02:00", "created_at": "2026-03-05T09:15:00",
            "title": "A recording with a long title that takes two lines of the top bar on a small phone, and then some",
            "participants": "Dana Whitlock, Marco Ibarra, Priya Raman, Tomas Ferreira, Lena Okafor, Jun Akiyama"}
        session.open_list()
        session.no_sideways_scroll(f"{width}: list")
        for opener, name in (("[data-phone-menu]", "menu"), ("[data-phone-more-filters]", "filters")):
            session.open_sheet(opener, name)
            session.no_sideways_scroll(f"{width}: {name}")
            session.close_sheet()
        session.open(recording_id)
        title = page.locator("[data-phone-title]").bounding_box()
        assert title["height"] <= 2 * 16 * 1.25 * 1.4 + 6, f"{width}: the title is taller than two lines: {title}"
        for name in ("summary", "transcript", "notes", "chat"):
            session.tab(name)
            session.no_sideways_scroll(f"{width}: {name}")
            inner = page.locator(SCROLL).evaluate("(el) => [el.scrollWidth, el.clientWidth]")
            assert inner[0] <= inner[1] + 1, f"{width}: {name} scrolls sideways inside: {inner}"
        session.tab("transcript")
        page.locator("[data-phone-find]").click()
        page.locator(f"{TBAR} {FIND} input").wait_for(state="visible", timeout=WAIT)
        bar = page.locator(TBAR).evaluate("(el) => [el.scrollWidth, el.clientWidth]")
        assert bar[0] <= bar[1] + 1, f"{width}: the pinned bar is wider than the screen: {bar}"
        buttons = page.locator(f"{TBAR} button:visible").evaluate_all(
            "(els) => els.map((el) => { const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right)]; })")
        assert all(left >= 0 and right <= width for left, right in buttons), f"{width}: {buttons}"
        page.locator("[data-phone-find]").click()
        session.tab("summary")
        for opener, name in (("[data-phone-more]", "actions"), ("[data-phone-info]", "info"), ("[data-phone-player-open]", "player")):
            sheet = session.open_sheet(opener, name)
            session.no_sideways_scroll(f"{width}: {name}")
            inner = sheet.locator(".ph-sheet__body").evaluate("(el) => [el.scrollWidth, el.clientWidth]")
            assert inner[0] <= inner[1] + 1, f"{width}: the {name} sheet scrolls sideways: {inner}"
            session.close_sheet()
        # Every icon button of the layout is a 44px target
        small = page.evaluate("""() => [...document.querySelectorAll('.ph-ib')].filter((el) => el.getClientRects().length)
            .map((el) => el.getBoundingClientRect()).filter((r) => r.width < 44 || r.height < 42).map((r) => [r.width, r.height])""")
        assert not small, f"{width}: small touch targets: {small}"
        session.shot(f"phone-layout-test-{width}")
        session.check(f"a {width}px phone")
    finally:
        session.close()


# ---- the themes ------------------------------------------------------------------

@pytest.mark.parametrize("theme", THEMES)
def test_the_same_structure_in_every_theme(browser, phone, theme):
    session = Phone(browser)
    saved = session.page.locator("html").get_attribute("data-ui-theme") or "default"
    try:
        page = session.page
        session.set_theme(theme)
        session.open_list()
        assert page.locator("html").get_attribute("data-ui-theme") == theme
        side = page.locator("aside.sidebar").bounding_box()
        assert round(side["x"]) == 0 and round(side["width"]) == PHONE["width"], f"{theme}: the list is not a full screen: {side}"
        assert page.locator(LIST_BAR).is_visible() and page.locator("[data-phone-new]").is_visible()
        assert page.locator("#app > header").count() == 0
        session.no_sideways_scroll(f"{theme}: list")
        session.shot(f"phone-layout-test-theme-{theme}-list")
        page.locator(ROWS).filter(has_text=MEETING_TITLE).first.click()
        page.locator(BAR).wait_for(state="visible", timeout=WAIT)
        page.wait_for_function("() => !!document.querySelector('[data-phone-duration]')", timeout=WAIT)
        session.wait("() => document.querySelector('aside.sidebar').classList.contains('collapsed')")
        # Top to bottom: top bar, the scrolling area with the details, the player strip, the tabs
        order = page.evaluate("""() => ['[data-phone-bar="recording"]', '[data-phone-scroll]', '[data-mobile-player]', '[data-mobile-bottom-nav]']
            .map((s) => document.querySelector(s).getBoundingClientRect()).map((r) => [Math.round(r.top), Math.round(r.bottom), Math.round(r.width)])""")
        assert order[0][0] == 0, f"{theme}: {order}"
        for above, below in zip(order, order[1:]):
            assert abs(above[1] - below[0]) <= 1, f"{theme}: a gap or an overlap between the regions: {order}"
        assert order[-1][1] == PHONE["height"], f"{theme}: {order}"
        assert all(width == PHONE["width"] for _, _, width in order), f"{theme}: {order}"
        assert page.locator("[data-phone-tab]").count() == 4
        assert page.locator(f"{BAR} > *").count() == 4
        sizes = page.locator(f"{BAR} .ph-ib, [data-phone-tab], [data-phone-play]").evaluate_all(
            "(els) => els.map((el) => Math.min(el.getBoundingClientRect().width, el.getBoundingClientRect().height))")
        assert min(sizes) >= 44, f"{theme}: {sizes}"
        session.no_sideways_scroll(f"{theme}: summary")
        session.shot(f"phone-layout-test-theme-{theme}-summary")
        # The pinned bar pins in this theme too
        session.tab("transcript")
        page.locator(SEGMENT).first.wait_for(state="visible", timeout=60000)
        page.evaluate("() => { const s = document.querySelector('[data-phone-scroll]'); s.scrollTop = s.scrollHeight; }")
        session.wait("() => document.querySelector('[data-phone-scroll]').scrollTop > 100")
        place = bar_place(page)
        assert abs(place["barTop"] - place["topBarBottom"]) <= 1 and place["switchOnTop"], f"{theme}: {place}"
        # A theme's own sticky speaker label is under the bar, not over the switch
        covered = page.evaluate("""() => { const bar = document.querySelector('[data-phone-transcript-bar]').getBoundingClientRect();
            return [...document.querySelectorAll('.speaker-run > .speaker-tablet, .speaker-run > .speaker-tablet-group')]
                .map((el) => el.getBoundingClientRect()).filter((r) => r.height > 0 && r.top < bar.bottom - 1 && r.bottom > bar.top + 1)
                .filter((r) => { const hit = document.elementFromPoint(r.left + 3, Math.max(r.top, bar.top) + 3); return !!hit && !hit.closest('[data-phone-transcript-bar]'); }).length; }""")
        assert covered == 0, f"{theme}: a speaker label covers the pinned bar"
        session.no_sideways_scroll(f"{theme}: transcript")
        session.shot(f"phone-layout-test-theme-{theme}-transcript")
        session.tab("chat")
        assert page.locator("[data-phone-composer] textarea").is_visible()
        height = page.locator("[data-phone-composer] textarea").bounding_box()["height"]
        assert 40 <= height <= 48, f"{theme}: the composer is {height}px high"
        session.tab("summary")
        sheet = session.open_sheet("[data-phone-more]", "actions")
        assert sheet.locator("[data-phone-action]").count() >= 18
        heights = sheet.locator(".ph-row").evaluate_all("(els) => els.map((el) => el.getBoundingClientRect().height)")
        assert min(heights) >= 44, f"{theme}: {heights}"
        session.no_sideways_scroll(f"{theme}: actions")
        session.shot(f"phone-layout-test-theme-{theme}-actions")
        session.close_sheet()
        sheet = session.open_sheet("[data-phone-player-open]", "player")
        assert sheet.locator("[data-phone-speed]").count() >= 5
        session.no_sideways_scroll(f"{theme}: player")
        session.close_sheet()
        session.check(f"the phone layout in the {theme} theme", utilities=False)
    finally:
        session.set_theme(saved)
        session.close()


# ---- the desktop -----------------------------------------------------------------

def test_desktop_switch_stays_in_view_while_the_transcript_scrolls(browser, phone, long_transcript):
    desk = Phone(browser, {"width": 1400, "height": 900})
    try:
        page = desk.page
        desk.leave()
        page.goto(f"{BASE}/recordings/{desk.ids[LONG_TITLE]}")
        page.locator(f"#leftMainColumn {SEGMENT}").first.wait_for(state="visible", timeout=60000)
        assert page.locator(BAR).count() == 0 and page.locator("[data-phone-detail]").count() == 0
        switch = page.locator("#leftMainColumn .tabs-bar .tab")
        assert switch.count() == 2
        before = [box for box in (switch.nth(0).bounding_box(), switch.nth(1).bounding_box())]
        page.evaluate("""() => { const list = [...document.querySelectorAll('#leftMainColumn [data-tv-list]')].find((el) => el.getClientRects().length);
            let s = list.parentElement; while (s && !/auto|scroll/.test(getComputedStyle(s).overflowY)) s = s.parentElement; s.scrollTop = s.scrollHeight; }""")
        page.wait_for_function(f"(last) => !!document.querySelector(`#leftMainColumn [data-segment-index='${{last}}']`)", arg=LARGE_COUNT - 1, timeout=WAIT)
        after = [switch.nth(0).bounding_box(), switch.nth(1).bounding_box()]
        assert after == before, (before, after)
        assert all(0 <= box["y"] and box["y"] + box["height"] <= 900 for box in after), after
        assert page.evaluate("() => window.scrollY") == 0
        desk.check("the desktop transcription panel")
    finally:
        desk.close()


def test_an_incognito_recording_on_the_desktop_has_no_download_control(browser, phone):
    desk = Phone(browser, {"width": 1400, "height": 900})
    try:
        page = desk.page
        open_incognito(desk)
        page.locator(f"#leftMainColumn {SEGMENT}").first.wait_for(state="visible", timeout=60000)
        # Transcript: copy and edit stay, download is gone
        tools = page.locator("#leftMainColumn .tabs-bar__actions")
        assert tools.locator(f"button[title='{en('tooltips.copyTranscript')}']").is_visible()
        assert page.locator("[data-download-toggle]").count() == 0
        # Summary: copy and edit stay, download is gone, also in the overflow menu
        right = page.locator("#rightMainColumn")
        right.locator(f"button[title='{en('buttons.copySummary')}']").first.wait_for(state="attached", timeout=WAIT)
        assert page.locator("[data-summary-download-toggle]").count() == 0
        assert right.locator(f"[title='{en('buttons.downloadSummary')}']").count() == 0
        # Notes
        right.locator(".tabs .tab").filter(has_text=en("notes.title")).first.click()
        right.locator(f"button[title='{en('buttons.copyNotes')}']").first.wait_for(state="attached", timeout=WAIT)
        assert right.locator(f"[title='{en('buttons.downloadNotes')}']").count() == 0
        assert right.locator("button").filter(has_text=en("buttons.downloadNotes")).count() == 0
        desk.check("an incognito recording on the desktop")
    finally:
        if page.url.startswith(BASE):
            page.evaluate("() => sessionStorage.removeItem('speakr_incognito_recording')")
        desk.close()
