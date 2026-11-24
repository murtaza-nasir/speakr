"""Browser test of the notifications (toasts) and of the processing queue.

Toasts (static/js/modules/utils/toast.js, styles.css "Toasts"): a raised card
in the fixed color of its type with a badge, a close button, a repeat count,
an action button and a line that counts the time down. This test checks each
type, how long each stays, the hold on hover, the repeat count, a stack, the
place (bottom center of the reading area, above the player; on a phone above
the queue strip, the player strip and the tab bar, and under the top bar
while a sheet is open), Escape, and in every UI theme, light and dark, that
the card is not a page color and that its text and icons have AA contrast.

Processing queue (templates/components/queue/, static/css/queue.css,
app.modular.js "THE QUEUE ON THE PAGE"): a one-line indicator at the foot of
the recording list and a panel that is docked there. This test checks the
text of the indicator for mixed states, the groups and their order, a job
stepped through queued, transcribing with part counts and summarizing, the
moving bar with the elapsed time where the server has no number (and that no
number is invented), the cancel of a waiting job, of a running job
("Canceling…") and of a running first transcription (asked about in the
app's own dialog), retry, delete, clear failed, clear finished, a finished
row that opens its recording, the empty state, the list that still scrolls
with the panel docked, the pill in the top bar while the list is collapsed,
the one clock of the elapsed times (stopped while the panel is minimized or
the tab is in the background), the strip and the sheet of the phone layout,
and one pass per UI theme. The card of a canceled first transcription is
checked in the recording view.

The answer of GET /api/recordings/job-queue-status is given to the page by
the test, and so are the answers of the cancel, retry and clear routes, so
the instance's data stays as it is. Two tests use the instance itself: an
upload held back in the browser is canceled, and a real upload is followed
through the queue of the instance to its end there; what it leaves is
deleted afterwards.

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
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, en, login, precondition_unmet  # noqa: E402
from seed_recordings import TITLES, write_tone  # noqa: E402
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
# The page asks for the queue every 5 seconds while something is in progress
POLL = 20000

TOAST = "#toastContainer .toast-message"
PANEL = "[data-queue-panel]"
ROW = "[data-queue-row]"
TYPE_COLORS = {"info": [37, 99, 235], "success": [21, 128, 61], "warning": [245, 158, 11], "error": [220, 38, 38]}
TYPE_GLYPHS = {"info": "fa-info", "success": "fa-check", "warning": "fa-exclamation", "error": "fa-xmark"}

SHOW = """async (args) => {
    const m = await import('/static/js/modules/utils/toast.js');
    m.showToast(...args);
}"""

# Colors as the browser paints them (a canvas resolves color-mix and oklab),
# and the contrast of a text or an icon against what lies behind it.
COLORS = """
const cv = document.createElement('canvas'); cv.width = cv.height = 1;
const cx = cv.getContext('2d', { willReadFrequently: true });
const parse = (c) => { if (!c || c === 'transparent') return null; cx.clearRect(0, 0, 1, 1); cx.fillStyle = '#000'; cx.fillStyle = c;
    cx.fillRect(0, 0, 1, 1); const d = cx.getImageData(0, 0, 1, 1).data; return { r: d[0], g: d[1], b: d[2], a: d[3] / 255 }; };
const over = (top, under) => ({ r: Math.round(top.r * top.a + under.r * (1 - top.a)), g: Math.round(top.g * top.a + under.g * (1 - top.a)),
    b: Math.round(top.b * top.a + under.b * (1 - top.a)), a: 1 });
const backdrop = (el) => { const layers = []; for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor);
        if (c && c.a > 0) { layers.push(c); if (c.a >= 0.999) break; } }
    let color = { r: 255, g: 255, b: 255, a: 1 }; for (const layer of layers.reverse()) color = over(layer, color); return color; };
const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
const ratio = (a, b) => { const x = lum(a), y = lum(b); return Math.round(100 * (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)) / 100; };
const contrast = (el) => { const bg = backdrop(el); return ratio(over(parse(getComputedStyle(el).color), bg), bg); };
const rgb = (c) => [c.r, c.g, c.b];
"""

TOAST_LOOK = "() => {" + COLORS + """
    const pages = ['body', 'aside.sidebar', 'main', '#app > header', '.ph-detail', '#mainContentColumns']
        .map((s) => document.querySelector(s)).filter((el) => el && el.getClientRects().length)
        .map((el) => parse(getComputedStyle(el).backgroundColor)).filter((c) => c && c.a > 0.5).map(rgb);
    return [...document.querySelectorAll('#toastContainer .toast-message')].map((toast) => {
        const badge = toast.querySelector('.toast__badge');
        const action = toast.querySelector('.toast__action');
        const style = getComputedStyle(toast);
        return {
            type: toast.dataset.toastType,
            role: toast.getAttribute('role'),
            glyph: badge.querySelector('i').className,
            badge: rgb(parse(getComputedStyle(badge).backgroundColor)),
            surface: rgb(backdrop(toast)),
            pages,
            border: parseFloat(style.borderTopWidth),
            borderColor: rgb(over(parse(style.borderTopColor), backdrop(toast))),
            shadow: style.boxShadow !== 'none',
            text: contrast(toast.querySelector('.toast__body')),
            glyphContrast: contrast(badge.querySelector('i')),
            close: contrast(toast.querySelector('.toast__close')),
            action: action ? contrast(action) : null,
            count: toast.querySelector('.toast__count').hidden ? null : contrast(toast.querySelector('.toast__count')),
        };
    });
}"""

QUEUE_LOOK = "() => {" + COLORS + """
    const panel = document.querySelector('[data-queue-panel]');
    const indicator = document.querySelector('[data-queue-indicator]');
    const host = panel || indicator;
    const around = host.closest('aside, .ph-sheet, header, footer') || document.body;
    const texts = [...host.querySelectorAll('.queue-row__title, .queue-row__stage, .queue-row__detail, .queue-row__num, .queue-row__error, ' +
        '.queue-row__reason, .queue-group__name, .queue-group__count, .queue-tbtn, .queue-panel__title strong, .queue-panel__title small, ' +
        '.queue-ind__part, .queue-sheet__summary')].filter((el) => el.getClientRects().length && el.textContent.trim());
    const icons = [...host.querySelectorAll('.queue-row__mark i, .queue-ibtn i, .queue-ind__chevron')].filter((el) => el.getClientRects().length);
    const worst = (els) => els.map((el) => [contrast(el), el.className + ' ' + el.textContent.trim().slice(0, 24)]).sort((a, b) => a[0] - b[0])[0];
    return {
        surface: rgb(backdrop(host)),
        around: rgb(backdrop(around)),
        border: parseFloat(getComputedStyle(host).borderTopWidth),
        text: worst(texts), icon: worst(icons), texts: texts.length, icons: icons.length,
    };
}"""

RECTS = """(selectors) => selectors.map((s) => { const el = [...document.querySelectorAll(s)].find((e) => e.getClientRects().length);
    if (!el) return null; const r = el.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height }; })"""

# Timers of one second that are running (the clock of the queue is one)
COUNT_TIMERS = """() => {
    if (window.__timers) return;
    const live = new Map(); window.__timers = live;
    const set = window.setInterval, clear = window.clearInterval;
    window.setInterval = (fn, ms, ...rest) => { const id = set(fn, ms, ...rest); live.set(id, ms); return id; };
    window.clearInterval = (id) => { live.delete(id); return clear(id); };
    window.__secondTimers = () => [...live.values()].filter((ms) => ms === 1000).length;
}"""


def iso(seconds_ago=0):
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def job(job_id, recording_id, title, status, job_type="transcribe", queue="transcription", **fields):
    """One row of the queue status answer (docs/advanced/job-queue-system.md)."""
    row = {
        "id": job_id, "recording_id": recording_id, "recording_title": title, "job_status": status,
        "job_type": job_type, "queue_type": queue, "is_new_upload": True, "error_message": None,
        "stage": {"queued": "queued", "completed": "completed", "failed": "failed"}.get(status), "stage_detail": None,
        "progress": None, "progress_kind": None, "progress_done": None, "progress_total": None,
        "position": None, "queue_position": None, "queue": None,
        "cancel_requested": False, "can_cancel": status in ("queued", "processing"),
        "created_at": iso(900), "started_at": None, "completed_at": None, "finished_at": None,
    }
    if status in ("completed", "failed"):
        row["completed_at"] = row["finished_at"] = iso(fields.pop("ago", 120))
    row.update(fields)
    return row


class App:
    """The signed-in admin on the main page, with the queue's answers given by the test."""

    signed_in = None

    def __init__(self, browser, viewport=None, dark=False, stub=True):
        viewport = viewport or DESKTOP
        phone = viewport["width"] < 768
        self.phone = phone
        # The page is rewritten on its way to the browser (the player position below), which a
        # route can only do while no service worker answers the navigation
        self.context = browser.new_context(viewport=viewport, locale="en-US", timezone_id="UTC", is_mobile=phone, has_touch=phone,
                                           color_scheme="dark" if dark else "light", storage_state=App.signed_in,
                                           service_workers="block")
        self.context.add_init_script("try { localStorage.setItem('darkMode', '%s'); } catch (e) {}" % ("true" if dark else "false"))
        self.context.add_init_script(f"({COUNT_TIMERS})()")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        self.dialogs = []
        self.page.on("dialog", self._dialog)
        self.jobs = []
        self.calls = []
        self.answers = {}
        self.patches = {}
        self.fake = None
        # 'top' or 'bottom' puts the desktop player there, whatever the account says
        self.player = None
        self.page.route(re.compile(r"/(recordings/\d+)?$"), self._document)
        if App.signed_in is None:
            login(self.page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
            App.signed_in = self.context.storage_state()
        listing = self.page.request.get(f"{BASE}/api/recordings?per_page=50")
        items = listing.json().get("recordings", []) if listing.ok else []
        self.ids = {r["title"]: r["id"] for r in items if r.get("title") in TITLES}
        if stub:
            self.page.route(re.compile(r"/api/recordings/job-queue-status$"), self._status)
            self.page.route(re.compile(r"/api/recordings/jobs/(\d+)(/retry)?$"), self._job)
            self.page.route(re.compile(r"/api/recordings/jobs/clear-(failed|completed)$"), self._clear)
        self.page.route(re.compile(r"/api/recordings/\d+$"), self._recording)

    def _dialog(self, dialog):
        self.dialogs.append(dialog.message)
        dialog.dismiss()

    # ---- what the page is told ------------------------------------------

    def _status(self, route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"server_time": iso(), "jobs": self.jobs}))

    def _job(self, route):
        request = route.request
        match = re.search(r"/jobs/(\d+)(/retry)?$", request.url)
        job_id, retry = int(match.group(1)), bool(match.group(2))
        self.calls.append((request.method, "retry" if retry else "job", job_id))
        found = next((j for j in self.jobs if j["id"] == job_id), None)
        if retry and found:
            found.update(job_status="queued", stage="queued", error_message=None, completed_at=None, finished_at=None,
                         can_cancel=True, queue="transcription", position=1, queue_position=1)
            return route.fulfill(status=200, content_type="application/json", body='{"success": true}')
        status, body = self.answers.get(job_id, (200, {"success": True, "canceled": True}))
        if status == 202 and found:
            found.update(cancel_requested=True, can_cancel=False)
        elif status == 200:
            self.jobs = [j for j in self.jobs if j["id"] != job_id]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def _clear(self, route):
        kind = "failed" if route.request.url.endswith("clear-failed") else "completed"
        self.calls.append((route.request.method, f"clear-{kind}", None))
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j["job_status"] != kind]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"success": True, "deleted": before - len(self.jobs)}))

    def _document(self, route):
        if not self.player or route.request.resource_type != "document":
            return route.continue_()
        response = route.fetch()
        body = re.sub(r'data-audio-player-position="\w+"', f'data-audio-player-position="{self.player}"', response.text())
        route.fulfill(response=response, body=body)

    def _recording(self, route):
        """A recording the queue names but the instance does not have is
        answered with a copy of a seeded one; a seeded one may be changed."""
        recording_id = int(route.request.url.rsplit("/", 1)[1])
        if route.request.method != "GET":
            return route.continue_()
        real = recording_id in self.ids.values()
        patch = self.patches.get(recording_id)
        if real and not patch:
            return route.continue_()
        if real:
            response = route.fetch()
            body = response.json()
            body.update(patch)
            return route.fulfill(response=response, body=json.dumps(body), headers={**response.headers, "content-type": "application/json"})
        if recording_id < 9000:
            return route.continue_()
        if self.fake is None:
            self.fake = self.page.request.get(f"{BASE}/api/recordings/{self.ids[MEETING_TITLE]}").json()
        body = dict(self.fake, id=recording_id, title=f"Queue test {recording_id}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    # ---- the page ----------------------------------------------------------

    def close(self):
        self.context.close()

    def csrf(self):
        return self.page.locator('meta[name="csrf-token"]').first.get_attribute("content")

    def set_prefs(self, **prefs):
        response = self.page.request.post(f"{BASE}/api/user/preferences", headers={"X-CSRFToken": self.csrf(), "Content-Type": "application/json"},
                                          data=json.dumps(prefs))
        assert response.ok, f"could not save {prefs}: HTTP {response.status}"

    def leave(self):
        self.page.goto("about:blank")
        self.page.wait_for_timeout(300)
        self.problems.reset()
        self.calls.clear()

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
            raise AssertionError(f"the app did not load: {self.page.url} shows {self.page.inner_text('body')[:60]!r}; "
                                 f"failed responses {self.problems.failed}, errors {self.problems.errors}, unanswered {self.problems.unanswered[-12:]}; DIAG "
                                 + str(self.page.evaluate("""() => ({ready: document.readyState, vue: typeof Vue, len: document.documentElement.outerHTML.length,
                                     tail: document.documentElement.outerHTML.slice(-300),
                                     scripts: [...document.scripts].map((x) => x.src.split('/').pop() || 'inline').slice(-8),
                                     res: performance.getEntriesByType('resource').filter((r) => r.responseEnd === 0 || r.transferSize === 0 && r.decodedBodySize === 0).map((r) => r.name.split('/').pop()).slice(0, 12)})"""))) from error
        if recording is not None and self.phone:
            self.page.locator('[data-phone-bar="recording"]').wait_for(state="visible", timeout=60000)
            self.wait("() => { const el = document.querySelector('[data-phone-detail]'); return !!el && el.dataset.phoneRecord === 'full'; }", timeout=120000)
        elif recording is not None:
            self.page.locator("#mainContentColumns").wait_for(state="visible", timeout=60000)
        else:
            self.page.locator("aside.sidebar .recording-group > *").first.wait_for(state="visible", timeout=60000)
        self.rendered()

    def rendered(self):
        """Two frames later: what the app was told is on the page."""
        self.page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 50))))")

    def wait(self, script, arg=None, timeout=WAIT):
        self.page.wait_for_function(script, arg=arg, timeout=timeout)

    def rects(self, *selectors):
        return self.page.evaluate(RECTS, list(selectors))

    def toast(self, message, icon="fa-info-circle", duration=3000, kind="info", action=False):
        args = [message, icon, duration, kind]
        if action:
            self.page.evaluate("""async (args) => {
                const m = await import('/static/js/modules/utils/toast.js');
                window.__toastActions = window.__toastActions || 0;
                m.showToast(...args, () => { window.__toastActions += 1; });
            }""", args)
        else:
            self.page.evaluate(SHOW, args)
        text = message["text"] if isinstance(message, dict) else message
        return self.page.locator(TOAST).filter(has_text=text.strip())

    def toasts_settled(self):
        """The toasts have finished moving in."""
        self.wait("() => [...document.querySelectorAll('#toastContainer .toast-message')].every((t) => t.classList.contains('toast-show') "
                  "&& t.getAnimations().filter((a) => a.constructor.name === 'CSSTransition').every((a) => a.playState === 'finished'))")

    def clear_toasts(self):
        self.page.evaluate("() => document.querySelectorAll('#toastContainer .toast__close').forEach((b) => b.click())")
        self.page.locator(TOAST).first.wait_for(state="detached", timeout=WAIT)

    def indicator(self, variant=None):
        return self.page.locator(f'[data-queue-indicator="{variant}"]' if variant else "[data-queue-indicator]")

    def indicator_text(self, text, variant=None):
        """Wait until the indicator reads `text` (without a variant: the
        indicator or, while the panel is open in its place, the line in the panel)."""
        selector = f'[data-queue-indicator="{variant}"] [data-queue-summary]' if variant else "[data-queue-summary]"
        self.wait("([selector, text]) => [...document.querySelectorAll(selector)].some((el) => el.getClientRects().length "
                  "&& el.innerText.replace(/\\s+/g, ' ').trim() === text)", [selector, text], timeout=POLL + WAIT)

    def open_panel(self):
        """Open the panel from the indicator unless it is open."""
        if self.page.locator(f"{PANEL}:visible").count() == 0:
            self.page.locator("[data-queue-indicator]:visible").first.click()
        self.page.locator(PANEL).first.wait_for(state="visible", timeout=WAIT)
        self.wait("() => document.querySelector('[data-queue-panel]').getAnimations().every((a) => a.playState === 'finished')")
        if self.phone:
            self.wait("() => document.querySelector('[data-phone-sheet] .ph-sheet').getAnimations().every((a) => a.playState === 'finished')")
        return self.page.locator(PANEL).first

    def row(self, title):
        return self.page.locator(f"{PANEL} {ROW}").filter(has_text=title)

    def wait_row(self, title, selector, text, timeout=POLL + WAIT):
        """Wait until the row with this title shows `text` in the element `selector`."""
        self.wait("([title, selector, text]) => [...document.querySelectorAll('[data-queue-panel] [data-queue-row]')].some((row) => "
                  "row.querySelector('.queue-row__title').textContent.trim() === title && "
                  "[...row.querySelectorAll(selector)].some((el) => el.innerText.replace(/\\s+/g, ' ').trim() === text))",
                  [title, selector, text], timeout=timeout)

    def groups(self):
        return self.page.evaluate("""() => [...document.querySelectorAll('[data-queue-panel] [data-queue-group]')].map((g) => ({
            name: g.dataset.queueGroup,
            title: g.querySelector('.queue-group__name').innerText.trim(),
            count: Number(g.querySelector('.queue-group__count').innerText),
            rows: [...g.querySelectorAll('[data-queue-row]')].map((r) => r.querySelector('.queue-row__title').textContent.trim()),
        }))""")

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


def mixed(app):
    """Three in progress, two waiting, one failed, two finished (one of them a recording of the instance)."""
    return [
        job(1, 9001, "Kickoff call", "processing", stage="transcribing", progress={"done": 3, "total": 8}, progress_kind="chunks",
            progress_done=3, progress_total=8, started_at=iso(134)),
        job(2, 9002, "One on one", "processing", stage="transcribing", stage_detail="aligning", started_at=iso(47)),
        job(3, 9003, "Design critique", "processing", job_type="summarize", queue="summary", is_new_upload=False,
            stage="summarizing", started_at=iso(12)),
        job(4, 9004, "Lecture on queues", "queued", position=2, queue_position=2, queue="transcription"),
        job(5, 9005, "Customer debrief", "queued", job_type="summarize", queue_type="summary", is_new_upload=False,
            position=1, queue_position=1, queue="summary"),
        job(6, 9006, "Pipeline review", "failed", can_cancel=False, ago=300,
            error_message="Connection error: could not reach the transcription service at the configured address"),
        job(7, app.ids[MEETING_TITLE], MEETING_TITLE, "completed", ago=120),
        job(8, 9008, "Contract walkthrough", "completed", ago=840),
    ]


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
    if session.page.url.startswith(BASE):
        session.set_prefs(ui_theme=saved[0], dark_mode=saved[1])
    session.close()


@pytest.fixture(autouse=True)
def plain_state(request):
    """Each test starts with an empty queue, no changed recording and no toasts."""
    session = request.getfixturevalue("desk") if "desk" in request.fixturenames else None
    if session:
        session.jobs = []
        session.player = None
        session.answers.clear()
        session.patches.clear()
        session.dialogs.clear()
    yield


# ---- toasts ----------------------------------------------------------------------

def test_each_toast_type_has_its_color_badge_role_and_time(desk):
    desk.open()
    page = desk.page
    desk.toast("Summary reprocessing started", "fa-info-circle", 3000, "info")
    desk.toast("Transcript saved", "fa-check-circle", 3000, "success")
    desk.toast("The screen may lock. Keep it on while you record.", "fa-exclamation-triangle", 3000, "warning")
    desk.toast({"text": "Transcription failed", "actionLabel": "Retry"}, "fa-exclamation-circle", 3000, "error", action=True)
    page.locator(TOAST).nth(3).wait_for(state="visible", timeout=WAIT)
    desk.toasts_settled()
    look = page.evaluate(TOAST_LOOK)
    assert [t["type"] for t in look] == ["info", "success", "warning", "error"]
    assert [t["role"] for t in look] == ["status", "status", "status", "alert"]
    for toast in look:
        kind = toast["type"]
        assert toast["badge"] == TYPE_COLORS[kind], f"{kind}: the badge is {toast['badge']}"
        assert TYPE_GLYPHS[kind] in toast["glyph"].split(), f"{kind}: {toast['glyph']}"
        assert toast["border"] >= 1 and toast["shadow"], f"{kind}: no border or no shadow"
        assert toast["surface"] not in toast["pages"], f"{kind}: the toast has a page color: {toast}"
        assert toast["text"] >= 4.5 and toast["close"] >= 4.5 and toast["glyphContrast"] >= 4.5, f"{kind}: contrast {toast}"
    assert look[3]["action"] >= 4.5
    # How long each stays: three seconds, eight for a warning, an error until it is closed
    lives = page.evaluate("""() => [...document.querySelectorAll('#toastContainer .toast-message')].map((t) => {
        const line = t.querySelector('.toast__countdown'); return line ? line.style.getPropertyValue('--toast-life') : null; })""")
    assert lives == ["3000ms", "3000ms", "8000ms", None]
    lines = page.evaluate("""() => [...document.querySelectorAll('#toastContainer .toast__countdown > i')].map((i) =>
        i.getAnimations().map((a) => [a.animationName, a.playState, a.effect.getTiming().duration]))""")
    assert lines == [[["toast-countdown", "running", 3000]], [["toast-countdown", "running", 3000]], [["toast-countdown", "running", 8000]]]
    desk.shot("toast-queue-test-toast-types")
    # The action is a button with a border; a click runs the action and closes the toast
    error = page.locator(TOAST).filter(has_text="Transcription failed")
    button = error.locator("button.toast__action")
    assert button.inner_text().strip() == "Retry"
    assert error.locator(".toast__body").inner_text().strip() == "Transcription failed"
    assert button.evaluate("(el) => parseFloat(getComputedStyle(el).borderTopWidth)") >= 1
    # The two that close in three seconds go; the warning and the error are still there then
    page.locator(TOAST).filter(has_text="Transcript saved").wait_for(state="detached", timeout=WAIT)
    page.locator(TOAST).filter(has_text="Summary reprocessing started").wait_for(state="detached", timeout=WAIT)
    assert page.locator(TOAST).filter(has_text="The screen may lock").is_visible()
    assert error.is_visible()
    # The warning goes after its eight seconds; the error stays
    page.locator(TOAST).filter(has_text="The screen may lock").wait_for(state="detached", timeout=WAIT)
    assert error.is_visible()
    button.click()
    error.wait_for(state="detached", timeout=WAIT)
    assert page.evaluate("() => window.__toastActions") == 1
    desk.check("toast types")


def test_hover_and_focus_hold_a_toast_and_escape_closes_it(desk):
    desk.open()
    page = desk.page
    toast = desk.toast("Held while the pointer is on it", "fa-check-circle", 3000, "success")
    toast.wait_for(state="visible", timeout=WAIT)
    toast.hover()
    desk.wait("() => document.querySelector('#toastContainer .toast-message').classList.contains('toast-paused')")
    # Well past its three seconds
    page.wait_for_timeout(4500)
    assert toast.is_visible()
    state = toast.locator(".toast__countdown > i").evaluate("(el) => el.getAnimations().map((a) => a.playState)")
    assert state == ["paused"], "the countdown line does not stop with the timer"
    page.mouse.move(5, 5)
    desk.wait("() => { const t = document.querySelector('#toastContainer .toast-message'); return !t || !t.classList.contains('toast-paused'); }")
    toast.wait_for(state="detached", timeout=WAIT)

    # Keyboard focus holds it too, and Escape closes it
    second = desk.toast("Held while it has the focus", "fa-check-circle", 3000, "success")
    second.wait_for(state="visible", timeout=WAIT)
    close = second.locator(".toast__close")
    assert close.get_attribute("aria-label") == en("toast.dismiss")
    close.focus()
    desk.wait("() => document.querySelector('#toastContainer .toast-message').classList.contains('toast-paused')")
    page.wait_for_timeout(4000)
    assert second.is_visible()
    page.keyboard.press("Escape")
    second.wait_for(state="detached", timeout=WAIT)
    desk.check("toast hold")


def test_the_same_message_is_counted_and_gets_its_time_again(desk):
    desk.open()
    page = desk.page
    toast = desk.toast("Transcript saved", "fa-check-circle", 600000, "success")
    toast.wait_for(state="visible", timeout=WAIT)
    assert toast.locator(".toast__count").is_hidden()
    toast.locator(".toast__countdown").evaluate("(el) => { el.dataset.first = '1'; }")
    desk.toast("Transcript saved", "fa-check-circle", 600000, "success")
    desk.wait("() => document.querySelector('#toastContainer .toast__count').textContent === '×2'")
    desk.toast("Transcript saved", "fa-check-circle", 600000, "success")
    desk.wait("() => document.querySelector('#toastContainer .toast__count').textContent === '×3'")
    assert page.locator(TOAST).count() == 1
    # A new countdown line: the time starts again
    assert toast.locator(".toast__countdown").count() == 1
    assert toast.locator(".toast__countdown").get_attribute("data-first") is None
    # The same words as another type are another toast
    desk.toast("Transcript saved", "fa-info-circle", 600000, "info")
    desk.wait("() => document.querySelectorAll('#toastContainer .toast-message').length === 2")
    desk.shot("toast-queue-test-toast-count")
    desk.clear_toasts()
    desk.check("toast count")


def test_a_stack_sits_at_the_bottom_center_of_the_reading_area_above_the_player(desk):
    desk.player = "bottom"
    desk.open()
    page = desk.page
    for index, kind in enumerate(("info", "success", "error")):
        desk.toast(f"Stacked message {index + 1}", "fa-info-circle", 600000, kind)
    page.locator(TOAST).nth(2).wait_for(state="visible", timeout=WAIT)
    desk.toasts_settled()
    boxes = page.evaluate("() => [...document.querySelectorAll('#toastContainer .toast-message')].map((t) => { const r = t.getBoundingClientRect(); "
                          "return [t.textContent.trim(), r.top, r.bottom, r.left, r.right]; })")
    assert [b[0][:17] for b in boxes] == ["Stacked message 1", "Stacked message 2", "Stacked message 3"]
    for above, below in zip(boxes, boxes[1:]):
        assert above[2] <= below[1], f"two toasts overlap: {boxes}"
    side, player, columns = desk.rects("aside.sidebar", "[data-toast-avoid]", "#mainContentColumns")
    assert player and player["top"] > DESKTOP["height"] / 2, "the player is not at the bottom of the recording"
    assert boxes[-1][2] <= player["top"], f"the toasts cover the player: {boxes[-1]} over {player}"
    assert boxes[0][1] >= columns["top"], "the stack reaches above the reading area"
    center = (side["right"] + DESKTOP["width"]) / 2
    for box in boxes:
        assert abs((box[3] + box[4]) / 2 - center) <= 2, f"not centered in the reading area ({center}): {box}"
        assert box[3] >= side["right"], f"a toast lies over the recording list: {box}"
    desk.shot("toast-queue-test-toast-stack")
    # With the list collapsed the reading area is the window: the stack follows
    page.locator("#app > header button:has(.fa-bars)").first.click()
    desk.wait("(want) => { const t = document.querySelector('#toastContainer .toast-message'); const r = t.getBoundingClientRect(); "
              "return Math.abs((r.left + r.right) / 2 - want) <= 2; }", DESKTOP["width"] / 2)
    page.locator("#app > header button:has(.fa-bars)").first.click()
    desk.wait("(want) => { const t = document.querySelector('#toastContainer .toast-message'); const r = t.getBoundingClientRect(); "
              "return Math.abs((r.left + r.right) / 2 - want) <= 2; }", center)
    desk.clear_toasts()
    desk.check("toast stack")

    # With the player at the top (the setting of a new account) the stack is at the bottom edge
    desk.player = "top"
    desk.open()
    desk.toast("Stacked message 1", "fa-info-circle", 600000, "info")
    desk.toast("Stacked message 2", "fa-info-circle", 600000, "error")
    page.locator(TOAST).nth(1).wait_for(state="visible", timeout=WAIT)
    desk.toasts_settled()
    player, first, last = desk.rects("[data-toast-avoid]", f"{TOAST}:first-child", f"{TOAST}:last-child")
    assert player["bottom"] < DESKTOP["height"] / 2, "the player is not at the top"
    assert 10 <= DESKTOP["height"] - last["bottom"] <= 30 and first["top"] > player["bottom"], f"{first} {last}"
    desk.clear_toasts()
    desk.check("toast stack, player at the top")


def test_an_old_call_gets_its_type_from_its_icon(desk):
    """Most callers pass only an icon, some the name of a type in its place."""
    desk.open()
    page = desk.page
    page.evaluate("""async () => {
        const m = await import('/static/js/modules/utils/toast.js');
        m.showToast('Could not save the tag', 'fa-exclamation-circle');
        m.showToast('Tag saved', 'fa-check-circle');
        m.showToast('Job queued for retry', 'success');
        m.showToast('Color scheme applied', 'fa-palette');
    }""")
    page.locator(TOAST).nth(3).wait_for(state="visible", timeout=WAIT)
    kinds = page.evaluate("() => [...document.querySelectorAll('#toastContainer .toast-message')].map((t) => [t.dataset.toastType, "
                          "t.getAttribute('role'), (t.querySelector('.toast__countdown') || { style: { getPropertyValue: () => null } }).style.getPropertyValue('--toast-life')])")
    # An error stays until it is closed, also when its type was read from the icon: it has no countdown
    assert kinds == [["error", "alert", None], ["success", "status", "3000ms"], ["success", "status", "3000ms"], ["info", "status", "3000ms"]]
    desk.clear_toasts()
    desk.check("toast types from icons")


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("dark", (False, True), ids=("light", "dark"))
def test_toasts_in_every_theme(browser, desk, theme, dark):
    session = App(browser, dark=dark)
    try:
        session.player = "bottom"
        session.open()
        session.set_prefs(ui_theme=theme, dark_mode=dark)
        session.open()
        page = session.page
        assert page.locator("html").get_attribute("data-ui-theme") == theme
        assert page.evaluate("() => document.documentElement.classList.contains('dark')") == dark
        session.toast("Summary reprocessing started", "fa-info-circle", 600000, "info")
        session.toast("Transcript saved", "fa-check-circle", 600000, "success")
        session.toast("Transcript saved", "fa-check-circle", 600000, "success")
        session.toast("The screen may lock. Keep it on while you record.", "fa-exclamation-triangle", 600000, "warning")
        session.toast({"text": "Transcription failed", "actionLabel": "Retry"}, "fa-exclamation-circle", 3000, "error", action=True)
        page.locator(TOAST).nth(3).wait_for(state="visible", timeout=WAIT)
        session.toasts_settled()
        look = page.evaluate(TOAST_LOOK)
        where = f"{theme} {'dark' if dark else 'light'}"
        assert [t["type"] for t in look] == ["info", "success", "warning", "error"], where
        for toast in look:
            kind = toast["type"]
            assert toast["badge"] == TYPE_COLORS[kind], f"{where} {kind}: the badge is {toast['badge']}"
            assert toast["surface"] not in toast["pages"], f"{where} {kind}: the toast has a page color: {toast}"
            assert toast["border"] >= 1 and toast["shadow"], f"{where} {kind}: no border or no shadow"
            # The border is the color of the type, not the theme's
            assert toast["borderColor"] != toast["surface"], f"{where} {kind}: {toast}"
            for part in ("text", "glyphContrast", "close"):
                assert toast[part] >= 4.5, f"{where} {kind}: {part} has a contrast of {toast[part]}"
        assert look[1]["count"] >= 4.5 and look[3]["action"] >= 4.5, f"{where}: {look}"
        player, first, last = session.rects("[data-toast-avoid]", f"{TOAST}:first-child", f"{TOAST}:last-child")
        assert last["bottom"] <= player["top"], f"{where}: the toasts cover the player: {last} over {player}"
        assert last["bottom"] <= DESKTOP["height"] - 8, f"{where}: {last}"
        session.shot(f"toast-queue-test-toasts-{theme}-{'dark' if dark else 'light'}")
        session.check(where, utilities=False)
    finally:
        if session.page.url.startswith(BASE):
            session.set_prefs(ui_theme="default", dark_mode=False)
        session.close()


# ---- the queue: indicator and panel ------------------------------------------------

def test_the_indicator_names_what_is_in_the_queue(desk):
    desk.jobs = mixed(desk)
    desk.open()
    page = desk.page
    desk.indicator_text("3 processing · 2 queued · 1 failed", "foot")
    indicator = desk.indicator("foot")
    assert indicator.get_attribute("aria-expanded") == "false"
    assert indicator.get_attribute("aria-label") == f"{en('progressQueue.title')}: 3 processing, 2 queued, 1 failed"
    assert "is-busy" in indicator.get_attribute("class") and indicator.locator(".queue-spin").is_visible()
    # Failures in red, the rest in the text color
    failed = indicator.locator('[data-queue-part="failed"]')
    assert failed.evaluate("(el) => getComputedStyle(el).color") == "rgb(185, 28, 28)"
    assert indicator.locator('[data-queue-part="processing"]').evaluate("(el) => getComputedStyle(el).color") != "rgb(185, 28, 28)"
    # The moving hairline
    assert indicator.evaluate("(el) => getComputedStyle(el, '::after').animationName") == "queue-slide"
    # It cannot be closed, and it is at the foot of the list, inside the list's column
    assert indicator.locator("xpath=..").locator("button").count() == 1
    side, listing, box = desk.rects("aside.sidebar", "aside.sidebar .sidebar-list", '[data-queue-indicator="foot"]')
    assert box["left"] >= side["left"] and box["right"] <= side["right"] and box["bottom"] <= side["bottom"]
    assert listing["bottom"] <= box["top"], "the indicator lies over the list"
    assert page.locator(PANEL).count() == 0
    desk.shot("toast-queue-test-indicator")

    # Nothing runs any more: the failure leads, then only finished rows, then nothing
    desk.jobs = [j for j in desk.jobs if j["job_status"] in ("failed", "completed")]
    desk.indicator_text("2 finished · 1 failed", "foot")
    assert "is-busy" not in desk.indicator("foot").get_attribute("class")
    assert desk.indicator("foot").locator(".queue-ind__icon.is-error").is_visible()
    desk.jobs = [j for j in desk.jobs if j["job_status"] == "completed"]
    desk.open()
    desk.indicator_text("2 finished", "foot")
    assert desk.indicator("foot").locator(".queue-ind__icon.is-ok").is_visible()
    desk.jobs = []
    desk.open()
    desk.rendered()
    assert page.locator("[data-queue-indicator]").count() == 0, "an empty queue still has an indicator"
    assert page.locator("[data-queue-dock]").count() == 0
    desk.check("queue indicator")


def test_the_panel_is_docked_and_has_four_groups_in_order(desk):
    desk.jobs = mixed(desk)
    desk.open()
    page = desk.page
    desk.indicator_text("3 processing · 2 queued · 1 failed", "foot")
    before = desk.rects("aside.sidebar .sidebar-list")[0]
    panel = desk.open_panel()
    assert panel.get_attribute("data-queue-panel") == "dock"
    assert page.locator('[data-queue-indicator="foot"]').count() == 0, "the indicator stays beside the open panel"
    groups = desk.groups()
    assert [g["name"] for g in groups] == ["active", "waiting", "failed", "done"]
    assert [g["title"].lower() for g in groups] == [en(f"progressQueue.groups.{k}").lower() for k in ("active", "waiting", "failed", "done")]
    assert [g["count"] for g in groups] == [3, 2, 1, 2]
    assert groups[0]["rows"] == ["Kickoff call", "One on one", "Design critique"]
    assert groups[1]["rows"] == ["Lecture on queues", "Customer debrief"]
    assert groups[2]["rows"] == ["Pipeline review"]
    assert groups[3]["rows"] == [MEETING_TITLE, "Contract walkthrough"]
    assert panel.locator("[data-queue-summary]").inner_text().strip() == "3 processing · 2 queued · 1 failed"
    # Docked: the panel is in the list's column and the list got shorter; nothing lies over it
    side, listing, box = desk.rects("aside.sidebar", "aside.sidebar .sidebar-list", PANEL)
    assert box["left"] >= side["left"] and box["right"] <= side["right"] and box["bottom"] <= side["bottom"] + 1
    assert listing["bottom"] <= box["top"], f"the panel covers the list: list {listing}, panel {box}"
    assert listing["height"] < before["height"] - 100
    assert "queue-open" in page.locator("body").get_attribute("class")
    # A surface of its own with a border
    look = page.evaluate(QUEUE_LOOK)
    assert look["surface"] != look["around"] and look["border"] >= 1, look
    assert look["text"][0] >= 4.5 and look["icon"][0] >= 3, look

    # Waiting rows: what they wait for, neutral, no bar and no number
    waiting = desk.row("Lecture on queues")
    assert waiting.locator("[data-queue-stage]").inner_text().strip() == "Queued #2 for transcription"
    assert desk.row("Customer debrief").locator("[data-queue-stage]").inner_text().strip() == "Queued #1 for summary"
    assert waiting.get_attribute("data-tone") == "wait"
    assert waiting.locator("[data-queue-bar]").count() == 0 and waiting.locator(".queue-row__num").count() == 0
    mark = waiting.locator(".queue-row__mark").evaluate("(el) => getComputedStyle(el).backgroundColor")
    tertiary = page.evaluate("() => { const el = document.createElement('i'); el.style.background = 'var(--bg-tertiary)'; document.body.append(el); "
                             "const c = getComputedStyle(el).backgroundColor; el.remove(); return c; }")
    assert mark == tertiary, "a waiting row is not neutral"

    # Failed: the cause, the hint on at most two lines, retry and delete
    failed = desk.row("Pipeline review")
    assert failed.locator("[data-queue-error]").inner_text().strip()
    reason = failed.locator("[data-queue-reason]")
    lines = reason.evaluate("(el) => Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight))")
    assert 1 <= lines <= 2 and reason.get_attribute("title")
    assert failed.locator("[data-queue-retry]").inner_text().strip() == en("progressQueue.retry")
    assert failed.locator("[data-queue-delete]").get_attribute("aria-label") == f"{en('progressQueue.delete')}: Pipeline review"
    assert failed.locator("[data-queue-cancel]").count() == 0

    # Finished: when, open and remove
    done = desk.row("Contract walkthrough")
    desk.wait_row("Contract walkthrough", ".queue-row__detail", f"{en('progressQueue.done')} · 14 min ago")
    desk.wait_row(MEETING_TITLE, ".queue-row__detail", f"{en('progressQueue.done')} · 2 min ago")
    assert done.locator("[data-queue-open]").count() == 1 and done.locator("[data-queue-remove]").count() == 1
    assert done.locator("[data-queue-cancel]").count() == 0 and done.locator("[data-queue-bar]").count() == 0
    desk.shot("toast-queue-test-panel")

    # Minimize: the indicator is back and the list has its room again
    panel.locator("[data-queue-minimize]").click()
    page.locator(PANEL).wait_for(state="detached", timeout=WAIT)
    desk.indicator("foot").wait_for(state="visible", timeout=WAIT)
    assert abs(desk.rects("aside.sidebar .sidebar-list")[0]["height"] - before["height"]) <= 1
    assert "queue-minimized" in page.locator("body").get_attribute("class")
    # Escape in the panel minimizes it as well
    panel = desk.open_panel()
    panel.locator(".queue-panel__body").focus()
    page.keyboard.press("Escape")
    page.locator(PANEL).wait_for(state="detached", timeout=WAIT)
    desk.check("queue panel")


def test_a_real_number_or_a_moving_bar_with_the_elapsed_time(desk):
    """A number only where the server has one; nothing is estimated."""
    desk.jobs = mixed(desk)
    desk.open()
    page = desk.page
    desk.indicator_text("3 processing · 2 queued · 1 failed")
    panel = desk.open_panel()

    # Parts of a total: "3 of 8" and a bar at that share
    parts = desk.row("Kickoff call")
    assert parts.locator("[data-queue-stage]").inner_text().strip() == en("status.transcribing")
    assert parts.locator("[data-queue-number]").inner_text().strip() == "3 of 8"
    bar = parts.locator('[data-queue-bar="real"]')
    assert bar.get_attribute("aria-valuenow") == "38" and bar.get_attribute("role") == "progressbar"
    fill = bar.evaluate("(el) => el.firstElementChild.getBoundingClientRect().width / el.getBoundingClientRect().width")
    assert 0.36 <= fill <= 0.40, fill
    assert parts.locator("[data-queue-elapsed]").count() == 0

    # No number from the server: the stage with the service's step, a moving bar and the elapsed time
    moving = desk.row("One on one")
    assert moving.locator("[data-queue-stage]").inner_text().strip() == en("status.transcribing")
    assert moving.locator("[data-queue-detail]").inner_text().strip() == f"· {en('progressQueue.detail.aligning')}"
    assert moving.locator("[data-queue-number]").count() == 0
    bar = moving.locator('[data-queue-bar="moving"]')
    assert bar.get_attribute("aria-valuenow") is None, "a moving bar carries a number"
    assert bar.evaluate("(el) => { const s = getComputedStyle(el.firstElementChild); return [s.animationName, s.animationIterationCount]; }") \
        == ["queue-slide", "infinite"]
    elapsed = moving.locator("[data-queue-elapsed]")
    desk.wait("() => /^0:(4[7-9]|5\\d)$/.test(document.querySelector('[data-queue-elapsed]').textContent)")
    first = elapsed.inner_text()
    desk.wait("(first) => document.querySelector('[data-queue-elapsed]').textContent !== first", first)
    assert re.fullmatch(r"\d:\d\d", elapsed.inner_text())
    summary = desk.row("Design critique")
    assert summary.locator("[data-queue-stage]").inner_text().strip() == en("status.summarizing")
    assert summary.locator('[data-queue-bar="moving"]').count() == 1 and summary.locator("[data-queue-number]").count() == 0
    # Nowhere a percent, an estimate or a remaining time
    text = panel.inner_text()
    assert "%" not in text and not re.search(r"\b(about|left|remaining|eta)\b", text, re.I), text

    # A measured fraction is a percent; an upload-style stage from the server is named
    desk.jobs[1].update(progress={"fraction": 0.42}, progress_kind="fraction", stage_detail="transcribing")
    desk.jobs[2].update(stage="titling")
    desk.wait_row("One on one", "[data-queue-number]", "42%")
    assert desk.row("One on one").locator('[data-queue-bar="real"]').get_attribute("aria-valuenow") == "42"
    assert desk.row("One on one").locator("[data-queue-detail]").count() == 0
    desk.wait_row("Design critique", "[data-queue-stage]", en("progressQueue.stage.titling"))
    desk.check("queue progress")


def test_a_job_steps_through_queued_transcribing_and_summarizing(desk):
    title = "Stepped recording"
    step = job(21, 9021, title, "queued", position=2, queue_position=2, queue="transcription")
    desk.jobs = [step, job(22, 9022, "Ahead of it", "processing", stage="transcribing", started_at=iso(30))]
    desk.open()
    desk.indicator_text("1 processing · 1 queued")
    desk.open_panel()
    assert desk.groups()[1]["rows"] == [title]
    desk.wait_row(title, "[data-queue-stage]", "Queued #2 for transcription")
    step.update(position=1, queue_position=1)
    desk.wait_row(title, "[data-queue-stage]", "Queued #1 for transcription")

    # Converting, then transcribing in parts
    step.update(job_status="processing", job_type="convert", stage="converting", started_at=iso(1), position=None, queue_position=None, queue=None)
    desk.wait_row(title, "[data-queue-stage]", en("progressQueue.converting"))
    assert desk.row(title).locator('[data-queue-bar="moving"]').count() == 1
    assert [g["name"] for g in desk.groups()] == ["active"]
    step.update(job_type="transcribe", stage="transcribing", started_at=iso(2), progress={"done": 0, "total": 4}, progress_kind="chunks")
    desk.wait_row(title, "[data-queue-number]", "0 of 4")
    assert desk.row(title).locator("[data-queue-stage]").inner_text().strip() == en("status.transcribing")
    for done, percent in ((1, "25"), (3, "75")):
        step.update(progress={"done": done, "total": 4})
        desk.wait_row(title, "[data-queue-number]", f"{done} of 4")
        assert desk.row(title).locator('[data-queue-bar="real"]').get_attribute("aria-valuenow") == percent

    # The title and the summary are stages of the same job, without a number
    step.update(stage="titling", progress=None, progress_kind=None)
    desk.wait_row(title, "[data-queue-stage]", en("progressQueue.stage.titling"))
    assert desk.row(title).locator("[data-queue-number]").count() == 0
    assert desk.row(title).locator('[data-queue-bar="moving"]').count() == 1
    step.update(stage="summarizing")
    desk.wait_row(title, "[data-queue-stage]", en("status.summarizing"))
    desk.shot("toast-queue-test-step-summarizing")

    # Finished: the row moves to Recently finished
    step.update(job_status="completed", stage="completed", can_cancel=False, completed_at=iso(), finished_at=iso())
    desk.wait("() => [...document.querySelectorAll('[data-queue-group=\"done\"] .queue-row__title')].some((el) => el.textContent.trim() === 'Stepped recording')",
              timeout=POLL + WAIT)
    desk.wait_row(title, ".queue-row__detail", f"{en('progressQueue.done')} · {en('progressQueue.justNow')}")
    desk.indicator_text("1 processing")
    desk.check("queue steps")


def test_cancel_of_a_waiting_job_and_of_a_running_job(desk):
    desk.jobs = mixed(desk)
    desk.open()
    page = desk.page
    desk.indicator_text("3 processing · 2 queued · 1 failed")
    desk.open_panel()

    # A waiting job: removed at once, without a question
    waiting = desk.row("Lecture on queues")
    cancel = waiting.locator("[data-queue-cancel]")
    assert cancel.get_attribute("aria-label") == f"{en('progressQueue.cancel')}: Lecture on queues"
    cancel.click()
    waiting.wait_for(state="detached", timeout=WAIT)
    assert desk.calls == [("DELETE", "job", 4)]
    assert page.locator(".modal-overlay.app-dialog").count() == 0
    desk.indicator_text("3 processing · 1 queued · 1 failed")

    # A running summary of a recording that has its transcript: no question either.
    # The server stops it at its next safe point; the row says so meanwhile.
    desk.answers[3] = (202, {"success": True, "cancel_requested": True})
    running = desk.row("Design critique")
    running.locator("[data-queue-cancel]").click()
    desk.wait_row("Design critique", "[data-queue-stage]", en("progressQueue.canceling"))
    assert desk.calls[-1] == ("DELETE", "job", 3)
    assert page.locator(".modal-overlay.app-dialog").count() == 0
    assert running.locator("[data-queue-cancel]").count() == 0, "a second cancel is offered while the first is pending"
    assert running.locator("[data-queue-detail]").count() == 0
    assert running.locator('[data-queue-bar="moving"]').count() == 1
    desk.shot("toast-queue-test-canceling")
    # The worker has stopped: the row leaves the queue
    desk.jobs = [j for j in desk.jobs if j["id"] != 3]
    running.wait_for(state="detached", timeout=POLL + WAIT)
    desk.indicator_text("2 processing · 1 queued · 1 failed")

    # A job the server does not cancel has no button
    desk.jobs[0].update(can_cancel=False)
    desk.wait("() => [...document.querySelectorAll('[data-queue-row]')].some((r) => r.querySelector('.queue-row__title').textContent.trim() === 'Kickoff call' "
              "&& !r.querySelector('[data-queue-cancel]'))", timeout=POLL + WAIT)
    desk.check("queue cancel")


def test_cancel_of_a_running_first_transcription_is_asked_about_in_the_apps_dialog(desk):
    desk.jobs = mixed(desk)
    desk.open()
    page = desk.page
    desk.indicator_text("3 processing · 2 queued · 1 failed")
    desk.open_panel()
    desk.answers[2] = (202, {"success": True, "cancel_requested": True})
    row = desk.row("One on one")
    dialog = page.locator(".modal-overlay.app-dialog")

    row.locator("[data-queue-cancel]").click()
    dialog.wait_for(state="visible", timeout=WAIT)
    assert en("progressQueue.cancelConfirm.title") in dialog.inner_text()
    assert en("progressQueue.cancelConfirm.message").replace("{{title}}", "One on one") in dialog.inner_text()
    desk.shot("toast-queue-test-cancel-question")
    # Keep processing: nothing is sent
    dialog.get_by_role("button", name=en("progressQueue.cancelConfirm.keep")).click()
    dialog.wait_for(state="detached", timeout=WAIT)
    assert desk.calls == []
    assert row.locator("[data-queue-stage]").inner_text().strip() == en("status.transcribing")

    row.locator("[data-queue-cancel]").click()
    dialog.wait_for(state="visible", timeout=WAIT)
    dialog.get_by_role("button", name=en("progressQueue.cancelConfirm.confirm")).click()
    dialog.wait_for(state="detached", timeout=WAIT)
    desk.wait_row("One on one", "[data-queue-stage]", en("progressQueue.canceling"))
    assert desk.calls == [("DELETE", "job", 2)]

    # From the title on the transcript of the run is stored: no question
    desk.jobs[0].update(stage="titling", progress=None, progress_kind=None)
    desk.answers[1] = (202, {"success": True, "cancel_requested": True})
    desk.wait_row("Kickoff call", "[data-queue-stage]", en("progressQueue.stage.titling"))
    desk.row("Kickoff call").locator("[data-queue-cancel]").click()
    desk.wait_row("Kickoff call", "[data-queue-stage]", en("progressQueue.canceling"))
    assert page.locator(".modal-overlay.app-dialog").count() == 0
    desk.check("queue cancel question")


def test_retry_delete_clear_failed_and_clear_finished(desk):
    desk.jobs = mixed(desk) + [job(9, 9009, "Second failure", "failed", can_cancel=False, ago=200, error_message="Transcription failed: timed out")]
    desk.open()
    page = desk.page
    desk.indicator_text("3 processing · 2 queued · 2 failed")
    panel = desk.open_panel()
    assert desk.groups()[2]["rows"] == ["Pipeline review", "Second failure"]

    # Retry: the job waits again
    desk.row("Pipeline review").locator("[data-queue-retry]").click()
    said = page.locator(TOAST).filter(has_text=en("messages.jobQueuedForRetry"))
    said.wait_for(state="visible", timeout=WAIT)
    assert said.get_attribute("data-toast-type") == "success"
    desk.wait("() => [...document.querySelectorAll('[data-queue-group=\"waiting\"] .queue-row__title')].some((el) => el.textContent.trim() === 'Pipeline review')")
    assert ("POST", "retry", 6) in desk.calls
    desk.wait_row("Pipeline review", "[data-queue-stage]", "Queued #1 for transcription")
    desk.clear_toasts()

    # Clear failed: every failed row in one step
    desk.jobs.append(job(10, 9010, "Third failure", "failed", can_cancel=False, ago=100, error_message="Transcription failed: timed out"))
    desk.wait("() => document.querySelectorAll('[data-queue-group=\"failed\"] [data-queue-row]').length === 2", timeout=POLL + WAIT)
    clear_failed = panel.locator("[data-queue-clear-failed]")
    assert clear_failed.inner_text().strip() == en("progressQueue.clearFailed")
    clear_failed.click()
    page.locator('[data-queue-group="failed"]').wait_for(state="detached", timeout=WAIT)
    assert ("POST", "clear-failed", None) in desk.calls
    assert [g["name"] for g in desk.groups()] == ["active", "waiting", "done"]

    # Delete of one failed row
    desk.jobs.append(job(11, 9011, "Fourth failure", "failed", can_cancel=False, ago=50, error_message="Transcription failed: timed out"))
    desk.row("Fourth failure").wait_for(state="visible", timeout=POLL + WAIT)
    desk.row("Fourth failure").locator("[data-queue-delete]").click()
    desk.row("Fourth failure").wait_for(state="detached", timeout=WAIT)
    assert ("DELETE", "job", 11) in desk.calls

    # Remove of one finished row, then Clear finished
    desk.row("Contract walkthrough").locator("[data-queue-remove]").click()
    desk.row("Contract walkthrough").wait_for(state="detached", timeout=WAIT)
    assert ("DELETE", "job", 8) in desk.calls and desk.groups()[-1]["rows"] == [MEETING_TITLE]
    clear_finished = panel.locator("[data-queue-clear-finished]")
    assert clear_finished.inner_text().strip() == en("progressQueue.clearFinished")
    clear_finished.click()
    page.locator('[data-queue-group="done"]').wait_for(state="detached", timeout=WAIT)
    assert ("POST", "clear-completed", None) in desk.calls
    assert [g["name"] for g in desk.groups()] == ["active", "waiting"]
    desk.check("queue retry and clear")


def test_a_finished_row_opens_its_recording_and_the_last_row_leaves_the_empty_state(desk):
    desk.jobs = [job(7, desk.ids[LONG_TITLE], LONG_TITLE, "completed", ago=60)]
    desk.open(MEETING_TITLE)
    page = desk.page
    desk.indicator_text("1 finished", "foot")
    panel = desk.open_panel()
    assert panel.locator(".queue-panel__icon.is-ok").is_visible()
    row = desk.row(LONG_TITLE)
    assert "is-link" in row.get_attribute("class")
    row.locator(".queue-row__title").click()
    desk.wait("(title) => document.querySelector('#app > header h1').textContent.trim() === title", LONG_TITLE)
    # The button of the row does the same
    page.locator("aside.sidebar .recording-group > *").filter(has_text=MEETING_TITLE).first.click()
    desk.wait("(title) => document.querySelector('#app > header h1').textContent.trim() === title", MEETING_TITLE)
    row.locator("[data-queue-open]").click()
    desk.wait("(title) => document.querySelector('#app > header h1').textContent.trim() === title", LONG_TITLE)

    # The last row is cleared: the open panel says that the queue is empty
    panel.locator("[data-queue-clear-finished]").click()
    empty = panel.locator("[data-queue-empty]")
    empty.wait_for(state="visible", timeout=WAIT)
    assert en("progressQueue.empty") in empty.inner_text() and en("progressQueue.emptyHint") in empty.inner_text()
    assert page.locator(f"{PANEL} {ROW}").count() == 0
    desk.shot("toast-queue-test-empty")
    # Minimized, nothing of the queue is left on the page
    panel.locator("[data-queue-minimize]").click()
    page.locator(PANEL).wait_for(state="detached", timeout=WAIT)
    assert page.locator("[data-queue-indicator]").count() == 0 and page.locator("[data-queue-dock]").count() == 0
    desk.check("queue empty")


def test_the_list_still_scrolls_with_the_panel_docked(browser, desk):
    """A low window: the panel takes at most its share and the list keeps a part that scrolls."""
    session = App(browser, viewport={"width": 1280, "height": 520})
    try:
        session.jobs = mixed(session)
        session.open()
        page = session.page
        session.indicator_text("3 processing · 2 queued · 1 failed")
        session.open_panel()
        wrapper, listing, panel, body = session.rects("aside.sidebar .sidebar-content-wrapper", "aside.sidebar .sidebar-list", PANEL, ".queue-panel__body")
        assert panel["height"] <= wrapper["height"] * 0.55, "the panel takes more than its share of the column"
        assert listing["height"] >= 40 and listing["bottom"] <= panel["top"]
        sizes = page.evaluate("() => { const l = document.querySelector('aside.sidebar .sidebar-list'); return [l.scrollHeight, l.clientHeight, getComputedStyle(l).overflowY]; }")
        assert sizes[0] > sizes[1] and sizes[2] == "auto", f"the list has nothing to scroll in this window: {sizes}"
        page.locator("aside.sidebar .sidebar-list").hover()
        page.mouse.wheel(0, 2000)
        session.wait("() => { const l = document.querySelector('aside.sidebar .sidebar-list'); return l.scrollTop > 0 && Math.abs(l.scrollHeight - l.scrollTop - l.clientHeight) <= 1; }")
        # The last row of the list can be brought above the panel
        last = page.evaluate("() => { const rows = document.querySelectorAll('aside.sidebar .recording-group > *'); return rows[rows.length - 1].getBoundingClientRect().bottom; }")
        assert last <= panel["top"] + 1, "the end of the list stays under the panel"
        # The panel scrolls inside itself
        assert page.evaluate("() => { const b = document.querySelector('.queue-panel__body'); return b.scrollHeight > b.clientHeight; }")
        page.locator(".queue-panel__body").hover()
        page.mouse.wheel(0, 3000)
        session.wait("() => { const b = document.querySelector('.queue-panel__body'); return Math.abs(b.scrollHeight - b.scrollTop - b.clientHeight) <= 1; }")
        assert session.row("Contract walkthrough").is_visible()
        session.shot("toast-queue-test-docked-low-window")
        session.check("queue docked in a low window")
    finally:
        session.close()


def test_the_pill_in_the_top_bar_while_the_list_is_collapsed(desk):
    desk.jobs = mixed(desk)
    desk.open()
    page = desk.page
    desk.indicator_text("3 processing · 2 queued · 1 failed", "foot")
    toggle = page.locator("#app > header button:has(.fa-bars)").first
    toggle.click()
    pill = desk.indicator("pill")
    pill.wait_for(state="visible", timeout=WAIT)
    # Shorter in the top bar; the full line is its accessible name
    desk.indicator_text("5 in queue · 1 failed", "pill")
    assert pill.get_attribute("aria-label") == f"{en('progressQueue.title')}: 3 processing, 2 queued, 1 failed"
    assert page.locator('[data-queue-indicator="foot"]:visible').count() == 0
    header, box = desk.rects("#app > header", '[data-queue-indicator="pill"]')
    assert box["top"] >= header["top"] and box["bottom"] <= header["bottom"], "the pill is not in the top bar"
    desk.shot("toast-queue-test-pill")
    # Its panel opens under the top bar, inside the window
    pill.click()
    panel = page.locator('[data-queue-panel="float"]')
    panel.wait_for(state="visible", timeout=WAIT)
    desk.wait("() => document.querySelector('[data-queue-panel]').getAnimations().every((a) => a.playState === 'finished')")
    assert pill.get_attribute("aria-expanded") == "true"
    area = desk.rects('[data-queue-panel="float"]')[0]
    assert area["top"] >= header["bottom"] and area["bottom"] <= DESKTOP["height"] and area["right"] <= DESKTOP["width"] and area["left"] >= 0
    assert [g["name"] for g in desk.groups()] == ["active", "waiting", "failed", "done"]
    desk.shot("toast-queue-test-pill-panel")
    # The list comes back: the open panel is docked at its foot again
    toggle.click()
    page.locator('[data-queue-panel="dock"]').wait_for(state="visible", timeout=WAIT)
    assert page.locator('[data-queue-indicator="pill"]').count() == 0 and page.locator('[data-queue-panel="float"]').count() == 0
    page.locator("[data-queue-minimize]").click()
    desk.indicator("foot").wait_for(state="visible", timeout=WAIT)
    desk.check("queue pill")


def test_one_clock_for_the_elapsed_times_that_stops_when_nothing_shows_them(desk):
    desk.jobs = mixed(desk)
    desk.open()
    page = desk.page
    desk.indicator_text("3 processing · 2 queued · 1 failed", "foot")
    idle = page.evaluate("() => window.__secondTimers()")
    desk.open_panel()
    desk.wait("(idle) => window.__secondTimers() === idle + 1", idle)
    assert page.locator(f"{PANEL} [data-queue-since]").count() == 2

    # A second that passes changes the two counters and nothing else on the page
    changed = page.evaluate("""() => new Promise((done) => {
        const seen = [];
        const observer = new MutationObserver((records) => { for (const r of records) {
            const el = r.target.nodeType === 1 ? r.target : r.target.parentElement;
            seen.push(el && el.closest('[data-queue-since], [data-queue-finished]') ? 'clock' : (el ? el.tagName + '.' + el.className : 'text')); } });
        observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
        setTimeout(() => { observer.disconnect(); done(seen); }, 2600);
    })""")
    assert changed and set(changed) == {"clock"}, f"more than the counters changed in a second: {sorted(set(changed))}"

    # The tab goes to the background: the clock stops, and starts again in front
    page.evaluate("() => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' }); document.dispatchEvent(new Event('visibilitychange')); }")
    desk.wait("(idle) => window.__secondTimers() === idle", idle)
    page.evaluate("() => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' }); document.dispatchEvent(new Event('visibilitychange')); }")
    desk.wait("(idle) => window.__secondTimers() === idle + 1", idle)

    # Minimized: no clock
    page.locator("[data-queue-minimize]").click()
    page.locator(PANEL).wait_for(state="detached", timeout=WAIT)
    desk.wait("(idle) => window.__secondTimers() === idle", idle)
    desk.check("queue clock")


def test_the_card_of_a_canceled_first_transcription(desk):
    """What the recording view shows in place of the transcript (src/services/job_cancel.py)."""
    card = {"t": "Processing canceled", "m": "The transcription of this recording was canceled before it finished.",
            "g": "Use Reprocess to start the transcription again.", "i": "fa-ban", "y": "canceled", "k": True}
    recording_id = desk.ids[MEETING_TITLE]
    desk.patches[recording_id] = {"status": "FAILED", "error_message": "Processing was canceled.", "transcription": "ERROR_JSON:" + json.dumps(card),
                                  "summary": None}
    desk.open()
    page = desk.page
    notice = page.locator(".error-display-container .notice").first
    notice.wait_for(state="visible", timeout=WAIT)
    # A state, not an error: the info level, the ban icon, the words of the interface language
    assert "notice--info" in notice.get_attribute("class") and "notice--danger" not in notice.get_attribute("class")
    assert notice.locator(".notice__icon i").get_attribute("class").split()[-1] == "fa-ban"
    assert notice.locator(".notice__title").inner_text().strip() == en("errors.canceledTitle")
    assert notice.locator(".notice__message").inner_text().strip() == en("errors.canceledMessage")
    assert notice.locator(".notice__hint").inner_text().strip() == en("errors.canceledGuidance")
    assert notice.locator(".notice__actions button").inner_text().strip() == en("buttons.reprocessTranscription")
    assert notice.locator(".notice__details").count() == 0
    look = page.evaluate("() => {" + COLORS + """
        const n = document.querySelector('.error-display-container .notice');
        return ['.notice__title', '.notice__message', '.notice__hint'].map((s) => contrast(n.querySelector(s))); }""")
    assert min(look) >= 4.5, look
    desk.shot("toast-queue-test-canceled-card")
    # Another failure is still an error
    failed = {"t": "Connection error", "m": "The transcription service could not be reached.", "g": "", "i": "fa-plug", "y": "connection", "k": True}
    desk.patches[recording_id]["transcription"] = "ERROR_JSON:" + json.dumps(failed)
    desk.open()
    notice = page.locator(".error-display-container .notice").first
    notice.wait_for(state="visible", timeout=WAIT)
    assert "notice--danger" in notice.get_attribute("class")
    assert notice.locator(".notice__title").inner_text().strip() == "Connection error"
    desk.check("canceled card")


# ---- every theme -------------------------------------------------------------------

@pytest.mark.parametrize("theme", THEMES)
def test_the_queue_in_every_theme(browser, desk, theme):
    """One structure: the indicator, the docked panel and the panel over the page, light and dark."""
    for dark in (False, True):
        session = App(browser, dark=dark)
        where = f"{theme} {'dark' if dark else 'light'}"
        try:
            session.jobs = mixed(session)
            session.open()
            session.set_prefs(ui_theme=theme, dark_mode=dark)
            session.open()
            page = session.page
            assert page.locator("html").get_attribute("data-ui-theme") == theme
            # The Desk theme has the indicator in its status bar; every other theme at the foot of the list
            variant = "status" if theme == "desk" else "foot"
            session.indicator_text("3 processing · 2 queued · 1 failed", variant)
            assert page.locator("[data-queue-indicator]:visible").count() == 1, where
            window, side, listing, box = session.rects("body", "aside.sidebar", "aside.sidebar .sidebar-list", f'[data-queue-indicator="{variant}"]')
            if theme == "desk":
                bar = session.rects(".desk-statusbar")[0]
                assert box["top"] >= bar["top"] and box["bottom"] <= bar["bottom"], f"{where}: the indicator is not in the status bar"
            else:
                assert box["left"] >= side["left"] and box["right"] <= side["right"] and box["bottom"] <= side["bottom"], f"{where}: {box}"
                assert listing["bottom"] <= box["top"], f"{where}: the indicator lies over the list"
            look = page.evaluate(QUEUE_LOOK)
            assert look["text"][0] >= 4.5, f"{where}: indicator text {look}"
            session.shot(f"toast-queue-test-queue-{theme}-{'dark' if dark else 'light'}-indicator")

            session.open_panel()
            assert [g["name"] for g in session.groups()] == ["active", "waiting", "failed", "done"], where
            side, listing, panel = session.rects("aside.sidebar", "aside.sidebar .sidebar-list", '[data-queue-panel="dock"]')
            assert panel, f"{where}: the panel is not docked"
            assert panel["left"] >= side["left"] and panel["right"] <= side["right"] and panel["bottom"] <= side["bottom"] + 1, f"{where}: {panel}"
            assert listing["bottom"] <= panel["top"] and listing["height"] >= 40, f"{where}: list {listing}, panel {panel}"
            assert panel["width"] >= 240, f"{where}: the panel is {panel['width']}px wide"
            look = page.evaluate(QUEUE_LOOK)
            assert look["border"] >= 1 and look["texts"] > 20 and look["icons"] > 10, f"{where}: {look}"
            assert look["text"][0] >= 4.5, f"{where}: text contrast {look['text']}"
            assert look["icon"][0] >= 3, f"{where}: icon contrast {look['icon']}"
            # Titles are cut with an ellipsis, never wrapped, and the buttons stay inside the panel
            inside = page.evaluate("""() => { const p = document.querySelector('[data-queue-panel]').getBoundingClientRect();
                return [...document.querySelectorAll('[data-queue-panel] button')].every((b) => { const r = b.getBoundingClientRect(); return r.left >= p.left && r.right <= p.right + 0.5; }); }""")
            assert inside, f"{where}: a button reaches out of the panel"
            session.shot(f"toast-queue-test-queue-{theme}-{'dark' if dark else 'light'}-panel")

            # The list collapsed: the panel lies over the page, opened from the top bar
            # (in the Desk theme from the status bar, which stays)
            page.locator("#app > header button:has(.fa-bars)").first.click()
            floating = page.locator('[data-queue-panel="float"]')
            floating.wait_for(state="visible", timeout=WAIT)
            session.wait("() => document.querySelector('[data-queue-panel]').getAnimations().every((a) => a.playState === 'finished')")
            variant = "status" if theme == "desk" else "pill"
            assert page.locator(f'[data-queue-indicator="{variant}"]').is_visible(), f"{where}: no indicator while the list is collapsed"
            assert page.locator("[data-queue-indicator]:visible").count() == 1, where
            area, header = session.rects('[data-queue-panel="float"]', "#app > header")
            assert area["top"] >= header["bottom"] - 1 and area["bottom"] <= DESKTOP["height"] and area["left"] >= 0 and area["right"] <= DESKTOP["width"], f"{where}: {area}"
            if theme == "desk":
                assert area["bottom"] <= session.rects(".desk-statusbar")[0]["top"], f"{where}: the panel covers the status bar"
            session.shot(f"toast-queue-test-queue-{theme}-{'dark' if dark else 'light'}-collapsed")
            session.check(where, utilities=False)
        finally:
            if session.page.url.startswith(BASE):
                session.set_prefs(ui_theme="default", dark_mode=False)
            session.close()


@pytest.mark.parametrize("theme", THEMES)
def test_the_phone_strip_and_sheet_in_every_theme(browser, desk, theme):
    session = App(browser, viewport=PHONE)
    try:
        session.jobs = mixed(session)
        session.open()
        session.set_prefs(ui_theme=theme, dark_mode=False)
        session.open()
        page = session.page
        assert page.locator("html").get_attribute("data-ui-theme") == theme
        session.indicator_text("3 processing · 2 queued · 1 failed", "strip")
        assert page.locator("[data-queue-indicator]:visible").count() == 1, theme
        # Top to bottom without a gap or an overlap: the scrolling area, the strip, the player strip, the tabs
        scroll, dock, strip, player, tabs = session.rects("[data-phone-scroll]", ".queue-dock--recording", '[data-queue-indicator="strip"]',
                                                         "[data-mobile-player]", "[data-mobile-bottom-nav]")
        assert abs(scroll["bottom"] - dock["top"]) <= 1 and abs(dock["bottom"] - player["top"]) <= 1 and abs(player["bottom"] - tabs["top"]) <= 1, \
            f"{theme}: {scroll} {dock} {player} {tabs}"
        assert strip["top"] >= dock["top"] and strip["bottom"] <= dock["bottom"] and strip["height"] >= 44
        assert strip["left"] >= 0 and strip["right"] <= PHONE["width"]
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth"), f"{theme}: the page scrolls sideways"
        session.shot(f"toast-queue-test-phone-{theme}-strip")

        # Toasts sit above the strip, at the full width
        session.toast("Transcript saved", "fa-check-circle", 600000, "success")
        session.toast("Upload failed", "fa-exclamation-circle", 3000, "error")
        page.locator(TOAST).nth(1).wait_for(state="visible", timeout=WAIT)
        session.toasts_settled()
        first, last = session.rects(f"{TOAST}:first-child", f"{TOAST}:last-child")
        assert last["bottom"] <= strip["top"], f"{theme}: the toasts cover the queue strip: {last} over {strip}"
        assert first["left"] <= 13 and first["right"] >= PHONE["width"] - 13, f"{theme}: a toast is not at the full width: {first}"
        look = page.evaluate(TOAST_LOOK)
        for toast in look:
            assert toast["badge"] == TYPE_COLORS[toast["type"]] and toast["surface"] not in toast["pages"] and toast["text"] >= 4.5, f"{theme}: {toast}"

        # The strip opens the queue as a sheet; the toasts go under the top bar meanwhile
        page.locator('[data-queue-indicator="strip"]').click()
        sheet = page.locator('[data-phone-sheet="queue"]')
        sheet.locator(".ph-sheet").wait_for(state="visible", timeout=WAIT)
        session.wait("() => document.querySelector('[data-phone-sheet] .ph-sheet').getAnimations().every((a) => a.playState === 'finished')")
        assert sheet.locator(".ph-sheet__title").inner_text().strip() == en("progressQueue.title")
        assert sheet.locator("[data-queue-summary]").inner_text().strip() == "3 processing · 2 queued · 1 failed"
        assert [g["name"] for g in session.groups()] == ["active", "waiting", "failed", "done"], theme
        session.wait("() => document.querySelector('#toastContainer').getBoundingClientRect().top < 120")
        session.toasts_settled()
        # At the top: under the top bar when the sheet leaves the room, otherwise as much
        # higher as the stack needs, so that the heading and the close button of the sheet stay free
        top_toast, last_toast, panel, closer = session.rects(f"{TOAST}:first-child", f"{TOAST}:last-child", '[data-phone-sheet="queue"] .ph-sheet',
                                                           '[data-phone-sheet="queue"] [data-phone-sheet-close]')
        stack = session.rects("#toastContainer")[0]
        assert 0 <= stack["top"] <= 64, f"{theme}: the toasts are not at the top: {stack}"
        # A stack that is too high for the room above the sheet shows its newest toast and is cut off there
        assert stack["bottom"] <= panel["top"] and last_toast["bottom"] <= panel["top"] + 1, f"{theme}: the toasts reach the sheet: {stack} {last_toast} {panel}"
        assert last_toast["top"] >= stack["top"] - 1, f"{theme}: the newest toast is cut off: {last_toast}"
        sheet.locator("[data-queue-summary]").click()   # nothing of the toasts lies over the sheet
        session.page.locator(f"{TOAST} .toast__close").last.click()
        session.wait("() => document.querySelectorAll('#toastContainer .toast-message').length === 1")
        session.wait("([sel, top]) => document.querySelector(sel).getBoundingClientRect().bottom <= top", [TOAST, closer["top"]])
        assert panel["bottom"] >= PHONE["height"] - 1 and panel["left"] == 0 and panel["width"] == PHONE["width"], f"{theme}: {panel}"
        assert session.rects(TOAST)[0]["bottom"] <= closer["top"], f"{theme}: a toast covers the close button of the sheet"
        # Touch targets, contrast, and nothing wider than the sheet
        small = page.evaluate("""() => [...document.querySelectorAll('[data-phone-sheet="queue"] .queue-ibtn')].map((b) => b.getBoundingClientRect())
            .filter((r) => r.width < 44 || r.height < 44).length""")
        assert small == 0, f"{theme}: {small} icon buttons of the sheet are smaller than 44px"
        look = page.evaluate(QUEUE_LOOK)
        assert look["text"][0] >= 4.5 and look["icon"][0] >= 3, f"{theme}: {look}"
        assert page.evaluate("() => { const b = document.querySelector('[data-phone-sheet=\"queue\"] .ph-sheet__body'); return b.scrollWidth <= b.clientWidth; }")
        session.shot(f"toast-queue-test-phone-{theme}-sheet")
        sheet.locator("[data-phone-sheet-close]").click()
        sheet.wait_for(state="detached", timeout=WAIT)
        session.wait("() => document.querySelector('#toastContainer').getBoundingClientRect().top > 300")
        session.clear_toasts()

        # Over the list: the strip at the foot, the New recording button above it
        page.locator("[data-phone-back]").click()
        page.locator('[data-phone-bar="list"]').wait_for(state="visible", timeout=WAIT)
        session.wait("() => Math.abs(document.querySelector('aside.sidebar').getBoundingClientRect().left) < 0.5")
        # The strip of the list is the one on top there (the recording, with its own strip, is behind the list)
        mine = 'aside.sidebar [data-queue-indicator="strip"]'
        page.locator(mine).wait_for(state="visible", timeout=WAIT)
        session.wait("(sel) => { const el = document.querySelector(sel); const r = el.getBoundingClientRect(); "
                     "const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !!top && el.contains(top); }", mine)
        strips = session.rects(mine)
        listing, new = session.rects("aside.sidebar .sidebar-list", "[data-phone-new]")
        assert strips[0]["bottom"] <= PHONE["height"] and listing["bottom"] <= strips[0]["top"], f"{theme}: list {listing}, strip {strips}"
        assert new["bottom"] <= strips[0]["top"], f"{theme}: New recording lies over the queue strip: {new} {strips}"
        session.shot(f"toast-queue-test-phone-{theme}-list")
        session.check(f"{theme} phone", utilities=False)
    finally:
        if session.page.url.startswith(BASE):
            session.set_prefs(ui_theme="default", dark_mode=False)
        session.close()


def test_phone_sheet_actions_and_a_finished_row_that_opens_its_recording(browser, desk):
    session = App(browser, viewport=PHONE)
    try:
        session.jobs = mixed(session)
        session.jobs[6] = job(7, session.ids[LONG_TITLE], LONG_TITLE, "completed", ago=120)
        session.open(MEETING_TITLE)
        page = session.page
        session.indicator_text("3 processing · 2 queued · 1 failed", "strip")
        session.open_panel()
        sheet = page.locator('[data-phone-sheet="queue"]')
        # Cancel of a waiting job from the sheet
        session.row("Lecture on queues").locator("[data-queue-cancel]").click()
        session.row("Lecture on queues").wait_for(state="detached", timeout=WAIT)
        assert session.calls == [("DELETE", "job", 4)]
        assert sheet.locator("[data-queue-summary]").inner_text().strip() == "3 processing · 1 queued · 1 failed"
        # A finished row opens its recording and the sheet closes
        session.row(LONG_TITLE).locator(".queue-row__title").click()
        sheet.wait_for(state="detached", timeout=WAIT)
        session.wait("(title) => document.querySelector('[data-phone-title]').textContent.trim() === title", LONG_TITLE)
        # With no rows left, the strip is gone and the regions meet again
        session.jobs = []
        session.open(MEETING_TITLE)
        session.rendered()
        assert page.locator("[data-queue-indicator]").count() == 0
        scroll, player = session.rects("[data-phone-scroll]", "[data-mobile-player]")
        assert abs(scroll["bottom"] - player["top"]) <= 1
        session.check("phone sheet actions")
    finally:
        session.close()


# ---- with the instance itself --------------------------------------------------------

def _wav(seconds=3):
    path = os.path.join(tempfile.mkdtemp(prefix="speakr-queue-test-"), "queue-test-upload.wav")
    write_tone(path, seconds=seconds)
    return path


def _choose_file(session, path):
    """Put a file in the upload dialog and start the upload."""
    page = session.page
    page.locator("#app > header").get_by_role("button", name=en("nav.newRecording")).first.click()
    page.locator('input[type="file"]').first.set_input_files(path)
    start = page.locator(".modal-footer .btn--primary").filter(has_text=re.compile("1")).first
    start.wait_for(state="visible", timeout=WAIT)
    session.wait("() => [...document.querySelectorAll('.modal-footer .btn--primary')].some((b) => b.getClientRects().length && !b.disabled)")
    start.click()


def test_an_upload_in_progress_is_canceled(browser, desk):
    """The upload request is held back in the browser, so the row stays at Uploading until it is canceled."""
    session = App(browser)
    path = _wav()
    held = []
    try:
        session.open()
        page = session.page
        page.route(re.compile(r"/upload$"), lambda route: held.append(route))
        canceled = []
        page.on("request", lambda r: canceled.append(r.post_data) if r.url.endswith("/upload/cancel") else None)
        _choose_file(session, path)
        # The panel opens with the upload
        panel = page.locator('[data-queue-panel="dock"]')
        panel.wait_for(state="visible", timeout=WAIT)
        row = session.row("queue-test-upload")
        row.wait_for(state="visible", timeout=WAIT)
        assert session.groups()[0]["name"] == "active"
        session.wait_row(row.locator(".queue-row__title").inner_text().strip(), "[data-queue-stage]", en("status.uploading"))
        assert row.get_attribute("data-tone") == "info"
        # Either the sent share of the file, or (all bytes sent) a moving bar; never both
        number, moving = row.locator("[data-queue-number]").count(), row.locator('[data-queue-bar="moving"]').count()
        assert number + moving == 1, "an upload row shows a number and a moving bar, or neither"
        session.shot("toast-queue-test-uploading")
        cancel = row.locator("[data-queue-cancel]")
        assert cancel.count() == 1
        cancel.click()
        row.wait_for(state="detached", timeout=WAIT)
        session.wait("() => document.querySelectorAll('[data-queue-panel] [data-queue-row]').length === 0")
        session.page.wait_for_timeout(500)
        assert len(canceled) == 1 and "client_upload_id" in canceled[0], "the server was not told about the canceled upload"
        assert len(held) == 1, "the upload was sent more than once"
        # No recording was made
        titles = [r.get("title") or "" for r in page.request.get(f"{BASE}/api/recordings?per_page=50").json().get("recordings", [])]
        assert not any("queue-test-upload" in t for t in titles)
        assert not session.dialogs
        assert not [e for e in session.problems.errors if "abort" not in e.lower() and "cancel" not in e.lower()], session.problems.errors
    finally:
        for route in held:
            try:
                route.abort()
            except Exception:
                pass
        session.close()


def test_a_real_upload_goes_through_the_queue_of_the_instance(browser, desk):
    """No answer is given by the test here: the upload, the queue status and
    the row actions are the instance's own. No transcription service runs on
    the test instance, so the transcription fails within seconds, or still
    waits or runs when the test reaches it. Each end is checked: a job that
    waits or runs is canceled from its row; a failed one is listed under
    Failed with its reason and deleted from its row."""
    session = App(browser, stub=False)
    path = _wav()
    page = session.page
    uploaded = []
    job_row = "[data-queue-panel] [data-queue-row][data-queue-job]"
    try:
        session.open()
        before = {r["id"] for r in page.request.get(f"{BASE}/api/recordings?per_page=50").json().get("recordings", [])}
        _choose_file(session, path)
        page.locator('[data-queue-panel="dock"]').wait_for(state="visible", timeout=WAIT)
        # The row of the upload becomes the row of its job on the server
        page.locator(job_row).first.wait_for(state="visible", timeout=120000)
        row = page.locator(job_row).first
        recording_id = int(row.get_attribute("data-queue-row").split("_")[1])
        uploaded.append(recording_id)
        assert recording_id not in before
        status = row.get_attribute("data-queue-status")
        print(f"\nreal upload: recording {recording_id}, first row of its job: {status!r} {row.locator('.queue-row__line').inner_text().strip()!r}")
        assert "%" not in row.inner_text(), "a number was invented for a job without one"
        canceled = False
        if status in ("queued", "transcribing"):
            try:
                row.locator("[data-queue-cancel]").click(timeout=3000)
                canceled = True
            except Exception:
                canceled = False   # the job ended first
        if canceled:
            dialog = page.locator(".modal-overlay.app-dialog")
            page.wait_for_function("() => !!document.querySelector('.modal-overlay.app-dialog') || !document.querySelector('[data-queue-panel] [data-queue-cancel]')", timeout=WAIT)
            if dialog.count():
                # A running first transcription is asked about
                dialog.get_by_role("button", name=en("progressQueue.cancelConfirm.confirm")).click()
                dialog.wait_for(state="detached", timeout=WAIT)
        # The job ends: canceled (its row leaves the queue) or failed (listed under Failed)
        session.wait("() => { const rows = [...document.querySelectorAll('[data-queue-panel] [data-queue-row]')]; "
                     "return rows.every((r) => !['queued', 'transcribing', 'uploading'].includes(r.dataset.queueStatus)); }", timeout=180000)
        failed = page.locator('[data-queue-group="failed"] [data-queue-row]')
        record = page.request.get(f"{BASE}/api/recordings/{recording_id}").json()
        print(f"real upload: canceled from the row: {canceled}; the recording is {record.get('status')!r} with {record.get('error_message')!r}; "
              f"failed rows: {failed.count()}")
        assert record.get("status") == "FAILED"
        session.shot("toast-queue-test-real-upload")
        if failed.count():
            # Failed for want of a transcription service: the reason, retry and delete
            session.indicator_text("1 failed")
            assert failed.locator("[data-queue-error]").inner_text().strip()
            assert failed.locator("[data-queue-retry]").count() == 1 and failed.locator("[data-queue-cancel]").count() == 0
            assert "%" not in failed.inner_text()
            # Delete on the failed row of a new upload removes the recording with it
            failed.locator("[data-queue-delete]").click()
            failed.wait_for(state="detached", timeout=WAIT)
            session.wait("async (id) => (await fetch(`/api/recordings/${id}`)).status === 404", recording_id)
            uploaded.clear()
        else:
            assert record.get("error_message") == "Processing was canceled."
            # The stored card of a canceled first transcription, in the recording view
            page.goto(f"{BASE}/recordings/{recording_id}")
            notice = page.locator(".error-display-container .notice").first
            notice.wait_for(state="visible", timeout=60000)
            assert "notice--info" in notice.get_attribute("class")
            assert notice.locator(".notice__title").inner_text().strip() == en("errors.canceledTitle")
            session.shot("toast-queue-test-real-canceled-card")
        assert not session.dialogs
        assert not session.problems.errors, session.problems.errors
    finally:
        # Nothing of this upload stays on the instance: its jobs and the recording
        try:
            listing = page.request.get(f"{BASE}/api/recordings?per_page=50").json().get("recordings", [])
            for record in listing:
                if "queue-test-upload" in (record.get("title") or "") or "queue-test-upload" in (record.get("original_filename") or ""):
                    uploaded.append(record["id"])
            page.goto(f"{BASE}/")
            token = session.csrf()
            for recording_id in set(uploaded):
                page.request.delete(f"{BASE}/recording/{recording_id}", headers={"X-CSRFToken": token})
            for route_name in ("clear-failed", "clear-completed"):
                page.request.post(f"{BASE}/api/recordings/jobs/{route_name}", headers={"X-CSRFToken": token, "Content-Type": "application/json"})
        finally:
            session.close()
