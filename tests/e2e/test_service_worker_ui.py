"""Browser test of the service worker (static/sw.js, served at /sw.js).

The worker was registered as /static/sw.js, so its scope was /static/ and
it controlled no page. It is now registered with scope / by
static/js/service-worker-client.js on the signed-in pages. This test covers
what that changes for a person, in headless Chromium:

- the worker controls the app after the first load, ``ready`` resolves and
  there is exactly one registration, also on the other signed-in pages; a
  browser that is not signed in gets no worker;
- after browsing, Cache Storage holds files under /static/ only: no page, no
  response of /api/, nothing of an incognito request;
- a recording page loaded again without a network shows the "You are
  offline" page of the worker, not the recording, and the app comes back
  when the network does;
- static files are answered without a network, API requests are not;
- sign-out clears Cache Storage, sessionStorage, the IndexedDB stores with
  recordings and the localStorage entry of unfinished uploads in slices,
  after a question when the browser holds copies that did not reach the
  server;
- a browser that still has the registration of an earlier release at
  /static/, and its cache with a page and an API response in it, ends with
  one worker at scope / and without that cache;
- no notification permission is asked for and no push subscription is made.

A service worker needs a secure context; http://127.0.0.1 and
http://localhost count as one, which is what SPEAKR_BASE_URL is here and in
CI. The unit tests of the worker's decisions are in static/js/sw.test.js,
those of the page's side in static/js/service-worker-client.test.js.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test.
"""

import os
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

CONTROLLED = "() => !!navigator.serviceWorker.controller"
REGISTRATIONS = """async () => (await navigator.serviceWorker.getRegistrations()).map((r) => ({
    scope: r.scope,
    script: (r.active || r.waiting || r.installing || {}).scriptURL || '',
}))"""
# Every entry of every cache, as path with query
CACHED = """async () => {
    const out = [];
    for (const name of await caches.keys()) {
        const cache = await caches.open(name);
        for (const request of await cache.keys()) {
            const url = new URL(request.url);
            out.push({ cache: name, path: url.pathname + url.search, origin: url.origin });
        }
    }
    return out;
}"""
OFFLINE_TITLE = "You are offline"
# The HTTP status of the document in the tab
NAVIGATION_STATUS = "() => performance.getEntriesByType('navigation')[0].responseStatus"
DIALOG = "[data-app-dialog]"
# A document of the app's origin that needs no session and is not rate
# limited as the sign-in page is (10 requests a minute): this file asks for
# the sign-in page five times.
PLAIN_PAGE = "/robots.txt"


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def session_cookies(browser):
    """One sign-in through the form for the whole file (sign-ins are rate
    limited); every test starts a browser profile of its own from its cookies."""
    ctx = browser.new_context(locale="en-US")
    page = ctx.new_page()
    login(page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
    cookies = ctx.cookies()
    ctx.close()
    return cookies


@pytest.fixture
def context(browser):
    """A browser profile of its own for each test: no worker, no caches."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
    yield ctx
    ctx.close()


@pytest.fixture
def signed_in(context, session_cookies):
    """The app's main page, signed in, in a fresh profile."""
    context.add_cookies(session_cookies)
    page = context.new_page()
    page.goto(f"{BASE}/")
    page.wait_for_load_state("load")
    assert "/login" not in page.url, "the session of this test file has ended"
    return page


def controlled(page):
    page.wait_for_function(CONTROLLED, timeout=15000)


def app_version(page):
    return page.locator('meta[name="app-version"]').first.get_attribute("content")


def recording_id(page):
    listing = page.request.get(f"{BASE}/api/recordings?per_page=50")
    items = listing.json().get("recordings", []) if listing.ok else []
    ids = [r["id"] for r in items if r.get("title") in TITLES]
    if not ids:
        precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    return ids[0], next(r["title"] for r in items if r["id"] == ids[0])


def sign_out(page):
    page.locator("[data-user-menu-toggle]:visible").first.click()
    page.locator('form[action="/logout"] [type=submit]:visible').first.click()


def test_the_worker_controls_the_app_after_the_first_load(context, session_cookies):
    context.add_cookies(session_cookies)
    page = context.new_page()
    problems = PageProblems(page, BASE)
    page.goto(f"{BASE}/")
    page.wait_for_load_state("load")
    controlled(page)

    # navigator.serviceWorker.ready never resolved while the scope was /static/.
    ready = page.evaluate("""() => Promise.race([
        navigator.serviceWorker.ready.then((r) => ({ scope: r.scope, script: r.active.scriptURL })),
        new Promise((resolve) => setTimeout(() => resolve(null), 5000)),
    ])""")
    version = app_version(page)
    assert version
    assert ready == {"scope": f"{BASE}/", "script": f"{BASE}/sw.js?v={version}"}
    assert page.evaluate(REGISTRATIONS) == [ready]
    assert page.evaluate("() => navigator.serviceWorker.controller.scriptURL") == ready["script"]

    # A second load is controlled from its first request, and still has one registration.
    page.reload()
    page.wait_for_load_state("load")
    assert page.evaluate(CONTROLLED)
    page.wait_for_timeout(1500)
    assert page.evaluate(REGISTRATIONS) == [ready]
    assert page.locator("[data-v-app]").count() >= 1, "/: the app did not mount under the worker"
    check_common(page, problems, "/")

    # The other signed-in pages are in the same scope.
    for path in ("/account", "/admin"):
        problems.reset()
        page.goto(f"{BASE}{path}")
        page.wait_for_load_state("load")
        assert page.evaluate(CONTROLLED), path
        page.wait_for_timeout(1500)
        assert page.evaluate(REGISTRATIONS) == [ready], path
        check_common(page, problems, path)


def test_a_browser_that_is_not_signed_in_gets_no_worker(context):
    page = context.new_page()
    page.goto(f"{BASE}/login")
    page.wait_for_load_state("load")
    page.wait_for_timeout(2000)
    assert page.evaluate(REGISTRATIONS) == []
    assert page.evaluate("() => caches.keys()") == []


def test_only_static_files_are_in_cache_storage_after_browsing(context, signed_in):
    page = signed_in
    controlled(page)
    rec_id, _ = recording_id(page)
    for path in ("/", f"/recordings/{rec_id}", "/account", "/admin", "/"):
        page.goto(f"{BASE}{path}")
        page.wait_for_load_state("load")
        page.wait_for_timeout(2000)

    # Requests the app makes, once more and by hand, with the worker in control.
    answers = page.evaluate("""async (id) => {
        const out = {};
        for (const path of ['/api/recordings?per_page=5', `/api/recordings/${id}`, '/api/config', '/api/csrf-token',
            `/recordings/${id}`, `/audio/${id}`, '/account', '/']) {
            const response = await fetch(path, { headers: path.startsWith('/audio/') ? { Range: 'bytes=0-99' } : {} });
            await response.arrayBuffer();
            out[path] = response.status;
        }
        return out;
    }""", rec_id)
    assert all(status in (200, 206) for status in answers.values()), answers

    # The incognito rule: an incognito request, and what answers it, is never stored.
    incognito = page.evaluate("""async () => {
        const token = document.querySelector('meta[name="csrf-token"]').content;
        const form = new FormData();
        form.append('file', new Blob([new Uint8Array(64)], { type: 'audio/wav' }), 'incognito-probe.wav');
        const out = [];
        for (const [path, options] of [
            ['/api/recordings/incognito', { method: 'POST', body: form, headers: { 'X-CSRFToken': token } }],
            ['/api/recordings/incognito/chat', { method: 'POST', headers: { 'X-CSRFToken': token, 'Content-Type': 'application/json' }, body: '{}' }],
            ['/api/recordings/incognito', { method: 'GET' }],
        ]) {
            const response = await fetch(path, options);
            await response.arrayBuffer();
            out.push(response.status);
        }
        return out;
    }""")
    assert len(incognito) == 3  # each was answered by the server (whatever the status), not by the worker
    page.wait_for_timeout(500)

    entries = page.evaluate(CACHED)
    assert len(entries) > 10, f"the worker stored almost nothing: {entries}"
    version = app_version(page)
    assert {e["cache"] for e in entries} == {f"speakr-static-{version}"}
    assert {e["origin"] for e in entries} == {BASE}
    outside = [e["path"] for e in entries if not e["path"].startswith("/static/")]
    assert not outside, f"entries outside /static/: {outside}"
    # The manifest and the worker file always come from the network.
    assert not [e["path"] for e in entries if e["path"].split("?")[0] in ("/static/manifest.json", "/static/sw.js")]


def test_a_recording_page_loaded_offline_is_the_offline_page(context, signed_in):
    page = signed_in
    controlled(page)
    rec_id, title = recording_id(page)
    url = f"{BASE}/recordings/{rec_id}"
    page.goto(url)
    page.wait_for_load_state("load")
    page.get_by_text(title).first.wait_for(state="visible", timeout=15000)
    online_text = page.inner_text("body")
    assert title in online_text

    context.set_offline(True)
    try:
        page.goto(url)
        page.wait_for_load_state("load")
        assert page.evaluate(NAVIGATION_STATUS) == 503
        assert page.locator("#speakr-offline h1").inner_text() == OFFLINE_TITLE
        body = page.inner_text("body")
        assert title not in body, "the recording is shown without a network"
        assert len(page.content()) < 4000
        assert page.url == url
        # Neither the recording nor any other page is in a cache to be shown.
        assert not [e for e in page.evaluate(CACHED) if not e["path"].startswith("/static/")]
        # The same for the other pages.
        for path in ("/", "/account", "/admin"):
            page.goto(f"{BASE}{path}")
            page.wait_for_load_state("load")
            assert page.evaluate(NAVIGATION_STATUS) == 503, path
            assert page.locator("#speakr-offline h1").inner_text() == OFFLINE_TITLE, path
        page.goto(url)
    finally:
        context.set_offline(False)

    # With the network back, the offline page loads the page it stands for
    # (it asks the server every five seconds, and when the browser reports a connection).
    page.get_by_text(title).first.wait_for(state="visible", timeout=20000)
    assert page.locator("#speakr-offline").count() == 0
    assert page.url == url


def test_static_files_are_served_offline_and_api_requests_are_not(context, signed_in):
    page = signed_in
    controlled(page)
    # Loaded once under the worker, so the files of the page are stored.
    page.reload()
    page.wait_for_load_state("load")
    page.wait_for_timeout(2000)
    version = app_version(page)
    scripts = page.evaluate("""() => [...document.querySelectorAll('script[src], link[rel=stylesheet][href]')]
        .map((el) => new URL(el.src || el.href).pathname + new URL(el.src || el.href).search)
        .filter((path) => path.startsWith('/static/'))""")
    versioned = [s for s in scripts if s.endswith(f"?v={version}")]
    assert len(versioned) >= 5, scripts

    context.set_offline(True)
    try:
        result = page.evaluate("""async (paths) => {
            const get = async (path) => {
                try {
                    const response = await fetch(path);
                    return [response.status, (await response.arrayBuffer()).byteLength];
                } catch (error) {
                    return String(error);
                }
            };
            const out = { files: {} };
            for (const path of paths) out.files[path] = await get(path);
            out.api = await get('/api/recordings?per_page=5');
            out.config = await get('/api/config');
            out.unseen = await get('/static/js/never-loaded.js?v=' + paths[0].split('?v=')[1]);
            out.otherRelease = await get(paths[0].split('?v=')[0] + '?v=0.0.0');
            return out;
        }""", versioned)
    finally:
        context.set_offline(False)
    for path, answer in result["files"].items():
        assert isinstance(answer, list) and answer[0] == 200 and answer[1] > 0, f"{path}: {answer}"
    for key in ("api", "config", "unseen", "otherRelease"):
        assert isinstance(result[key], str) and "Failed to fetch" in result[key], f"{key}: {result[key]}"


SEED_LOCAL_COPIES = """() => new Promise((resolve, reject) => {
    // The stores as the app creates them (modules/db/failed-uploads.js, recording-persistence.js)
    const uploads = indexedDB.open('SpeakrFailedUploads', 1);
    uploads.onupgradeneeded = () => {
        const store = uploads.result.createObjectStore('failedUploads', { keyPath: 'id', autoIncrement: true });
        store.createIndex('timestamp', 'timestamp', { unique: false });
        store.createIndex('clientId', 'clientId', { unique: false });
    };
    uploads.onerror = () => reject(uploads.error);
    uploads.onsuccess = () => {
        const tx = uploads.result.transaction(['failedUploads'], 'readwrite');
        tx.objectStore('failedUploads').add({ fileName: 'failed.wav', fileData: new ArrayBuffer(16), timestamp: Date.now(), clientId: 'c1' });
        tx.oncomplete = () => {
            uploads.result.close();
            const recordings = indexedDB.open('SpeakrRecordings', 1);
            recordings.onupgradeneeded = () => {
                recordings.result.createObjectStore('activeRecording', { keyPath: 'id' })
                    .createIndex('timestamp', 'timestamp', { unique: false });
            };
            recordings.onerror = () => reject(recordings.error);
            recordings.onsuccess = () => {
                const tx2 = recordings.result.transaction(['activeRecording'], 'readwrite');
                tx2.objectStore('activeRecording').put({ id: 'current', chunks: [new Blob(['abc'])], notes: 'private notes', timestamp: Date.now() });
                tx2.oncomplete = () => { recordings.result.close(); resolve(true); };
            };
        };
    };
})"""

LOCAL_STATE = """async () => {
    const count = (name, store) => new Promise((resolve) => {
        const request = indexedDB.open(name);
        request.onupgradeneeded = () => request.transaction.abort();
        request.onerror = (event) => { event.preventDefault(); resolve(null); };
        request.onsuccess = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(store)) { db.close(); resolve(null); return; }
            const counting = db.transaction([store], 'readonly').objectStore(store).count();
            counting.onsuccess = () => { db.close(); resolve(counting.result); };
        };
    });
    return {
        uploads: await count('SpeakrFailedUploads', 'failedUploads'),
        recordings: await count('SpeakrRecordings', 'activeRecording'),
        session: Object.keys(sessionStorage),
        sliced: localStorage.getItem('speakr.slicedUploads'),
        scheme: localStorage.getItem('e2eKeptPreference'),
        mark: /(?:^|; )speakr_signed_out=/.test(document.cookie),
    };
}"""


def test_sign_out_empties_cache_storage(context, signed_in):
    page = signed_in
    controlled(page)
    page.goto(f"{BASE}/account")
    page.wait_for_load_state("load")
    page.wait_for_timeout(1500)
    version = app_version_of_worker(page)
    # Entries that only this account's session put there, and a session value.
    page.evaluate("""async (name) => {
        const cache = await caches.open(name);
        await cache.put('/static/sentinel-of-the-signed-in-user.js?v=1', new Response('x'));
        await (await caches.open('another-cache')).put('/static/other', new Response('y'));
        sessionStorage.setItem('speakr_incognito_recording', JSON.stringify({
            title: 'private', original_filename: 'Jane Doe intake.mp3', file_size: 123 }));
        sessionStorage.setItem('speakr_chat_threads:1', '[]');
        // An upload in slices that ended before the sign-out (past its 24 hours:
        // no question is asked for it), and a preference of the interface.
        localStorage.setItem('speakr.slicedUploads', JSON.stringify({
            'Jane Doe intake.mp4|900000000|1700000000000': { sessionId: 'gone', sliceBytes: 1, savedAt: 1 } }));
        localStorage.setItem('e2eKeptPreference', 'kept');
    }""", f"speakr-static-{version}")
    assert page.evaluate("() => window.SpeakrServiceWorker.localCopies()") == 0
    before = page.evaluate(CACHED)
    assert len(before) > 10 and any("sentinel" in e["path"] for e in before)

    sign_out(page)
    page.wait_for_url("**/login*")
    page.wait_for_load_state("load")
    assert page.evaluate("() => window.SpeakrServiceWorker.ready.then(() => true)")

    after = page.evaluate(CACHED)
    assert not [e for e in after if "sentinel" in e["path"]], after
    assert "another-cache" not in {e["cache"] for e in after}
    assert page.evaluate("() => caches.keys().then((names) => names.includes('another-cache'))") is False
    # What is there now was requested by the sign-in page itself.
    assert not [e["path"] for e in after if not e["path"].startswith("/static/")]
    state = page.evaluate(LOCAL_STATE)
    assert state["session"] == [] and state["mark"] is False, state
    # The file name of the upload in slices is gone; other localStorage entries stay.
    assert state["sliced"] is None and state["scheme"] == "kept", state
    page.evaluate("() => localStorage.removeItem('e2eKeptPreference')")
    # Empty where the app had created them, and none created by the clearing.
    assert state["uploads"] in (None, 0) and state["recordings"] in (None, 0), state

    # The clearing itself leaves Cache Storage empty.
    names = page.evaluate("() => window.SpeakrServiceWorker.clearAfterSignOut().then(() => caches.keys())")
    assert names == []

    # The session has ended (asked of the API: the sign-in page is rate limited).
    assert page.request.get(f"{BASE}/api/recordings?per_page=1", max_redirects=0).status in (302, 401)


def app_version_of_worker(page):
    return page.evaluate("() => new URL(navigator.serviceWorker.controller.scriptURL).searchParams.get('v')")


def test_sign_out_asks_before_copies_in_the_browser_are_deleted(context, signed_in):
    page = signed_in
    controlled(page)
    page.goto(f"{BASE}/account")
    page.wait_for_load_state("load")
    page.wait_for_timeout(1000)
    native = []
    page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    assert page.evaluate(SEED_LOCAL_COPIES)
    assert page.evaluate("() => window.SpeakrServiceWorker.localCopies()") == 2
    # An upload in slices that did not finish is the third copy.
    page.evaluate("""() => localStorage.setItem('speakr.slicedUploads', JSON.stringify({
        'Jane Doe intake.mp4|900000000|1700000000000': { sessionId: 'open', sliceBytes: 1, savedAt: Date.now() } }))""")
    assert page.evaluate("() => window.SpeakrServiceWorker.localCopies()") == 3
    stored = page.evaluate("() => { sessionStorage.setItem('probe', '1'); return sessionStorage.length; }")

    # Cancel: still signed in, nothing deleted.
    sign_out(page)
    dialog = page.locator(DIALOG)
    dialog.wait_for(state="visible", timeout=5000)
    text = dialog.inner_text()
    assert en("signOut.localCopiesTitle") in text
    assert en("signOut.localCopiesMessage").replace("{{count}}", "3") in text
    assert page.locator(f"{DIALOG} [data-app-dialog-confirm]").inner_text().strip() == en("signOut.deleteAndSignOut")
    page.locator(f"{DIALOG} [data-app-dialog-cancel]").click()
    dialog.wait_for(state="hidden", timeout=5000)
    page.wait_for_timeout(500)
    assert "/account" in page.url
    state = page.evaluate(LOCAL_STATE)
    assert (state["uploads"], state["recordings"], state["mark"]) == (1, 1, False), state
    assert state["sliced"] and "Jane Doe intake.mp4" in state["sliced"], state
    assert "probe" in state["session"] and len(state["session"]) == stored, state
    assert page.request.get(f"{BASE}/api/recordings?per_page=1").ok

    # Confirm: signed out, and the copies are gone on the page that follows.
    page.keyboard.press("Escape")
    page.mouse.click(5, 400)
    sign_out(page)
    page.locator(DIALOG).wait_for(state="visible", timeout=5000)
    page.locator(f"{DIALOG} [data-app-dialog-confirm]").click()
    page.wait_for_url("**/login*")
    page.wait_for_load_state("load")
    assert page.evaluate("() => window.SpeakrServiceWorker.ready.then(() => true)")
    state = page.evaluate(LOCAL_STATE)
    assert state == {"uploads": 0, "recordings": 0, "session": [], "sliced": None, "scheme": None,
                     "mark": False}, state
    assert not native, f"a dialog of the browser appeared: {native}"


# A worker as the releases before this one installed it: registered at
# /static/sw.js, a cache named Speakr-cache-<version> with the page and an
# API response in it.
OLD_WORKER = """
self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open('Speakr-cache-0.0.1').then((cache) => Promise.all([
    cache.put('/', new Response('<h1>recordings of the earlier user</h1>', { headers: { 'Content-Type': 'text/html' } })),
    cache.put('/api/recordings', new Response('{"recordings":["earlier"]}', { headers: { 'Content-Type': 'application/json' } })),
  ])));
});
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""


def test_an_earlier_registration_at_static_ends_as_one_worker_at_the_root(context, session_cookies):
    served = []

    def old_worker(route):
        served.append(route.request.url)
        route.fulfill(status=200, content_type="text/javascript", body=OLD_WORKER)

    # The earlier release's worker file, for this browser only.
    context.route("**/static/sw.js*", old_worker)
    page = context.new_page()
    page.goto(f"{BASE}{PLAIN_PAGE}")
    page.wait_for_load_state("load")
    old = page.evaluate("""async () => {
        const registration = await navigator.serviceWorker.register('/static/sw.js?v=0.0.1');
        const worker = registration.installing || registration.waiting || registration.active;
        await new Promise((resolve) => {
            if (worker.state === 'activated') resolve();
            worker.addEventListener('statechange', () => { if (worker.state === 'activated') resolve(); });
        });
        return { scope: registration.scope, script: registration.active.scriptURL, caches: await caches.keys() };
    }""")
    assert old == {"scope": f"{BASE}/static/", "script": f"{BASE}/static/sw.js?v=0.0.1", "caches": ["Speakr-cache-0.0.1"]}
    if not served:
        pytest.skip("this Playwright does not route service worker scripts; the earlier worker cannot be installed")
    assert page.evaluate(CONTROLLED) is False  # as in every earlier release: no page was controlled
    context.unroute("**/static/sw.js*")

    # The upgrade: the person opens the new release, signed in.
    context.add_cookies(session_cookies)
    page.goto(f"{BASE}/")
    page.wait_for_load_state("load")
    assert "/login" not in page.url
    controlled(page)
    version = app_version(page)
    page.wait_for_function("async () => (await navigator.serviceWorker.getRegistrations()).length === 1", timeout=10000)
    assert page.evaluate(REGISTRATIONS) == [{"scope": f"{BASE}/", "script": f"{BASE}/sw.js?v={version}"}]
    names = page.evaluate("() => caches.keys()")
    assert "Speakr-cache-0.0.1" not in names and all(n == f"speakr-static-{version}" for n in names), names
    assert not [e for e in page.evaluate(CACHED) if not e["path"].startswith("/static/")]

    # Again after another load: still one, and the earlier page is not shown offline.
    page.reload()
    page.wait_for_load_state("load")
    page.wait_for_timeout(1500)
    assert page.evaluate(REGISTRATIONS) == [{"scope": f"{BASE}/", "script": f"{BASE}/sw.js?v={version}"}]
    context.set_offline(True)
    try:
        page.goto(f"{BASE}/")
        page.wait_for_load_state("load")
        assert page.evaluate(NAVIGATION_STATUS) == 503
        assert page.locator("#speakr-offline h1").inner_text() == OFFLINE_TITLE
        assert "earlier user" not in page.content()
    finally:
        context.set_offline(False)


def test_the_new_file_at_the_old_url_removes_its_own_registration(context):
    """Without the page's help: the browser's update check gives a registration
    at /static/ the new worker file, which then unregisters itself."""
    page = context.new_page()
    page.goto(f"{BASE}{PLAIN_PAGE}")
    page.wait_for_load_state("load")
    page.evaluate("() => navigator.serviceWorker.register('/static/sw.js?v=0.0.1').then(() => true)")
    page.wait_for_function("async () => (await navigator.serviceWorker.getRegistrations()).length === 0", timeout=15000)
    assert page.evaluate("() => caches.keys()") == []


COUNT_PERMISSION_CALLS = """() => {
    window.__asked = [];
    if (window.Notification) {
        const ask = Notification.requestPermission;
        Notification.requestPermission = function () { window.__asked.push('notification'); return ask.apply(this, arguments); };
    }
    if (window.PushManager) {
        const subscribe = PushManager.prototype.subscribe;
        PushManager.prototype.subscribe = function () { window.__asked.push('push'); return subscribe.apply(this, arguments); };
    }
    if (window.ServiceWorkerRegistration) {
        const show = ServiceWorkerRegistration.prototype.showNotification;
        ServiceWorkerRegistration.prototype.showNotification = function () { window.__asked.push('show'); return show.apply(this, arguments); };
    }
}"""


def test_no_notification_permission_is_asked_and_no_push_subscription_is_made(context, session_cookies):
    """With the worker in control, navigator.serviceWorker.ready resolves; the
    code that waited on it for push and notifications is gone, so nothing
    asks for a permission."""
    context.add_cookies(session_cookies)
    context.add_init_script(f"({COUNT_PERMISSION_CALLS})()")
    page = context.new_page()
    rec_id = None
    for path in ("/", "/account", "/"):
        page.goto(f"{BASE}{path}")
        page.wait_for_load_state("load")
        controlled(page)
        page.wait_for_timeout(2000)
        assert page.evaluate("() => window.__asked") == [], path
        if rec_id is None:
            rec_id, _ = recording_id(page)
    page.goto(f"{BASE}/recordings/{rec_id}")
    page.wait_for_load_state("load")
    page.wait_for_timeout(2000)
    state = page.evaluate("""async () => {
        const registration = await navigator.serviceWorker.ready;
        return {
            asked: window.__asked,
            subscription: await registration.pushManager.getSubscription(),
            notifications: (await registration.getNotifications()).length,
        };
    }""")
    assert state == {"asked": [], "subscription": None, "notifications": 0}, state
