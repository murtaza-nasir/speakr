/**
 * Vitest tests for the server-recording-session client (#287 c/d Phase B).
 *
 * The interesting code is the uploader queue's retry + ordering semantics.
 * The HTTP wrappers themselves are thin; we exercise them via a fetch mock.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
    createSession,
    uploadChunk,
    finalizeSession,
    abortSession,
    createUploader,
    getRememberedSession,
} from './server-recording-sessions.js';


function _installFetchMock(responses) {
    // responses: array of { status, body } -- consumed in order
    let i = 0;
    return vi.fn(async () => {
        const r = responses[i++] ?? responses[responses.length - 1];
        const status = r.status ?? 200;
        return {
            status,
            ok: status >= 200 && status < 300,
            json: async () => r.body ?? null,
        };
    });
}


function _installDOM() {
    // Provide just enough of document + localStorage for the module.
    const meta = { getAttribute: (k) => (k === 'content' ? 'csrf-test-token' : null) };
    global.document = { querySelector: () => meta };
    const store = new Map();
    global.localStorage = {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
        clear: () => store.clear(),
    };
}


describe('createSession', () => {
    beforeEach(() => { _installDOM(); });
    afterEach(() => { vi.restoreAllMocks(); delete global.fetch; });

    it('POSTs to /upload/session with csrf header and remembers the session', async () => {
        global.fetch = _installFetchMock([{
            status: 201,
            body: { session_id: 'abc-123', mime_type: 'audio/webm', max_chunk_bytes: 16 * 1024 * 1024 },
        }]);

        const res = await createSession('audio/webm');
        expect(res.session_id).toBe('abc-123');

        const call = global.fetch.mock.calls[0];
        expect(call[0]).toBe('/upload/session');
        expect(call[1].method).toBe('POST');
        expect(call[1].headers['X-CSRFToken']).toBe('csrf-test-token');
        expect(call[1].credentials).toBe('same-origin');
        expect(JSON.parse(call[1].body)).toEqual({ mime_type: 'audio/webm' });

        // Remembered for resume-on-reload
        const remembered = getRememberedSession();
        expect(remembered.session_id).toBe('abc-123');
    });

    it('throws with a useful message on non-201 response', async () => {
        global.fetch = _installFetchMock([{ status: 400, body: { error: 'Unsupported mime_type' } }]);
        await expect(createSession('video/h264')).rejects.toThrow(/Unsupported mime_type/);
    });
});


describe('uploadChunk', () => {
    beforeEach(() => { _installDOM(); });
    afterEach(() => { vi.restoreAllMocks(); delete global.fetch; });

    it('returns {ok:true} on 204', async () => {
        global.fetch = _installFetchMock([{ status: 204, body: null }]);
        const result = await uploadChunk('s-1', 1, new Uint8Array([1, 2, 3]));
        expect(result).toEqual({ ok: true });
    });

    it('throws with expectedChunkIndex on 409', async () => {
        global.fetch = _installFetchMock([{
            status: 409,
            body: { error: 'Out-of-order chunk', expected_chunk_index: 3, got: 5 },
        }]);
        try {
            await uploadChunk('s-1', 5, new Uint8Array([1]));
            expect.fail('should have thrown');
        } catch (e) {
            expect(e.status).toBe(409);
            expect(e.expectedChunkIndex).toBe(3);
        }
    });

    it('throws on 500', async () => {
        global.fetch = _installFetchMock([{ status: 500, body: { error: 'server boom' } }]);
        await expect(uploadChunk('s-1', 1, new Uint8Array([1]))).rejects.toThrow(/server boom/);
    });
});


describe('finalizeSession', () => {
    beforeEach(() => { _installDOM(); });
    afterEach(() => { vi.restoreAllMocks(); delete global.fetch; });

    it('forgets the remembered session on 202 success', async () => {
        global.fetch = _installFetchMock([
            { status: 201, body: { session_id: 'fin-1', mime_type: 'audio/webm', max_chunk_bytes: 1 } },
            { status: 202, body: { recording_id: 42, status: 'finalizing' } },
        ]);
        await createSession('audio/webm');
        expect(getRememberedSession().session_id).toBe('fin-1');
        const result = await finalizeSession('fin-1', { title: 't' });
        expect(result.recording_id).toBe(42);
        expect(getRememberedSession()).toBeNull();
    });

    it('throws on non-202 response', async () => {
        global.fetch = _installFetchMock([{ status: 409, body: { error: 'No chunks uploaded yet' } }]);
        await expect(finalizeSession('x', {})).rejects.toThrow(/No chunks uploaded/);
    });
});


describe('abortSession', () => {
    beforeEach(() => { _installDOM(); });
    afterEach(() => { vi.restoreAllMocks(); delete global.fetch; });

    it('returns {ok:true} on 204', async () => {
        global.fetch = _installFetchMock([{ status: 204, body: null }]);
        const r = await abortSession('abort-1');
        expect(r).toEqual({ ok: true });
    });

    it('returns {ok:true} on 404 (already gone)', async () => {
        global.fetch = _installFetchMock([{ status: 404, body: null }]);
        const r = await abortSession('gone-1');
        expect(r).toEqual({ ok: true });
    });
});


describe('createUploader queue', () => {
    beforeEach(() => { _installDOM(); });
    afterEach(() => { vi.restoreAllMocks(); delete global.fetch; });

    it('uploads chunks in order with monotonic index', async () => {
        global.fetch = _installFetchMock([
            { status: 204 }, { status: 204 }, { status: 204 },
        ]);
        const uploader = createUploader('sid');
        uploader.enqueue(new Uint8Array([1]));
        uploader.enqueue(new Uint8Array([2]));
        uploader.enqueue(new Uint8Array([3]));
        await uploader.drain();
        expect(uploader.getBacklog()).toBe(0);
        // Verify the URLs (in order) include the right index
        const urls = global.fetch.mock.calls.map(c => c[0]);
        expect(urls).toEqual([
            '/upload/session/sid/chunks/1',
            '/upload/session/sid/chunks/2',
            '/upload/session/sid/chunks/3',
        ]);
    });

    it('resumes from startIndex so a new segment appends in order', async () => {
        // After a reload, the server already has 5 chunks; the resumed
        // MediaRecorder must POST its first chunk as index 6.
        global.fetch = _installFetchMock([
            { status: 204 }, { status: 204 },
        ]);
        const uploader = createUploader('sid', { startIndex: 6 });
        uploader.enqueue(new Uint8Array([1]));
        uploader.enqueue(new Uint8Array([2]));
        await uploader.drain();
        const urls = global.fetch.mock.calls.map(c => c[0]);
        expect(urls).toEqual([
            '/upload/session/sid/chunks/6',
            '/upload/session/sid/chunks/7',
        ]);
    });

    it('retries with backoff on 500 then succeeds', async () => {
        global.fetch = _installFetchMock([
            { status: 500, body: { error: 'transient' } },
            { status: 500, body: { error: 'transient' } },
            { status: 204 },
        ]);
        const errors = [];
        const uploader = createUploader('sid', {
            baseDelayMs: 1,
            maxDelayMs: 2,
            onError: (info) => errors.push(info),
        });
        uploader.enqueue(new Uint8Array([1]));
        await uploader.drain();
        expect(uploader.getBacklog()).toBe(0);
        expect(global.fetch.mock.calls).toHaveLength(3);
        expect(errors.filter(e => !e.droppedFromQueue)).toHaveLength(2);
    });

    it('drops a chunk after maxAttempts', async () => {
        global.fetch = _installFetchMock(
            new Array(10).fill({ status: 500, body: { error: 'down' } })
        );
        const errors = [];
        const uploader = createUploader('sid', {
            maxAttempts: 3,
            baseDelayMs: 1,
            maxDelayMs: 1,
            onError: (info) => errors.push(info),
        });
        uploader.enqueue(new Uint8Array([1]));
        await uploader.drain();
        expect(uploader.getBacklog()).toBe(0);
        const dropped = errors.filter(e => e.droppedFromQueue);
        expect(dropped).toHaveLength(1);
    });

    it('resyncs nextIndex on 409 (server expects different index)', async () => {
        // Simulate: client sends chunk 1, server says "I already have 2,
        // give me 3". The uploader should skip the locally-staged chunk and
        // advance nextIndex to 3, then continue.
        global.fetch = _installFetchMock([
            { status: 409, body: { error: 'out of order', expected_chunk_index: 3 } },
            { status: 204 },
        ]);
        const uploader = createUploader('sid', { baseDelayMs: 1, maxDelayMs: 1 });
        uploader.enqueue(new Uint8Array([1]));
        uploader.enqueue(new Uint8Array([2]));
        await uploader.drain();
        // After the resync, the second enqueued chunk was sent as index 3
        const urls = global.fetch.mock.calls.map(c => c[0]);
        expect(urls[urls.length - 1]).toBe('/upload/session/sid/chunks/3');
    });
});
