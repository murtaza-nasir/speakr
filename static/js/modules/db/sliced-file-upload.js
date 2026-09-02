/**
 * Sliced upload of a file the user already has on disk.
 *
 * Speakr's normal upload is one multipart POST of the whole file, so a
 * reverse proxy with a body-size limit below the file size (Cloudflare's
 * 100 MB, for instance) rejects it before Speakr sees it. This module
 * sends the same file as fixed-size byte slices through the
 * recording-session endpoints instead, then POSTs the upload form to
 * /finalize-upload, which byte-joins the slices server-side and ingests
 * the result exactly as POST /upload would.
 *
 * SLICE_BYTES mirrors the server's RECORDING_SESSION_MAX_CHUNK_BYTES
 * default; the create response is authoritative, so a deployment that
 * lowered that env var lowers the slice size with it.
 *
 * It deliberately does NOT reuse server-recording-sessions.js's
 * createSession / abortSession: those maintain the recorder's
 * localStorage crash-recovery marker, which a file upload must not
 * touch. The slice POSTs go through XMLHttpRequest rather than fetch so
 * the progress bar moves within a slice and not just between slices.
 *
 * File slices are Blob views, so nothing here holds the file in memory:
 * a multi-gigabyte upload costs one slice's worth of buffering in the
 * browser's network stack.
 *
 * An upload interrupted by the network resumes rather than restarting:
 * the session id is kept in localStorage under the file's identity
 * (name, size, lastModified), and re-adding the same file continues from
 * the slice count the server reports. Only a rejection the server will
 * repeat (an oversize slice, an exhausted quota, a size mismatch) drops
 * the session, because retrying it would fail the same way.
 */

import { getUploadCsrfToken, isCsrfRejection } from '../csrf.js';
import { computeUploadTimeout } from '../utils/upload-timeout.js';

const SESSION_BASE = '/upload/session';

export const SLICE_BYTES = 16 * 1024 * 1024;

const MAX_SLICE_ATTEMPTS = 6;
const MAX_STALLED_RESYNCS = 2;
const BASE_RETRY_MS = 1000;
const MAX_RETRY_MS = 30000;

const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527]);

const INCONCLUSIVE_STATUSES = new Set([408, 429, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527]);

const INGEST_POLL_MS = 3000;
const INGEST_WAIT_MS = 10 * 60 * 1000;
const MAX_FINALIZE_ATTEMPTS = 3;

const RESUME_KEY = 'speakr.slicedUploads';
const RESUME_TTL_MS = 24 * 60 * 60 * 1000;
const RESUME_MAX_ENTRIES = 20;

/** True when the file is large enough that slicing it is worth a session. */
export function shouldSliceUpload(file) {
    return !!file && file.size > SLICE_BYTES;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Identity of the file on this machine. `lastModified` is in it so an
 * edited or re-exported file of the same name and size is not resumed
 * onto slices of the older one.
 */
function fileKey(file) {
    return `${file.name}|${file.size}|${file.lastModified || 0}`;
}

/** Open sessions by file key. Best-effort: private mode has no store. */
function readResumeMemory() {
    try {
        const parsed = JSON.parse(localStorage.getItem(RESUME_KEY) || '{}');
        const fresh = {};
        for (const [key, entry] of Object.entries(parsed)) {
            if (entry?.sessionId && Date.now() - (entry.savedAt || 0) < RESUME_TTL_MS) {
                fresh[key] = entry;
            }
        }
        return fresh;
    } catch (_) {
        return {};
    }
}

function writeResumeMemory(memory) {
    try {
        const entries = Object.entries(memory)
            .sort((a, b) => (b[1].savedAt || 0) - (a[1].savedAt || 0))
            .slice(0, RESUME_MAX_ENTRIES);
        localStorage.setItem(RESUME_KEY, JSON.stringify(Object.fromEntries(entries)));
    } catch (_) { /* private mode / quota: resuming is a bonus, not a requirement */ }
}

function rememberSession(file, sessionId, sliceBytes) {
    const memory = readResumeMemory();
    memory[fileKey(file)] = { sessionId, sliceBytes, savedAt: Date.now() };
    writeResumeMemory(memory);
}

export function forgetSession(file) {
    const memory = readResumeMemory();
    delete memory[fileKey(file)];
    writeResumeMemory(memory);
}

/** The server's view of a session, or null when it no longer has one. */
async function readSessionStatus(sessionId, token) {
    const response = await fetch(`${SESSION_BASE}/${encodeURIComponent(sessionId)}`, {
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': token },
    });
    if (!response.ok) return null;
    return parseJson(await response.text());
}

/**
 * The session we opened for this file, when it can still be continued.
 * Returns `{sessionId, sliceBytes, chunkCount, status}` or null, and
 * forgets anything the server no longer recognises.
 *
 * `finalizing` counts as continuable: it is what an ingest that outran
 * the proxy's patience looks like from here, and the caller waits it out
 * rather than sending the file again.
 */
async function resumableSession(file, token) {
    const remembered = readResumeMemory()[fileKey(file)];
    if (!remembered) return null;

    let status;
    try {
        status = await readSessionStatus(remembered.sessionId, token);
    } catch (error) {
        console.warn('[SlicedUpload] Could not read the remembered session:', error);
        return null;
    }

    const continuable = status
        && status.kind === 'sliced_upload'
        && status.upload_total_bytes === file.size
        && ['recording', 'finalizing', 'finalized'].includes(status.status)
        && remembered.sliceBytes > 0;
    if (!continuable) {
        forgetSession(file);
        return null;
    }

    return {
        sessionId: remembered.sessionId,
        sliceBytes: remembered.sliceBytes,
        chunkCount: status.chunk_count || 0,
        status: status.status,
    };
}

const retryDelay = (attempts) =>
    Math.min(MAX_RETRY_MS, BASE_RETRY_MS * Math.pow(2, attempts - 1));

function parseJson(text) {
    try { return JSON.parse(text); } catch (_) { return null; }
}

/**
 * POST via XHR, resolving for every HTTP status so callers can decide
 * what is retryable. Rejects only on a network error or timeout, where
 * there is no status and a retry is the right answer.
 */
function xhrPost(url, body, { token, timeoutMs, onProgress, contentType, onXhr }) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', url);
        if (token) xhr.setRequestHeader('X-CSRFToken', token);
        if (contentType) xhr.setRequestHeader('Content-Type', contentType);
        xhr.timeout = timeoutMs;
        if (onProgress) {
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) onProgress(e.loaded, e.total);
            };
        }
        xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText });
        xhr.onerror = () => reject(new Error('Network error during upload'));
        xhr.ontimeout = () => reject(new Error('Upload timed out'));
        xhr.onabort = () => reject(new Error('Upload cancelled'));
        if (onXhr) onXhr(xhr);
        xhr.send(body);
    });
}

async function createFileSession(file, token) {
    const response = await fetch(SESSION_BASE, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
        credentials: 'same-origin',
        body: JSON.stringify({
            filename: file.name,
            mime_type: file.type || '',
            total_bytes: file.size,
        }),
    });
    const body = parseJson(await response.text());
    if (response.status !== 201 || !body?.session_id) {
        throw new Error(body?.error || `Could not open upload session (HTTP ${response.status})`);
    }
    if (!body.upload_filename) {
        throw new Error('Server does not support sliced uploads');
    }
    return body;
}

async function deleteFileSession(sessionId, token) {
    try {
        await fetch(`${SESSION_BASE}/${encodeURIComponent(sessionId)}`, {
            method: 'DELETE',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': token },
        });
    } catch (error) {
        console.warn('[SlicedUpload] Could not abort session:', error);
    }
}

/**
 * Send one slice, retrying transport failures and refreshing the CSRF
 * token when the server rejects it. Returns the index the server expects
 * next, which is normally `index + 1` but can jump when a 409 reveals
 * the server already holds slices whose responses we lost.
 *
 * Failures that outlive the retries are marked resumable when nothing
 * about them says the next attempt would fail too: the transport, and
 * the statuses a gateway returns while something behind it restarts.
 */
async function sendSlice(sessionId, index, blob, tokenRef, options) {
    const url = `${SESSION_BASE}/${encodeURIComponent(sessionId)}/chunks/${index}`;
    for (let attempt = 1; ; attempt++) {
        let result;
        try {
            result = await xhrPost(url, blob, {
                token: tokenRef.token,
                timeoutMs: computeUploadTimeout(blob.size),
                contentType: 'application/octet-stream',
                onProgress: options.onSliceProgress,
                onXhr: options.onXhr,
            });
        } catch (transportError) {
            if (attempt >= MAX_SLICE_ATTEMPTS) {
                transportError.resumable = true;
                throw transportError;
            }
            await sleep(retryDelay(attempt));
            continue;
        }

        if (result.status === 204) return index + 1;

        const body = parseJson(result.text);

        if (result.status === 409 && Number.isInteger(body?.expected_chunk_index)) {
            return body.expected_chunk_index;
        }

        if (isCsrfRejection(result.status, result.text) && attempt < MAX_SLICE_ATTEMPTS) {
            tokenRef.token = await getUploadCsrfToken();
            continue;
        }

        const error = new Error(sliceRejectionMessage(index, blob.size, result, body));
        error.status = result.status;
        if (!RETRYABLE_STATUSES.has(result.status)) throw error;
        if (attempt >= MAX_SLICE_ATTEMPTS) {
            error.resumable = true;
            throw error;
        }
        await sleep(retryDelay(attempt));
    }
}

/**
 * A 413 on a slice is the reverse proxy, not Speakr: its body limit is
 * below the slice size. Naming it the way a proxy does lets the upload
 * composable's error mapping give the user the client_max_body_size
 * guidance instead of a generic processing error.
 */
function sliceRejectionMessage(index, sliceSize, result, body) {
    if (result.status === 413 && !body?.error) {
        const megabytes = Math.round(sliceSize / (1024 * 1024));
        return `Request entity too large: the reverse proxy rejected a ${megabytes} MB upload slice`;
    }
    return body?.error || `Slice ${index} rejected (HTTP ${result.status})`;
}

/**
 * True when the session has to outlive a failed finalize.
 *
 * Covers the statuses that leave the outcome unknown, and the 409 that
 * says another request holds the finalize claim. Cloudflare answers 524
 * at 125 seconds while the origin carries on ingesting, which is the
 * case that matters: the retry is then the replay that hands back the
 * recording rather than a second upload of the same file.
 *
 * Guessing wrong in this direction is cheap. The resume check reads the
 * session's real state first and only continues one the server still
 * has, so a session that did fail costs one request to discover it.
 */
function finalizeCouldStillLand(status) {
    return INCONCLUSIVE_STATUSES.has(status) || status === 409;
}

/**
 * Turn the uploaded slices into a recording.
 *
 * A transport failure or an inconclusive status here is marked resumable:
 * the server may be ingesting at that moment, so the slices have to
 * survive, and a retry gets back the recording that call produced rather
 * than sending the file a second time.
 */
async function finalize(sessionId, formData, tokenRef, options) {
    const url = `${SESSION_BASE}/${encodeURIComponent(sessionId)}/finalize-upload`;
    for (let attempt = 1; ; attempt++) {
        let result;
        try {
            result = await xhrPost(url, formData, {
                token: tokenRef.token,
                timeoutMs: options.finalizeTimeoutMs,
                onXhr: options.onXhr,
            });
        } catch (transportError) {
            transportError.resumable = true;
            throw transportError;
        }
        const body = parseJson(result.text);
        if (result.status >= 200 && result.status < 300 && body?.id) return body;
        if (isCsrfRejection(result.status, result.text) && attempt === 1) {
            tokenRef.token = await getUploadCsrfToken();
            continue;
        }
        const error = new Error(body?.error
            || `Upload could not be finalized (HTTP ${result.status})`);
        error.status = result.status;
        error.resumable = finalizeCouldStillLand(result.status);
        throw error;
    }
}

/**
 * Settle a finalize whose answer never arrived.
 *
 * The ingest runs in-request, so Cloudflare's 125-second Proxy Read
 * Timeout can cut the answer off while the origin carries on and
 * produces the recording anyway. Rather than report a failure the user
 * has to interpret, wait the session out: once it leaves `finalizing`,
 * asking again either replays the recording that landed (`finalized`) or
 * finalizes for real (`recording`, meaning the request never reached
 * Speakr at all). Only when the wait runs out does `firstError` surface,
 * with the session left in place for another attempt.
 */
async function settleUnfinishedFinalize(sessionId, formData, tokenRef, options, firstError) {
    const deadline = Date.now() + INGEST_WAIT_MS;
    let finalizeAttempts = 1;

    while (Date.now() < deadline) {
        let status;
        try {
            status = await readSessionStatus(sessionId, tokenRef.token);
        } catch (_) {
            status = undefined;
        }

        if (status === null) throw firstError;

        if (status?.status === 'finalizing' || status === undefined) {
            await sleep(INGEST_POLL_MS);
            continue;
        }

        if (['recording', 'finalized'].includes(status.status)
            && ++finalizeAttempts <= MAX_FINALIZE_ATTEMPTS) {
            return await finalize(sessionId, formData, tokenRef, options);
        }

        throw firstError;
    }

    throw firstError;
}

/**
 * Upload `file` in slices and finalize with `formData` (the same form the
 * single-shot path builds, minus the file part). Resolves to the created
 * recording, matching what POST /upload returns.
 *
 * An interrupted upload of the same file continues where it stopped. A
 * failure the server would repeat takes the session down with it; one
 * that might not (the network, a timeout) leaves it for the next attempt.
 *
 * `onProgress` is called with a 0..1 fraction of bytes accepted by the
 * server. `onXhr` receives each in-flight XHR so a caller can cancel.
 */
export async function uploadFileInSlices(file, formData, options = {}) {
    const tokenRef = { token: await getUploadCsrfToken() };
    const onProgress = options.onProgress || (() => {});

    const resumed = await resumableSession(file, tokenRef.token);
    let sessionId = resumed?.sessionId;
    let sliceBytes = resumed?.sliceBytes;
    if (!resumed) {
        const created = await createFileSession(file, tokenRef.token);
        sessionId = created.session_id;
        sliceBytes = Math.min(SLICE_BYTES, created.max_chunk_bytes || SLICE_BYTES);
        rememberSession(file, sessionId, sliceBytes);
    }

    try {
        let index = (resumed?.chunkCount || 0) + 1;
        let highWaterMark = index;
        let stalledResyncs = 0;
        onProgress(Math.min(1, ((index - 1) * sliceBytes) / file.size));
        while ((index - 1) * sliceBytes < file.size) {
            const start = (index - 1) * sliceBytes;
            const end = Math.min(start + sliceBytes, file.size);
            index = await sendSlice(sessionId, index, file.slice(start, end), tokenRef, {
                onXhr: options.onXhr,
                onSliceProgress: (loaded) => onProgress(Math.min(1, (start + loaded) / file.size)),
            });
            if (index > highWaterMark) {
                highWaterMark = index;
                stalledResyncs = 0;
            } else if (++stalledResyncs > MAX_STALLED_RESYNCS) {
                throw new Error(
                    `Server keeps asking for slice ${index} after accepting it; `
                    + 'its slice bookkeeping is not advancing');
            }
            onProgress(Math.min(1, ((index - 1) * sliceBytes) / file.size));
        }
        const finalizeOptions = {
            finalizeTimeoutMs: computeUploadTimeout(file.size),
            onXhr: options.onXhr,
        };
        let recording;
        try {
            recording = await finalize(sessionId, formData, tokenRef, finalizeOptions);
        } catch (error) {
            if (!error.resumable) throw error;
            recording = await settleUnfinishedFinalize(
                sessionId, formData, tokenRef, finalizeOptions, error);
        }
        forgetSession(file);
        return recording;
    } catch (error) {
        if (error.resumable) throw error;
        await deleteFileSession(sessionId, tokenRef.token);
        forgetSession(file);
        throw error;
    }
}
