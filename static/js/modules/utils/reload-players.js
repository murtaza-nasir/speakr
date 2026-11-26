/**
 * Load a recording's stored media again in every player of the page.
 *
 * The stored file is replaced by a trim in the transcript editor (#660) and
 * by the convert job that keeps an upload's span (#699). The address gets a
 * version: the browser's media cache keeps the old file under the plain one.
 * Vue leaves the attribute alone, as its bound value is unchanged.
 *
 * A player already on this version is left alone, so the editor's reload and
 * the reload that follows the refreshed recording load the file once.
 *
 * @param {number|string} recordingId
 * @param {number|string} [version] the stored size, or a time when it is unknown
 * @param {Document} [root]
 * @returns {number} how many players were loaded again
 */
export function reloadPlayers(recordingId, version = Date.now(), root = (typeof document === 'undefined' ? null : document)) {
    if (!root) return 0;
    const target = `/audio/${recordingId}?v=${version}`;
    let reloaded = 0;
    for (const media of root.querySelectorAll('audio, video')) {
        const src = media.getAttribute('src') || '';
        if (src === target) continue;
        if (src === `/audio/${recordingId}` || src.startsWith(`/audio/${recordingId}?`)) {
            try {
                media.setAttribute('src', target);
                media.load();
                reloaded += 1;
            } catch (e) { /* not loaded yet */ }
        }
    }
    return reloaded;
}

/**
 * Whether a refreshed copy of the open recording names another stored file
 * than the copy before it: its size changed.
 */
export function storedFileReplaced(previous, next) {
    if (!previous || !next || previous.id !== next.id) return false;
    const before = Number(previous.file_size);
    const after = Number(next.file_size);
    return Number.isFinite(before) && before > 0 && Number.isFinite(after) && after > 0 && before !== after;
}
