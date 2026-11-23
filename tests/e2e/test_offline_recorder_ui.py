"""Browser test of recording without a connection and of the upload that
follows it (#650), in Chromium with a fake microphone.

The issue had two parts:

- a recording made in the app while the connection was down was uploaded
  when it came back, and offered again for recovery at the next start, so
  it was uploaded a second time;
- with no connection when the app was opened, the service worker's
  "You are offline" page was all there was: nothing could be recorded.

Covered here:

1. Online load, a recording made in the app, the connection lost before
   its upload, the connection back: the recording is uploaded once, and
   two reloads later the server still holds it once and no recovery is
   offered.
2. The server unreachable while the browser reports a connection (only the
   upload is refused): the recording is saved in the browser and uploaded
   once at the next load. The same when the first request reached the
   server but its answer was lost (#626).
3. The app opened while the server is down, after one online visit: the
   offline recorder is shown, records, lists the recording, deletes one,
   goes back to the app when the server is back, and the app uploads the
   recording once; a reload uploads nothing more. Once with the server
   stopped, once with the browser offline.
4. Sign-out deletes the caches and the recorder's entry; the offline page
   then shows no user data and records nothing. A recording saved for one
   account is not uploaded to another account signed in in the same
   browser, and is uploaded by its own account afterwards.
5. The recorder in the six UI themes, light and dark, at desktop and phone
   width, and in the seven languages.
6. The limits of a recording (length, size, storage) and the expiry of the
   recorder's entry.
7. The three sources of the in-app recorder (#697): the system audio of a
   screen share and the microphone mixed with it are recorded and uploaded
   at the next online load; the source is kept in the crash-recovery copy,
   which the app offers with that source; a share ended from the browser
   stops and saves the recording; a share that is canceled or has no audio
   is refused with the in-app recorder's texts; and every track and the
   AudioContext are stopped. Chromium's fake capture (FAKE_MEDIA) answers
   getDisplayMedia with a fake screen and a fake audio track.

Stopping the server is what a server or a connection that is down looks
like to the browser: navigator.onLine stays true and every request fails.
The instance is started by ``tests/e2e/run_speakr.py --control-port``,
whose control port (SPEAKR_CONTROL_URL) stops and starts gunicorn; the
scenarios that need it are skipped without it. ``context.set_offline``
also reaches the service worker's requests in this Chromium; scenario 3 is
run both ways.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test. With
SPEAKR_E2E_SHOTS set, screenshots of the recorder are saved there.
"""

import json
import os
import sys
import time
import urllib.request
from contextlib import contextmanager

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import ROOT, clear_finished_jobs, en, login  # noqa: E402
from seed_recordings import TITLES  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
CONTROL = os.environ.get("SPEAKR_CONTROL_URL", "").rstrip("/")
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS", "")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
RUN = str(int(time.time()))[-6:]

needs_control = pytest.mark.skipif(not CONTROL, reason="needs run_speakr.py --control-port (SPEAKR_CONTROL_URL)")

FAKE_MEDIA = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
WAIT = 20000
LANGUAGES = ("en", "de", "es", "fr", "pt-BR", "ru", "zh")
THEMES = ("default", "classic", "workspace", "focus", "desk", "cards")


def locale_table(code):
    with open(os.path.join(ROOT, "static", "locales", f"{code}.json"), encoding="utf-8") as f:
        return json.load(f)


# ---- the server -----------------------------------------------------------

def control(action):
    request = urllib.request.Request(f"{CONTROL}/{action}", method="POST", data=b"")
    with urllib.request.urlopen(request, timeout=180) as response:
        state = json.loads(response.read())
    if action == "start":
        assert state.get("answering"), f"Speakr did not start again: {state}"
    return state


@contextmanager
def server_down():
    """The server stopped: its port refuses connections, as when it is down."""
    control("stop")
    try:
        yield
    finally:
        control("start")


# ---- fixtures ---------------------------------------------------------------

@pytest.fixture(scope="module")
def pw():
    with playwright_api.sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(pw):
    instance = pw.chromium.launch(args=FAKE_MEDIA)
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def admin_cookies(browser):
    """One sign-in through the form for the whole file (sign-ins are rate limited)."""
    ctx = browser.new_context(locale="en-US")
    page = ctx.new_page()
    login(page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
    cookies = ctx.cookies()
    ctx.close()
    return cookies


@pytest.fixture(scope="module")
def admin_api(pw, admin_cookies):
    """The admin's session outside every browser profile, to ask the server what it holds.

    Afterwards the recordings this file uploaded and their finished jobs are
    removed: they would be rows of the processing queue and of the inbox in
    the browser test files that run after this one (test_phone_layout_ui
    measures both)."""
    api = pw.request.new_context(base_url=BASE, storage_state={"cookies": admin_cookies, "origins": []})
    before = server_ids(api)
    yield api
    try:
        token = clear_finished_jobs(api, BASE)
        for recording_id in server_ids(api) - before:
            api.delete(f"{BASE}/recording/{recording_id}", headers={"X-CSRFToken": token})
        clear_finished_jobs(api, BASE)
    finally:
        api.dispose()


@pytest.fixture
def context(browser):
    """A browser profile of its own for each test: no worker, no caches, no IndexedDB."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US", permissions=["microphone"])
    yield ctx
    try:
        ctx.set_offline(False)
    finally:
        ctx.close()


@pytest.fixture
def app(context, admin_cookies):
    """The app, signed in as the admin, its worker in control and the offline recorder stored."""
    context.add_cookies(admin_cookies)
    page = context.new_page()
    page.on("dialog", lambda d: d.dismiss())
    open_app(page)
    wait_for_recorder_files(page)
    return page


# ---- helpers ----------------------------------------------------------------

def open_app(page, path="/"):
    page.goto(f"{BASE}{path}")
    page.wait_for_load_state("load")
    assert "/login" not in page.url, "the session of this test file has ended"
    page.wait_for_function("() => !!document.querySelector('[data-v-app]')", timeout=WAIT)
    page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=WAIT)


def reload_app(page):
    page.reload()
    page.wait_for_load_state("load")
    page.wait_for_function("() => !!document.querySelector('[data-v-app]')", timeout=WAIT)


RECORDER_CACHED = """async () => {
    const v = new URL(navigator.serviceWorker.controller.scriptURL).searchParams.get('v');
    const cache = await caches.open('speakr-static-' + v);
    const has = async (path) => !!(await cache.match(path + '?v=' + encodeURIComponent(v), { ignoreVary: true }));
    return (await has('/static/offline-recorder.html')) && (await has('/static/js/offline-recorder.js'))
        && (await has('/static/js/modules/db/failed-uploads.js')) && (await has('/static/locales/en.json'));
}"""


def until(page, script, arg=None, timeout=WAIT, what=""):
    """Evaluate an async script until it is truthy. (page.wait_for_function
    takes the promise of an async function for a truthy answer.)"""
    deadline = time.monotonic() + timeout / 1000
    while True:
        value = page.evaluate(script, arg)
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or script[:80]}")
        page.wait_for_timeout(300)


def wait_for_recorder_files(page):
    """The app has written the recorder's entry and the worker holds its files."""
    until(page, "() => !!localStorage.getItem('speakr.offlineRecorder')", what="the recorder's entry")
    until(page, RECORDER_CACHED, what="the recorder's files in the cache")


PENDING = """() => new Promise((resolve) => {
    const request = indexedDB.open('SpeakrFailedUploads');
    request.onupgradeneeded = () => request.transaction.abort();
    request.onerror = (event) => { event.preventDefault(); resolve([]); };
    request.onsuccess = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains('failedUploads')) { db.close(); resolve([]); return; }
        const all = db.transaction(['failedUploads'], 'readonly').objectStore('failedUploads').getAll();
        all.onsuccess = () => {
            db.close();
            resolve(all.result.map(({ fileData, ...rest }) => ({ ...rest, bytes: fileData ? fileData.byteLength : 0 })));
        };
    };
})"""

LOCAL_SESSION = """() => new Promise((resolve) => {
    const request = indexedDB.open('SpeakrRecordings');
    request.onupgradeneeded = () => request.transaction.abort();
    request.onerror = (event) => { event.preventDefault(); resolve(null); };
    request.onsuccess = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains('activeRecording')) { db.close(); resolve(null); return; }
        const get = db.transaction(['activeRecording'], 'readonly').objectStore('activeRecording').get('current');
        get.onsuccess = () => {
            db.close();
            const s = get.result;
            resolve(s && s.chunks && s.chunks.length ? { startTime: s.startTime, chunks: s.chunks.length } : null);
        };
    };
})"""


def pending(page):
    return page.evaluate(PENDING)


def wait_pending(page, count, timeout=WAIT):
    until(page, f"async () => (await ({PENDING})()).length === {count}", timeout=timeout, what=f"{count} waiting uploads")
    return pending(page)


def server_ids(admin_api):
    response = admin_api.get("/api/recordings?per_page=200")
    assert response.ok, response.status
    return {r["id"] for r in response.json().get("recordings", [])}


def wait_new_recordings(admin_api, before, count, timeout=30):
    deadline = time.monotonic() + timeout
    new = set()
    while time.monotonic() < deadline:
        new = server_ids(admin_api) - before
        if len(new) >= count:
            break
        time.sleep(0.5)
    return new


def recovery_offered(page):
    return page.get_by_text(en("recording.recoveryTitle"), exact=True).count() > 0


def record_in_app(page, seconds=3):
    """A recording with the in-app recorder; returns before its upload is asked for."""
    page.locator(f'button.header-action[aria-label="{en("nav.newRecording")}"]:visible').first.click()
    page.locator("#upload-tab-record").click()
    page.locator(".modal-footer button", has_text=en("buttons.startRecording")).click()
    stop = page.locator("button", has_text=en("recording.stopRecording")).first
    stop.wait_for(state="visible", timeout=WAIT)
    page.wait_for_timeout(seconds * 1000)
    stop.click()
    upload = page.locator("button", has_text=en("help.uploadRecordingNotes")).first
    upload.wait_for(state="visible", timeout=WAIT)
    return upload


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, f"offline-{name}.png"), full_page=True)


def open_recorder(page, path="/"):
    """Navigate while the server is away: the worker answers the offline recorder."""
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{BASE}{path}")
    page.wait_for_load_state("load")
    page.wait_for_function("() => !!document.body.dataset.orReady", timeout=WAIT)
    assert page.evaluate("() => performance.getEntriesByType('navigation')[0].responseStatus") == 503
    return errors


def record_offline(page, seconds=3):
    page.locator("#or-record").click()
    page.locator("#or-stop").wait_for(state="visible", timeout=WAIT)
    page.wait_for_timeout(seconds * 1000)
    page.locator("#or-stop").click()
    page.wait_for_function(f"() => document.getElementById('or-status').textContent === {json.dumps(en('offlineRecorder.saved'))}",
                           timeout=WAIT)


def back_online(context, page):
    """Online again, from a blank page: an offline page would load itself
    again on the browser's online event and abort the next navigation."""
    page.goto("about:blank")
    context.set_offline(False)


def no_user_data(page):
    body = page.inner_text("body")
    for title in TITLES:
        assert title not in body, f"a recording of the library is shown offline: {title}"
    html = page.content()
    assert "csrf" not in html.lower()
    assert "/api/" not in html


# ---- 1 and 2: the duplicate upload --------------------------------------------

def test_a_recording_whose_upload_failed_offline_is_uploaded_once(context, app, admin_api):
    page = app
    before = server_ids(admin_api)
    upload = record_in_app(page)
    assert page.evaluate(LOCAL_SESSION), "no crash-recovery copy was kept while recording"

    context.set_offline(True)
    upload.click()
    [record] = wait_pending(page, 1)
    assert record["fromRecorder"] is True and record["recordingSessionStartTime"], record
    assert record["ownerId"] == page.evaluate("() => document.getElementById('app').dataset.currentUserId")
    assert record["bytes"] > 1000, record
    # The whole recording is in the record; the chunks saved while recording are not kept beside it.
    assert page.evaluate(LOCAL_SESSION) is None

    # The connection is back: the browser says so, and the app sends the record.
    context.set_offline(False)
    new = wait_new_recordings(admin_api, before, 1)
    assert len(new) == 1, new
    wait_pending(page, 0)

    for _ in range(2):
        reload_app(page)
        page.wait_for_timeout(3000)
        assert not recovery_offered(page), "the uploaded recording is offered for recovery"
        assert pending(page) == []
        assert page.evaluate(LOCAL_SESSION) is None
    assert server_ids(admin_api) - before == new


def test_an_upload_the_server_did_not_take_is_saved_and_uploaded_once_at_the_next_load(context, app, admin_api):
    page = app
    before = server_ids(admin_api)
    upload = record_in_app(page)
    # The browser reports a connection; only the upload is refused, as by a server that is down.
    refused = []
    page.route("**/upload", lambda route: (refused.append(route.request.method), route.abort("connectionrefused")))
    upload.click()
    wait_pending(page, 1)
    assert refused == ["POST"]
    assert page.evaluate("() => navigator.onLine") is True
    assert server_ids(admin_api) == before
    page.unroute("**/upload")

    reload_app(page)
    new = wait_new_recordings(admin_api, before, 1)
    assert len(new) == 1, new
    wait_pending(page, 0)
    reload_app(page)
    page.wait_for_timeout(3000)
    assert not recovery_offered(page)
    assert server_ids(admin_api) - before == new


def test_an_upload_whose_answer_was_lost_is_not_stored_twice(context, app, admin_api):
    """The request reached the server and its answer did not reach the browser (#626, #650)."""
    page = app
    before = server_ids(admin_api)
    upload = record_in_app(page)
    seen = []

    def lose_the_answer(route):
        seen.append(route.request.method)
        route.fetch()  # the server stores the recording
        route.abort("connectionreset")

    page.route("**/upload", lose_the_answer)
    upload.click()
    wait_pending(page, 1)
    stored = wait_new_recordings(admin_api, before, 1)
    assert len(stored) == 1 and seen == ["POST"]
    page.unroute("**/upload")

    reload_app(page)
    wait_pending(page, 0)
    page.wait_for_timeout(2000)
    assert server_ids(admin_api) - before == stored
    reload_app(page)
    page.wait_for_timeout(2000)
    assert server_ids(admin_api) - before == stored
    assert not recovery_offered(page)


# ---- 3: the app opened without a connection ------------------------------------

def _offline_start(context, page, admin_api, away):
    before = server_ids(admin_api)
    user_id = page.evaluate("() => document.getElementById('app').dataset.currentUserId")
    with away():
        errors = open_recorder(page)
        assert page.locator("#speakr-offline h1").inner_text() == en("offlineRecorder.title")
        assert page.locator("#or-lead").inner_text() == en("offlineRecorder.lead")
        assert page.evaluate("() => document.body.dataset.orReady") == "recorder"
        assert page.evaluate("() => document.body.dataset.orLocale") == "en", "the locale file was not loaded"
        no_user_data(page)
        shot(page, "recorder-ready")

        record_offline(page, 3)
        record_offline(page, 2)
        rows = page.locator(".or-item")
        assert rows.count() == 2
        meta = rows.first.locator(".or-item-meta").inner_text()
        assert meta.startswith("0:02") and "KB" in meta, meta
        assert rows.first.locator(".or-item-name").inner_text().startswith("Recording of ")
        shot(page, "recorder-two-saved")

        # One is deleted, after the question.
        rows.first.locator("[data-or-delete]").click()
        page.locator("[data-or-delete-cancel]").click()
        assert rows.count() == 2
        rows.first.locator("[data-or-delete]").click()
        page.locator("[data-or-delete-confirm]").click()
        page.wait_for_function("() => document.querySelectorAll('.or-item').length === 1", timeout=WAIT)
        [record] = pending(page)
        assert record["source"] == "offline" and record["fromRecorder"] is True and record["ownerId"] == user_id
        assert 2.5 <= record["duration"] <= 6, record
        assert page.evaluate(LOCAL_SESSION) is None, "the crash-recovery copy was left after the save"
        assert errors == [], errors
        # The page asked nothing but static files and whether the server is back
        # (every request of this document, as the browser lists them).
        loaded = page.evaluate("() => performance.getEntriesByType('resource').map((e) => e.name)")
        asked = {u.split("?")[0][len(BASE):] for u in loaded if u.startswith(BASE)}
        assert "/static/js/offline-recorder.js" in asked and "/static/locales/en.json" in asked, asked
        assert all(p.startswith("/static/") or p == "/" for p in asked), asked
        assert page.evaluate("() => document.body.dataset.orLocale") == "en"

    # The server is back: the page loads the app, and the app uploads the recording.
    page.wait_for_function("() => !!document.querySelector('[data-v-app]')", timeout=30000)
    new = wait_new_recordings(admin_api, before, 1)
    assert len(new) == 1, new
    wait_pending(page, 0)
    reload_app(page)
    page.wait_for_timeout(3000)
    assert not recovery_offered(page)
    assert server_ids(admin_api) - before == new


@needs_control
def test_the_app_opened_while_the_server_is_down_records_and_uploads_once(context, app, admin_api):
    _offline_start(context, app, admin_api, server_down)


def test_the_app_opened_while_the_browser_is_offline_records_and_uploads_once(context, app, admin_api):
    @contextmanager
    def browser_offline():
        context.set_offline(True)
        try:
            yield
        finally:
            context.set_offline(False)

    _offline_start(context, app, admin_api, browser_offline)


@needs_control
def test_the_recorder_waits_for_the_end_of_a_recording_before_it_opens_the_app(context, app, admin_api):
    page = app
    before = server_ids(admin_api)
    control("stop")
    try:
        open_recorder(page)
        page.locator("#or-record").click()
        page.locator("#or-stop").wait_for(state="visible", timeout=WAIT)
    finally:
        control("start")
    # The server answers again while the recording goes on: the page stays and says so.
    page.wait_for_function(f"() => document.getElementById('or-status').textContent === {json.dumps(en('offlineRecorder.backWhileRecording'))}",
                           timeout=20000)
    assert page.locator("#speakr-offline").count() == 1
    shot(page, "recorder-back-while-recording")
    page.locator("#or-stop").click()
    page.wait_for_function("() => !!document.querySelector('[data-v-app]')", timeout=30000)
    new = wait_new_recordings(admin_api, before, 1)
    assert len(new) == 1
    wait_pending(page, 0)


# ---- 4: sign-out and other accounts ---------------------------------------------

def sign_out(page):
    page.locator("[data-user-menu-toggle]:visible").first.click()
    page.locator('form[action="/logout"] [type=submit]:visible').first.click()


def test_sign_out_warns_deletes_the_waiting_recording_and_removes_the_recorder(context, app, admin_api):
    page = app
    before = server_ids(admin_api)
    assert page.evaluate("() => JSON.parse(localStorage.getItem('speakr.offlineRecorder')).userId")
    # A recording waits in the browser: its upload was refused.
    upload = record_in_app(page)
    page.route("**/upload", lambda route: route.abort("connectionrefused"))
    upload.click()
    wait_pending(page, 1)
    page.unroute("**/upload")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)

    sign_out(page)
    dialog = page.locator("[data-app-dialog]")
    dialog.wait_for(state="visible", timeout=WAIT)
    assert en("signOut.localCopiesMessageOne") in dialog.inner_text()
    shot(page, "sign-out-question")
    page.locator("[data-app-dialog-confirm]").click()
    page.wait_for_url("**/login*")
    page.wait_for_load_state("load")
    page.evaluate("() => window.SpeakrServiceWorker.ready.then(() => true)")
    assert page.evaluate("() => localStorage.getItem('speakr.offlineRecorder')") is None
    assert pending(page) == []
    assert page.evaluate(LOCAL_SESSION) is None
    assert server_ids(admin_api) == before, "the recording deleted at sign-out was uploaded"
    # What is in Cache Storage now was requested by the sign-in page itself; the recorder is not.
    cached = page.evaluate("""async () => {
        const out = [];
        for (const name of await caches.keys()) {
            for (const request of await (await caches.open(name)).keys()) out.push(new URL(request.url).pathname);
        }
        return out;
    }""")
    assert "/static/offline-recorder.html" not in cached and "/static/js/offline-recorder.js" not in cached, cached

    context.set_offline(True)
    try:
        page.goto(f"{BASE}/")
        page.wait_for_load_state("load")
        # No recorder in the cache: the plain page of the worker.
        assert page.locator("#speakr-offline h1").inner_text() == "You are offline"
        assert page.locator("#or-record").count() == 0
        no_user_data(page)
    finally:
        back_online(context, page)

    # Should the recorder's files be in the cache again (asked for from the
    # sign-in page), it records nothing without an account.
    page.goto(f"{BASE}/login")
    page.wait_for_load_state("load")
    page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=WAIT)
    page.evaluate("() => navigator.serviceWorker.controller.postMessage({ type: 'PREPARE_OFFLINE_RECORDER', locale: 'en' })")
    until(page, RECORDER_CACHED, what="the recorder's files in the cache")
    context.set_offline(True)
    try:
        open_recorder(page)
        assert page.evaluate("() => document.body.dataset.orReady") == "notSignedIn"
        assert page.locator("#or-unavailable").inner_text() == en("offlineRecorder.notSignedIn")
        assert not page.locator("#or-record").is_visible()
        no_user_data(page)
        shot(page, "recorder-signed-out")
    finally:
        back_online(context, page)


@pytest.fixture(scope="module")
def second_user(browser, admin_api, admin_cookies):
    """Another account, signed in once through the form."""
    ctx = browser.new_context(locale="en-US")
    ctx.add_cookies(admin_cookies)
    page = ctx.new_page()
    page.goto(f"{BASE}/admin")
    token = page.locator("meta[name=csrf-token]").get_attribute("content")
    user = {"username": f"e2eoffline_{RUN}", "email": f"e2eoffline_{RUN}@example.com", "password": f"Pw-{RUN}-offline-1"}
    r = page.request.post(f"{BASE}/admin/users", headers={"X-CSRFToken": token}, data=user)
    assert r.status == 201, f"{r.status} {r.text()}"
    user["id"] = r.json()["id"]
    ctx.close()
    other = browser.new_context(locale="en-US")
    page = other.new_page()
    login(page, BASE, user["email"], user["password"])
    user["cookies"] = other.cookies()
    other.close()
    yield user
    ctx = browser.new_context(locale="en-US")
    ctx.add_cookies(admin_cookies)
    page = ctx.new_page()
    page.goto(f"{BASE}/admin")
    token = page.locator("meta[name=csrf-token]").get_attribute("content")
    page.request.delete(f"{BASE}/admin/users/{user['id']}", headers={"X-CSRFToken": token})
    ctx.close()


SEED_LOCAL_SESSION = """(owner) => new Promise((resolve, reject) => {
    const request = indexedDB.open('SpeakrRecordings', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('activeRecording', { keyPath: 'id' })
        .createIndex('timestamp', 'timestamp', { unique: false });
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
        const tx = request.result.transaction(['activeRecording'], 'readwrite');
        tx.objectStore('activeRecording').put({ id: 'current', timestamp: Date.now(), startTime: new Date().toISOString(),
            mode: 'microphone', notes: '', tags: [], asrOptions: {}, chunks: [{ index: 0, data: new ArrayBuffer(64), size: 64 }],
            mimeType: 'audio/webm', duration: 3, incognito: false, ownerId: String(owner) });
        tx.oncomplete = () => { request.result.close(); resolve(true); };
    };
})"""


def test_another_account_signing_in_deletes_what_waits_for_the_first(context, app, admin_api, admin_cookies, second_user, pw):
    page = app
    admin_id = page.evaluate("() => document.getElementById('app').dataset.currentUserId")
    before = server_ids(admin_api)
    other_api = pw.request.new_context(base_url=BASE, storage_state={"cookies": second_user["cookies"], "origins": []})
    try:
        other_before = {r["id"] for r in other_api.get("/api/recordings?per_page=50").json().get("recordings", [])}
        with server_down():
            open_recorder(page)
            record_offline(page, 2)
            # And a crash-recovery copy of the admin, as from a closed tab.
            page.evaluate(SEED_LOCAL_SESSION, admin_id)
            # Another account in the same browser before the server is back
            # (the admin's session ended without a sign-out).
            context.clear_cookies()
            context.add_cookies(second_user["cookies"])
        # The recorder opens the app, now the other account's: both copies
        # of the admin are deleted, with a notice and no question.
        page.wait_for_function("() => !!document.querySelector('[data-v-app]')", timeout=30000)
        notice = page.get_by_text(en("upload.otherAccountCopiesDeleted").replace("{{count}}", "2"))
        notice.first.wait_for(state="visible", timeout=WAIT)
        page.wait_for_function("() => !document.documentElement.classList.contains('app-loading')", timeout=WAIT)
        page.wait_for_timeout(1200)  # the page and the notice fade in
        shot(page, "account-change-notice")
        assert page.locator("[data-app-dialog]:visible").count() == 0
        assert page.evaluate("() => document.getElementById('app').dataset.currentUserId") == str(second_user["id"])
        assert pending(page) == []
        assert page.evaluate(LOCAL_SESSION) is None
        assert not recovery_offered(page)
        page.wait_for_timeout(3000)
        other_now = {r["id"] for r in other_api.get("/api/recordings?per_page=50").json().get("recordings", [])}
        assert other_now == other_before, "the admin's recording went to the other account"
        assert server_ids(admin_api) == before
    finally:
        other_api.dispose()

    # The admin again: nothing of the admin is left to upload or recover.
    context.clear_cookies()
    context.add_cookies(admin_cookies)
    open_app(page)
    page.wait_for_timeout(4000)
    assert pending(page) == []
    assert not recovery_offered(page)
    assert server_ids(admin_api) == before


# ---- 6: expiry of the note, and the limits of a recording ---------------------------

SET_ENTRY = """(changes) => {
    const entry = JSON.parse(localStorage.getItem('speakr.offlineRecorder'));
    for (const [key, value] of Object.entries(changes)) {
        if (key === 'limits') Object.assign(entry.limits, value); else entry[key] = value;
    }
    localStorage.setItem('speakr.offlineRecorder', JSON.stringify(entry));
    return entry;
}"""
DAY_MS = 24 * 3600 * 1000


def test_the_note_expires_and_is_renewed_at_each_signed_in_load(context, app, admin_api):
    page = app
    entry = page.evaluate("() => JSON.parse(localStorage.getItem('speakr.offlineRecorder'))")
    now = page.evaluate("() => Date.now()")
    # REMEMBER_COOKIE_DAYS (30 in the example settings) from this load.
    assert abs(entry["expiresAt"] - (entry["savedAt"] + 30 * DAY_MS)) < 1000, entry
    assert abs(entry["savedAt"] - now) < 120000, entry
    assert entry["limits"]["maxSeconds"] == 8 * 3600, entry
    assert entry["limits"]["maxRecorderBytes"] == 200 * 1024 * 1024, entry
    assert entry["limits"]["maxUploadBytes"] > 0, entry
    before = server_ids(admin_api)
    context.set_offline(True)
    try:
        open_recorder(page)
        # Older than its expiry: the plain offline message, no Record button.
        page.evaluate(SET_ENTRY, {"expiresAt": now - 1})
        open_recorder(page)
        assert page.evaluate("() => document.body.dataset.orReady") == "notSignedIn"
        assert page.locator("#or-lead").inner_text() == en("offlineRecorder.leadPlain")
        assert not page.locator("#or-record").is_visible()
        shot(page, "expired")
        # Within its expiry: recording works.
        page.evaluate(SET_ENTRY, {"expiresAt": now + DAY_MS})
        open_recorder(page)
        assert page.evaluate("() => document.body.dataset.orReady") == "recorder"
        record_offline(page, 2)
    finally:
        back_online(context, page)
    # The next signed-in load uploads it, and writes the note again with a new expiry.
    open_app(page)
    assert len(wait_new_recordings(admin_api, before, 1)) == 1
    wait_pending(page, 0)
    renewed = page.evaluate("() => JSON.parse(localStorage.getItem('speakr.offlineRecorder'))")
    assert renewed["expiresAt"] > now + 29 * DAY_MS, renewed


def _limited_recording(context, page, admin_api, limits, reason, shot_name):
    before = server_ids(admin_api)
    context.set_offline(True)
    try:
        open_recorder(page)
        page.evaluate(SET_ENTRY, {"limits": limits})
        open_recorder(page)
        # Every text the limit note shows, in order, however briefly.
        page.evaluate("""() => {
            window.__notes = [];
            const note = document.getElementById('or-limit');
            new MutationObserver(() => { if (!note.hidden) window.__notes.push(note.textContent); })
                .observe(note, { childList: true, characterData: true, subtree: true, attributes: true });
        }""")
        page.locator("#or-record").click()
        page.locator("#or-stop").wait_for(state="visible", timeout=WAIT)
        # The time left is shown, then the warning at 80 %, then the stop.
        page.locator("#or-left").wait_for(state="visible", timeout=WAIT)
        assert page.locator("#or-left").inner_text().endswith(" left")
        warning = page.locator("#or-limit")
        warning.wait_for(state="visible", timeout=60000)
        if page.locator("#or-stop").is_visible():
            shot(page, f"{shot_name}-warning")
        page.wait_for_function(f"() => document.body.dataset.orStopped === '{reason}'", timeout=60000)
        notes = page.evaluate("() => window.__notes")
        assert notes and notes[0].startswith("The recording stops by itself in "), notes
        assert page.locator("#or-status").inner_text() == en("offlineRecorder.saved")
        if reason == "duration":
            assert warning.inner_text() == en("offlineRecorder.stoppedDuration").replace(
                "{{limit}}", "0:%02d" % limits["maxSeconds"]), warning.inner_text()
        else:
            assert warning.inner_text().endswith("It was stopped and saved."), warning.inner_text()
        assert page.locator("#or-record").is_visible() and not page.locator("#or-stop").is_visible()
        shot(page, f"{shot_name}-stopped")
        [record] = pending(page)
    finally:
        back_online(context, page)
    open_app(page)
    new = wait_new_recordings(admin_api, before, 1)
    assert len(new) == 1, new
    wait_pending(page, 0)
    reload_app(page)
    page.wait_for_timeout(2000)
    assert server_ids(admin_api) - before == new
    return record, new.pop()


def test_a_recording_stops_at_its_maximum_length(context, app, admin_api):
    record, rec_id = _limited_recording(context, app, admin_api, {"maxSeconds": 6}, "duration", "limit-duration")
    assert 5.5 <= record["duration"] <= 7.5, record
    details = admin_api.get(f"/api/recordings/{rec_id}")
    assert details.ok
    # The server knows the length once it has read the file (no ASR service
    # runs here, so it may not have yet).
    seconds = details.json().get("audio_duration")
    print(f"server audio_duration of the stopped recording: {seconds}")
    if seconds is not None:
        assert 4.5 <= seconds <= 8, seconds


def test_a_recording_stops_at_its_maximum_size(context, app, admin_api):
    record, _ = _limited_recording(context, app, admin_api, {"maxRecorderBytes": 90000, "maxSeconds": 3600}, "size", "limit-size")
    # Stopped before the next chunk would have carried it over the limit.
    assert 50000 <= record["bytes"] <= 90000, record
    assert record["duration"] < 30, record


STORAGE_STUB = """(() => {
    const mode = localStorage.getItem('e2eStorage');
    if (!mode || !navigator.storage) return;
    let calls = 0;
    const MB = 1024 * 1024;
    navigator.storage.estimate = async () => {
        calls += 1;
        if (mode === 'low') return { quota: 100 * MB, usage: 100 * MB - 10 * MB };
        // 'shrinking': room to start, then little left.
        return calls === 1 ? { quota: 100 * MB, usage: 36 * MB } : { quota: 100 * MB, usage: 100 * MB - (16 * MB + 60000) };
    };
})()"""


def test_little_storage_prevents_a_recording_or_stops_it_with_a_save(context, app, admin_api):
    page = app
    context.add_init_script(STORAGE_STUB)
    before = server_ids(admin_api)
    context.set_offline(True)
    try:
        open_recorder(page)
        page.evaluate("() => localStorage.setItem('e2eStorage', 'low')")
        open_recorder(page)
        page.locator("#or-record").click()
        page.wait_for_function(f"() => document.getElementById('or-status').textContent === {json.dumps(en('offlineRecorder.noStorage'))}",
                               timeout=WAIT)
        assert not page.locator("#or-stop").is_visible()
        assert pending(page) == []
        shot(page, "storage-low")

        page.evaluate("() => localStorage.setItem('e2eStorage', 'shrinking')")
        open_recorder(page)
        page.locator("#or-record").click()
        page.wait_for_function("() => document.body.dataset.orStopped === 'storage'", timeout=60000)
        assert page.locator("#or-limit").inner_text() == en("offlineRecorder.stoppedStorage")
        assert page.locator("#or-status").inner_text() == en("offlineRecorder.saved")
        [record] = pending(page)
        assert 0 < record["bytes"] <= 60000 + 40000, record
        page.evaluate("() => localStorage.removeItem('e2eStorage')")
    finally:
        back_online(context, page)
    open_app(page)
    assert len(wait_new_recordings(admin_api, before, 1)) == 1
    wait_pending(page, 0)


def test_screens_of_the_new_states_in_light_and_dark_at_two_widths(context, app):
    """Screenshots (SPEAKR_E2E_SHOTS) of the expired note, the warning, the
    stop at a limit and the account-change notice; each checked for a
    horizontal scroll."""
    page = app
    scroll = "() => document.scrollingElement.scrollWidth - window.innerWidth"
    for dark in (False, True):
        mode = "dark" if dark else "light"
        for width, height in ((1280, 860), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            context.set_offline(True)
            try:
                open_recorder(page)
                page.evaluate("(d) => localStorage.setItem('darkMode', String(d))", dark)
                entry = page.evaluate(SET_ENTRY, {"limits": {"maxSeconds": 5}})
                page.evaluate(SET_ENTRY, {"expiresAt": 1})
                open_recorder(page)
                shot(page, f"state-expired-{mode}-{width}")
                assert page.evaluate(scroll) <= 0
                page.evaluate(SET_ENTRY, {"expiresAt": entry["savedAt"] + 30 * DAY_MS})
                open_recorder(page)
                page.locator("#or-record").click()
                page.locator("#or-limit").wait_for(state="visible", timeout=WAIT)
                shot(page, f"state-warning-{mode}-{width}")
                assert page.evaluate(scroll) <= 0
                page.wait_for_function("() => document.body.dataset.orStopped === 'duration'", timeout=WAIT)
                shot(page, f"state-stopped-{mode}-{width}")
                assert page.evaluate(scroll) <= 0
                # Not uploaded: the next step shows it as another account's.
                page.evaluate("""() => new Promise((resolve) => {
                    const request = indexedDB.open('SpeakrFailedUploads');
                    request.onsuccess = () => {
                        const tx = request.result.transaction(['failedUploads'], 'readwrite');
                        const store = tx.objectStore('failedUploads');
                        const all = store.getAll();
                        all.onsuccess = () => { for (const r of all.result) { r.ownerId = '999999'; store.put(r); } };
                        tx.oncomplete = () => { request.result.close(); resolve(true); };
                    };
                })""")
            finally:
                back_online(context, page)
            open_app(page)
            page.evaluate("(d) => localStorage.setItem('darkMode', String(d))", dark)
            notice = page.get_by_text(en("upload.otherAccountCopiesDeletedOne"))
            notice.first.wait_for(state="visible", timeout=WAIT)
            page.wait_for_function("() => !document.documentElement.classList.contains('app-loading')", timeout=WAIT)
            page.wait_for_timeout(1200)  # the page and the notice fade in
            shot(page, f"state-account-notice-{mode}-{width}")
            assert pending(page) == []
    page.evaluate("() => localStorage.removeItem('darkMode')")
    page.set_viewport_size({"width": 1400, "height": 900})


# ---- 5: themes, widths and languages ---------------------------------------------

LOOK = """() => {
    const card = document.querySelector('.or-card').getBoundingClientRect();
    const items = [...document.querySelectorAll('#or-source [data-or-mode]')].map((el) => el.getBoundingClientRect());
    const button = document.getElementById('or-record');
    const b = button.getBoundingClientRect();
    const style = (el) => getComputedStyle(el);
    return {
        scroll: document.scrollingElement.scrollWidth - window.innerWidth,
        cardInside: card.left >= 0 && card.right <= window.innerWidth + 0.5,
        buttonInside: b.left >= card.left && b.right <= card.right && b.width > 100,
        sourceInside: items.length === 3 && items.every((r) => r.left >= card.left && r.right <= card.right && r.height >= 36),
        buttonBg: style(button).backgroundColor,
        buttonColor: style(button).color,
        cardBg: style(document.querySelector('.or-card')).backgroundColor,
        bodyBg: style(document.body).backgroundColor,
        theme: document.documentElement.dataset.uiTheme,
        dark: document.documentElement.classList.contains('dark'),
    };
}"""


def test_the_recorder_in_every_theme_mode_and_width(context, app):
    page = app
    results = []
    context.set_offline(True)
    try:
        open_recorder(page)
        for theme in THEMES:
            for dark in (False, True):
                # Set on the recorder page itself: the app writes the account's theme at each load.
                page.evaluate("([t, d]) => { localStorage.setItem('uiTheme', t); localStorage.setItem('darkMode', String(d)); }",
                              [theme, dark])
                for width, height in ((1280, 860), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    open_recorder(page)
                    page.wait_for_timeout(300)
                    look = page.evaluate(LOOK)
                    shot(page, f"theme-{theme}-{'dark' if dark else 'light'}-{width}")
                    assert look["theme"] == theme and look["dark"] == dark, look
                    assert look["scroll"] <= 0, f"{theme} {dark} {width}: horizontal scroll {look}"
                    assert look["cardInside"] and look["buttonInside"], f"{theme} {dark} {width}: {look}"
                    assert look["sourceInside"], f"{theme} {dark} {width}: the source picker leaves the card {look}"
                    assert look["buttonBg"] != look["cardBg"], f"{theme} {dark} {width}: the record button does not stand out {look}"
                    assert look["buttonBg"] != look["buttonColor"], look
                    results.append((theme, dark, width, look["bodyBg"], look["cardBg"]))
    finally:
        back_online(context, page)
    # Dark is dark: the page differs from light in every theme.
    for theme in THEMES:
        light = {r[3] for r in results if r[0] == theme and not r[1]}
        darker = {r[3] for r in results if r[0] == theme and r[1]}
        assert light.isdisjoint(darker), f"{theme}: the same page background in light and dark"
    page.set_viewport_size({"width": 1400, "height": 900})


def test_the_recorder_speaks_the_language_of_the_app(context, app):
    page = app
    # The worker keeps the locale file of the language the app asks for.
    for code in LANGUAGES:
        page.evaluate("(code) => navigator.serviceWorker.controller.postMessage({ type: 'PREPARE_OFFLINE_RECORDER', locale: code })", code)
        until(page, """async (code) => {
            const v = new URL(navigator.serviceWorker.controller.scriptURL).searchParams.get('v');
            const cache = await caches.open('speakr-static-' + v);
            return !!(await cache.match('/static/locales/' + code + '.json?v=' + encodeURIComponent(v), { ignoreVary: true }));
        }""", code, what=f"the locale file {code}")
    context.set_offline(True)
    try:
        open_recorder(page)
        for code in LANGUAGES:
            table = locale_table(code)["offlineRecorder"]
            # The language the app wrote at its last online load.
            page.evaluate("""(code) => {
                const entry = JSON.parse(localStorage.getItem('speakr.offlineRecorder'));
                entry.locale = code;
                localStorage.setItem('speakr.offlineRecorder', JSON.stringify(entry));
            }""", code)
            open_recorder(page)
            assert page.evaluate("() => document.body.dataset.orLocale") == code
            assert page.evaluate("() => document.documentElement.lang") == code
            assert page.title() == table["pageTitle"], code
            assert page.locator("#or-title").inner_text() == table["title"], code
            assert page.locator("#or-lead").inner_text() == table["lead"], code
            assert page.locator("#or-record").inner_text() == table["record"], code
            if code in ("de", "zh"):
                shot(page, f"lang-{code}")
    finally:
        back_online(context, page)


# ---- 7: system audio and both (#697) ------------------------------------------------

def media_spy(share=""):
    """An init script that keeps every stream and AudioContext the page makes
    (window.__media), to check that each is stopped. share: '' as the browser
    answers; 'cancel' a canceled share; 'silent' a share without audio;
    'none' a browser without getDisplayMedia."""
    return """(() => {
        const share = %s;
        const md = navigator.mediaDevices;
        if (!md) return;
        window.__media = { shares: [], mics: [], contexts: [] };
        if (share === 'none') { delete MediaDevices.prototype.getDisplayMedia; return; }
        const display = md.getDisplayMedia.bind(md);
        md.getDisplayMedia = async (constraints) => {
            window.__media.asked = constraints;
            if (share === 'cancel') throw new DOMException('Permission denied', 'NotAllowedError');
            const stream = await display(constraints);
            if (share === 'silent') for (const t of stream.getAudioTracks()) { t.stop(); stream.removeTrack(t); }
            window.__media.shares.push(stream);
            return stream;
        };
        const user = md.getUserMedia.bind(md);
        md.getUserMedia = async (constraints) => {
            const stream = await user(constraints);
            window.__media.mics.push(stream);
            return stream;
        };
        const Base = window.AudioContext;
        window.AudioContext = class extends Base {
            constructor(...args) { super(...args); window.__media.contexts.push(this); }
        };
    })()""" % json.dumps(share)


MEDIA_STATE = """() => {
    const m = window.__media;
    const tracks = (list) => list.flatMap((s) => s.getTracks().map((t) => t.kind + ':' + t.readyState));
    return { shares: tracks(m.shares), mics: tracks(m.mics), contexts: m.contexts.map((c) => c.state),
             shareCount: m.shares.length, micCount: m.mics.length, asked: m.asked || null };
}"""

SESSION_MODE = """() => new Promise((resolve) => {
    const request = indexedDB.open('SpeakrRecordings');
    request.onupgradeneeded = () => request.transaction.abort();
    request.onerror = (event) => { event.preventDefault(); resolve(null); };
    request.onsuccess = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains('activeRecording')) { db.close(); resolve(null); return; }
        const get = db.transaction(['activeRecording'], 'readonly').objectStore('activeRecording').get('current');
        get.onsuccess = () => {
            db.close();
            const s = get.result;
            resolve(s ? { mode: s.mode, chunks: (s.chunks || []).length } : null);
        };
    };
})"""


def choose_source(page, mode):
    page.locator(f'#or-source [data-or-mode="{mode}"]').click()
    assert page.locator(f'#or-source [data-or-mode="{mode}"]').get_attribute("aria-checked") == "true"


def all_stopped(state):
    return all(t.endswith(":ended") for t in state["shares"] + state["mics"]) and all(c == "closed" for c in state["contexts"])


def test_the_recorder_records_system_audio_and_both_and_the_app_uploads_them(context, app, admin_api):
    page = app
    context.add_init_script(media_spy())
    before = server_ids(admin_api)
    context.set_offline(True)
    try:
        open_recorder(page)
        source = page.locator("#or-source")
        assert source.is_visible()
        assert source.locator("#or-source-label").inner_text() == en("recording.source")
        labels = [b.inner_text().strip() for b in source.locator("[data-or-mode]").all()]
        assert labels == [en("recording.microphone"), en("recording.systemAudio"), en("recording.microphoneAndSystem")], labels
        assert source.locator('[aria-checked="true"]').get_attribute("data-or-mode") == "microphone"
        assert not page.locator("#or-source-hint").is_visible()
        assert source.locator("[data-or-mode]:disabled").count() == 0

        # System audio: the share's audio alone, its video stopped at once.
        choose_source(page, "system")
        page.locator("#or-record").click()
        page.locator("#or-stop").wait_for(state="visible", timeout=WAIT)
        assert not source.is_visible(), "the source can be changed while recording"
        state = page.evaluate(MEDIA_STATE)
        assert state["asked"] == {"video": True, "audio": True, "audioSelection": "preferred"}, state
        assert state["shares"] == ["audio:live", "video:ended"] and state["micCount"] == 0, state
        page.wait_for_timeout(3000)
        assert page.evaluate(SESSION_MODE)["mode"] == "system"
        page.locator("#or-stop").click()
        page.wait_for_function(f"() => document.getElementById('or-status').textContent === {json.dumps(en('offlineRecorder.saved'))}",
                               timeout=WAIT)
        assert all_stopped(page.evaluate(MEDIA_STATE)), page.evaluate(MEDIA_STATE)
        assert page.evaluate(LOCAL_SESSION) is None

        # Both: the share first, then the microphone, mixed in an AudioContext.
        choose_source(page, "both")
        page.locator("#or-record").click()
        page.locator("#or-stop").wait_for(state="visible", timeout=WAIT)
        state = page.evaluate(MEDIA_STATE)
        assert state["shareCount"] == 2 and state["micCount"] == 1 and state["contexts"] == ["running"], state
        page.wait_for_timeout(3000)
        assert page.evaluate(SESSION_MODE)["mode"] == "both"
        shot(page, "source-both-recording")
        page.locator("#or-stop").click()
        page.wait_for_function(f"() => document.getElementById('or-status').textContent === {json.dumps(en('offlineRecorder.saved'))}",
                               timeout=WAIT)
        state = page.evaluate(MEDIA_STATE)
        assert all_stopped(state) and state["contexts"] == ["closed"], state

        records = sorted(pending(page), key=lambda r: r["timestamp"])
        assert [r["recordingMode"] for r in records] == ["system", "both"], records
        for record in records:
            assert record["source"] == "offline" and record["fromRecorder"] is True, record
            assert record["bytes"] > 1000 and 2.5 <= record["duration"] <= 8, record
        # The choice is remembered, as in the app.
        open_recorder(page)
        assert page.locator('#or-source [aria-checked="true"]').get_attribute("data-or-mode") == "both"
        shot(page, "source-remembered")
    finally:
        back_online(context, page)
    open_app(page)
    new = wait_new_recordings(admin_api, before, 2)
    assert len(new) == 2, new
    wait_pending(page, 0)
    reload_app(page)
    page.wait_for_timeout(2000)
    assert not recovery_offered(page)
    assert server_ids(admin_api) - before == new
    page.evaluate("() => localStorage.removeItem('recordSource')")


def test_a_share_ended_from_the_browser_stops_and_saves_the_recording(context, app):
    page = app
    context.add_init_script(media_spy())
    context.set_offline(True)
    try:
        open_recorder(page)
        choose_source(page, "both")
        page.locator("#or-record").click()
        page.locator("#or-stop").wait_for(state="visible", timeout=WAIT)
        page.wait_for_timeout(3000)
        # What the browser's "Stop sharing" does: the share's audio track ends.
        page.evaluate("() => window.__media.shares[0].getAudioTracks()[0].dispatchEvent(new Event('ended'))")
        page.wait_for_function(f"() => document.getElementById('or-status').textContent === {json.dumps(en('offlineRecorder.saved'))}",
                               timeout=WAIT)
        assert page.locator("#or-record").is_visible() and not page.locator("#or-stop").is_visible()
        assert all_stopped(page.evaluate(MEDIA_STATE)), page.evaluate(MEDIA_STATE)
        [record] = pending(page)
        assert record["recordingMode"] == "both" and record["bytes"] > 1000, record
        assert page.evaluate(LOCAL_SESSION) is None
        page.evaluate("() => localStorage.removeItem('recordSource')")
    finally:
        back_online(context, page)


@pytest.mark.parametrize("share,message", [("cancel", "toasts.screenShareCancelled"), ("silent", "offlineRecorder.noAudioTrack")])
def test_a_canceled_share_or_one_without_audio_records_nothing(context, app, share, message):
    page = app
    context.add_init_script(media_spy(share))
    context.set_offline(True)
    try:
        open_recorder(page)
        for mode in ("system", "both"):
            choose_source(page, mode)
            page.locator("#or-record").click()
            page.wait_for_function(f"() => document.getElementById('or-status').textContent === {json.dumps(en(message))}",
                                   timeout=WAIT)
            assert page.locator("#or-record").is_visible() and not page.locator("#or-stop").is_visible()
            state = page.evaluate(MEDIA_STATE)
            # No microphone is asked for after a refused share, and nothing is left running.
            assert state["micCount"] == 0 and all_stopped(state), state
        shot(page, f"source-{share}")
        assert pending(page) == []
        assert page.evaluate(LOCAL_SESSION) is None
        page.evaluate("() => localStorage.removeItem('recordSource')")
    finally:
        back_online(context, page)


def test_without_display_capture_only_the_microphone_is_offered(context, app):
    page = app
    context.add_init_script(media_spy("none"))
    context.set_offline(True)
    try:
        open_recorder(page)
        # A choice of the app on a browser that could share is not used here.
        page.evaluate("() => localStorage.setItem('recordSource', 'both')")
        open_recorder(page)
        source = page.locator("#or-source")
        assert source.locator('[aria-checked="true"]').get_attribute("data-or-mode") == "microphone"
        assert source.locator("[data-or-mode]:disabled").count() == 2
        assert page.locator("#or-source-hint").inner_text() == en("recording.systemAudioNotSupported")
        shot(page, "source-unsupported")
        page.locator("#or-record").click()
        page.locator("#or-stop").wait_for(state="visible", timeout=WAIT)
        page.wait_for_timeout(2000)
        page.locator("#or-stop").click()
        page.wait_for_function(f"() => document.getElementById('or-status').textContent === {json.dumps(en('offlineRecorder.saved'))}",
                               timeout=WAIT)
        [record] = pending(page)
        assert record["recordingMode"] == "microphone", record
        assert all_stopped(page.evaluate(MEDIA_STATE))
        page.evaluate("() => localStorage.removeItem('recordSource')")
    finally:
        back_online(context, page)


def test_the_app_offers_an_interrupted_recording_with_its_source_and_uploads_it(context, app, admin_api):
    """A tab closed during a recording of both: the crash-recovery copy has
    the source, and the app offers it as a recording of both."""
    page = app
    before = server_ids(admin_api)
    context.set_offline(True)
    try:
        open_recorder(page)
        choose_source(page, "both")
        page.locator("#or-record").click()
        page.locator("#or-stop").wait_for(state="visible", timeout=WAIT)
        until(page, f"async () => {{ const s = await ({SESSION_MODE})(); return s && s.chunks >= 2 && s.mode === 'both'; }}",
              what="two chunks of the crash-recovery copy")
        page.close(run_before_unload=False)
    finally:
        context.set_offline(False)
    page = context.new_page()
    page.on("dialog", lambda d: d.dismiss())
    open_app(page)
    modal = page.locator(".modal-panel", has=page.get_by_text(en("recording.recoveryTitle"), exact=True))
    modal.wait_for(state="visible", timeout=WAIT)
    assert en("recording.modeBoth") in modal.inner_text(), modal.inner_text()
    page.wait_for_function("() => !document.documentElement.classList.contains('app-loading')", timeout=WAIT)
    page.wait_for_timeout(1200)  # the page and the dialog fade in
    shot(page, "source-recovery")
    modal.locator("button", has_text=en("recording.restoreRecording")).click()
    upload = page.locator("button", has_text=en("help.uploadRecordingNotes")).first
    upload.wait_for(state="visible", timeout=WAIT)
    upload.click()
    new = wait_new_recordings(admin_api, before, 1)
    assert len(new) == 1, new
    reload_app(page)
    page.wait_for_timeout(3000)
    assert not recovery_offered(page)
    assert page.evaluate(LOCAL_SESSION) is None
    assert server_ids(admin_api) - before == new
    page.evaluate("() => localStorage.removeItem('recordSource')")
