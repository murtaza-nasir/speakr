/**
 * Tests for the local-download safety-net helper (issue #297).
 *
 * triggerLocalDownload is the last-resort fallback when both the upload and
 * IndexedDB persistence fail. It builds a blob URL, attaches an anchor,
 * synthesises a click, and revokes the URL afterwards. We mock the DOM
 * and URL APIs to verify the call shape.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { triggerLocalDownload } from './failed-uploads.js';

describe('triggerLocalDownload', () => {
    let originalDocument;
    let originalURL;
    let createObjectURL;
    let revokeObjectURL;
    let appendChild;
    let removeChild;
    let click;
    let anchor;

    beforeEach(() => {
        createObjectURL = vi.fn(() => 'blob:test-url');
        revokeObjectURL = vi.fn();
        click = vi.fn();
        appendChild = vi.fn();
        removeChild = vi.fn();
        anchor = {
            click,
            style: {},
            set href(val) { this._href = val; },
            get href() { return this._href; },
            set download(val) { this._download = val; },
            get download() { return this._download; },
        };
        originalURL = global.URL;
        originalDocument = global.document;
        global.URL = { createObjectURL, revokeObjectURL };
        global.document = {
            createElement: vi.fn(() => anchor),
            body: { appendChild, removeChild },
        };
    });

    afterEach(() => {
        global.URL = originalURL;
        global.document = originalDocument;
    });

    it('returns false for falsy file', () => {
        expect(triggerLocalDownload(null, 'a.webm')).toBe(false);
        expect(triggerLocalDownload(undefined, 'a.webm')).toBe(false);
    });

    it('returns false for zero-size blob', () => {
        expect(triggerLocalDownload({ size: 0 }, 'a.webm')).toBe(false);
        expect(createObjectURL).not.toHaveBeenCalled();
    });

    it('synthesises a click on an anchor for a non-empty blob', () => {
        const fakeFile = { name: 'recording.webm', size: 1024, type: 'audio/webm' };
        const result = triggerLocalDownload(fakeFile, 'recording.webm');
        expect(result).toBe(true);
        expect(createObjectURL).toHaveBeenCalledWith(fakeFile);
        expect(global.document.createElement).toHaveBeenCalledWith('a');
        expect(anchor.href).toBe('blob:test-url');
        expect(anchor.download).toBe('recording.webm');
        expect(click).toHaveBeenCalledOnce();
        expect(appendChild).toHaveBeenCalledWith(anchor);
        expect(removeChild).toHaveBeenCalledWith(anchor);
    });

    it('falls back to a generated name when none is supplied', () => {
        const fakeFile = { size: 100 };
        triggerLocalDownload(fakeFile);
        expect(anchor.download).toMatch(/^speakr-recording-\d+\.webm$/);
    });

    it('returns false when DOM access throws', () => {
        global.URL.createObjectURL = vi.fn(() => { throw new Error('boom'); });
        const result = triggerLocalDownload({ size: 1 }, 'x.webm');
        expect(result).toBe(false);
    });
});

/**
 * Regression tests for the IndexedDB transaction-lifetime bug (TransactionInactiveError).
 *
 * IndexedDB auto-commits ("finishes") a transaction once control returns to the
 * event loop with no request pending against it. So `await`-ing a non-IndexedDB
 * promise (e.g. File.arrayBuffer(), or a nested helper that opens its own
 * transaction) between `db.transaction(...)` and the first request kills the
 * transaction; a request chained from an earlier request's onsuccess, however,
 * keeps it alive. The mock below models both: it commits only on a microtask
 * checkpoint that finds no request pending, and add()/put()/get() throw if used
 * after that, exactly like a real browser.
 */
describe('IndexedDB transaction lifetime', () => {
    let originalIndexedDB;
    let store;
    let storeFailedUpload;
    let updateRetryCount;

    const inactiveError = (op) => new DOMException(
        `Failed to execute '${op}' on 'IDBObjectStore': The transaction has finished.`,
        'TransactionInactiveError'
    );

    // Build a mock that mirrors real auto-commit semantics: a transaction stays
    // active while a request is outstanding, and commits on the first microtask
    // checkpoint where none is.
    const makeMockDB = () => {
        store = new Map();
        let nextId = 1;

        const db = {
            transaction() {
                const tx = { _active: true, _pending: 0 };
                // Commit if the turn yields with nothing pending against the tx.
                const scheduleCommitCheck = () => queueMicrotask(() => {
                    if (tx._pending === 0) tx._active = false;
                });
                scheduleCommitCheck();

                const makeRequest = (opName, op) => {
                    if (!tx._active) throw inactiveError(opName);
                    tx._pending++;
                    const request = {};
                    queueMicrotask(() => {
                        try {
                            request.result = op();
                            request.onsuccess?.();
                        } catch (err) {
                            request.error = err;
                            request.onerror?.();
                        } finally {
                            tx._pending--;
                            scheduleCommitCheck();
                        }
                    });
                    return request;
                };

                const objectStore = {
                    add: (value) => makeRequest('add', () => {
                        const id = nextId++;
                        store.set(id, { ...value, id });
                        return id;
                    }),
                    put: (value) => makeRequest('put', () => {
                        store.set(value.id, { ...value });
                        return value.id;
                    }),
                    get: (id) => makeRequest('get', () => store.get(id)),
                };
                tx.objectStore = () => objectStore;
                return tx;
            },
            objectStoreNames: { contains: () => true },
        };
        return db;
    };

    beforeEach(async () => {
        // Reset the module so its cached dbInstance does not leak between tests;
        // re-import after wiring the fresh indexedDB mock so initDB() opens it.
        vi.resetModules();
        originalIndexedDB = global.indexedDB;
        const db = makeMockDB();
        global.indexedDB = {
            open: vi.fn(() => {
                const request = {};
                queueMicrotask(() => {
                    request.result = db;
                    request.onsuccess?.();
                });
                return request;
            }),
        };
        ({ storeFailedUpload, updateRetryCount } = await import('./failed-uploads.js'));
    });

    afterEach(() => {
        global.indexedDB = originalIndexedDB;
    });

    it('storeFailedUpload persists a record when the file must be read to an ArrayBuffer', async () => {
        const file = {
            name: 'recording.webm',
            size: 2048,
            type: 'audio/webm',
            arrayBuffer: async () => new ArrayBuffer(8),
        };

        const id = await storeFailedUpload({
            file,
            clientId: 'client-123',
            notes: 'n',
            tags: ['t'],
            asrOptions: {},
            error: '413 Payload Too Large',
        });

        expect(id).toBeTruthy();
        const stored = store.get(id);
        expect(stored.fileName).toBe('recording.webm');
        expect(stored.fileData).toBeInstanceOf(ArrayBuffer);
        expect(stored.lastError).toBe('413 Payload Too Large');
    });

    it('updateRetryCount reads and writes in one transaction without a finished transaction', async () => {
        // Seed a record first.
        const file = {
            name: 'r.webm', size: 10, type: 'audio/webm',
            arrayBuffer: async () => new ArrayBuffer(4),
        };
        const id = await storeFailedUpload({ file, clientId: 'c' });

        await expect(updateRetryCount(id, 2, 'still failing')).resolves.not.toThrow();
        const updated = store.get(id);
        expect(updated.retryCount).toBe(2);
        expect(updated.lastError).toBe('still failing');
    });

    it('updateRetryCount resolves without writing when the id is absent', async () => {
        await expect(updateRetryCount(999, 1, 'nope')).resolves.not.toThrow();
        expect(store.has(999)).toBe(false);
    });
});

describe('storeFailedUpload structured-clone safety (Proxy metadata)', () => {
    // A mock whose add() runs structuredClone(value) first — exactly what a real
    // browser does. The previous mocks skipped this, which is why the
    // "Proxy object could not be cloned" DataCloneError slipped through: tags /
    // asrOptions arrive as Vue reactive proxies and IndexedDB rejects them.
    let store;
    let originalIndexedDB;
    let storeFailedUpload;

    beforeEach(async () => {
        vi.resetModules();
        store = new Map();
        let nextId = 1;
        const makeRequest = (op) => {
            const request = {};
            queueMicrotask(() => {
                try { request.result = op(); request.onsuccess?.(); }
                catch (err) { request.error = err; request.onerror?.(); }
            });
            return request;
        };
        const db = {
            transaction() {
                const objectStore = {
                    add: (value) => makeRequest(() => {
                        // Throws DataCloneError on a Proxy, like a real engine.
                        const cloned = structuredClone(value);
                        const id = nextId++;
                        store.set(id, { ...cloned, id });
                        return id;
                    }),
                };
                return { objectStore: () => objectStore };
            },
            objectStoreNames: { contains: () => true },
        };
        originalIndexedDB = global.indexedDB;
        global.indexedDB = {
            open: vi.fn(() => {
                const request = {};
                queueMicrotask(() => { request.result = db; request.onsuccess?.(); });
                return request;
            }),
        };
        ({ storeFailedUpload } = await import('./failed-uploads.js'));
    });

    afterEach(() => { global.indexedDB = originalIndexedDB; });

    it('persists even when tags/asrOptions are reactive proxies', async () => {
        const file = {
            name: 'r.webm', size: 8, type: 'audio/webm',
            arrayBuffer: async () => new ArrayBuffer(8),
        };
        // Simulate Vue reactive() state: a Proxy that structuredClone rejects.
        const reactiveTags = new Proxy([{ id: 1, name: 'Work' }], {});
        const reactiveAsr = new Proxy({ language: 'en', diarize: true }, {});

        const id = await storeFailedUpload({
            file, clientId: 'c', tags: reactiveTags, asrOptions: reactiveAsr,
        });

        expect(id).toBeTruthy();
        const stored = store.get(id);
        expect(stored.tags).toEqual([{ id: 1, name: 'Work' }]);
        expect(stored.asrOptions).toEqual({ language: 'en', diarize: true });
    });
});
