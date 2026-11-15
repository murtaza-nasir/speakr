/**
 * Waveform peaks for the timeline of the segment editor.
 *
 * The server computes them once per audio file (GET /audio/<id>/peaks,
 * src/services/waveform_peaks.py): min/max pairs as int8 at several
 * resolutions. parsePeaks() reads the file, levelFor() picks the level to
 * draw from at a zoom, quietNear() finds a pause to snap to, and
 * loadPeaks() fetches the file, asking again while the server computes it.
 *
 * Everything but loadPeaks() is pure.
 */

const MAGIC = [0x53, 0x50, 0x4b, 0x31];       // "SPK1"
const QUIET_LEVEL = 10;                       // of 127: at or below this a bucket is quiet
const QUIET_SECONDS = 0.04;                   // a pause is at least this long
const QUIET_SCAN = 4000;                      // buckets looked at per search, at most

/**
 * Read a peaks file. Returns { duration, levels: [{ rate, length, data }] }
 * with the finest level first, or null when the bytes are not a peaks file.
 * `rate` is buckets per second; `data` is an Int8Array of (min, max) pairs.
 */
export function parsePeaks(buffer) {
    if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 8) return null;
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < 4; i++) if (bytes[i] !== MAGIC[i]) return null;
    const headerLength = new DataView(buffer).getUint32(4, true);
    const dataStart = 8 + headerLength;
    if (dataStart > buffer.byteLength) return null;
    let header;
    try {
        header = JSON.parse(new TextDecoder('utf-8').decode(bytes.subarray(8, dataStart)));
    } catch (e) {
        return null;
    }
    if (!header || !Array.isArray(header.levels)) return null;
    const levels = [];
    for (const level of header.levels) {
        const { rate, length, offset } = level || {};
        if (!(rate > 0) || !Number.isInteger(length) || !Number.isInteger(offset) || length < 0 || offset < 0) return null;
        if (dataStart + offset + length * 2 > buffer.byteLength) return null;
        if (length > 0) levels.push({ rate, length, data: new Int8Array(buffer, dataStart + offset, length * 2) });
    }
    if (!levels.length) return null;
    levels.sort((a, b) => b.rate - a.rate);
    return { duration: Number(header.duration) || levels[0].length / levels[0].rate, levels };
}

/**
 * Index of the level to draw from at `pps` pixels per second: the coarsest
 * one that still has a bucket per pixel, else the finest.
 */
export function levelFor(levels, pps) {
    let pick = 0;
    for (let i = 1; i < levels.length; i++) {
        if (levels[i].rate >= pps) pick = i; else break;
    }
    return pick;
}

/**
 * The time of the quiet point nearest to t within `tolerance` seconds: the
 * middle of a pause (a run of quiet buckets of the finest level, between
 * words or between speakers). NaN when there is none.
 */
export function quietNear(peaks, t, tolerance) {
    if (!peaks || !peaks.levels.length || !(tolerance > 0)) return NaN;
    const { rate, length, data } = peaks.levels[0];
    const isQuiet = (i) => -data[i * 2] <= QUIET_LEVEL && data[i * 2 + 1] <= QUIET_LEVEL;
    let first = Math.max(0, Math.floor((t - tolerance) * rate));
    let last = Math.min(length - 1, Math.ceil((t + tolerance) * rate));
    if (first > last) return NaN;
    if (last - first > QUIET_SCAN) {
        const middle = Math.round(t * rate);
        first = Math.max(first, middle - QUIET_SCAN / 2);
        last = Math.min(last, middle + QUIET_SCAN / 2);
    }
    // A pause that reaches out of the window is followed to its ends
    for (let n = 0; first > 0 && n < QUIET_SCAN && isQuiet(first); n++) first--;
    for (let n = 0; last < length - 1 && n < QUIET_SCAN && isQuiet(last); n++) last++;
    const needed = Math.max(1, Math.round(QUIET_SECONDS * rate));
    let best = NaN;
    let bestDistance = Infinity;
    let runStart = -1;
    for (let i = first; i <= last + 1; i++) {
        if (i <= last && isQuiet(i)) {
            if (runStart < 0) runStart = i;
        } else if (runStart >= 0) {
            if (i - runStart >= needed) {
                const middle = (runStart + i) / 2 / rate;
                const distance = Math.abs(middle - t);
                if (distance < bestDistance) { bestDistance = distance; best = middle; }
            }
            runStart = -1;
        }
    }
    return bestDistance <= tolerance ? Math.round(best * 1000) / 1000 : NaN;
}

/**
 * Fetch the peaks of a recording. The server answers 202 while it computes
 * them; this asks again until they are there. Resolves to the parsed peaks,
 * or null when there are none (no audio, cannot be decoded, not allowed) or
 * `signal` was aborted. `onPending` is called once when the first answer is 202.
 */
export async function loadPeaks(recordingId, { signal = null, onPending = null, fetchFn = null, sleep = null, maxWaitMs = 30 * 60 * 1000 } = {}) {
    const get = fetchFn || ((url, options) => fetch(url, options));
    const wait = sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    const started = Date.now();
    let told = false;
    let delay = 1000;
    try {
        for (;;) {
            if (signal && signal.aborted) return null;
            const response = await get(`/audio/${recordingId}/peaks`, { signal, credentials: 'same-origin' });
            if (response.status === 200) return parsePeaks(await response.arrayBuffer());
            if (response.status !== 202) return null;
            if (!told && onPending) { told = true; onPending(); }
            if (Date.now() - started > maxWaitMs) return null;
            await wait(delay);
            delay = Math.min(5000, Math.round(delay * 1.5));
        }
    } catch (error) {
        return null;
    }
}
