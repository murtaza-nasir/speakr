/**
 * Vitest tests for the IndexedDB recording-persistence layer (crash recovery).
 *
 * There is no real IndexedDB (or browser) in the node test environment and the
 * repo deliberately avoids adding test dependencies, so we install a small
 * in-memory IndexedDB stub that implements only the surface this module uses:
 * indexedDB.open() with onupgradeneeded/onsuccess/onerror, and a single object
 * store supporting get/put/delete via IDBRequest-style callbacks. The module
 * caches its db handle at module scope, so each test re-imports it fresh with
 * vi.resetModules() against a freshly-installed mock for deterministic state.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const STORE_NAME = 'activeRecording';

/**
 * Build an in-memory IndexedDB stub.
 *
 * config.failOpen   -> indexedDB.open() fires onerror
 * config.opError    -> get/put/delete requests fire onerror (to exercise catch
 *                      blocks). Toggle it after seeding data.
 */
function createMockIndexedDB() {
    const stores = new Map(); // storeName -> Map(key -> value)
    let opened = false;
    const config = { failOpen: false, opError: false };

    function makeRequest(resultFn) {
        const req = { onsuccess: null, onerror: null, result: undefined, error: null };
        queueMicrotask(() => {
            if (config.opError) {
                req.error = new Error('op failed');
                if (req.onerror) req.onerror({ target: req });
                return;
            }
            try {
                req.result = resultFn();
                if (req.onsuccess) req.onsuccess({ target: req });
            } catch (e) {
                req.error = e;
                if (req.onerror) req.onerror({ target: req });
            }
        });
        return req;
    }

    function makeObjectStore(name) {
        return {
            createIndex: () => {},
            get: (key) => makeRequest(() => {
                const s = stores.get(name);
                return s ? s.get(key) : undefined;
            }),
            put: (value) => makeRequest(() => {
                if (!stores.has(name)) stores.set(name, new Map());
                stores.get(name).set(value.id, value);
                return value.id;
            }),
            delete: (key) => makeRequest(() => {
                const s = stores.get(name);
                if (s) s.delete(key);
                return undefined;
            }),
        };
    }

    function makeDB() {
        return {
            objectStoreNames: { contains: (n) => stores.has(n) },
            createObjectStore: (name) => {
                stores.set(name, new Map());
                return makeObjectStore(name);
            },
            transaction: () => ({ objectStore: (name) => makeObjectStore(name) }),
        };
    }

    const indexedDB = {
        open: () => {
            const req = {
                onsuccess: null, onerror: null, onupgradeneeded: null,
                result: null, error: null,
            };
            queueMicrotask(() => {
                if (config.failOpen) {
                    req.error = new Error('open failed');
                    if (req.onerror) req.onerror({ target: req });
                    return;
                }
                const db = makeDB();
                req.result = db;
                if (!opened) {
                    opened = true;
                    if (req.onupgradeneeded) req.onupgradeneeded({ target: { result: db } });
                }
                if (req.onsuccess) req.onsuccess({ target: req });
            });
            return req;
        },
    };

    return { indexedDB, config, stores, openCount: () => stores };
}

// Minimal Blob stub: records its parts/type and exposes size + arrayBuffer.
class MockBlob {
    constructor(parts = [], opts = {}) {
        this.parts = parts;
        this.type = opts.type;
        this.size = parts.reduce((n, p) => n + (p && p.byteLength ? p.byteLength : (p && p.length ? p.length : 0)), 0);
    }
    async arrayBuffer() {
        return new ArrayBuffer(this.size);
    }
}

function makeChunkBlob(size = 4) {
    const buf = new ArrayBuffer(size);
    return { size, arrayBuffer: vi.fn(async () => buf) };
}

describe('recording-persistence', () => {
    let mock;
    let mod;

    beforeEach(async () => {
        vi.resetModules();
        mock = createMockIndexedDB();
        global.indexedDB = mock.indexedDB;
        global.Blob = MockBlob;
        vi.spyOn(console, 'log').mockImplementation(() => {});
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        mod = await import('./recording-persistence.js');
    });

    afterEach(() => {
        vi.restoreAllMocks();
        delete global.indexedDB;
        delete global.Blob;
        delete global.navigator;
    });

    describe('initDB', () => {
        it('opens the database and resolves a handle', async () => {
            const db = await mod.initDB();
            expect(db).toBeTruthy();
            expect(typeof db.transaction).toBe('function');
        });

        it('caches the handle so repeated calls return the same instance', async () => {
            const a = await mod.initDB();
            const b = await mod.initDB();
            expect(a).toBe(b);
        });

        it('rejects when the open request errors', async () => {
            mock.config.failOpen = true;
            await expect(mod.initDB()).rejects.toThrow(/open failed/);
        });
    });

    describe('startRecordingSession', () => {
        it('stores a session with sensible defaults', async () => {
            const session = await mod.startRecordingSession({ mode: 'meeting' });
            expect(session.id).toBe('current');
            expect(session.mode).toBe('meeting');
            expect(session.notes).toBe('');
            expect(session.tags).toEqual([]);
            expect(session.asrOptions).toEqual({});
            expect(session.chunks).toEqual([]);
            expect(session.mimeType).toBe('audio/webm');
            expect(session.duration).toBe(0);
            // Persisted under key 'current'.
            expect(mock.stores.get(STORE_NAME).get('current')).toMatchObject({ mode: 'meeting' });
        });

        it('keeps caller-provided values', async () => {
            const session = await mod.startRecordingSession({
                mode: 'voice', notes: 'hi', tags: ['x'], asrOptions: { diarize: true }, mimeType: 'audio/ogg',
            });
            expect(session.notes).toBe('hi');
            expect(session.tags).toEqual(['x']);
            expect(session.asrOptions).toEqual({ diarize: true });
            expect(session.mimeType).toBe('audio/ogg');
        });

        it('rejects when the DB cannot be opened', async () => {
            mock.config.failOpen = true;
            await expect(mod.startRecordingSession({ mode: 'm' })).rejects.toThrow();
        });
    });

    describe('saveChunk', () => {
        it('appends a chunk to the active session', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            await mod.saveChunk(makeChunkBlob(10), 0);
            await mod.saveChunk(makeChunkBlob(20), 1);

            const session = mock.stores.get(STORE_NAME).get('current');
            expect(session.chunks).toHaveLength(2);
            expect(session.chunks[0]).toMatchObject({ index: 0, size: 10 });
            expect(session.chunks[1]).toMatchObject({ index: 1, size: 20 });
            expect(session.chunks[0].data).toBeInstanceOf(ArrayBuffer);
        });

        it('warns and does nothing when there is no active session', async () => {
            await mod.initDB(); // store exists, but no 'current' record
            await mod.saveChunk(makeChunkBlob(5), 0);
            expect(console.warn).toHaveBeenCalled();
            expect(mock.stores.get(STORE_NAME).get('current')).toBeUndefined();
        });

        it('does not throw when arrayBuffer() fails', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            const bad = { size: 1, arrayBuffer: vi.fn(async () => { throw new Error('decode'); }) };
            await expect(mod.saveChunk(bad, 0)).resolves.toBeUndefined();
            expect(console.error).toHaveBeenCalled();
        });
    });

    describe('updateRecordingMetadata', () => {
        it('merges updates into the session', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            await mod.updateRecordingMetadata({ duration: 42, notes: 'updated' });
            const session = mock.stores.get(STORE_NAME).get('current');
            expect(session.duration).toBe(42);
            expect(session.notes).toBe('updated');
        });

        it('warns and returns when there is no active session', async () => {
            await mod.initDB();
            await mod.updateRecordingMetadata({ duration: 1 });
            expect(console.warn).toHaveBeenCalled();
        });

        it('swallows errors from the store', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            mock.config.opError = true;
            await expect(mod.updateRecordingMetadata({ duration: 1 })).resolves.toBeUndefined();
            expect(console.error).toHaveBeenCalled();
        });
    });

    describe('pruneOldChunks', () => {
        async function seedChunks(n) {
            await mod.startRecordingSession({ mode: 'm' });
            for (let i = 0; i < n; i++) {
                await mod.saveChunk(makeChunkBlob(1), i);
            }
        }

        it('keeps only the last keepLast chunks', async () => {
            await seedChunks(8);
            await mod.pruneOldChunks(3);
            const session = mock.stores.get(STORE_NAME).get('current');
            expect(session.chunks).toHaveLength(3);
            expect(session.chunks.map(c => c.index)).toEqual([5, 6, 7]);
        });

        it('is a no-op when chunk count is within keepLast', async () => {
            await seedChunks(2);
            await mod.pruneOldChunks(5);
            expect(mock.stores.get(STORE_NAME).get('current').chunks).toHaveLength(2);
        });

        it('is a no-op when there is no session', async () => {
            await mod.initDB();
            await expect(mod.pruneOldChunks(3)).resolves.toBeUndefined();
        });

        it('defaults keepLast to 5', async () => {
            await seedChunks(9);
            await mod.pruneOldChunks();
            expect(mock.stores.get(STORE_NAME).get('current').chunks).toHaveLength(5);
        });

        it('swallows errors (best-effort)', async () => {
            await seedChunks(8);
            mock.config.opError = true;
            await expect(mod.pruneOldChunks(2)).resolves.toBeUndefined();
        });
    });

    describe('checkForRecoverableRecording', () => {
        it('returns null when there is no session', async () => {
            await mod.initDB();
            expect(await mod.checkForRecoverableRecording()).toBeNull();
        });

        it('returns null when the session has no chunks', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            expect(await mod.checkForRecoverableRecording()).toBeNull();
        });

        it('reports total size and the tracked duration', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            await mod.saveChunk(makeChunkBlob(10), 0);
            await mod.saveChunk(makeChunkBlob(30), 1);
            await mod.updateRecordingMetadata({ duration: 17 });

            const info = await mod.checkForRecoverableRecording();
            expect(info.totalSize).toBe(40);
            expect(info.duration).toBe(17);
            expect(info.chunks).toHaveLength(2);
        });

        it('estimates duration from chunk count (x5s) when not tracked', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            await mod.saveChunk(makeChunkBlob(1), 0);
            await mod.saveChunk(makeChunkBlob(1), 1);
            await mod.saveChunk(makeChunkBlob(1), 2);
            // duration stays 0 -> falls back to chunks.length * 5
            const info = await mod.checkForRecoverableRecording();
            expect(info.duration).toBe(15);
        });

        it('returns null on store error', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            mock.config.opError = true;
            expect(await mod.checkForRecoverableRecording()).toBeNull();
        });
    });

    describe('recoverRecording', () => {
        it('rebuilds chunks as Blobs and returns metadata', async () => {
            await mod.startRecordingSession({
                mode: 'voice', notes: 'n', tags: ['t'], asrOptions: { d: 1 }, mimeType: 'audio/ogg',
            });
            await mod.saveChunk(makeChunkBlob(8), 0);
            await mod.saveChunk(makeChunkBlob(8), 1);
            await mod.updateRecordingMetadata({ duration: 9 });

            const result = await mod.recoverRecording();
            expect(result.chunks).toHaveLength(2);
            expect(result.chunks[0]).toBeInstanceOf(MockBlob);
            expect(result.chunks[0].type).toBe('audio/ogg');
            expect(result.metadata).toMatchObject({
                mode: 'voice', notes: 'n', tags: ['t'], asrOptions: { d: 1 },
                mimeType: 'audio/ogg', duration: 9,
            });
        });

        it('returns null when there is nothing to recover', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            expect(await mod.recoverRecording()).toBeNull();
        });

        it('returns null on store error', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            mock.config.opError = true;
            expect(await mod.recoverRecording()).toBeNull();
        });
    });

    describe('clearRecordingSession', () => {
        it('deletes the stored session', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            expect(mock.stores.get(STORE_NAME).get('current')).toBeTruthy();
            await mod.clearRecordingSession();
            expect(mock.stores.get(STORE_NAME).get('current')).toBeUndefined();
        });

        it('swallows errors', async () => {
            await mod.startRecordingSession({ mode: 'm' });
            mock.config.opError = true;
            await expect(mod.clearRecordingSession()).resolves.toBeUndefined();
            expect(console.error).toHaveBeenCalled();
        });
    });

    describe('getDatabaseSize', () => {
        it('returns usage/quota/percentage from navigator.storage.estimate', async () => {
            global.navigator = { storage: { estimate: vi.fn(async () => ({ usage: 25, quota: 100 })) } };
            const size = await mod.getDatabaseSize();
            expect(size).toEqual({ usage: 25, quota: 100, percentage: '25.00' });
        });

        it('returns null when the Storage API is unavailable', async () => {
            global.navigator = {};
            expect(await mod.getDatabaseSize()).toBeNull();
        });

        it('returns null when estimate throws', async () => {
            global.navigator = { storage: { estimate: vi.fn(async () => { throw new Error('nope'); }) } };
            expect(await mod.getDatabaseSize()).toBeNull();
        });
    });
});
