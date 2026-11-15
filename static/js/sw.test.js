// The service worker (static/sw.js) is a classic script, so it is evaluated
// here against small stand-ins for the worker globals. Covered: the app shell
// is precached under the versioned URLs the templates link, and a request that
// carries another release's ?v= is never answered from this release's cache
// while the network is reachable.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ORIGIN = 'https://speakr.test';
// SW_FILE points the same checks at another copy of the worker, for example
// `git show v0.10.22-alpha:static/sw.js`, to see how an older worker answers.
const SW_PATH = process.env.SW_FILE || join(dirname(fileURLToPath(import.meta.url)), '..', 'sw.js');
const SOURCE = readFileSync(SW_PATH, 'utf-8');

class FakeRequest {
    constructor(url, init = {}) {
        this.url = new URL(url, ORIGIN).href;
        this.method = init.method || 'GET';
        this.mode = init.mode || 'no-cors';
        this.destination = init.destination || '';
        this.headers = new Headers(init.headers || {});
        this.cache = init.cache;
    }
}

const urlOf = (request) => (typeof request === 'string' ? new URL(request, ORIGIN).href : request.url);
const pathOf = (href) => { const u = new URL(href); return u.origin + u.pathname; };

class FakeCache {
    constructor() { this.entries = new Map(); }
    async match(request, options = {}) {
        const href = urlOf(request);
        if (this.entries.has(href)) return this.entries.get(href).clone();
        if (options.ignoreSearch) {
            for (const [key, response] of this.entries) {
                if (pathOf(key) === pathOf(href)) return response.clone();
            }
        }
        return undefined;
    }
    async put(request, response) { this.entries.set(urlOf(request), response); }
    async addAll(requests) {
        for (const request of requests) this.entries.set(urlOf(request), new Response(`precached ${urlOf(request)}`));
    }
    async add(request) { return this.addAll([request]); }
    async keys() { return [...this.entries.keys()].map((href) => new FakeRequest(href)); }
    async delete(request) { return this.entries.delete(urlOf(request)); }
}

function loadWorker(search) {
    const stores = new Map();
    const caches = {
        async open(name) { if (!stores.has(name)) stores.set(name, new FakeCache()); return stores.get(name); },
        async keys() { return [...stores.keys()]; },
        async delete(name) { return stores.delete(name); },
        async match(request, options) {
            for (const cache of stores.values()) {
                const hit = await cache.match(request, options);
                if (hit) return hit;
            }
            return undefined;
        },
    };
    const listeners = {};
    const self = {
        location: new URL(`/static/sw.js${search}`, ORIGIN),
        origin: ORIGIN,
        addEventListener: (type, fn) => { listeners[type] = fn; },
        skipWaiting: () => {},
        clients: { claim: async () => {} },
    };
    const net = { online: true, calls: [] };
    const fetch = async (request) => {
        net.calls.push(urlOf(request));
        if (!net.online) throw new TypeError('offline');
        return new Response(`network ${urlOf(request)}`);
    };
    const quiet = { log() {}, warn() {}, error() {} };
    new Function('self', 'caches', 'fetch', 'Request', 'console', 'clients', SOURCE)(
        self, caches, fetch, FakeRequest, quiet, self.clients);

    const install = async () => {
        let done;
        listeners.install({ waitUntil: (p) => { done = p; } });
        await done;
    };
    const get = async (url, init) => {
        let answer;
        listeners.fetch({ request: new FakeRequest(url, init), respondWith: (p) => { answer = p; } });
        const response = await answer;
        return response ? response.text() : undefined;
    };
    return { stores, net, install, get };
}

describe('service worker app shell', () => {
    it('precaches scripts and stylesheets under the versioned URLs', async () => {
        const sw = loadWorker('?v=v2');
        await sw.install();
        const keys = [...sw.stores.get('Speakr-cache-v2').entries.keys()];
        expect(keys).toContain(`${ORIGIN}/static/js/app.modular.js?v=v2`);
        expect(keys).toContain(`${ORIGIN}/static/css/styles.css?v=v2`);
        expect(keys).toContain(`${ORIGIN}/static/css/tailwind.css?v=v2`);
        expect(keys).toContain(`${ORIGIN}/`);
        expect(keys).toContain(`${ORIGIN}/static/img/favicon.ico`);
        expect(keys).not.toContain(`${ORIGIN}/static/js/app.modular.js`);
    });

    it('answers a request for its own release from the cache', async () => {
        const sw = loadWorker('?v=v2');
        await sw.install();
        expect(await sw.get('/static/js/app.modular.js?v=v2'))
            .toBe(`precached ${ORIGIN}/static/js/app.modular.js?v=v2`);
    });

    it('starts offline from the precached shell', async () => {
        const sw = loadWorker('?v=v2');
        await sw.install();
        sw.net.online = false;
        for (const path of ['/static/js/app.modular.js', '/static/css/styles.css', '/static/js/i18n.js']) {
            expect(await sw.get(`${path}?v=v2`)).toBe(`precached ${ORIGIN}${path}?v=v2`);
        }
    });

    it('answers an unversioned request for a precached path from the cache', async () => {
        const sw = loadWorker('?v=v2');
        await sw.install();
        expect(await sw.get('/static/js/i18n.js')).toBe(`precached ${ORIGIN}/static/js/i18n.js?v=v2`);
    });

    it("sends another release's URL to the network and does not store it", async () => {
        const sw = loadWorker('?v=v2');
        await sw.install();
        expect(await sw.get('/static/js/app.modular.js?v=v3'))
            .toBe(`network ${ORIGIN}/static/js/app.modular.js?v=v3`);
        const keys = [...sw.stores.get('Speakr-cache-v2').entries.keys()];
        expect(keys).not.toContain(`${ORIGIN}/static/js/app.modular.js?v=v3`);
    });

    it("falls back to the cached path for another release's URL only when offline", async () => {
        const sw = loadWorker('?v=v2');
        await sw.install();
        sw.net.online = false;
        expect(await sw.get('/static/js/app.modular.js?v=v3'))
            .toBe(`precached ${ORIGIN}/static/js/app.modular.js?v=v2`);
    });

    it('keeps unversioned URLs when registered without a version', async () => {
        const sw = loadWorker('');
        await sw.install();
        const keys = [...sw.stores.get('Speakr-cache-v5').entries.keys()];
        expect(keys).toContain(`${ORIGIN}/static/js/app.modular.js`);
    });
});
