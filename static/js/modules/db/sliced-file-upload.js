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
 */

import { getUploadCsrfToken, isCsrfRejection } from '../csrf.js';
import { computeUploadTimeout } from '../utils/upload-timeout.js';

const SESSION_BASE = '/upload/session';

export const SLICE_BYTES = 16 * 1024 * 1024;

const MAX_SLICE_ATTEMPTS = 6;
const BASE_RETRY_MS = 1000;
const MAX_RETRY_MS = 30000;

const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

/** True when the file is large enough that slicing it is worth a session. */
export function shouldSliceUpload(file) {
    return !!file && file.size > SLICE_BYTES;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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
        body: JSON.stringify({ filename: file.name, mime_type: file.type || '' }),
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
            if (attempt >= MAX_SLICE_ATTEMPTS) throw transportError;
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
        if (!RETRYABLE_STATUSES.has(result.status) || attempt >= MAX_SLICE_ATTEMPTS) throw error;
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

async function finalize(sessionId, formData, tokenRef, options) {
    const url = `${SESSION_BASE}/${encodeURIComponent(sessionId)}/finalize-upload`;
    for (let attempt = 1; ; attempt++) {
        const result = await xhrPost(url, formData, {
            token: tokenRef.token,
            timeoutMs: options.finalizeTimeoutMs,
            onXhr: options.onXhr,
        });
        const body = parseJson(result.text);
        if (result.status === 202 && body?.id) return body;
        if (isCsrfRejection(result.status, result.text) && attempt === 1) {
            tokenRef.token = await getUploadCsrfToken();
            continue;
        }
        throw new Error(body?.error
            || `Upload could not be finalized (HTTP ${result.status})`);
    }
}

/**
 * Upload `file` in slices and finalize with `formData` (the same form the
 * single-shot path builds, minus the file part). Resolves to the created
 * recording, matching what POST /upload returns.
 *
 * `onProgress` is called with a 0..1 fraction of bytes accepted by the
 * server. `onXhr` receives each in-flight XHR so a caller can cancel.
 */
export async function uploadFileInSlices(file, formData, options = {}) {
    const tokenRef = { token: await getUploadCsrfToken() };
    const session = await createFileSession(file, tokenRef.token);
    const sliceBytes = Math.min(SLICE_BYTES, session.max_chunk_bytes || SLICE_BYTES);
    const onProgress = options.onProgress || (() => {});

    try {
        let index = 1;
        while ((index - 1) * sliceBytes < file.size) {
            const start = (index - 1) * sliceBytes;
            const end = Math.min(start + sliceBytes, file.size);
            index = await sendSlice(session.session_id, index, file.slice(start, end), tokenRef, {
                onXhr: options.onXhr,
                onSliceProgress: (loaded) => onProgress(Math.min(1, (start + loaded) / file.size)),
            });
            onProgress(Math.min(1, ((index - 1) * sliceBytes) / file.size));
        }
        return await finalize(session.session_id, formData, tokenRef, {
            finalizeTimeoutMs: computeUploadTimeout(file.size),
            onXhr: options.onXhr,
        });
    } catch (error) {
        await deleteFileSession(session.session_id, tokenRef.token);
        throw error;
    }
}
