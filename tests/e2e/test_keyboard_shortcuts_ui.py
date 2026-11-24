"""Browser test of the keyboard shortcuts.

The shortcuts are one table of bindings and one keydown listener per page
(static/js/modules/utils/keyboard-shortcuts.js, utils/shortcut-help.js; the
keys of the main app are in composables/shortcuts.js). This test checks, in
the browser, what the unit tests cannot: that each key calls the function of
its button, and that the keys leave the rest of the app alone.

- The help: opened with ? and from the account menu, generated from the
  table, in the app's own dialog, closed with Esc, in every UI theme, light
  and dark, with AA contrast for its text and keys.
- The recording list: j and k move a frame, Enter and o open that recording,
  Esc takes the frame back to the open recording, s and i change the star
  and the inbox mark through the app's own routes, # asks in the delete
  dialog and deletes nothing.
- The open recording and the player: the tab keys (with events given to the
  page for the fourth), the view key, the summary editor, archive and
  unarchive, play and pause with p and with Space, skipping and the speed.
- No key fires while text is typed in the search field, the notes editor or
  the chat field, and Enter and Space on a focused button press that button.
  Space plays only while nothing has the focus.
- One Esc closes one thing: the dialog before the panel under it. A dialog
  with a changed field is not closed by Esc; the same dialog unchanged is.
- Ctrl+F still opens the find bar of a long transcript, and the segment
  editor keeps its own keys.
- g then a letter opens another page, where the help lists that page's keys.
- The account setting turns every key off, and the phone layout has none.

The queue's answer and a long transcript are given to the page by the test,
so the instance's data stays as it is, apart from the star, the inbox mark
and the archive mark, which are set back, and the account setting, which is
turned on again.

Every test fails on a JavaScript error, a same-origin response of 400 or
higher, template text left in the page and a dialog of the browser.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test. With
SPEAKR_E2E_SHOTS set, screenshots are saved in that directory.
"""

import datetime
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

from browser_checks import PageProblems, check_common, en, login, precondition_unmet  # noqa: E402
from seed_recordings import TITLES  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")

LONG_TITLE, MEETING_TITLE = TITLES
THEMES = ("default", "cards", "classic", "desk", "focus", "workspace")
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}
# Loose limits: a slow machine must not fail the test
WAIT = 30000

HELP = "[data-shortcut-help]"
DIALOG = "[data-app-dialog]"
CURSOR = ".recording-row[data-kbd-cursor]"
MODAL = ".modal-overlay:not(.app-dialog):visible"
CHAT_FIELD = ".floating-chat-root textarea"
PANEL = '[data-queue-panel="dock"]'
FIND = "[data-transcript-find]"

# The help as the reader sees it: its groups, and per row the label and the keys
HELP_ROWS = """() => [...document.querySelectorAll('[data-shortcut-help] [data-shortcut-group]')].map((group) => ({
    group: group.dataset.shortcutGroup,
    heading: group.querySelector('.shortcut-help__heading').textContent.trim(),
    rows: [...group.querySelectorAll('.shortcut-help__row')].map((row) => ({
        label: row.querySelector('dd').textContent.trim(),
        keys: [...row.querySelectorAll('.shortcut-help__combo')].map((combo) => [...combo.querySelectorAll('kbd')].map((k) => k.textContent.trim()).join(' ')),
    })),
}))"""

# Colors as the browser paints them, and the contrast of the help's text and keys
HELP_LOOK = """() => {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    const rgba = (color) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data]; };
    const over = (top, under) => { const a = top[3] / 255; return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat(255); };
    const lum = (c) => { const [r, g, b] = c.slice(0, 3).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
    const contrast = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
    const style = (el) => getComputedStyle(el);
    const panel = document.querySelector('[data-app-dialog] .modal-panel');
    const surface = over(rgba(style(panel).backgroundColor), [255, 255, 255, 255]);
    const help = document.querySelector('[data-shortcut-help]');
    const key = help.querySelector('kbd');
    const keySurface = over(rgba(style(key).backgroundColor), surface);
    const box = panel.getBoundingClientRect();
    const text = (el, under) => contrast(over(rgba(style(el).color), under), under);
    return {
        label: text(help.querySelector('dd'), surface),
        heading: text(help.querySelector('.shortcut-help__heading'), surface),
        then: text(help.querySelector('.shortcut-help__then'), surface),
        key: text(key, keySurface),
        keyBorder: parseFloat(style(key).borderTopWidth),
        panel: { left: box.left, top: box.top, right: box.right, bottom: box.bottom },
        columns: new Set([...help.querySelectorAll('[data-shortcut-group]')].map((g) => Math.round(g.getBoundingClientRect().left))).size,
        sideways: help.closest('.modal-body').scrollWidth - help.closest('.modal-body').clientWidth,
    };
}"""

AUDIO = """() => {
    const a = [...document.querySelectorAll('audio, video')].find((el) => el.currentSrc || el.src);
    return a ? { paused: a.paused, time: a.currentTime, rate: a.playbackRate, ready: a.readyState, duration: a.duration } : null;
}"""


def iso(seconds_ago=0):
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def queued_job(recording_id, title):
    """One waiting row of the queue status answer (docs/advanced/job-queue-system.md)."""
    return {
        "id": 1, "recording_id": recording_id, "recording_title": title, "job_status": "queued", "job_type": "summarize",
        "queue_type": "summary", "is_new_upload": False, "error_message": None, "stage": "queued", "stage_detail": None,
        "progress": None, "progress_kind": None, "progress_done": None, "progress_total": None,
        "position": 1, "queue_position": 1, "queue": "summary", "cancel_requested": False, "can_cancel": True,
        "created_at": iso(900), "started_at": None, "completed_at": None, "finished_at": None,
    }


def many_segments(count=400):
    """More segments than the page renders whole, so the transcript has its own find bar."""
    return [{"speaker": f"SPEAKER_0{i % 2}", "sentence": f"Segment {i}: budget and hiring.", "start_time": i * 0.2, "end_time": i * 0.2 + 0.2}
            for i in range(count)]


class App:
    """The signed-in admin, with the queue's answer given by the test."""

    signed_in = None

    def __init__(self, browser, viewport=None, dark=False):
        viewport = viewport or DESKTOP
        phone = viewport["width"] < 768
        self.phone = phone
        # Answers are changed on their way to the browser, which a route can
        # only do while no service worker answers the request
        self.context = browser.new_context(viewport=viewport, locale="en-US", timezone_id="UTC", is_mobile=phone, has_touch=phone,
                                           color_scheme="dark" if dark else "light", storage_state=App.signed_in,
                                           service_workers="block")
        self.context.add_init_script("try { localStorage.setItem('darkMode', '%s'); } catch (e) {}" % ("true" if dark else "false"))
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        self.dialogs = []
        self.page.on("dialog", self._dialog)
        self.jobs = []
        self.patches = {}
        if App.signed_in is None:
            login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
            App.signed_in = self.context.storage_state()
        listing = self.page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        self.ids = {r["title"]: r["id"] for r in items if r.get("title") in TITLES}
        self.page.route(re.compile(r"/api/recordings/job-queue-status$"), self._status)
        self.page.route(re.compile(r"/api/recordings/\d+$"), self._recording)

    def _dialog(self, dialog):
        self.dialogs.append(dialog.message)
        dialog.dismiss()

    def _status(self, route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"server_time": iso(), "jobs": self.jobs}))

    def _recording(self, route):
        """A seeded recording may be changed on its way to the page."""
        recording_id = int(route.request.url.rsplit("/", 1)[1])
        patch = self.patches.get(recording_id)
        if route.request.method != "GET" or not patch:
            return route.continue_()
        response = route.fetch()
        body = response.json()
        body.update(patch)
        route.fulfill(response=response, body=json.dumps(body), headers={**response.headers, "content-type": "application/json"})

    # ---- the page ----------------------------------------------------------

    def close(self):
        self.context.close()

    def csrf(self):
        return self.page.locator('meta[name="csrf-token"]').first.get_attribute("content")

    def set_prefs(self, **prefs):
        response = self.page.request.post(f"{BASE}/api/user/preferences", headers={"X-CSRFToken": self.csrf(), "Content-Type": "application/json"},
                                          data=json.dumps(prefs))
        assert response.ok, f"could not save {prefs}: HTTP {response.status}"

    def record(self, title):
        """The recording as the instance has it."""
        return self.page.request.get(f"{BASE}/api/recordings/{self.ids[title]}").json()

    def leave(self):
        self.page.goto("about:blank")
        self.page.wait_for_timeout(300)
        self.problems.reset()

    def open(self, recording=MEETING_TITLE):
        """Open the app on a recording (or on the list with None)."""
        self.leave()
        if recording is None:
            self.page.goto(f"{BASE}/")
            self.page.evaluate("() => localStorage.removeItem('lastSelectedRecordingId')")
            self.page.goto(f"{BASE}/")
        else:
            self.page.goto(f"{BASE}/recordings/{self.ids[recording]}")
        try:
            self.page.locator("aside.sidebar").wait_for(state="attached", timeout=60000)
        except Exception as error:
            raise AssertionError(f"the app did not load: {self.page.url} shows {self.page.inner_text('body')[:300]!r}") from error
        if recording is not None and self.phone:
            self.page.locator('[data-phone-bar="recording"]').wait_for(state="visible", timeout=60000)
            self.wait("() => { const el = document.querySelector('[data-phone-detail]'); return !!el && el.dataset.phoneRecord === 'full'; }", timeout=120000)
        elif recording is not None:
            self.page.locator("#mainContentColumns").wait_for(state="visible", timeout=60000)
            # The full record has arrived: its transcript is on the page
            self.page.locator("[data-tv-list]").first.wait_for(state="attached", timeout=60000)
        else:
            self.page.locator("aside.sidebar .recording-group > *").first.wait_for(state="visible", timeout=60000)
        self.rendered()

    def open_page(self, path):
        """Open a page outside the main app and wait for its header."""
        self.leave()
        self.page.goto(f"{BASE}{path}")
        self.page.locator("[data-user-menu-toggle]").wait_for(state="visible", timeout=60000)
        self.rendered()

    def rendered(self):
        """Two frames later: what the app was told is on the page."""
        self.page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 50))))")

    def wait(self, script, arg=None, timeout=WAIT):
        self.page.wait_for_function(script, arg=arg, timeout=timeout)

    def press(self, *keys):
        """Press keys one after the other, with the page rendered after each."""
        for key in keys:
            self.page.keyboard.press(key)
            try:
                self.rendered()
            except playwright_api.Error:
                # The key opened another page
                self.page.wait_for_load_state("load")

    def blur(self):
        """Take the focus out of any field, as a click on an empty part of the page does."""
        self.page.evaluate("() => { if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur(); }")

    def cursor(self):
        """The id of the framed row of the list, or None."""
        return self.page.evaluate("""() => {
            const row = [...document.querySelectorAll('.recording-row[data-kbd-cursor]')].find((el) => el.getClientRects().length);
            return row ? Number(row.dataset.recordingId) : null;
        }""")

    def row_ids(self):
        return self.page.evaluate("() => [...document.querySelectorAll('aside.sidebar .recording-row')].filter((el) => el.getClientRects().length).map((el) => Number(el.dataset.recordingId))")

    def open_id(self):
        """The id of the open recording, from the address."""
        match = re.search(r"/recordings/(\d+)", self.page.url)
        return int(match.group(1)) if match else None

    def audio(self):
        return self.page.evaluate(AUDIO)

    def help_open(self):
        return self.page.locator(HELP).count() == 1 and self.page.locator(HELP).is_visible()

    def nothing_opened(self, label):
        assert self.page.locator(DIALOG).count() == 0, f"{label}: a dialog was opened"
        assert self.page.locator(MODAL).count() == 0, f"{label}: a modal was opened"

    def check(self, label, utilities=True):
        assert not self.dialogs, f"{label}: a dialog of the browser was shown: {self.dialogs}"
        if utilities:
            return check_common(self.page, self.problems, label)
        body = self.page.inner_text("body")
        leftovers = [m for m in ("${", "{{") if m in body]
        assert not leftovers, f"{label}: template text left in the page: {leftovers}"
        assert not self.problems.failed, f"{label}: failed requests: {self.problems.failed}"
        assert not self.problems.errors, f"{label}: JavaScript errors: {self.problems.errors}"

    def shot(self, name):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            self.page.wait_for_timeout(300)
            self.page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))

    def set_shortcuts(self, on):
        """Turn the account's keyboard shortcuts on or off with the checkbox under Account, Preferences."""
        self.open_page("/account")
        page = self.page
        page.locator('button:has([data-i18n="accountTabs.preferences"]), a:has([data-i18n="accountTabs.preferences"])').first.click()
        box = page.locator("#prefKeyboardShortcuts")
        box.wait_for(state="visible", timeout=WAIT)
        box.scroll_into_view_if_needed()
        if box.is_checked() != on:
            box.click()
        assert box.is_checked() == on
        with page.expect_response(lambda r: r.url.rstrip("/").endswith("/account") and r.request.method == "POST", timeout=WAIT) as saved:
            page.locator('button[type="submit"][form="preferencesForm"]').click()
        assert saved.value.ok, f"the preferences were not saved: HTTP {saved.value.status}"


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def desk(browser):
    """The desktop session of the module (the default theme, light)."""
    App.signed_in = None
    session = App(browser)
    if set(session.ids) != set(TITLES):
        session.close()
        precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    session.open()
    saved = session.page.evaluate("() => [document.documentElement.dataset.uiTheme || 'default', document.documentElement.classList.contains('dark')]")
    session.set_prefs(ui_theme="default", dark_mode=False)
    yield session
    if not session.page.url.startswith(BASE):
        session.page.goto(f"{BASE}/")
    session.set_prefs(ui_theme=saved[0], dark_mode=saved[1])
    session.close()


@pytest.fixture(autouse=True)
def plain_state(request):
    """Each test starts with an empty queue, no changed recording and no dialog of the browser."""
    session = request.getfixturevalue("desk") if "desk" in request.fixturenames else None
    if session:
        session.jobs = []
        session.patches.clear()
        session.dialogs.clear()
    yield


# ---- the help --------------------------------------------------------------------

def test_the_help_lists_the_keys_of_the_page_and_closes_with_escape(desk):
    page = desk.page
    desk.open()
    desk.press("?")
    page.locator(HELP).wait_for(state="visible", timeout=WAIT)
    # It is the app's own message dialog, with a title and one button
    assert page.locator('[data-app-dialog="alert"]').count() == 1
    assert page.locator(f"{DIALOG} .modal-title").inner_text().strip() == en("shortcuts.title")
    assert page.locator(f"{DIALOG} [data-app-dialog-confirm]").inner_text().strip() == en("common.close")

    groups = page.evaluate(HELP_ROWS)
    assert [g["group"] for g in groups] == ["general", "navigation", "list", "recording", "player", "editor", "video"]
    assert [g["heading"] for g in groups] == [en(f"shortcuts.groups.{g['group']}") for g in groups]
    rows = {row["label"]: row["keys"] for g in reversed(groups) for row in g["rows"]}
    # Every label is a translated text, never its key
    assert not [label for label in rows if not label or label.startswith("shortcuts.")], rows
    assert rows[en("shortcuts.showHelp")] == ["?"]
    assert rows[en("shortcuts.close")] == ["Esc"]
    assert rows[en("shortcuts.moveInList")] == ["j", "k"]
    assert rows[en("shortcuts.openRecording")] == ["Enter", "o"]
    assert rows[en("shortcuts.goRecordings")] == ["g r"]
    assert rows[en("shortcuts.goSettings")] == ["g s"]
    assert rows[en("shortcuts.goAdmin")] == ["g d"], "the seeded account is an administrator"
    assert rows[en("shortcuts.playPause")] == ["p", en("shortcuts.keys.space")], "play and pause in the Player group"
    assert not [label for label in rows if "dark" in label.lower()], "there is no key for light and dark"
    assert rows[en("shortcuts.skip15")] == ["Shift H", "Shift L"]
    assert rows[en("shortcuts.findInTranscript")] == ["Ctrl F"]
    assert rows[en("shortcuts.openChat").replace("{{name}}", "Ambrose")] == ["m"]
    # A sequence is written with "then" between its keys
    assert page.locator(f"{HELP} .shortcut-help__then").first.inner_text().strip() == en("shortcuts.then")
    assert page.locator(f"{HELP} .shortcut-help__note").inner_text().strip() == en("shortcuts.settingHint")
    desk.shot("keyboard-shortcuts-help")

    # While it is open, no key reaches the page: no second dialog, no moved frame
    desk.press("?", "j", "p")
    assert page.locator(DIALOG).count() == 1
    assert desk.cursor() is None and desk.audio()["paused"]
    desk.press("Escape")
    page.locator(HELP).wait_for(state="detached", timeout=WAIT)

    # The entry of the account menu opens the same help, and shows the key
    page.locator("[data-user-menu-toggle]").click()
    entry = page.locator("[data-user-menu-dropdown] [data-shortcuts-help]")
    entry.wait_for(state="visible", timeout=WAIT)
    assert en("shortcuts.title") in entry.inner_text() and entry.locator("kbd").inner_text().strip() == "?"
    entry.click()
    page.locator(HELP).wait_for(state="visible", timeout=WAIT)
    assert page.locator("[data-user-menu-dropdown]").count() == 0, "the menu stayed open under the help"
    page.locator(f"{DIALOG} [data-app-dialog-confirm]").click()
    page.locator(HELP).wait_for(state="detached", timeout=WAIT)
    desk.check("help")


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("dark", (False, True), ids=("light", "dark"))
def test_the_help_in_every_theme(browser, desk, theme, dark):
    session = App(browser, dark=dark)
    try:
        session.open()
        session.set_prefs(ui_theme=theme, dark_mode=dark)
        session.open()
        page = session.page
        where = f"{theme} {'dark' if dark else 'light'}"
        assert page.locator("html").get_attribute("data-ui-theme") == theme
        assert page.evaluate("() => document.documentElement.classList.contains('dark')") == dark
        session.press("?")
        page.locator(HELP).wait_for(state="visible", timeout=WAIT)
        page.wait_for_timeout(300)  # the panel's opening animation
        look = page.evaluate(HELP_LOOK)
        for part in ("label", "heading", "then", "key"):
            assert look[part] >= 4.5, f"{where}: {part} has a contrast of {look[part]:.2f}"
        assert look["keyBorder"] >= 1, f"{where}: a key has no border"
        box = look["panel"]
        assert box["left"] >= 0 and box["top"] >= 0 and box["right"] <= DESKTOP["width"] and box["bottom"] <= DESKTOP["height"], f"{where}: {box}"
        assert look["columns"] == 2, f"{where}: the groups are in {look['columns']} columns"
        assert look["sideways"] <= 0, f"{where}: the help scrolls sideways by {look['sideways']}px"
        assert len(page.evaluate(HELP_ROWS)) == 7, where
        session.shot(f"keyboard-shortcuts-help-{theme}-{'dark' if dark else 'light'}")
        session.press("Escape")
        page.locator(HELP).wait_for(state="detached", timeout=WAIT)
        # The frame of the list in this theme
        session.press("j")
        framed = session.cursor()
        assert framed is not None and framed != session.open_id(), where
        outline = page.evaluate("() => { const s = getComputedStyle(document.querySelector('.recording-row[data-kbd-cursor]')); return [s.outlineStyle, parseFloat(s.outlineWidth)]; }")
        assert outline[0] == "solid" and outline[1] >= 2, f"{where}: the framed row has the outline {outline}"
        session.shot(f"keyboard-shortcuts-list-{theme}-{'dark' if dark else 'light'}")
        session.check(where, utilities=False)
    finally:
        if session.page.url.startswith(BASE):
            session.set_prefs(ui_theme="default", dark_mode=False)
        session.close()


# ---- the recording list ----------------------------------------------------------

def test_j_and_k_move_in_the_list_and_enter_opens(desk):
    page = desk.page
    desk.open(None)
    rows = desk.row_ids()
    assert len(rows) >= 2 and desk.open_id() is None
    assert desk.cursor() is None
    desk.press("j")
    assert desk.cursor() == rows[0], "j from nowhere frames the first row"
    desk.press("j")
    assert desk.cursor() == rows[1]
    desk.press("k")
    assert desk.cursor() == rows[0]
    desk.press("k")
    assert desk.cursor() == rows[0], "the list does not wrap"
    assert desk.open_id() is None, "moving opens nothing"

    desk.press("j", "Enter")
    desk.wait("(id) => location.pathname === '/recordings/' + id", arg=rows[1])
    page.locator("#mainContentColumns").wait_for(state="visible", timeout=WAIT)
    assert desk.cursor() is None, "the open recording has its own mark, not the frame"
    assert page.evaluate("(id) => document.querySelector('.recording-row[data-recording-id=\"' + id + '\"]').classList.contains('is-active')", rows[1])
    # Enter without a framed row does nothing
    desk.press("Enter")
    assert desk.open_id() == rows[1]

    # o opens as Enter does, and from the open recording k goes to its neighbor
    desk.press("k")
    assert desk.cursor() == rows[0]
    desk.press("o")
    desk.wait("(id) => location.pathname === '/recordings/' + id", arg=rows[0])
    # Esc takes the frame back to the open recording
    desk.press("j")
    assert desk.cursor() == rows[1]
    desk.press("Escape")
    assert desk.cursor() is None and desk.open_id() == rows[0]
    desk.check("list")


def test_star_inbox_and_delete_keys_call_the_functions_of_their_buttons(desk):
    page = desk.page
    desk.open(MEETING_TITLE)
    opened = desk.ids[MEETING_TITLE]
    other = desk.ids[LONG_TITLE]
    before = desk.record(MEETING_TITLE)

    def saved(field, recording_id, value):
        """The instance has the value: the key went through the app's own route."""
        page.wait_for_function("([base, id, field, v]) => fetch(`${base}/api/recordings/${id}`).then((r) => r.json()).then((d) => d[field] === v)",
                               arg=[BASE, recording_id, field, value], timeout=WAIT)
        desk.rendered()

    try:
        # On the open recording: the header's star and inbox buttons
        starred = "() => getComputedStyle(document.querySelector('header .fa-star').closest('button')).color === 'rgb(234, 179, 8)'"
        desk.press("s")
        saved("is_highlighted", opened, not before["is_highlighted"])
        assert page.evaluate(starred) == (not before["is_highlighted"]), "the header's star does not show the change"
        desk.press("s")
        saved("is_highlighted", opened, bool(before["is_highlighted"]))
        desk.press("i")
        saved("is_inbox", opened, not before["is_inbox"])
        desk.press("i")
        saved("is_inbox", opened, bool(before["is_inbox"]))

        # On the framed row: the other recording gets the star, the open one does not
        was = desk.record(LONG_TITLE)["is_highlighted"]
        desk.press("j" if desk.row_ids().index(other) > desk.row_ids().index(opened) else "k")
        assert desk.cursor() == other
        desk.press("s")
        saved("is_highlighted", other, not was)
        assert desk.record(MEETING_TITLE)["is_highlighted"] == before["is_highlighted"]
        desk.press("s")
        saved("is_highlighted", other, bool(was))

        # # asks in the delete dialog, about the framed recording, and Esc closes it
        desk.press("#")
        modal = page.locator(MODAL)
        modal.wait_for(state="visible", timeout=WAIT)
        assert modal.locator(".modal-title").inner_text().strip() == en("modal.deleteRecording")
        assert LONG_TITLE in modal.locator(".modal-body").inner_text()
        desk.press("j", "s", "n")
        assert modal.count() == 1, "a key of the page was handled under the dialog"
        desk.press("Escape")
        modal.wait_for(state="detached", timeout=WAIT)
        assert set(desk.row_ids()) >= {opened, other}, "a recording was deleted"
        assert page.request.get(f"{BASE}/api/recordings/{other}").ok
    finally:
        now = desk.record(MEETING_TITLE)
        headers = {"X-CSRFToken": desk.csrf(), "Content-Type": "application/json"}
        if now["is_highlighted"] != before["is_highlighted"]:
            page.request.post(f"{BASE}/recording/{opened}/toggle_highlight", headers=headers)
        if now["is_inbox"] != before["is_inbox"]:
            page.request.post(f"{BASE}/recording/{opened}/toggle_inbox", headers=headers)
    desk.check("list actions")


def test_new_recording_and_search_keys(desk):
    page = desk.page
    desk.open()
    desk.press("n")
    upload = page.locator(MODAL)
    upload.wait_for(state="visible", timeout=WAIT)
    desk.blur()
    desk.press("j")
    assert desk.cursor() is None, "a list key was handled under the upload dialog"
    desk.press("Escape")
    upload.wait_for(state="detached", timeout=WAIT)

    desk.press("/")
    field = page.locator("[data-shortcut-search]:visible")
    field.wait_for(state="visible", timeout=WAIT)
    assert page.evaluate("() => document.activeElement.hasAttribute('data-shortcut-search')")
    assert field.input_value() == "", "the key itself was typed into the field"
    desk.check("new and search")


# ---- the open recording and the player -------------------------------------------

def test_tab_and_view_keys(desk):
    page = desk.page
    recording_id = desk.ids[MEETING_TITLE]
    desk.open(MEETING_TITLE)

    # The tabs beside the transcript are the second tab bar of the page
    def tabs():
        return page.evaluate("() => [...document.querySelectorAll('#mainContentColumns .tabs-bar')][1].querySelector('.tab.is-active span').textContent.trim()")

    def tab_names():
        return page.evaluate("() => [...[...document.querySelectorAll('#mainContentColumns .tabs-bar')][1].querySelectorAll('.tab')].map((t) => t.querySelector('span').textContent.trim())")

    # The seeded recording has two speakers, so it has speaker stats; it has no events
    assert tab_names() == [en("summary.title"), en("notes.title"), en("stats.title")]
    assert tabs() == en("summary.title")
    desk.press("2")
    assert tabs() == en("notes.title")
    desk.press("3")
    assert tabs() == en("stats.title")
    assert page.locator("#mainContentColumns").get_by_text("SPEAKER_00").first.is_visible(), "the speaker stats are not shown"
    desk.press("1")
    assert tabs() == en("summary.title")
    # Without events 4 has no button to stand for
    desk.press("4")
    assert tabs() == en("summary.title")

    bubble = "() => !!document.querySelector('#mainContentColumns .transcription-with-speakers')"
    assert page.evaluate(bubble) is False
    desk.press("v")
    desk.wait(bubble)
    desk.press("v")
    desk.wait(f"() => !({bubble})()")
    desk.check("tabs and view")

    # With events the fourth tab is there, and 4 shows it
    desk.patches[recording_id] = {"events": [{"id": 901, "title": "Design review", "description": "Screens",
                                              "start_datetime": "2026-03-10T14:00:00", "end_datetime": "2026-03-10T14:30:00",
                                              "location": "Room 2", "attendees": ["Dana", "Marco"]}]}
    desk.open(MEETING_TITLE)
    assert tab_names() == [en("summary.title"), en("notes.title"), en("stats.title"), en("events.title")]
    desk.press("4")
    assert tabs() == en("events.title")
    assert page.locator("#mainContentColumns").get_by_text("Design review").first.is_visible()
    desk.press("3")
    assert tabs() == en("stats.title")
    desk.press("1")
    desk.check("events tab")


def test_shift_e_edits_the_summary(desk):
    page = desk.page
    desk.open(MEETING_TITLE)
    summary = desk.record(MEETING_TITLE)["summary"]
    assert summary, "the seeded recording has a summary"
    editor = page.locator("#mainContentColumns .CodeMirror")
    assert editor.count() == 0
    # Without Shift, e is the transcript's key: only Shift+E opens the summary editor
    desk.press("Shift+E")
    editor.wait_for(state="visible", timeout=WAIT)
    assert page.evaluate("() => document.querySelector('#mainContentColumns .CodeMirror').CodeMirror.getValue()").strip() == summary.strip()
    assert page.locator('.modal-overlay[data-shortcut-scope="editor"]').count() == 0, "the transcript editor opened"
    # While the editor is shown the key has no button to stand for
    desk.blur()
    desk.press("Shift+E")
    assert editor.count() == 1
    page.locator("#mainContentColumns button", has_text=en("buttons.cancel")).first.click()
    editor.wait_for(state="detached", timeout=WAIT)
    assert desk.record(MEETING_TITLE)["summary"] == summary
    desk.check("summary editor")


def test_a_archives_and_unarchives(desk):
    page = desk.page
    desk.open(MEETING_TITLE)
    opened = desk.ids[MEETING_TITLE]
    assert desk.record(MEETING_TITLE)["is_archived"] is False
    archived = "([base, id, v]) => fetch(`${base}/api/recordings/${id}`).then((r) => r.json()).then((d) => d.is_archived === v)"
    pressed = "(v) => document.querySelector('header .fa-box-archive').closest('button').getAttribute('aria-pressed') === v"
    try:
        desk.press("a")
        page.wait_for_function(archived, arg=[BASE, opened, True], timeout=WAIT)
        desk.wait(pressed, arg="true")
        # An archived recording leaves the list and stays open
        desk.wait("(id) => ![...document.querySelectorAll('aside.sidebar .recording-row')].some((el) => el.dataset.recordingId === String(id))", arg=opened)
        assert desk.open_id() == opened
        # Its row is gone, so the key acts on the open recording: unarchived again
        desk.press("a")
        page.wait_for_function(archived, arg=[BASE, opened, False], timeout=WAIT)
        desk.wait(pressed, arg="false")
    finally:
        if desk.record(MEETING_TITLE)["is_archived"]:
            page.request.post(f"{BASE}/recording/{opened}/toggle_archive", headers={"X-CSRFToken": desk.csrf(), "Content-Type": "application/json"})
    assert desk.record(MEETING_TITLE)["is_archived"] is False
    desk.check("archive")


def test_player_keys(desk):
    page = desk.page
    desk.open(MEETING_TITLE)
    desk.wait(f"() => {{ const a = ({AUDIO})(); return !!a && a.ready >= 1 && a.duration > 30; }}", timeout=60000)
    saved_rate = desk.audio()["rate"]
    try:
        assert desk.audio()["paused"]
        desk.press("p")
        desk.wait(f"() => !({AUDIO})().paused")
        desk.press("p")
        desk.wait(f"() => ({AUDIO})().paused")

        # Space while nothing has the focus: the same button, and the key is kept from the browser
        desk.blur()
        assert page.evaluate("() => document.activeElement === document.body")
        page.evaluate("() => { window.__space = []; document.addEventListener('keydown', (e) => { if (e.key === ' ') setTimeout(() => window.__space.push(e.defaultPrevented)); }); }")
        desk.press("Space")
        desk.wait(f"() => !({AUDIO})().paused")
        desk.press("Space")
        desk.wait(f"() => ({AUDIO})().paused")
        desk.wait("() => window.__space.length === 2")
        assert page.evaluate("() => window.__space") == [True, True]

        # Space on a focused button presses that button and plays nothing
        notes = page.locator("#mainContentColumns .tabs-bar .tab", has_text=en("notes.title")).first
        notes.focus()
        desk.press("Space")
        assert notes.evaluate("(el) => el.classList.contains('is-active')"), "Space did not press the focused tab"
        page.wait_for_timeout(300)
        assert desk.audio()["paused"], "Space on a button started the audio"
        # Space in the recording list, which it scrolls, is left to the browser
        page.locator("aside.sidebar .sidebar-list").focus()
        desk.press("Space")
        page.wait_for_timeout(300)
        assert desk.audio()["paused"], "Space in the list started the audio"
        desk.wait("() => window.__space.length === 4")
        assert page.evaluate("() => window.__space.slice(2)") == [False, False], "a Space that was not handled was kept from the browser"
        desk.blur()
        desk.press("1")

        page.evaluate("() => { const a = [...document.querySelectorAll('audio, video')].find((el) => el.currentSrc || el.src); a.currentTime = 30; }")
        desk.wait(f"() => Math.abs(({AUDIO})().time - 30) < 0.5")
        for key, moved in (("l", 35), ("Shift+L", 50), ("h", 45), ("Shift+H", 30)):
            desk.press(key)
            desk.wait(f"(t) => Math.abs(({AUDIO})().time - t) < 0.75", arg=moved)

        page.evaluate("() => document.querySelectorAll('audio, video').forEach((el) => { el.playbackRate = 1; })")
        desk.press("Shift+>")
        desk.wait(f"() => ({AUDIO})().rate === 1.25")
        desk.press("Shift+<", "Shift+<")
        desk.wait(f"() => ({AUDIO})().rate === 0.75")
        assert page.locator("[data-speed-toggle]").inner_text().strip().startswith("0.75")
    finally:
        page.evaluate("(rate) => { localStorage.setItem('playbackRate', rate); }", saved_rate)
    desk.check("player")


def test_copy_and_edit_keys(desk):
    page = desk.page
    desk.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
    desk.open(MEETING_TITLE)
    page.evaluate("() => navigator.clipboard.writeText('')")
    desk.press("c")
    desk.wait("() => navigator.clipboard.readText().then((text) => text.includes('budget'))")
    desk.press("Shift+C")
    desk.wait("() => navigator.clipboard.readText().then((text) => text.includes('seeded for the browser tests'))")

    # e opens the segment editor, which has its own keys and its own help
    desk.press("e")
    editor = page.locator('.modal-overlay[data-shortcut-scope="editor"]')
    editor.wait_for(state="visible", timeout=WAIT)
    page.locator(".asr-seg__text").first.wait_for(state="visible", timeout=WAIT)
    desk.blur()
    desk.press("j", "?")
    assert desk.cursor() is None, "a list key was handled under the segment editor"
    assert page.locator(HELP).count() == 0, "the page's help opened over the segment editor"
    page.locator(".asr-help").wait_for(state="visible", timeout=WAIT)
    desk.press("Escape")
    page.locator(".asr-help").wait_for(state="detached", timeout=WAIT)
    assert editor.count() == 1, "Esc on the editor's help closed the editor"
    editor.locator(".modal-close").last.click()
    editor.wait_for(state="detached", timeout=WAIT)
    desk.check("copy and edit")


# ---- typing ----------------------------------------------------------------------

def test_no_key_fires_while_text_is_typed(desk):
    page = desk.page
    desk.open(MEETING_TITLE)
    opened = desk.open_id()
    typed = "jk gs p? #n"

    def untouched(where):
        desk.nothing_opened(where)
        assert desk.cursor() is None, f"{where}: the frame moved"
        assert desk.open_id() == opened and page.url.startswith(f"{BASE}/recordings/"), f"{where}: the page changed"
        assert desk.audio()["paused"], f"{where}: the audio plays"

    # The search field of the list
    desk.press("/")
    field = page.locator("[data-shortcut-search]:visible")
    field.wait_for(state="visible", timeout=WAIT)
    page.keyboard.type(typed)
    desk.rendered()
    # Every character was typed, the spaces among them
    assert field.input_value() == typed
    untouched("search field")
    field.fill("")
    desk.blur()
    page.locator(f"aside.sidebar .recording-row[data-recording-id='{opened}']").wait_for(state="visible", timeout=WAIT)

    # The notes editor (Shift+E on the Notes tab)
    desk.press("2", "Shift+E")
    editor = page.locator("#mainContentColumns .CodeMirror")
    editor.wait_for(state="visible", timeout=WAIT)
    editor.click()
    page.keyboard.type(typed)
    desk.rendered()
    assert typed in page.evaluate("() => document.querySelector('#mainContentColumns .CodeMirror').CodeMirror.getValue()")
    untouched("notes editor")
    page.locator("#mainContentColumns button", has_text=en("buttons.cancel")).first.click()
    editor.wait_for(state="detached", timeout=WAIT)
    desk.press("1")

    # The chat field (m opens the panel with the focus in it)
    desk.press("m")
    chat = page.locator(CHAT_FIELD)
    chat.wait_for(state="visible", timeout=WAIT)
    desk.wait("() => document.activeElement && document.activeElement.tagName === 'TEXTAREA'")
    page.keyboard.type(typed)
    desk.rendered()
    assert chat.input_value() == typed
    untouched("chat field")
    # Esc in the chat field closes its panel, and nothing else
    desk.press("Escape")
    chat.wait_for(state="detached", timeout=WAIT)
    untouched("after the chat")
    desk.check("typing")


def test_enter_on_a_focused_button_presses_the_button(desk):
    page = desk.page
    desk.open(MEETING_TITLE)
    opened = desk.open_id()
    desk.press("j" if desk.row_ids().index(opened) == 0 else "k")
    framed = desk.cursor()
    assert framed is not None and framed != opened
    notes = page.locator("#mainContentColumns .tabs-bar .tab", has_text=en("notes.title")).first
    notes.focus()
    desk.press("Enter")
    assert notes.evaluate("(el) => el.classList.contains('is-active')"), "Enter did not press the focused tab"
    assert desk.open_id() == opened, "Enter on a button opened the framed recording"
    # Space as well: the button, and no key of the page
    summary = page.locator("#mainContentColumns .tabs-bar .tab", has_text=en("summary.title")).first
    summary.focus()
    desk.press("Space")
    assert summary.evaluate("(el) => el.classList.contains('is-active')")
    # A letter is still a shortcut there
    desk.press("2")
    assert notes.evaluate("(el) => el.classList.contains('is-active')")
    desk.press("1", "Escape")
    assert desk.cursor() is None
    desk.check("focused button")


# ---- Escape ----------------------------------------------------------------------

def test_escape_closes_the_dialog_before_the_panel(desk):
    page = desk.page
    desk.jobs = [queued_job(9001, "Kickoff call")]
    desk.open(MEETING_TITLE)
    page.locator("[data-queue-indicator]").first.wait_for(state="visible", timeout=WAIT)
    panel = page.locator(PANEL)
    if panel.count() and panel.is_visible():
        desk.press("q")
        panel.wait_for(state="detached", timeout=WAIT)
    desk.press("q")
    panel.wait_for(state="visible", timeout=WAIT)
    desk.blur()

    # The delete dialog over the panel
    desk.press("#")
    modal = page.locator(MODAL)
    modal.wait_for(state="visible", timeout=WAIT)
    desk.press("?")
    assert page.locator(HELP).count() == 0, "the help opened over another dialog"
    desk.press("Escape")
    modal.wait_for(state="detached", timeout=WAIT)
    assert panel.is_visible(), "one Esc closed the dialog and the panel"

    # The help over the panel
    desk.press("?")
    page.locator(HELP).wait_for(state="visible", timeout=WAIT)
    desk.press("Escape")
    page.locator(HELP).wait_for(state="detached", timeout=WAIT)
    assert panel.is_visible(), "one Esc closed the help and the panel"

    # The chat panel is above the queue panel
    desk.press("m")
    page.locator(CHAT_FIELD).wait_for(state="visible", timeout=WAIT)
    desk.blur()
    desk.press("Escape")
    page.locator(CHAT_FIELD).wait_for(state="detached", timeout=WAIT)
    assert panel.is_visible(), "one Esc closed the chat and the queue panel"

    desk.press("Escape")
    panel.wait_for(state="detached", timeout=WAIT)
    assert page.locator("[data-queue-indicator]").first.is_visible()
    # Nothing is left to close: Esc does nothing
    desk.press("Escape")
    assert desk.open_id() == desk.ids[MEETING_TITLE]
    desk.check("escape order")


def test_escape_does_not_close_a_dialog_with_unsaved_input(desk):
    page = desk.page
    desk.open(MEETING_TITLE)
    dialog = page.locator(".modal-overlay:visible", has=page.locator(".spk-modal"))
    names = dialog.locator(".spk-name input[type=text]")

    def open_dialog():
        page.locator("header .fa-user-tag").first.click()
        dialog.wait_for(state="visible", timeout=WAIT)
        names.first.wait_for(state="visible", timeout=WAIT)
        desk.rendered()

    # Unchanged, with the focus on a button: Esc closes it as the Close button does
    open_dialog()
    dialog.locator(".modal-close").focus()
    desk.press("Escape")
    dialog.wait_for(state="detached", timeout=WAIT)

    # A changed field, with the focus on a button: Esc leaves the dialog open
    open_dialog()
    before = names.first.input_value()
    names.first.click()
    page.keyboard.type("Ana")
    assert names.first.input_value() == before + "Ana"
    # Esc in the field is the field's own key (its list of suggestions)
    desk.press("Escape")
    assert dialog.count() == 1
    dialog.locator(".modal-close").focus()
    desk.press("Escape")
    page.wait_for_timeout(400)
    assert dialog.count() == 1, "Esc closed a dialog with a changed field"
    assert names.first.input_value() == before + "Ana"
    # ... and with the focus nowhere
    desk.blur()
    desk.press("Escape")
    page.wait_for_timeout(400)
    assert dialog.count() == 1, "Esc closed a dialog with a changed field"

    # The field as it was: nothing is unsaved, and Esc closes the dialog
    names.first.fill(before)
    dialog.locator(".modal-close").focus()
    desk.press("Escape")
    dialog.wait_for(state="detached", timeout=WAIT)

    # A dialog in which the choice is made with buttons is never closed by Esc
    assert page.locator('[data-shortcut-scope="dialog"]').count() == 0
    desk.check("unsaved input")


def test_ctrl_f_still_opens_the_find_bar_of_a_long_transcript(desk):
    page = desk.page
    desk.patches[desk.ids[LONG_TITLE]] = {"transcription": json.dumps(many_segments())}
    desk.open(LONG_TITLE)
    desk.wait("() => document.querySelectorAll('[data-tv-list] [data-segment-index]').length > 0")
    page.keyboard.press("Control+f")
    bar = page.locator(FIND)
    bar.wait_for(state="visible", timeout=WAIT)
    desk.wait("() => !!document.activeElement.closest('[data-transcript-find]')")
    page.keyboard.type("hiring jk")
    desk.rendered()
    assert bar.locator("input").input_value() == "hiring jk"
    assert desk.cursor() is None
    # Esc in the bar is the bar's own key
    desk.press("Escape")
    bar.wait_for(state="detached", timeout=WAIT)
    # With the focus elsewhere, Esc closes the bar too
    page.keyboard.press("Control+f")
    bar.wait_for(state="visible", timeout=WAIT)
    desk.blur()
    desk.press("Escape")
    bar.wait_for(state="detached", timeout=WAIT)
    desk.check("ctrl+f")


# ---- other pages -----------------------------------------------------------------

def test_g_then_a_letter_opens_a_page_and_its_help(desk):
    page = desk.page
    desk.open(MEETING_TITLE)
    desk.press("g", "s")
    page.wait_for_url(re.compile(r"/account$"), timeout=WAIT)
    page.locator("[data-user-menu-toggle]").wait_for(state="visible", timeout=60000)
    page.wait_for_timeout(500)
    desk.problems.reset()  # requests of the page that was left

    desk.press("?")
    page.locator(HELP).wait_for(state="visible", timeout=WAIT)
    groups = page.evaluate(HELP_ROWS)
    assert [g["group"] for g in groups] == ["general", "navigation"], "the account page lists the keys of the main app"
    assert en("shortcuts.moveInList") not in [row["label"] for g in groups for row in g["rows"]]
    desk.shot("keyboard-shortcuts-help-account")
    desk.press("Escape")
    page.locator(HELP).wait_for(state="detached", timeout=WAIT)

    # Light and dark have no key
    dark = "() => document.documentElement.classList.contains('dark')"
    desk.press("Shift+D", "d")
    assert page.evaluate(dark) is False
    desk.check("account", utilities=False)

    desk.press("g", "d")
    page.wait_for_url(re.compile(r"/admin$"), timeout=WAIT)
    page.locator("[data-user-menu-toggle]").wait_for(state="visible", timeout=60000)
    desk.press("g", "a")
    page.wait_for_url(re.compile(r"/inquire$"), timeout=WAIT)
    field = page.locator("#inquire-app textarea").first
    field.wait_for(state="visible", timeout=60000)
    page.wait_for_timeout(500)
    desk.blur()
    desk.press("/")
    desk.wait("() => document.activeElement && document.activeElement.tagName === 'TEXTAREA'")
    assert field.input_value() == ""
    page.keyboard.type("g r")
    assert field.input_value() == "g r" and page.url.endswith("/inquire")
    field.fill("")
    desk.blur()
    desk.press("?")
    page.locator(HELP).wait_for(state="visible", timeout=WAIT)
    assert [g["group"] for g in page.evaluate(HELP_ROWS)] == ["general", "navigation", "assistant"]
    desk.press("Escape")
    desk.press("g", "r")
    page.wait_for_url(re.compile(r"127\.0\.0\.1:\d+/(recordings/\d+)?$|/$"), timeout=WAIT)
    page.locator("aside.sidebar").wait_for(state="attached", timeout=60000)


def test_the_account_setting_turns_every_key_off(desk):
    page = desk.page
    try:
        desk.set_shortcuts(False)
        # Already on the account page: the keys are off after the next load
        desk.open(MEETING_TITLE)
        assert page.evaluate("() => window.speakrShortcuts.enabled") is False
        desk.press("?", "j", "p", "2", "m", "n")
        desk.nothing_opened("shortcuts off")
        assert desk.cursor() is None and desk.audio()["paused"]
        assert page.locator(CHAT_FIELD).count() == 0
        desk.press("g", "s")
        page.wait_for_timeout(400)
        assert desk.open_id() == desk.ids[MEETING_TITLE]
        page.locator("[data-user-menu-toggle]").click()
        page.locator("[data-user-menu-dropdown]").wait_for(state="visible", timeout=WAIT)
        assert page.locator("[data-shortcuts-help]").count() == 0, "the help entry is shown while shortcuts are off"
        page.locator("[data-user-menu-toggle]").click()
        desk.check("shortcuts off")
    finally:
        desk.set_shortcuts(True)
    desk.open(MEETING_TITLE)
    assert page.evaluate("() => window.speakrShortcuts.enabled") is True
    desk.press("?")
    page.locator(HELP).wait_for(state="visible", timeout=WAIT)
    desk.press("Escape")
    page.locator(HELP).wait_for(state="detached", timeout=WAIT)


def test_the_phone_layout_has_no_shortcuts(browser, desk):
    session = App(browser, PHONE)
    try:
        session.open(MEETING_TITLE)
        page = session.page
        # A hardware keyboard on a phone: the keys do nothing
        session.press("?", "j", "p", "2", "n", "g", "s")
        page.wait_for_timeout(300)
        session.nothing_opened("phone")
        assert session.cursor() is None
        assert session.open_id() == session.ids[MEETING_TITLE]
        assert page.locator('[data-phone-tab="summary"]').get_attribute("aria-current") == "true"
        assert page.locator("[data-shortcuts-help]:visible").count() == 0
        session.check("phone recording", utilities=False)

        # The account page on a phone: the menu has no help entry
        session.open_page("/account")
        page.locator("[data-user-menu-toggle]").click()
        page.locator("[data-user-menu-dropdown]").wait_for(state="visible", timeout=WAIT)
        assert page.locator("[data-shortcuts-help]:visible").count() == 0
        page.locator("[data-user-menu-toggle]").click()
        session.press("?")
        assert page.locator(HELP).count() == 0
        session.check("phone account", utilities=False)
    finally:
        session.close()
