/**
 * Client wrapper around the server-side recording-session API (#287 c/d).
 *
 * Pairs with src/api/recording_sessions.py on the backend. While the user
 * is recording in-browser, every MediaRecorder chunk is POSTed to
 * /upload/session/{id}/chunks/{N}. On Stop + Upload, the client calls
 * /finalize and the backend stitch worker assembles the file via
 * ffmpeg concat demux.
 *
 * This module owns:
 *   - Session lifecycle: createSession, finalizeSession, abortSession,
 *     getSessionStatus.
 *   - The retry queue: chunks that fail to POST are pushed onto an
 *     in-memory queue and re-tried with exponential backoff. While the
 *     queue is non-empty, callers can read `getSyncBacklog()` to drive
 *     a "X chunks waiting to sync" banner.
 *   - CSRF: every POST/DELETE attaches the page's meta csrf-token.
 *
 * Feature-flagged off by default; the audio composable checks
 * `state.serverRecordingChunksEnabled` before opening a session.
 */

const SESSION_BASE = '/upload/session';

const PENDING_KEY = 'speakr.serverRecordingSession';

/** Read the CSRF token written into the page by Flask-WTF. */
function csrfToken() {
    const el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute('content') : '';
}

/** Persist the active session id so we can resume after refresh. */
function rememberActiveSession(sessionId, mimeType) {
    try {
        localStorage.setItem(PENDING_KEY, JSON.stringify({
            session_id: sessionId,
            mime_type: mimeType,
            stamped_at: Date.now(),
        }));
    } catch (_) { /* private mode: best-effort */ }
}

export function forgetActiveSession() {
    try { localStorage.removeItem(PENDING_KEY); } catch (_) { /* ignore */ }
}

export function getRememberedSession() {
    try {
        const raw = localStorage.getItem(PENDING_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || !parsed.session_id) return null;
        return parsed;
    } catch (_) {
        return null;
    }
}

/**
 * POST /upload/session  →  { session_id, expires_at, max_chunk_bytes, ... }
 */
export async function createSession(mimeType = 'audio/webm') {
    const resp = await fetch(SESSION_BASE, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken(),
        },
        credentials: 'same-origin',
        body: JSON.stringify({ mime_type: mimeType }),
    });
    if (!resp.ok) {
        const body = await safeJson(resp);
        const err = new Error(body?.error || `Could not open recording session (HTTP ${resp.status})`);
        err.status = resp.status;
        err.body = body;
        throw err;
    }
    const data = await resp.json();
    rememberActiveSession(data.session_id, data.mime_type);
    return data;
}

/**
 * POST /upload/session/{id}/chunks/{N} with the raw blob body.
 * Returns true on 204, throws otherwise.
 */
export async function uploadChunk(sessionId, chunkIndex, blob) {
    const resp = await fetch(`${SESSION_BASE}/${encodeURIComponent(sessionId)}/chunks/${chunkIndex}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/octet-stream',
            'X-CSRFToken': csrfToken(),
        },
        credentials: 'same-origin',
        body: blob,
    });
    if (resp.status === 204) return { ok: true };
    if (resp.status === 409) {
        // Out of order; the body tells us the expected index so we can
        // resync. The caller decides whether to back off and replay.
        const body = await safeJson(resp);
        const err = new Error(body?.error || 'Chunk out of order');
        err.status = 409;
        err.expectedChunkIndex = body?.expected_chunk_index;
        err.body = body;
        throw err;
    }
    const body = await safeJson(resp);
    const err = new Error(body?.error || `Chunk POST failed (HTTP ${resp.status})`);
    err.status = resp.status;
    err.body = body;
    throw err;
}

/**
 * GET /upload/session/{id}  →  status payload from the server.
 */
export async function getSessionStatus(sessionId) {
    const resp = await fetch(`${SESSION_BASE}/${encodeURIComponent(sessionId)}`, {
        method: 'GET',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrfToken() },
    });
    if (resp.status === 404) return null;
    if (!resp.ok) {
        const body = await safeJson(resp);
        throw new Error(body?.error || `Could not read session status (HTTP ${resp.status})`);
    }
    return resp.json();
}

/**
 * POST /upload/session/{id}/finalize with the user's metadata.
 * Returns { recording_id, status } from the backend.
 */
export async function finalizeSession(sessionId, metadata = {}) {
    const resp = await fetch(`${SESSION_BASE}/${encodeURIComponent(sessionId)}/finalize`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken(),
        },
        credentials: 'same-origin',
        body: JSON.stringify(metadata || {}),
    });
    const body = await safeJson(resp);
    if (resp.status === 202) {
        forgetActiveSession();
        return body;
    }
    const err = new Error(body?.error || `Finalize failed (HTTP ${resp.status})`);
    err.status = resp.status;
    err.body = body;
    throw err;
}

/**
 * DELETE /upload/session/{id} to abort. 204 on success, 404 if gone.
 */
export async function abortSession(sessionId) {
    const resp = await fetch(`${SESSION_BASE}/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrfToken() },
    });
    forgetActiveSession();
    if (resp.status === 204 || resp.status === 404) return { ok: true };
    const body = await safeJson(resp);
    throw new Error(body?.error || `Abort failed (HTTP ${resp.status})`);
}

async function safeJson(resp) {
    try { return await resp.json(); } catch (_) { return null; }
}


/**
 * createUploader(sessionId, opts)
 *
 * Returns a queue helper that callers (audio composable's
 * MediaRecorder.ondataavailable) push blobs into. Chunks POST in order;
 * failures retry with exponential backoff (1s, 2s, 4s, 8s, 16s, capped
 * at 30s, max 6 attempts) and the queue blocks subsequent chunks until
 * the head succeeds — preserving on-disk ordering on the server.
 *
 *  const uploader = createUploader(sessionId);
 *  recorder.ondataavailable = (e) => uploader.enqueue(e.data);
 *  await uploader.drain();      // optional: wait for backlog to clear
 *  uploader.getBacklog();       // number of pending chunks
 *  uploader.lastError();        // most recent network/server error
 */
export function createUploader(sessionId, opts = {}) {
    const onError = opts.onError || (() => {});
    const onProgress = opts.onProgress || (() => {});
    const maxAttempts = opts.maxAttempts || 6;
    const baseDelayMs = opts.baseDelayMs || 1000;
    const maxDelayMs = opts.maxDelayMs || 30000;

    const queue = [];          // [{blob, attempts}]
    // 1-based, monotonic. On RESUME (a new MediaRecorder continuing an existing
    // session after a page reload), start from the server's next expected index
    // so the new segment's chunks append in order rather than colliding at 1.
    let nextIndex = opts.startIndex && opts.startIndex > 1 ? opts.startIndex : 1;
    let pumpRunning = false;
    let lastErrorObj = null;
    let drainResolvers = [];

    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const backoff = (attempts) => Math.min(maxDelayMs, baseDelayMs * Math.pow(2, attempts));

    async function pump() {
        if (pumpRunning) return;
        pumpRunning = true;
        try {
            while (queue.length > 0) {
                const head = queue[0];
                const indexForThis = nextIndex;
                try {
                    await uploadChunk(sessionId, indexForThis, head.blob);
                    queue.shift();
                    nextIndex++;
                    lastErrorObj = null;
                    onProgress({ uploaded: indexForThis, backlog: queue.length });
                } catch (e) {
                    head.attempts = (head.attempts || 0) + 1;
                    lastErrorObj = e;

                    // 409 out-of-order: re-sync nextIndex from the server's
                    // expected value, drop chunks already on disk, and retry.
                    if (e.status === 409 && Number.isInteger(e.expectedChunkIndex)) {
                        const expected = e.expectedChunkIndex;
                        // If the server expects an index we already passed,
                        // assume the server got the chunk and we lost the
                        // response. Drop the head and resume.
                        if (expected > indexForThis) {
                            queue.shift();
                            nextIndex = expected;
                            continue;
                        }
                        // Otherwise the server expects fewer than we have;
                        // realign nextIndex and retry.
                        nextIndex = expected;
                        continue;
                    }

                    if (head.attempts >= maxAttempts) {
                        // Give up on this chunk; surface the error so the
                        // caller can decide (typically: pause recording and
                        // surface a banner).
                        onError({
                            error: e,
                            chunkIndex: indexForThis,
                            droppedFromQueue: true,
                        });
                        queue.shift();
                        nextIndex++;
                        continue;
                    }

                    onError({ error: e, chunkIndex: indexForThis, droppedFromQueue: false });
                    await sleep(backoff(head.attempts));
                }
            }
        } finally {
            pumpRunning = false;
            const resolvers = drainResolvers;
            drainResolvers = [];
            resolvers.forEach(r => r());
        }
    }

    return {
        enqueue(blob) {
            queue.push({ blob, attempts: 0 });
            // Fire-and-forget pump
            pump();
        },
        drain() {
            if (queue.length === 0 && !pumpRunning) return Promise.resolve();
            return new Promise(resolve => { drainResolvers.push(resolve); });
        },
        getBacklog() { return queue.length; },
        lastError() { return lastErrorObj; },
        sessionId,
    };
}
