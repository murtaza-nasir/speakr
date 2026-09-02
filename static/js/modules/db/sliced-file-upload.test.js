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

function fakeFile(size, name = 'talk.mp4', type = 'video/mp4') {
    return {
        name,
        type,
        size,
        slice: (start, end) => ({ size: end - start, start, end }),
    };
}

let sent;
let sliceResponses;
let finalizeResponse;
let fetchCalls;

function _installMocks() {
    sent = [];
    fetchCalls = [];
    sliceResponses = [];
    finalizeResponse = { status: 202, text: JSON.stringify({ id: 42, title: 'talk' }) };

    global.window = { csrfManager: { refreshToken: async () => 'fresh-token' } };
    global.document = { querySelector: () => null };

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
                ? finalizeResponse
                : (sliceResponses.shift() || { status: 204, text: '' });
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

const slicePosts = () => sent.filter((call) => !call.url.endsWith('/finalize-upload'));

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
        finalizeResponse = { status: 409, text: JSON.stringify({ error: 'Slices on disk do not match the session' }) };

        await expect(uploadFileInSlices(fakeFile(20 * MB), new Map(), {}))
            .rejects.toThrow('Slices on disk do not match the session');
        expect(fetchCalls[fetchCalls.length - 1].options.method).toBe('DELETE');
    });
});
