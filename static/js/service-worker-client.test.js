import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// service-worker-client.js is a classic script; it is run here in a fresh
// context per test with stand-ins for the document, the service worker
// container, Cache Storage, sessionStorage and IndexedDB. The real browser
// behavior is covered by tests/e2e/test_service_worker_ui.py.
const SOURCE = readFileSync(fileURLToPath(new URL('./service-worker-client.js', import.meta.url)), 'utf8');
const ORIGIN = 'https://speakr.test';

const flush = async () => { for (let i = 0; i < 20; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };

/** A stand-in for IndexedDB: { database: { store: { key: value } } }. */
function fakeIndexedDB(databases) {
    const later = (request, result) => setTimeout(() => {
        request.result = result;
        if (request.onsuccess) request.onsuccess({ target: request });
    }, 0);
    return {
        opened: [],
        open(name) {
            this.opened.push(name);
            const request = {};
            setTimeout(() => {
                if (!(name in databases)) {
                    // A real browser would create the database here.
                    request.transaction = { abort: () => { request.aborted = true; } };
                    if (request.onupgradeneeded) request.onupgradeneeded({ target: request });
                    if (request.aborted) {
                        if (request.onerror) request.onerror({ preventDefault() {} });
                        return;
                    }
                    databases[name] = {};
                }
                const stores = databases[name];
                request.result = {
                    objectStoreNames: { contains: (store) => store in stores },
                    close() {},
                    transaction: ([store]) => {
                        const transaction = {};
                        const done = () => setTimeout(() => transaction.oncomplete && transaction.oncomplete(), 0);
                        transaction.objectStore = () => ({
                            get: (key) => { const r = {}; later(r, stores[store][key]); done(); return r; },
                            count: () => { const r = {}; later(r, Object.keys(stores[store]).length); done(); return r; },
                            clear: () => { const r = {}; stores[store] = {}; later(r, undefined); done(); return r; },
                        });
                        return transaction;
                    },
                };
                if (request.onsuccess) request.onsuccess({ target: request });
            }, 0);
            return request;
        },
    };
}

function load({
    register = false, version = '9.9.9', cookie = '', serviceWorker = true, registrations = [],
    databases = null, confirmAnswer = true, cacheNames = [], controller = true,
} = {}) {
    const listeners = {};
    const cookies = { value: cookie, writes: [] };
    const document = {
        currentScript: {
            src: `${ORIGIN}/static/js/service-worker-client.js${version ? `?v=${version}` : ''}`,
            dataset: register ? { register: '' } : {},
        },
        readyState: 'complete',
        addEventListener: (type, handler, capture) => { listeners[type] = { handler, capture }; },
        get cookie() { return cookies.value; },
        set cookie(v) { cookies.writes.push(v); if (/Max-Age=0/.test(v)) cookies.value = ''; },
    };
    const caches = {
        names: [...cacheNames],
        keys: vi.fn(async () => [...caches.names]),
        delete: vi.fn(async (name) => { caches.names = caches.names.filter((n) => n !== name); return true; }),
    };
    const worker = {
        postMessage: vi.fn((message, ports) => { ports[0].postMessage({ type: 'SIGNED_OUT' }); }),
    };
    const container = {
        controller: controller ? worker : null,
        getRegistrations: vi.fn(async () => registrations),
        register: vi.fn(async (url, options) => ({ url, scope: `${ORIGIN}${options.scope}` })),
    };
    const sessionStorage = { clear: vi.fn() };
    const confirmDialog = vi.fn(async () => confirmAnswer);
    const window = {
        document,
        location: { href: `${ORIGIN}/` },
        navigator: serviceWorker ? { serviceWorker: container } : {},
        caches,
        sessionStorage,
        confirmDialog,
        addEventListener: vi.fn(),
    };
    if (databases) window.indexedDB = fakeIndexedDB(databases);
    const context = vm.createContext({ window, URL, console, setTimeout, clearTimeout, MessageChannel, Promise, WeakSet, RegExp });
    vm.runInContext(SOURCE, context);

    const form = (action = '/logout') => {
        const f = {
            getAttribute: (name) => (name === 'action' ? action : null),
            submit: vi.fn(),
            requestSubmit: vi.fn(() => {
                // The browser fires submit again; the handler must let it through.
                const second = submitEvent(f);
                listeners.submit.handler(second);
                f.secondPass = second;
            }),
        };
        return f;
    };
    const submitEvent = (target) => ({
        target, submitter: null, preventDefault: vi.fn(), stopImmediatePropagation: vi.fn(),
    });
    const submit = (target) => { const event = submitEvent(target); listeners.submit.handler(event); return event; };

    return { api: window.SpeakrServiceWorker, window, listeners, container, caches, worker, sessionStorage, confirmDialog, cookies, form, submit };
}

describe('registration', () => {
    it('registers /sw.js with the app version and scope / on a page that asks for it', async () => {
        const { container } = load({ register: true });
        await flush();
        expect(container.register).toHaveBeenCalledTimes(1);
        expect(container.register).toHaveBeenCalledWith('/sw.js?v=9.9.9', { scope: '/' });
    });

    it('registers nothing on a page that does not ask for it (the sign-in page)', async () => {
        const { container } = load({ register: false });
        await flush();
        expect(container.register).not.toHaveBeenCalled();
        expect(container.getRegistrations).not.toHaveBeenCalled();
    });

    it('removes the registration an earlier release left at /static/ and keeps the one at /', async () => {
        const old = { scope: `${ORIGIN}/static/`, unregister: vi.fn(async () => true) };
        const current = { scope: `${ORIGIN}/`, unregister: vi.fn(async () => true) };
        const { container } = load({ register: true, registrations: [old, current] });
        await flush();
        expect(old.unregister).toHaveBeenCalledTimes(1);
        expect(current.unregister).not.toHaveBeenCalled();
        expect(container.register).toHaveBeenCalledTimes(1);
    });

    it('does nothing where service workers do not exist (plain http on a LAN address)', async () => {
        const { api } = load({ register: true, serviceWorker: false });
        await flush();
        await expect(api.register()).resolves.toBeNull();
    });

    it('survives a registration that is refused', async () => {
        const { api, container } = load();
        container.register.mockRejectedValueOnce(new Error('SecurityError'));
        await expect(api.register()).resolves.toBeNull();
    });

    it('registers without a version when the script has none', async () => {
        const { api, container } = load({ version: '' });
        await api.register();
        expect(container.register).toHaveBeenCalledWith('/sw.js', { scope: '/' });
    });
});

describe('the sign-out form', () => {
    it('leaves other forms alone', () => {
        const { form, submit } = load();
        const event = submit(form('/account/save'));
        expect(event.preventDefault).not.toHaveBeenCalled();
        expect(event.stopImmediatePropagation).not.toHaveBeenCalled();
    });

    it('sends the form without a question when the browser holds no copies', async () => {
        const { form, submit, confirmDialog, listeners } = load({ databases: {} });
        expect(listeners.submit.capture).toBe(true);
        const f = form();
        const first = submit(f);
        expect(first.preventDefault).toHaveBeenCalledTimes(1);
        expect(first.stopImmediatePropagation).toHaveBeenCalledTimes(1);
        await flush();
        expect(confirmDialog).not.toHaveBeenCalled();
        expect(f.requestSubmit).toHaveBeenCalledTimes(1);
        // The second pass is the real one: nothing stops it, so the other
        // listeners (the chat threads) run and the form is sent.
        expect(f.secondPass.preventDefault).not.toHaveBeenCalled();
        expect(f.secondPass.stopImmediatePropagation).not.toHaveBeenCalled();
    });

    it('creates no database while looking for copies', async () => {
        const databases = {};
        const { form, submit } = load({ databases });
        submit(form());
        await flush();
        expect(databases).toEqual({});
    });

    it('asks before copies that did not reach the server are deleted, and stays on Cancel', async () => {
        const databases = {
            SpeakrRecordings: { activeRecording: { current: { id: 'current', chunks: [1, 2] } } },
            SpeakrFailedUploads: { failedUploads: { 1: {}, 2: {} } },
        };
        const { api, form, submit, confirmDialog } = load({ databases, confirmAnswer: false });
        await expect(api.localCopies()).resolves.toBe(3);
        const f = form();
        submit(f);
        await flush();
        expect(confirmDialog).toHaveBeenCalledTimes(1);
        const options = confirmDialog.mock.calls[0][0];
        expect(options.danger).toBe(true);
        expect(options.message).toContain('3 recordings or uploads');
        expect(options.confirmLabel).toBe('Delete and sign out');
        expect(f.requestSubmit).not.toHaveBeenCalled();
        expect(f.submit).not.toHaveBeenCalled();
        // Nothing was deleted by the question.
        expect(Object.keys(databases.SpeakrFailedUploads.failedUploads)).toHaveLength(2);
        expect(databases.SpeakrRecordings.activeRecording.current.chunks).toEqual([1, 2]);
    });

    it('sends the form after the question is confirmed, still without deleting', async () => {
        const databases = { SpeakrFailedUploads: { failedUploads: { 1: {} } } };
        const { form, submit, confirmDialog } = load({ databases, confirmAnswer: true });
        const f = form();
        submit(f);
        await flush();
        expect(confirmDialog.mock.calls[0][0].message).toContain('1 recording or upload');
        expect(f.requestSubmit).toHaveBeenCalledTimes(1);
        // The copies are deleted by the page after the sign-out, not here.
        expect(Object.keys(databases.SpeakrFailedUploads.failedUploads)).toHaveLength(1);
    });

    it('does not count a recording session without audio', async () => {
        const databases = { SpeakrRecordings: { activeRecording: { current: { id: 'current', chunks: [] } } } };
        const { api } = load({ databases });
        await expect(api.localCopies()).resolves.toBe(0);
    });

    it('signs out when the browser has no IndexedDB or the lookup fails', async () => {
        const { form, submit } = load();
        const f = form();
        submit(f);
        await flush();
        expect(f.requestSubmit).toHaveBeenCalledTimes(1);
    });

    it('sends the form plainly where requestSubmit does not exist', async () => {
        const { form, submit } = load();
        const f = form();
        f.requestSubmit = undefined;
        submit(f);
        await flush();
        expect(f.submit).toHaveBeenCalledTimes(1);
    });
});

describe('after a sign-out', () => {
    it('clears sessionStorage, the IndexedDB stores and every cache, tells the worker and removes the mark', async () => {
        const databases = {
            SpeakrRecordings: { activeRecording: { current: { chunks: [1] } } },
            SpeakrFailedUploads: { failedUploads: { 1: {}, 2: {} } },
            Other: { things: { a: 1 } },
        };
        const { api, sessionStorage, caches, worker, cookies } = load({
            cookie: 'theme=dark; speakr_signed_out=1', databases, cacheNames: ['speakr-static-9.9.9', 'Speakr-cache-9.9.8'],
        });
        await api.ready;
        expect(sessionStorage.clear).toHaveBeenCalledTimes(1);
        expect(databases.SpeakrRecordings.activeRecording).toEqual({});
        expect(databases.SpeakrFailedUploads.failedUploads).toEqual({});
        expect(databases.Other).toEqual({ things: { a: 1 } });
        expect(caches.names).toEqual([]);
        expect(worker.postMessage.mock.calls[0][0]).toEqual({ type: 'SIGN_OUT' });
        expect(cookies.writes).toEqual(['speakr_signed_out=; Max-Age=0; path=/; SameSite=Lax']);
        expect(cookies.value).toBe('');
    });

    it('clears nothing without the mark of a sign-out', async () => {
        const databases = { SpeakrFailedUploads: { failedUploads: { 1: {} } } };
        for (const cookie of ['', 'session=abc', 'speakr_signed_out=0', 'xspeakr_signed_out=1', 'speakr_signed_out=10']) {
            const { api, sessionStorage, caches, worker, cookies } = load({ cookie, databases, cacheNames: ['speakr-static-9.9.9'] });
            await api.ready;
            await flush();
            expect(sessionStorage.clear, cookie).not.toHaveBeenCalled();
            expect(caches.delete, cookie).not.toHaveBeenCalled();
            expect(worker.postMessage, cookie).not.toHaveBeenCalled();
            expect(cookies.writes, cookie).toEqual([]);
        }
        expect(Object.keys(databases.SpeakrFailedUploads.failedUploads)).toHaveLength(1);
    });

    it('clears what it can without a worker, Cache Storage or IndexedDB', async () => {
        const { api, sessionStorage, cookies, window } = load({ cookie: 'speakr_signed_out=1', serviceWorker: false });
        delete window.caches;
        await api.ready;
        expect(sessionStorage.clear).toHaveBeenCalledTimes(1);
        expect(cookies.value).toBe('');
    });

    it('registers the worker only after the clearing', async () => {
        const order = [];
        const { api, container, caches } = load({ register: true, cookie: 'speakr_signed_out=1', cacheNames: ['a'] });
        caches.delete.mockImplementation(async () => { order.push('cleared'); return true; });
        container.register.mockImplementation(async () => { order.push('registered'); return {}; });
        await api.ready;
        await flush();
        expect(order).toEqual(['cleared', 'registered']);
    });
});
