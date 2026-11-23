import { describe, expect, it, vi } from 'vitest';

import { sendTrim, waitForTrim } from './recording-trim.js';

const answer = (ok, body) => ({ ok, json: async () => body });

// #660: the requests of a trim
describe('sendTrim', () => {
    it('posts the range and the summary choice', async () => {
        const fetchFn = vi.fn(async () => answer(true, { job_id: 9, state: 'queued' }));
        const result = await sendTrim(fetchFn, 4, { start: 1.5, end: 20 }, { regenerateSummary: true });
        expect(result).toEqual({ ok: true, jobId: 9 });
        const [url, options] = fetchFn.mock.calls[0];
        expect(url).toBe('/api/recordings/4/trim');
        expect(options.method).toBe('POST');
        expect(JSON.parse(options.body)).toEqual({ start: 1.5, end: 20, regenerate_summary: true });
    });

    it("gives the server's message for a refusal and the fallback for a failed request", async () => {
        const refused = await sendTrim(async () => answer(false, { error: 'Keep at least one second of audio.', code: 'trim_too_short' }),
            4, { start: 1, end: 1.5 }, { fallback: 'fallback' });
        expect(refused).toEqual({ ok: false, error: 'Keep at least one second of audio.', code: 'trim_too_short' });
        const offline = await sendTrim(async () => { throw new Error('offline'); }, 4, { start: 1, end: 5 }, { fallback: 'fallback' });
        expect(offline).toEqual({ ok: false, error: 'fallback' });
    });
});

describe('waitForTrim', () => {
    const sleep = async () => {};

    it('asks until the trim is completed', async () => {
        const states = [{ state: 'queued' }, { state: 'processing' }, { state: 'completed', summary_queued: true }];
        const fetchFn = vi.fn(async () => answer(true, states.shift()));
        const result = await waitForTrim(fetchFn, 4, { sleep });
        expect(result).toEqual({ state: 'completed', summary_queued: true });
        expect(fetchFn).toHaveBeenCalledTimes(3);
        expect(fetchFn.mock.calls[0][0]).toBe('/api/recordings/4/trim');
    });

    it('stops at a failure and asks again after a failed request', async () => {
        const replies = [() => { throw new Error('offline'); }, () => answer(false, {}), () => answer(true, { state: 'failed', error: 'No.' })];
        const result = await waitForTrim(async () => replies.shift()(), 4, { sleep });
        expect(result).toEqual({ state: 'failed', error: 'No.' });
    });

    it('gives up after the last attempt', async () => {
        const result = await waitForTrim(async () => answer(true, { state: 'processing' }), 4, { sleep, maxAttempts: 3 });
        expect(result).toEqual({ state: 'timeout' });
    });
});
