/**
 * Vitest tests for the sliced file-upload transport.
 *
 * The interesting code is the slice loop: fixed-size offsets, in-order
 * indexes, retry, 409 resync, and cleaning up the session when the upload
 * cannot continue. HTTP is exercised through fetch and XMLHttpRequest
 * mocks.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { SLICE_BYTES, shouldSliceUpload, uploadFileInSlices } from './sliced-file-upload.js';

const MB = 1024 * 1024;

function fakeFile(size, name = 'talk.mp4', type = 'video/mp4', lastModified = 0) {
    return {
        name,
        type,
        size,
        lastModified,
        slice: (start, end) => ({ size: end - start, start, end }),
    };
}

let sent;
let sliceResponses;
let finalizeResponse;
let fetchCalls;
let sessionStatus;
let defaultSliceResponse;
let store;

function _installMocks() {
    sent = [];
    fetchCalls = [];
    sliceResponses = [];
    defaultSliceResponse = { status: 204, text: '' };
    finalizeResponse = { status: 202, text: JSON.stringify({ id: 42, title: 'talk' }) };
    sessionStatus = null;

    global.window = { csrfManager: { refreshToken: async () => 'fresh-token' } };
    global.document = { querySelector: () => null };

    store = new Map();
    global.localStorage = {
        getItem: (key) => (store.has(key) ? store.get(key) : null),
        setItem: (key, value) => store.set(key, String(value)),
        removeItem: (key) => store.delete(key),
    };

    global.fetch = vi.fn(async (url, options) => {
        fetchCalls.push({ url, options });
        if (options.method === 'POST') {
            return {
                status: 201,
                text: async () => JSON.stringify({
                    session_id: 'sess-1',
                    upload_filename: JSON.parse(options.body).filename,
                    max_chunk_bytes: SLICE_BYTES,
                }),
            };
        }
        if (!options.method) {
            const answer = nextOf(sessionStatus);
            if (answer?.httpStatus) {
                return { status: answer.httpStatus, ok: false, text: async () => 'proxy error' };
            }
            return {
                status: answer ? 200 : 404,
                ok: !!answer,
                text: async () => JSON.stringify(answer || {}),
            };
        }
        return { status: 204, text: async () => '' };
    });

    global.XMLHttpRequest = class {
        constructor() {
            this.upload = {};
            this.timeout = 0;
            this.headers = {};
        }

        open(method, url) { this.url = url; }

        setRequestHeader(key, value) { this.headers[key] = value; }

        send(body) {
            const call = { url: this.url, body, headers: this.headers };
            sent.push(call);
            const response = this.url.endsWith('/finalize-upload')
                ? nextOf(finalizeResponse)
                : (sliceResponses.shift() || defaultSliceResponse);
            queueMicrotask(() => {
                if (response.networkError) {
                    this.onerror();
                    return;
                }
                if (this.upload.onprogress && body?.size) {
                    this.upload.onprogress({ lengthComputable: true, loaded: body.size, total: body.size });
                }
                this.status = response.status;
                this.responseText = response.text ?? '';
                this.onload();
            });
        }
    };
}

/** Queued answers are consumed in order; the last one repeats. */
function nextOf(answers) {
    if (!Array.isArray(answers)) return answers;
    return answers.length > 1 ? answers.shift() : answers[0];
}

const slicePosts = () => sent.filter((call) => !call.url.endsWith('/finalize-upload'));
const finalizePosts = () => sent.filter((call) => call.url.endsWith('/finalize-upload'));
const createCalls = () => fetchCalls.filter((call) => call.options.method === 'POST');
const deleteCalls = () => fetchCalls.filter((call) => call.options.method === 'DELETE');
const rememberedSessions = () => JSON.parse(store.get('speakr.slicedUploads') || '{}');

describe('shouldSliceUpload', () => {
    it('leaves files that fit one request on the single-shot path', () => {
        expect(shouldSliceUpload(fakeFile(SLICE_BYTES))).toBe(false);
        expect(shouldSliceUpload(fakeFile(SLICE_BYTES + 1))).toBe(true);
        expect(shouldSliceUpload(null)).toBe(false);
    });
});

describe('uploadFileInSlices', () => {
    beforeEach(() => { _installMocks(); });
    afterEach(() => {
        vi.restoreAllMocks();
        vi.useRealTimers();
        delete global.fetch;
        delete global.XMLHttpRequest;
        delete global.window;
        delete global.document;
        delete global.localStorage;
    });

    it('opens a session for the filename, sends whole slices in order, and finalizes', async () => {
        const file = fakeFile(40 * MB);
        const form = new Map([['title', 'Sliced']]);
        const progress = [];

        const result = await uploadFileInSlices(file, form, {
            onProgress: (fraction) => progress.push(fraction),
        });

        expect(JSON.parse(fetchCalls[0].options.body)).toEqual({
            filename: 'talk.mp4',
            mime_type: 'video/mp4',
            total_bytes: 40 * MB,
        });
        expect(slicePosts().map((call) => call.url)).toEqual([
            '/upload/session/sess-1/chunks/1',
            '/upload/session/sess-1/chunks/2',
            '/upload/session/sess-1/chunks/3',
        ]);
        expect(slicePosts().map((call) => call.body.size)).toEqual([SLICE_BYTES, SLICE_BYTES, 8 * MB]);
        expect(slicePosts()[0].headers['X-CSRFToken']).toBe('fresh-token');

        const finalizeCall = sent[sent.length - 1];
        expect(finalizeCall.url).toBe('/upload/session/sess-1/finalize-upload');
        expect(finalizeCall.body).toBe(form);
        expect(result).toEqual({ id: 42, title: 'talk' });
        expect(progress[progress.length - 1]).toBe(1);
    });

    it('honours a lower max_chunk_bytes from the server', async () => {
        global.fetch = vi.fn(async (url, options) => ({
            status: 201,
            text: async () => JSON.stringify({
                session_id: 'sess-1',
                upload_filename: 'talk.mp4',
                max_chunk_bytes: 4 * MB,
            }),
        }));

        await uploadFileInSlices(fakeFile(10 * MB), new Map(), {});

        expect(slicePosts().map((call) => call.body.size)).toEqual([4 * MB, 4 * MB, 2 * MB]);
    });

    it('resends the same offset after a network error', async () => {
        vi.useFakeTimers();
        sliceResponses = [{ networkError: true }];

        const upload = uploadFileInSlices(fakeFile(20 * MB), new Map(), {});
        await vi.advanceTimersByTimeAsync(2000);
        await upload;

        expect(slicePosts().map((call) => [call.url, call.body.start])).toEqual([
            ['/upload/session/sess-1/chunks/1', 0],
            ['/upload/session/sess-1/chunks/1', 0],
            ['/upload/session/sess-1/chunks/2', SLICE_BYTES],
        ]);
    });

    it('skips ahead when a 409 says the server already holds the slice', async () => {
        sliceResponses = [{ status: 409, text: JSON.stringify({ expected_chunk_index: 3 }) }];

        await uploadFileInSlices(fakeFile(40 * MB), new Map(), {});

        expect(slicePosts().map((call) => [call.url, call.body.start])).toEqual([
            ['/upload/session/sess-1/chunks/1', 0],
            ['/upload/session/sess-1/chunks/3', 2 * SLICE_BYTES],
        ]);
    });

    it('gives up on a rejected slice and deletes the half-uploaded session', async () => {
        sliceResponses = [
            { status: 204, text: '' },
            { status: 413, text: JSON.stringify({ error: 'Chunk exceeds max_chunk_bytes' }) },
        ];

        await expect(uploadFileInSlices(fakeFile(40 * MB), new Map(), {}))
            .rejects.toThrow('Chunk exceeds max_chunk_bytes');

        expect(slicePosts()).toHaveLength(2);
        expect(fetchCalls[fetchCalls.length - 1]).toMatchObject({
            url: '/upload/session/sess-1',
            options: { method: 'DELETE' },
        });
    });

    // A server running RECORDING_SESSION_COMMIT_BATCH_SIZE > 1 rolls its slice bookkeeping back per request.
    it('stops uploading when the server keeps asking for a slice it accepted', async () => {
        vi.useFakeTimers();
        global.XMLHttpRequest = class extends global.XMLHttpRequest {
            send(body) {
                sent.push({ url: this.url, body, headers: this.headers });
                const accepted = this.url.endsWith('/chunks/1');
                queueMicrotask(() => {
                    this.status = accepted ? 204 : 409;
                    this.responseText = accepted ? '' : JSON.stringify({ expected_chunk_index: 1 });
                    this.onload();
                });
            }
        };

        const upload = uploadFileInSlices(fakeFile(40 * MB), new Map(), {});
        const assertion = expect(upload).rejects.toThrow('bookkeeping is not advancing');
        await vi.advanceTimersByTimeAsync(60000);
        await assertion;

        expect(slicePosts().length).toBeLessThan(10);
    });

    it('blames the reverse proxy for a 413 with no Speakr error body', async () => {
        sliceResponses = [{ status: 413, text: '<html><head><title>413 Request Entity Too Large</title></head></html>' }];

        await expect(uploadFileInSlices(fakeFile(40 * MB), new Map(), {}))
            .rejects.toThrow('Request entity too large: the reverse proxy rejected a 16 MB upload slice');
    });

    it('retries a slice with a fresh token when the server rejects the CSRF one', async () => {
        sliceResponses = [{ status: 400, text: 'The CSRF token has expired.' }];

        await uploadFileInSlices(fakeFile(20 * MB), new Map(), {});

        expect(slicePosts().map((call) => call.body.start)).toEqual([0, 0, SLICE_BYTES]);
    });

    it('refuses to upload when the server does not understand sliced uploads', async () => {
        global.fetch = vi.fn(async () => ({
            status: 201,
            text: async () => JSON.stringify({ session_id: 'sess-1', mime_type: 'audio/webm' }),
        }));

        await expect(uploadFileInSlices(fakeFile(20 * MB), new Map(), {}))
            .rejects.toThrow('does not support sliced uploads');
        expect(sent).toHaveLength(0);
    });

    it('surfaces the finalize error and deletes the session', async () => {
        finalizeResponse = { status: 400, text: JSON.stringify({ error: 'No file provided' }) };

        await expect(uploadFileInSlices(fakeFile(20 * MB), new Map(), {}))
            .rejects.toThrow('No file provided');
        expect(fetchCalls[fetchCalls.length - 1].options.method).toBe('DELETE');
    });

    it('forgets the session once the upload is finalized', async () => {
        await uploadFileInSlices(fakeFile(20 * MB), new Map(), {});

        expect(rememberedSessions()).toEqual({});
    });
});

describe('resuming an interrupted upload', () => {
    beforeEach(() => { _installMocks(); });
    afterEach(() => {
        vi.restoreAllMocks();
        vi.useRealTimers();
        delete global.fetch;
        delete global.XMLHttpRequest;
        delete global.window;
        delete global.document;
        delete global.localStorage;
    });

    const interrupt = async (file) => {
        vi.useFakeTimers();
        sliceResponses = [{ status: 204, text: '' }];
        defaultSliceResponse = { networkError: true };
        const upload = uploadFileInSlices(file, new Map(), {});
        const assertion = expect(upload).rejects.toThrow('Network error');
        await vi.advanceTimersByTimeAsync(120000);
        await assertion;
        vi.useRealTimers();
        defaultSliceResponse = { status: 204, text: '' };
    };

    it('keeps the session and its slices when the network drops', async () => {
        const file = fakeFile(40 * MB);

        await interrupt(file);

        expect(deleteCalls()).toHaveLength(0);
        expect(rememberedSessions()[`talk.mp4|${40 * MB}|0`]).toMatchObject({
            sessionId: 'sess-1',
            sliceBytes: SLICE_BYTES,
        });
    });

    const TIMED_OUT = { status: 524, text: '<html><title>524: A timeout occurred</title></html>' };
    const INGESTED = { status: 202, text: JSON.stringify({ id: 42, title: 'talk', idempotent_replay: true }) };

    // Cloudflare cuts the answer off at 125s while the origin finishes the ingest, so the recording exists and the client must not re-send the file.
    it('waits out an ingest the edge timed out on and returns its recording', async () => {
        vi.useFakeTimers();
        finalizeResponse = [TIMED_OUT, INGESTED];
        sessionStatus = [
            { kind: 'sliced_upload', status: 'finalizing', chunk_count: 3, upload_total_bytes: 40 * MB },
            { kind: 'sliced_upload', status: 'finalized', chunk_count: 3, upload_total_bytes: 40 * MB },
        ];

        const upload = uploadFileInSlices(fakeFile(40 * MB), new Map(), {});
        await vi.advanceTimersByTimeAsync(10000);

        expect(await upload).toMatchObject({ id: 42, idempotent_replay: true });
        expect(slicePosts()).toHaveLength(3);
        expect(finalizePosts()).toHaveLength(2);
        expect(deleteCalls()).toHaveLength(0);
        expect(rememberedSessions()).toEqual({});
    });

    // The wait runs while a proxy is misbehaving, so its errors are part of that outage, not an answer.
    it('keeps waiting when the status endpoint itself is failing', async () => {
        vi.useFakeTimers();
        finalizeResponse = [TIMED_OUT, INGESTED];
        sessionStatus = [
            { httpStatus: 502 },
            { httpStatus: 504 },
            { kind: 'sliced_upload', status: 'finalizing', chunk_count: 3, upload_total_bytes: 40 * MB },
            { kind: 'sliced_upload', status: 'finalized', chunk_count: 3, upload_total_bytes: 40 * MB },
        ];

        const upload = uploadFileInSlices(fakeFile(40 * MB), new Map(), {});
        await vi.advanceTimersByTimeAsync(20000);

        expect(await upload).toMatchObject({ id: 42 });
        expect(deleteCalls()).toHaveLength(0);
    });

    it('gives up on the wait when the server says the session is gone', async () => {
        vi.useFakeTimers();
        finalizeResponse = TIMED_OUT;
        sessionStatus = null;

        const upload = uploadFileInSlices(fakeFile(40 * MB), new Map(), {});
        const assertion = expect(upload).rejects.toThrow('HTTP 524');
        await vi.advanceTimersByTimeAsync(10000);
        await assertion;

        expect(finalizePosts()).toHaveLength(1);
    });

    it('keeps polling when a second finalize is cut off as well', async () => {
        vi.useFakeTimers();
        finalizeResponse = [TIMED_OUT, TIMED_OUT, INGESTED];
        sessionStatus = [
            { kind: 'sliced_upload', status: 'recording', chunk_count: 3, upload_total_bytes: 40 * MB },
            { kind: 'sliced_upload', status: 'finalizing', chunk_count: 3, upload_total_bytes: 40 * MB },
            { kind: 'sliced_upload', status: 'finalized', chunk_count: 3, upload_total_bytes: 40 * MB },
        ];

        const upload = uploadFileInSlices(fakeFile(40 * MB), new Map(), {});
        await vi.advanceTimersByTimeAsync(30000);

        expect(await upload).toMatchObject({ id: 42 });
        expect(finalizePosts()).toHaveLength(3);
    });

    it('finalizes for real when the timed-out request never reached Speakr', async () => {
        vi.useFakeTimers();
        finalizeResponse = [{ status: 502, text: 'Bad Gateway' },
                            { status: 202, text: JSON.stringify({ id: 43 }) }];
        sessionStatus = { kind: 'sliced_upload', status: 'recording', chunk_count: 3, upload_total_bytes: 40 * MB };

        const upload = uploadFileInSlices(fakeFile(40 * MB), new Map(), {});
        await vi.advanceTimersByTimeAsync(10000);

        expect(await upload).toMatchObject({ id: 43 });
        expect(slicePosts()).toHaveLength(3);
    });

    it('surfaces the timeout and keeps the session when the ingest never settles', async () => {
        vi.useFakeTimers();
        finalizeResponse = TIMED_OUT;
        sessionStatus = { kind: 'sliced_upload', status: 'finalizing', chunk_count: 3, upload_total_bytes: 40 * MB };
        const file = fakeFile(40 * MB);

        const upload = uploadFileInSlices(file, new Map(), {});
        const assertion = expect(upload).rejects.toThrow('HTTP 524');
        await vi.advanceTimersByTimeAsync(11 * 60 * 1000);
        await assertion;

        expect(deleteCalls()).toHaveLength(0);
        expect(rememberedSessions()[`talk.mp4|${40 * MB}|0`]).toMatchObject({ sessionId: 'sess-1' });
    });

    it('waits out an ingest that was already running when the file was added again', async () => {
        vi.useFakeTimers();
        const file = fakeFile(40 * MB);
        finalizeResponse = TIMED_OUT;
        sessionStatus = { kind: 'sliced_upload', status: 'finalizing', chunk_count: 3, upload_total_bytes: 40 * MB };
        const abandoned = uploadFileInSlices(file, new Map(), {});
        const abandonedAssertion = expect(abandoned).rejects.toThrow('HTTP 524');
        await vi.advanceTimersByTimeAsync(11 * 60 * 1000);
        await abandonedAssertion;

        sent = [];
        fetchCalls = [];
        finalizeResponse = [{ status: 409, text: JSON.stringify({ error: 'Session is already being finalized' }) }, INGESTED];
        sessionStatus = [
            { kind: 'sliced_upload', status: 'finalizing', chunk_count: 3, upload_total_bytes: 40 * MB },
            { kind: 'sliced_upload', status: 'finalized', chunk_count: 3, upload_total_bytes: 40 * MB },
        ];

        const retry = uploadFileInSlices(file, new Map(), {});
        await vi.advanceTimersByTimeAsync(10000);

        expect(await retry).toMatchObject({ id: 42 });
        expect(createCalls()).toHaveLength(0);
        expect(slicePosts()).toHaveLength(0);
    });

    it('retries a slice the edge timed out instead of abandoning the upload', async () => {
        vi.useFakeTimers();
        sliceResponses = [
            { status: 204, text: '' },
            { status: 524, text: '<html><title>524: A timeout occurred</title></html>' },
        ];

        const upload = uploadFileInSlices(fakeFile(40 * MB), new Map(), {});
        await vi.advanceTimersByTimeAsync(5000);
        await upload;

        expect(slicePosts().map((call) => [call.url, call.body.start])).toEqual([
            ['/upload/session/sess-1/chunks/1', 0],
            ['/upload/session/sess-1/chunks/2', SLICE_BYTES],
            ['/upload/session/sess-1/chunks/2', SLICE_BYTES],
            ['/upload/session/sess-1/chunks/3', 2 * SLICE_BYTES],
        ]);
        expect(deleteCalls()).toHaveLength(0);
    });

    it('keeps the session when a gateway error outlives the retries', async () => {
        vi.useFakeTimers();
        sliceResponses = [{ status: 204, text: '' }];
        defaultSliceResponse = { status: 502, text: 'Bad Gateway' };

        const upload = uploadFileInSlices(fakeFile(40 * MB), new Map(), {});
        const assertion = expect(upload).rejects.toThrow('HTTP 502');
        await vi.advanceTimersByTimeAsync(120000);
        await assertion;

        expect(deleteCalls()).toHaveLength(0);
        expect(Object.keys(rememberedSessions())).toHaveLength(1);
    });

    it('sends only the slices the server is missing on the next attempt', async () => {
        const file = fakeFile(40 * MB);
        await interrupt(file);
        sent = [];
        fetchCalls = [];
        sessionStatus = {
            kind: 'sliced_upload',
            status: 'recording',
            chunk_count: 1,
            upload_total_bytes: 40 * MB,
        };

        const progress = [];
        const result = await uploadFileInSlices(file, new Map(), {
            onProgress: (fraction) => progress.push(fraction),
        });

        expect(createCalls()).toHaveLength(0);
        expect(slicePosts().map((call) => [call.url, call.body.start])).toEqual([
            ['/upload/session/sess-1/chunks/2', SLICE_BYTES],
            ['/upload/session/sess-1/chunks/3', 2 * SLICE_BYTES],
        ]);
        expect(progress[0]).toBeCloseTo(SLICE_BYTES / (40 * MB));
        expect(result).toEqual({ id: 42, title: 'talk' });
        expect(rememberedSessions()).toEqual({});
    });

    it('replays the recording when the upload had already been finalized', async () => {
        const file = fakeFile(40 * MB);
        await interrupt(file);
        sent = [];
        sessionStatus = {
            kind: 'sliced_upload',
            status: 'finalized',
            chunk_count: 3,
            upload_total_bytes: 40 * MB,
        };
        finalizeResponse = {
            status: 202,
            text: JSON.stringify({ id: 42, title: 'talk', idempotent_replay: true }),
        };

        const result = await uploadFileInSlices(file, new Map(), {});

        expect(slicePosts()).toHaveLength(0);
        expect(result.id).toBe(42);
    });

    it('starts over when the file is not the one the session was opened for', async () => {
        await interrupt(fakeFile(40 * MB));
        sent = [];
        fetchCalls = [];
        sessionStatus = {
            kind: 'sliced_upload',
            status: 'recording',
            chunk_count: 1,
            upload_total_bytes: 40 * MB,
        };

        await uploadFileInSlices(fakeFile(40 * MB, 'talk.mp4', 'video/mp4', 99), new Map(), {});

        expect(createCalls()).toHaveLength(1);
        expect(slicePosts()[0].body.start).toBe(0);
    });

    it('starts over when the server no longer has the session', async () => {
        const file = fakeFile(40 * MB);
        await interrupt(file);
        sent = [];
        fetchCalls = [];
        sessionStatus = null;

        await uploadFileInSlices(file, new Map(), {});

        expect(createCalls()).toHaveLength(1);
        expect(slicePosts()[0].body.start).toBe(0);
    });
});
