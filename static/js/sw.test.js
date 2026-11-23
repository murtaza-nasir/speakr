import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// static/sw.js is a classic worker script. It is run here in a fresh context
// per test, with a stand-in for the worker scope, Cache Storage and the
// network, so that both the routing functions and the handlers are tested.
const SOURCE = readFileSync(fileURLToPath(new URL('../sw.js', import.meta.url)), 'utf8');
const ORIGIN = 'https://speakr.test';

function fakeResponse({ status = 200, type = 'basic', headers = {}, body = '' } = {}) {
    const response = {
        status,
        type,
        body,
        headers: new Headers(headers),
        clone: () => response,
        text: async () => body,
    };
    return response;
}

function fakeRequest(path, { method = 'GET', mode = 'no-cors', destination = '', range = false, cache = 'default' } = {}) {
    const url = path.startsWith('http') ? path : `${ORIGIN}${path}`;
    return { url, method, mode, destination, cache, range, headers: { has: (name) => range && name.toLowerCase() === 'range' } };
}

function loadWorker({ version = '1.2.3', scope = `${ORIGIN}/`, network = () => fakeResponse(), existing = {}, bodies = {} } = {}) {
    const listeners = {};
    const stores = new Map(Object.entries(existing).map(([name, urls]) => [name, new Map(urls.map((u) => [u, fakeResponse({ body: bodies[u] || 'cached' })]))]));
    const cacheOf = (name) => {
        if (!stores.has(name)) stores.set(name, new Map());
        const entries = stores.get(name);
        const keyOf = (request) => (typeof request === 'string' ? request : request.url);
        return {
            match: async (request) => entries.get(keyOf(request)),
            put: async (request, response) => { entries.set(keyOf(request), response); },
            keys: async () => [...entries.keys()].map((url) => ({ url })),
        };
    };
    const caches = {
        open: vi.fn(async (name) => cacheOf(name)),
        keys: vi.fn(async () => [...stores.keys()]),
        delete: vi.fn(async (name) => stores.delete(name)),
        match: vi.fn(async () => { throw new Error('caches.match is not used: lookups name the cache'); }),
    };
    const online = { value: true };
    const fetch = vi.fn(async (request) => {
        if (!online.value) throw new TypeError('Failed to fetch');
        return network(typeof request === 'string' ? { url: request, method: 'GET' } : request);
    });
    const self = {
        location: new URL(`${ORIGIN}/sw.js${version ? `?v=${version}` : ''}`),
        registration: { scope, unregister: vi.fn(async () => true) },
        clients: { claim: vi.fn(async () => {}) },
        skipWaiting: vi.fn(),
        addEventListener: (type, handler) => { listeners[type] = handler; },
    };
    const context = vm.createContext({ self, caches, fetch, URL, URLSearchParams, Response, Promise, console });
    vm.runInContext(`${SOURCE}\n;self.__api = { routeRequest, mayStore, isPrivatePath, CACHE_NAME, OFFLINE_HTML, PRIVATE_PREFIXES, RECORDER_FILES, RECORDER_CSP, recorderFiles };`, context);

    /** Dispatch a fetch event; resolves with what the worker answered, or undefined when it did not. */
    const dispatchFetch = async (request) => {
        let answer;
        const waits = [];
        listeners.fetch({ request, respondWith: (p) => { answer = Promise.resolve(p); }, waitUntil: (p) => waits.push(p) });
        const handled = answer !== undefined;
        let response;
        let error;
        if (handled) {
            try { response = await answer; } catch (e) { error = e; }
        }
        await Promise.all(waits);
        return { handled, response, error };
    };
    const dispatch = async (type, event = {}) => {
        const waits = [];
        listeners[type]({ ...event, waitUntil: (p) => waits.push(p) });
        await Promise.all(waits);
    };
    const cached = () => Object.fromEntries([...stores.entries()].map(([name, entries]) => [name, [...entries.keys()]]));
    return { api: self.__api, self, listeners, caches, fetch, online, dispatchFetch, dispatch, cached, stores };
}

const { api } = loadWorker();
const route = (path, options = {}, version = '1.2.3') => api.routeRequest(fakeRequest(path, options), ORIGIN, version);

describe('routeRequest', () => {
    it('leaves every request that is not a GET to the browser', () => {
        for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']) {
            expect(route('/static/js/app.modular.js?v=1.2.3', { method })).toBe('bypass');
            expect(route('/api/recordings', { method })).toBe('bypass');
            expect(route('/', { method, mode: 'navigate' })).toBe('bypass');
        }
    });

    it('leaves cross-origin requests to the browser', () => {
        expect(route('https://cdn.example/static/js/x.js?v=1.2.3')).toBe('bypass');
        expect(route('https://speakr.test:8443/static/js/x.js?v=1.2.3')).toBe('bypass');
        expect(route('http://speakr.test/', { mode: 'navigate' })).toBe('bypass');
    });

    it('leaves user data, sign-in and uploads to the browser', () => {
        const paths = [
            '/api/recordings', '/api/recordings/12', '/api/recordings/12/transcript', '/api/config',
            '/api/csrf-token', '/api/v1/recordings', '/api/users/search?q=a', '/api/inbox',
            '/audio/12', '/audio/12/peaks', '/recording/12/summary', '/recordings/12/download',
            '/share/abc123', '/share/audio/abc123', '/admin', '/admin/users', '/upload', '/upload/part',
            '/speakers', '/speakers/snippet-audio/1', '/login', '/logout', '/register', '/auth/sso/callback',
            '/account', '/inquire/search', '/chat', '/status/12', '/tags', '/folders', '/tokens',
        ];
        for (const path of paths) {
            expect(route(path, { mode: 'cors' }), path).toBe('bypass');
            expect(route(path, { mode: 'same-origin' }), path).toBe('bypass');
            expect(api.isPrivatePath(new URL(path, ORIGIN).pathname), path).toBe(true);
        }
    });

    it('leaves the incognito requests to the browser', () => {
        // They are POST requests; a GET to the same paths is not handled either.
        for (const path of ['/api/recordings/incognito', '/api/recordings/incognito/chat', '/api/recordings/incognito/summary']) {
            expect(route(path, { method: 'POST', mode: 'cors' })).toBe('bypass');
            expect(route(path, { mode: 'cors' })).toBe('bypass');
            expect(api.mayStore('static-cache-first', `${ORIGIN}${path}`, { status: 200, type: 'basic' })).toBe(false);
        }
    });

    it('sends a page to the network, with the offline page as the fallback', () => {
        for (const path of ['/', '/recordings/12', '/account', '/admin', '/inquire', '/group-management', '/login', '/share/abc123', '/docs']) {
            expect(route(path, { mode: 'navigate', destination: 'document' }), path).toBe('navigate');
        }
    });

    it('leaves a navigation to a file or a stream to the browser', () => {
        for (const path of ['/api/recordings/12/download', '/audio/12', '/recording/12/download/transcript',
            '/share/audio/abc123', '/speakers/snippet-audio/1', '/upload']) {
            expect(route(path, { mode: 'navigate', destination: 'document' }), path).toBe('bypass');
        }
    });

    it('answers a static file of its own release from the cache first', () => {
        expect(route('/static/js/app.modular.js?v=1.2.3', { destination: 'script' })).toBe('static-cache-first');
        expect(route('/static/css/styles.css?v=1.2.3', { destination: 'style' })).toBe('static-cache-first');
        expect(route('/static/locales/en.json?v=1.2.3', { mode: 'cors' })).toBe('static-cache-first');
    });

    it('asks the network first for a static file without a version', () => {
        expect(route('/static/img/icon-64.png', { destination: 'image' })).toBe('static-network-first');
        expect(route('/static/locales/de.json', { mode: 'cors' })).toBe('static-network-first');
    });

    it('never answers another release\'s static URL from the cache, nor stores it', () => {
        expect(route('/static/js/app.modular.js?v=1.2.4')).toBe('static-network-only');
        expect(route('/static/js/app.modular.js?v=', {}, '1.2.3')).toBe('static-network-only');
        // A worker that does not know its release trusts no version.
        expect(route('/static/js/app.modular.js?v=1.2.3', {}, '')).toBe('static-network-only');
        expect(api.mayStore('static-network-only', `${ORIGIN}/static/js/app.modular.js?v=1.2.4`, { status: 200, type: 'basic' })).toBe(false);
    });

    it('leaves the manifest, the worker file, media and Range requests to the browser', () => {
        expect(route('/static/manifest.json')).toBe('bypass');
        expect(route('/static/sw.js?v=1.2.3')).toBe('bypass');
        expect(route('/static/img/demo.mp4?v=1.2.3', { destination: 'video' })).toBe('bypass');
        expect(route('/static/audio/x.mp3?v=1.2.3', { destination: 'audio' })).toBe('bypass');
        expect(route('/static/js/app.modular.js?v=1.2.3', { range: true })).toBe('bypass');
    });

    it('leaves everything else to the browser', () => {
        expect(route('/robots.txt')).toBe('bypass');
        expect(route('/openapi.json', { mode: 'cors' })).toBe('bypass');
        expect(api.routeRequest(null, ORIGIN, '1.2.3')).toBe('bypass');
        expect(api.routeRequest({ method: 'GET', url: 'not a url' }, ORIGIN, '1.2.3')).toBe('bypass');
    });
});

describe('mayStore', () => {
    const url = `${ORIGIN}/static/js/app.modular.js?v=1.2.3`;
    const ok = { status: 200, type: 'basic', cacheControl: 'no-cache, max-age=0', vary: 'Cookie' };

    it('stores a plain static response', () => {
        expect(api.mayStore('static-cache-first', url, ok)).toBe(true);
        expect(api.mayStore('static-network-first', `${ORIGIN}/static/img/icon-64.png`, ok)).toBe(true);
    });

    it('stores nothing for the other routes', () => {
        for (const r of ['bypass', 'navigate', 'static-network-only', undefined]) {
            expect(api.mayStore(r, url, ok)).toBe(false);
        }
    });

    it('stores nothing outside /static/, whatever the route says', () => {
        for (const path of ['/', '/api/recordings', '/share/abc', '/admin', '/account', '/audio/1']) {
            expect(api.mayStore('static-cache-first', `${ORIGIN}${path}`, ok), path).toBe(false);
        }
        expect(api.mayStore('static-cache-first', 'not a url', ok)).toBe(false);
    });

    it('refuses no-store, private, Set-Cookie, Vary: *, errors, redirects and opaque responses', () => {
        expect(api.mayStore('static-cache-first', url, { ...ok, cacheControl: 'no-store' })).toBe(false);
        expect(api.mayStore('static-cache-first', url, { ...ok, cacheControl: 'max-age=60, No-Store' })).toBe(false);
        expect(api.mayStore('static-cache-first', url, { ...ok, cacheControl: 'private, max-age=60' })).toBe(false);
        expect(api.mayStore('static-cache-first', url, { ...ok, setCookie: true })).toBe(false);
        expect(api.mayStore('static-cache-first', url, { ...ok, vary: '*' })).toBe(false);
        for (const status of [0, 206, 301, 302, 304, 401, 403, 404, 500]) {
            expect(api.mayStore('static-cache-first', url, { ...ok, status }), String(status)).toBe(false);
        }
        expect(api.mayStore('static-cache-first', url, { ...ok, type: 'opaque' })).toBe(false);
        expect(api.mayStore('static-cache-first', url, { ...ok, type: 'opaqueredirect' })).toBe(false);
        expect(api.mayStore('static-cache-first', url, null)).toBe(false);
    });
});

describe('the fetch handler', () => {
    it('does not answer an API request, read a cache for it, or store its response', async () => {
        const worker = loadWorker({ existing: { 'speakr-static-1.2.3': [`${ORIGIN}/api/recordings`] } });
        for (const path of ['/api/recordings', '/api/recordings/3', '/audio/3', '/share/abc', '/admin/users']) {
            const { handled } = await worker.dispatchFetch(fakeRequest(path, { mode: 'cors' }));
            expect(handled, path).toBe(false);
        }
        expect(worker.fetch).not.toHaveBeenCalled();
        expect(worker.caches.open).not.toHaveBeenCalled();
    });

    it('does not answer a POST, the incognito requests among them', async () => {
        const worker = loadWorker();
        for (const path of ['/api/recordings/incognito', '/api/recordings/incognito/chat', '/upload', '/static/js/x.js?v=1.2.3']) {
            const { handled } = await worker.dispatchFetch(fakeRequest(path, { method: 'POST', mode: 'cors' }));
            expect(handled, path).toBe(false);
        }
        expect(worker.fetch).not.toHaveBeenCalled();
        expect(worker.cached()).toEqual({});
    });

    it('fetches a static file of its release once and answers from the cache afterwards', async () => {
        const worker = loadWorker({ network: () => fakeResponse({ body: 'fresh' }) });
        const request = fakeRequest('/static/js/app.modular.js?v=1.2.3', { destination: 'script' });
        const first = await worker.dispatchFetch(request);
        expect(first.response.body).toBe('fresh');
        expect(worker.cached()).toEqual({ 'speakr-static-1.2.3': [request.url] });
        worker.online.value = false;
        const second = await worker.dispatchFetch(request);
        expect(second.response.body).toBe('fresh');
        expect(worker.fetch).toHaveBeenCalledTimes(1); // the second request never reached the network
    });

    it('answers from the cache without the network when the file is there', async () => {
        const url = `${ORIGIN}/static/css/styles.css?v=1.2.3`;
        const worker = loadWorker({ existing: { 'speakr-static-1.2.3': [url] } });
        const { response } = await worker.dispatchFetch(fakeRequest('/static/css/styles.css?v=1.2.3'));
        expect(response.body).toBe('cached');
        expect(worker.fetch).not.toHaveBeenCalled();
    });

    it('does not store a static response that says no-store, or an error', async () => {
        const answers = [fakeResponse({ headers: { 'Cache-Control': 'no-store' } }), fakeResponse({ status: 404 })];
        const worker = loadWorker({ network: () => answers.shift() });
        await worker.dispatchFetch(fakeRequest('/static/js/a.js?v=1.2.3'));
        await worker.dispatchFetch(fakeRequest('/static/js/b.js?v=1.2.3'));
        expect(worker.cached()['speakr-static-1.2.3'] || []).toEqual([]);
    });

    it('asks the network first for an unversioned static file and uses the copy only offline', async () => {
        let n = 0;
        const worker = loadWorker({ network: () => fakeResponse({ body: `image ${++n}` }) });
        const request = fakeRequest('/static/img/icon-64.png', { destination: 'image' });
        expect((await worker.dispatchFetch(request)).response.body).toBe('image 1');
        expect((await worker.dispatchFetch(request)).response.body).toBe('image 2');
        worker.online.value = false;
        expect((await worker.dispatchFetch(request)).response.body).toBe('image 2');
        const missing = await worker.dispatchFetch(fakeRequest('/static/img/other.png', { destination: 'image' }));
        expect(missing.handled).toBe(true);
        expect(missing.error).toBeInstanceOf(TypeError); // a network error, as without a worker
    });

    it('leaves a static file of another release to the browser', async () => {
        const worker = loadWorker({ existing: { 'speakr-static-1.2.3': [`${ORIGIN}/static/js/app.modular.js?v=1.2.3`] } });
        const { handled } = await worker.dispatchFetch(fakeRequest('/static/js/app.modular.js?v=1.2.4'));
        expect(handled).toBe(false);
        expect(worker.fetch).not.toHaveBeenCalled();
    });

    it('passes a page through from the network and stores nothing', async () => {
        const page = fakeResponse({ body: '<html>recordings of Ada</html>' });
        const worker = loadWorker({ network: () => page });
        const { response } = await worker.dispatchFetch(fakeRequest('/', { mode: 'navigate', destination: 'document' }));
        expect(response).toBe(page);
        expect(worker.cached()).toEqual({});
        expect(worker.caches.open).not.toHaveBeenCalled();
    });

    it('answers the offline page when a page cannot be fetched, never a stored page', async () => {
        // A page left in a cache by any earlier code must not come back.
        const worker = loadWorker({ existing: { 'speakr-static-1.2.3': [`${ORIGIN}/`, `${ORIGIN}/account`] } });
        worker.online.value = false;
        for (const path of ['/', '/account', '/share/abc123']) {
            const { response } = await worker.dispatchFetch(fakeRequest(path, { mode: 'navigate', destination: 'document' }));
            expect(response.status).toBe(503);
            expect(response.headers.get('Content-Type')).toBe('text/html; charset=utf-8');
            expect(response.headers.get('Cache-Control')).toBe('no-store');
            const html = await response.text();
            expect(html).toContain('You are offline');
            expect(html).not.toContain('cached');
        }
        // The cache is read only for the offline recorder's page, which is not there.
        expect(worker.caches.open.mock.calls.every(([name]) => name === 'speakr-static-1.2.3')).toBe(true);
    });

    const RECORDER = `${ORIGIN}/static/offline-recorder.html?v=1.2.3`;
    const RECORDER_JS = `${ORIGIN}/static/js/offline-recorder.js?v=1.2.3`;
    const RECORDER_BODY = '<html><link href="/static/css/styles.css?v=__SPEAKR_VERSION__"><h1>Recorder</h1>'
        + '<script type="module" src="/static/js/offline-recorder.js?v=__SPEAKR_VERSION__"></script></html>';
    const withRecorder = (options = {}) => loadWorker({
        existing: { 'speakr-static-1.2.3': [RECORDER, RECORDER_JS, `${ORIGIN}/`] },
        bodies: { [RECORDER]: RECORDER_BODY },
        ...options,
    });

    it('answers the offline recorder of its release when a page cannot be fetched (#650)', async () => {
        const worker = withRecorder();
        worker.online.value = false;
        for (const path of ['/', '/recordings/12', '/account', '/login']) {
            const { response } = await worker.dispatchFetch(fakeRequest(path, { mode: 'navigate', destination: 'document' }));
            expect(response.status, path).toBe(503);
            expect(response.headers.get('Content-Type')).toBe('text/html; charset=utf-8');
            expect(response.headers.get('Cache-Control')).toBe('no-store');
            expect(response.headers.get('Content-Security-Policy')).toBe(api.RECORDER_CSP);
            const html = await response.text();
            expect(html).toContain('<h1>Recorder</h1>');
            expect(html).toContain('/static/js/offline-recorder.js?v=1.2.3');
            expect(html).not.toContain('__SPEAKR_VERSION__');
            expect(html).not.toContain('cached');
        }
        // The answer is built each time; nothing is added to the cache.
        expect(worker.cached()).toEqual({ 'speakr-static-1.2.3': [RECORDER, RECORDER_JS, `${ORIGIN}/`] });
    });

    it('allows the recorder its own files only: no inline script, no other origin', () => {
        expect(api.RECORDER_CSP).toContain("script-src 'self';");
        expect(api.RECORDER_CSP).not.toMatch(/unsafe-inline|unsafe-eval|https?:/);
        expect(api.RECORDER_CSP).toContain("media-src 'self' blob:");
        expect(api.RECORDER_CSP).toContain("connect-src 'self'");
    });

    it('answers the plain offline page when the recorder\'s script or page is not in the cache', async () => {
        for (const existing of [[RECORDER], [RECORDER_JS], [`${ORIGIN}/static/offline-recorder.html?v=1.2.2`, RECORDER_JS]]) {
            const worker = loadWorker({ existing: { 'speakr-static-1.2.3': existing }, bodies: { [RECORDER]: RECORDER_BODY } });
            worker.online.value = false;
            const { response } = await worker.dispatchFetch(fakeRequest('/', { mode: 'navigate', destination: 'document' }));
            expect(await response.text()).toBe(api.OFFLINE_HTML);
        }
        // A worker without a release has no recorder.
        const unversioned = loadWorker({ version: '', existing: { 'speakr-static-unversioned': [RECORDER, RECORDER_JS] } });
        unversioned.online.value = false;
        const { response } = await unversioned.dispatchFetch(fakeRequest('/', { mode: 'navigate', destination: 'document' }));
        expect(await response.text()).toBe(api.OFFLINE_HTML);
    });

    it('answers the recorder for a proxy\'s 502 or 504 and passes every other answer through', async () => {
        for (const status of [502, 504]) {
            const worker = withRecorder({ network: () => fakeResponse({ status, body: 'Bad Gateway' }) });
            const { response } = await worker.dispatchFetch(fakeRequest('/', { mode: 'navigate', destination: 'document' }));
            expect(response.status).toBe(503);
            expect(await response.text()).toContain('<h1>Recorder</h1>');
        }
        for (const status of [200, 302, 404, 500, 503]) {
            const page = fakeResponse({ status, body: 'from the server' });
            const worker = withRecorder({ network: () => page });
            const { response } = await worker.dispatchFetch(fakeRequest('/', { mode: 'navigate', destination: 'document' }));
            expect(response, String(status)).toBe(page);
        }
        // Without the recorder, the proxy's page is shown as before.
        const gateway = fakeResponse({ status: 502, body: 'Bad Gateway' });
        const plain = loadWorker({ network: () => gateway });
        const { response } = await plain.dispatchFetch(fakeRequest('/', { mode: 'navigate', destination: 'document' }));
        expect(response).toBe(gateway);
    });

    it('carries an offline page that needs no file and asks the server only whether it is back', () => {
        expect(api.OFFLINE_HTML).toContain('<h1>You are offline</h1>');
        expect(api.OFFLINE_HTML).not.toMatch(/src=|href=|url\(/);
        // One kind of request: a HEAD, which the worker leaves to the browser.
        expect(api.OFFLINE_HTML.match(/fetch\(/g)).toHaveLength(1);
        expect(api.OFFLINE_HTML).toContain("fetch('/static/manifest.json', { method: 'HEAD', cache: 'no-store' })");
        expect(route('/static/manifest.json', { method: 'HEAD' })).toBe('bypass');
    });

    it('reloads the offline page only after the server has answered', async () => {
        const html = api.OFFLINE_HTML;
        const script = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
        const run = async ({ reachable }) => {
            const listeners = {};
            const location = { reload: vi.fn() };
            const fetch = vi.fn(async () => { if (!reachable.value) throw new TypeError('Failed to fetch'); return {}; });
            let tick;
            const context = vm.createContext({
                window: { addEventListener: (type, fn) => { listeners[type] = fn; } },
                document: { visibilityState: 'visible' },
                location,
                fetch,
                setInterval: (fn) => { tick = fn; },
            });
            vm.runInContext(script, context);
            return { listeners, location, fetch, tick: () => tick() };
        };
        const reachable = { value: false };
        const page = await run({ reachable });
        // The browser says "online" while the server is still away: no reload, so no loop.
        page.listeners.online();
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(page.fetch).toHaveBeenCalledTimes(1);
        expect(page.location.reload).not.toHaveBeenCalled();
        // The next look finds the server.
        reachable.value = true;
        page.tick();
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(page.location.reload).toHaveBeenCalledTimes(1);
    });

    it('does not count a proxy\'s 502 or 504 as the server being back', async () => {
        const html = api.OFFLINE_HTML;
        const script = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
        const status = { value: 502 };
        const location = { reload: vi.fn() };
        let tick;
        const context = vm.createContext({
            window: { addEventListener: () => {} },
            document: { visibilityState: 'visible' },
            location,
            fetch: vi.fn(async () => ({ status: status.value })),
            setInterval: (fn) => { tick = fn; },
        });
        vm.runInContext(script, context);
        for (const away of [502, 504]) {
            status.value = away;
            tick();
            await new Promise((resolve) => setTimeout(resolve, 0));
        }
        expect(location.reload).not.toHaveBeenCalled();
        status.value = 200;
        tick();
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(location.reload).toHaveBeenCalledTimes(1);
    });

    it('leaves a request that only the browser cache may answer alone', async () => {
        const worker = loadWorker();
        const { handled } = await worker.dispatchFetch(fakeRequest('/static/js/a.js?v=1.2.3', { cache: 'only-if-cached', mode: 'no-cors' }));
        expect(handled).toBe(false);
    });
});

describe('install, activate and sign-out', () => {
    it('fetches nothing at install and takes over at once', async () => {
        const worker = loadWorker();
        await worker.dispatch('install');
        expect(worker.self.skipWaiting).toHaveBeenCalledTimes(1);
        expect(worker.fetch).not.toHaveBeenCalled();
        expect(worker.cached()).toEqual({});
    });

    it('deletes the caches of other releases and of the workers before it, then claims the pages', async () => {
        const worker = loadWorker({
            existing: {
                'Speakr-cache-1.2.2': [`${ORIGIN}/`, `${ORIGIN}/api/recordings`],
                'speakr-static-1.2.2': [`${ORIGIN}/static/js/app.modular.js?v=1.2.2`],
                'speakr-static-1.2.3': [`${ORIGIN}/static/js/app.modular.js?v=1.2.3`],
            },
        });
        await worker.dispatch('activate');
        expect(worker.cached()).toEqual({ 'speakr-static-1.2.3': [`${ORIGIN}/static/js/app.modular.js?v=1.2.3`] });
        expect(worker.self.clients.claim).toHaveBeenCalledTimes(1);
        expect(worker.self.registration.unregister).not.toHaveBeenCalled();
    });

    it('names its cache after its release', () => {
        expect(loadWorker({ version: '0.41.0' }).api.CACHE_NAME).toBe('speakr-static-0.41.0');
        expect(loadWorker({ version: '' }).api.CACHE_NAME).toBe('speakr-static-unversioned');
    });

    it('deletes every cache on the SIGN_OUT message and answers', async () => {
        const worker = loadWorker({ existing: { 'speakr-static-1.2.3': [`${ORIGIN}/static/a.js?v=1.2.3`], other: ['x'] } });
        const port = { postMessage: vi.fn() };
        await worker.dispatch('message', { data: { type: 'SIGN_OUT' }, ports: [port] });
        expect(worker.cached()).toEqual({});
        expect(port.postMessage).toHaveBeenCalledWith({ type: 'SIGNED_OUT' });
    });

    it('stores the recorder\'s files of its release when the app asks, with the locale files of English and the language', async () => {
        const worker = loadWorker({ network: (request) => fakeResponse({ body: request.url }) });
        const port = { postMessage: vi.fn() };
        await worker.dispatch('message', { data: { type: 'PREPARE_OFFLINE_RECORDER', locale: 'de' }, ports: [port] });
        const stored = worker.cached()['speakr-static-1.2.3'];
        const expected = [...api.RECORDER_FILES, '/static/locales/en.json', '/static/locales/de.json']
            .map((path) => `${ORIGIN}${path}?v=1.2.3`);
        expect(stored.sort()).toEqual(expected.sort());
        expect(stored.every((url) => new URL(url).pathname.startsWith('/static/'))).toBe(true);
        expect(port.postMessage).toHaveBeenCalledWith({ type: 'OFFLINE_RECORDER_READY', stored: expected.length });
        // Asked again: everything is there, nothing is fetched.
        worker.fetch.mockClear();
        await worker.dispatch('message', { data: { type: 'PREPARE_OFFLINE_RECORDER', locale: 'de' } });
        expect(worker.fetch).not.toHaveBeenCalled();
    });

    it('stores only English for a language it does not know, and nothing a response forbids', async () => {
        const worker = loadWorker({
            network: (request) => (request.url.includes('/themes/desk.css')
                ? fakeResponse({ headers: { 'Cache-Control': 'no-store' } })
                : request.url.includes('favicon') ? fakeResponse({ status: 404 }) : fakeResponse()),
        });
        await worker.dispatch('message', { data: { type: 'PREPARE_OFFLINE_RECORDER', locale: '../../api/recordings' } });
        const stored = worker.cached()['speakr-static-1.2.3'].map((url) => new URL(url).pathname);
        expect(stored.filter((path) => path.startsWith('/static/locales/'))).toEqual(['/static/locales/en.json']);
        expect(stored).not.toContain('/static/css/themes/desk.css');
        expect(stored).not.toContain('/static/img/favicon.ico');
        expect(stored.length).toBe(api.RECORDER_FILES.length - 1);
    });

    it('stores nothing for the recorder without a release, or offline', async () => {
        const unversioned = loadWorker({ version: '' });
        await unversioned.dispatch('message', { data: { type: 'PREPARE_OFFLINE_RECORDER', locale: 'en' } });
        expect(unversioned.fetch).not.toHaveBeenCalled();
        expect(unversioned.cached()).toEqual({});
        const offline = loadWorker();
        offline.online.value = false;
        await offline.dispatch('message', { data: { type: 'PREPARE_OFFLINE_RECORDER', locale: 'en' } });
        expect(offline.cached()).toEqual({ 'speakr-static-1.2.3': [] });
    });

    it('ignores every other message', async () => {
        const worker = loadWorker({ existing: { 'speakr-static-1.2.3': ['x'] } });
        for (const data of [null, {}, { type: 'RECORDING_STATE', isRecording: true }, { type: 'SKIP_WAITING' }, { type: 'UPDATE_SHORTCUTS' }]) {
            await worker.dispatch('message', { data });
        }
        expect(worker.cached()).toEqual({ 'speakr-static-1.2.3': ['x'] });
        expect(worker.self.skipWaiting).not.toHaveBeenCalled();
    });

    it('deletes every cache when the sign-out request is sent, without answering it', async () => {
        const worker = loadWorker({ existing: { 'speakr-static-1.2.3': ['x'] } });
        const { handled } = await worker.dispatchFetch(fakeRequest('/logout', { method: 'POST', mode: 'navigate' }));
        expect(handled).toBe(false);
        expect(worker.cached()).toEqual({});
        // Another POST deletes nothing.
        const other = loadWorker({ existing: { 'speakr-static-1.2.3': ['x'] } });
        await other.dispatchFetch(fakeRequest('/login', { method: 'POST', mode: 'navigate' }));
        await other.dispatchFetch(fakeRequest('https://elsewhere.test/logout', { method: 'POST', mode: 'navigate' }));
        expect(other.cached()).toEqual({ 'speakr-static-1.2.3': ['x'] });
    });

    it('has no push, notification or background sync handler', () => {
        const worker = loadWorker();
        expect(Object.keys(worker.listeners).sort()).toEqual(['activate', 'fetch', 'install', 'message']);
        expect(SOURCE).not.toMatch(/showNotification|pushManager|addEventListener\('(push|sync|notificationclick)'/);
    });
});

describe('a registration left at /static/ by an earlier release', () => {
    it('removes itself and touches nothing', async () => {
        const existing = { 'speakr-static-1.2.3': [`${ORIGIN}/static/js/app.modular.js?v=1.2.3`] };
        const worker = loadWorker({ version: '1.2.2', scope: `${ORIGIN}/static/`, existing });
        await worker.dispatch('install');
        await worker.dispatch('activate');
        expect(worker.self.registration.unregister).toHaveBeenCalledTimes(1);
        expect(worker.self.clients.claim).not.toHaveBeenCalled();
        expect(worker.cached()).toEqual(existing); // the cache of the worker at / is left alone
        const { handled } = await worker.dispatchFetch(fakeRequest('/static/js/app.modular.js?v=1.2.2'));
        expect(handled).toBe(false);
        await worker.dispatch('message', { data: { type: 'SIGN_OUT' } });
        expect(worker.cached()).toEqual(existing);
    });
});

describe('the offline recorder page (#650)', () => {
    const read = (path) => readFileSync(fileURLToPath(new URL(`..${path.replace(/^\/static/, '')}`, import.meta.url)), 'utf8');
    const { api } = loadWorker();
    const html = read('/static/offline-recorder.html');

    it('is a static file that the worker keeps, as every file it loads', () => {
        for (const path of api.RECORDER_FILES) expect(() => read(path), path).not.toThrow();
        const referenced = [...html.matchAll(/(?:src|href)="([^"]*)"/g)].map((m) => m[1]).filter((ref) => ref !== '');
        expect(referenced.length).toBeGreaterThan(4);
        for (const ref of referenced) {
            expect(ref, ref).toMatch(/^\/static\/[^?]+\?v=__SPEAKR_VERSION__$/);
            expect(api.RECORDER_FILES, ref).toContain(ref.split('?')[0]);
        }
        // The modules the page imports and the theme stylesheets it adds.
        const script = read('/static/js/offline-recorder.js');
        const modules = [...script.matchAll(/mod\('([^']+)'\)/g)].map((m) => `/static/js/${m[1]}`);
        expect(modules.length).toBe(5);
        const theme = read('/static/js/offline-recorder-theme.js');
        const themes = JSON.parse(theme.match(/var THEMES = (\[[^\]]+\])/)[1].replace(/'/g, '"'));
        for (const path of [...modules, ...themes.map((id) => `/static/css/themes/${id}.css`)]) {
            expect(api.RECORDER_FILES, path).toContain(path);
        }
        // A module loaded by the page imports nothing: an import would be asked for without ?v=.
        for (const path of modules) expect(read(path), path).not.toMatch(/^\s*import\s/m);
    });

    it('holds no user data and runs no inline script', () => {
        expect(html).not.toMatch(/\{\{|\{%|csrf/i);
        expect(html).not.toMatch(/<script(?![^>]*\bsrc=)[^>]*>/);
        expect(html).not.toMatch(/\son[a-z]+=/i);
        expect(html).not.toMatch(/\/api\/|\/audio\/|\/recordings/);
        expect(html).toContain('<h1 id="or-title" data-or-text="title">You are offline</h1>');
    });

    it('offers a theme stylesheet for every UI theme but the default', () => {
        const registry = readFileSync(fileURLToPath(new URL('../../src/config/ui_themes.py', import.meta.url)), 'utf8');
        const ids = [...registry.matchAll(/UiTheme\('(\w+)', available=True\)/g)].map((m) => m[1]);
        expect(ids.length).toBeGreaterThan(3);
        for (const id of ids) expect(api.RECORDER_FILES).toContain(`/static/css/themes/${id}.css`);
    });
});
