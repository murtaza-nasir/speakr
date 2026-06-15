/**
 * Upload timeout calculation.
 *
 * XMLHttpRequest has no timeout by default (xhr.timeout === 0), so a TCP
 * connection that stalls mid-stream — no FIN, no RST, just no progress — sits
 * open indefinitely and neither onerror nor ontimeout ever fires. The upload
 * then never reaches the catch block that persists the file to IndexedDB /
 * triggers the local-download fallback, so a stalled upload is a silent
 * data-loss path. We set an explicit, size-scaled ceiling so a stalled upload
 * eventually fails over into that recovery path instead of hanging forever.
 */

const MINUTE_MS = 60 * 1000;

// Floor for any upload, regardless of size.
const BASE_TIMEOUT_MS = 10 * MINUTE_MS;

// Additional allowance per 10 MB of payload, so large recordings that simply
// take a long time to transfer over a slow link are not killed prematurely.
const MS_PER_10MB = 1 * MINUTE_MS;

const BYTES_PER_10MB = 10 * 1024 * 1024;

/**
 * Compute the XHR timeout (in milliseconds) for an upload of the given size.
 * @param {number} fileSizeBytes - size of the file being uploaded, in bytes.
 * @returns {number} timeout in milliseconds (always >= BASE_TIMEOUT_MS).
 */
export function computeUploadTimeout(fileSizeBytes) {
    const size = Number(fileSizeBytes);
    if (!Number.isFinite(size) || size <= 0) {
        return BASE_TIMEOUT_MS;
    }
    return Math.round(BASE_TIMEOUT_MS + (size / BYTES_PER_10MB) * MS_PER_10MB);
}

export const UPLOAD_TIMEOUT_CONSTANTS = Object.freeze({
    MINUTE_MS,
    BASE_TIMEOUT_MS,
    MS_PER_10MB,
    BYTES_PER_10MB,
});
