/**
 * The requests of a trim (#660): POST /api/recordings/<id>/trim queues it,
 * GET on the same route tells how it went. The segment editor asks and then
 * waits here (composables/asr-editor.js).
 */

import { getCsrfTokenFromMeta } from '../csrf.js';

const csrfHeaders = () => {
    const token = getCsrfTokenFromMeta();
    return token ? { 'X-CSRFToken': token } : {};
};

const POLL_MS = 1500;
const MAX_ATTEMPTS = 400;          // ten minutes

/**
 * Queue the trim. Resolves to { ok: true, jobId } or { ok: false, error }
 * with the server's message (or `fallback` when it sent none).
 */
export async function sendTrim(fetchFn, recordingId, range, { regenerateSummary = false, fallback = '' } = {}) {
    let response;
    try {
        response = await fetchFn(`/api/recordings/${recordingId}/trim`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
            body: JSON.stringify({ start: range.start, end: range.end, regenerate_summary: !!regenerateSummary }),
        });
    } catch (error) {
        return { ok: false, error: fallback };
    }
    let data = {};
    try { data = await response.json(); } catch (error) { data = {}; }
    if (!response.ok) return { ok: false, error: (data && data.error) || fallback, code: data && data.code };
    return { ok: true, jobId: data.job_id };
}

/**
 * Poll the trim until it is completed or failed. Resolves to the last state
 * ({ state: 'completed', summary_queued } or { state: 'failed', error }), or
 * { state: 'timeout' }.
 */
export async function waitForTrim(fetchFn, recordingId, {
    pollMs = POLL_MS, maxAttempts = MAX_ATTEMPTS, sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
} = {}) {
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
        await sleep(pollMs);
        let data = null;
        try {
            const response = await fetchFn(`/api/recordings/${recordingId}/trim`);
            if (response.ok) data = await response.json();
        } catch (error) {
            data = null;            // offline for a moment: asked again
        }
        if (data && (data.state === 'completed' || data.state === 'failed')) return data;
    }
    return { state: 'timeout' };
}
