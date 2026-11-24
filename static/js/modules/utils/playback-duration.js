/**
 * The length the player shows, and the time a seek is allowed to land on.
 *
 * An upload answers before the cut. That first recording still carries the
 * whole file's length. The kept file is shorter, and a later reading of the
 * same recording must replace the length the player already shows. A seek
 * past the file makes the browser jump back to the start.
 */

/**
 * URL of the stored media. The query carries the stored size, so replacing
 * the file (a deferred video cut) changes the URL and the player loads the
 * new bytes instead of keeping the whole upload it fetched first.
 */
export function recordingMediaUrl(recording, options = {}) {
    if (!recording || recording.id == null || recording.id === '' || recording.id === 'incognito') {
        return '';
    }
    const params = [];
    if (options.download) params.push('download=true');
    const size = Number(recording.file_size);
    if (Number.isFinite(size) && size > 0) params.push(`v=${size}`);
    const query = params.length ? `?${params.join('&')}` : '';
    return `/audio/${recording.id}${query}`;
}

export function playedDuration(current, incoming) {
    const next = Number(incoming);
    if (!Number.isFinite(next) || next <= 0) return current || 0;
    return next;
}

/**
 * The latest moment that still lies in both the length on screen and the
 * file the browser has. A missing length does not extend the seek.
 */
export function seekableTime(time, shown, media) {
    if (!Number.isFinite(time)) return null;
    let limit = time;
    if (Number.isFinite(shown) && shown > 0) limit = Math.min(limit, shown);
    if (Number.isFinite(media) && media > 0) limit = Math.min(limit, Math.max(0, media - 0.05));
    return Math.max(0, limit);
}
