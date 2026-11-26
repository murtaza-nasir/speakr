/**
 * The version of a recording's stored audio in the address of its players.
 *
 * The stored file is replaced under the same recording id by the convert job
 * of an upload (#543, #699), a trim in the transcript editor (#660), a remux
 * and the legacy video extraction of a reprocess. The browser's media cache
 * keeps the old file under an unchanged address, and a player that asks for
 * more of the old file (a range with If-Range) is answered with the whole new
 * file and stalls (#707). The players therefore address the audio as
 * <route>?v=<audio_revision>: the server gives every recording payload its
 * audio_revision, and a new revision is a new address. A player that still
 * continues an old revision is refused with a 410 (src/utils/mime.py), and
 * is loaded again under the current one.
 *
 * When the address changes, Vue sets the new src and the browser loads it
 * from the start. The position and the playing state are read before that
 * (holdPlayback) and given back once the new file has its metadata
 * (resumePlayback).
 */

/**
 * The address of a recording's audio for its players.
 *
 * @param {string} route e.g. '/audio/7' or '/share/audio/abc'
 * @param {string|null|undefined} revision the recording's audio_revision
 * @returns {string}
 */
export function audioAddress(route, revision) {
    return revision ? `${route}?v=${encodeURIComponent(revision)}` : route;
}

/** Whether a player's src attribute is the audio of `route`, with or without a version. */
export function isAddressOf(src, route) {
    return src === route || (typeof src === 'string' && src.startsWith(`${route}?`));
}

/**
 * The revision the players keep after a refreshed copy of the recording:
 * the copy's own, when it reports one, otherwise the one they have.
 */
export function revisionAfter(current, next) {
    if (next && Object.prototype.hasOwnProperty.call(next, 'audio_revision')) {
        return next.audio_revision ?? null;
    }
    return current ?? null;
}

const players = (route, root) => {
    if (!root) return [];
    return Array.from(root.querySelectorAll('audio, video'))
        .filter((media) => isAddressOf(media.getAttribute('src'), route));
};

/**
 * The position and playing state of every player of `route`, read before
 * its address changes.
 *
 * @returns {{route: string, time: number, playing: Set<Element>, root: *}}
 */
export function holdPlayback(route, root = (typeof document === 'undefined' ? null : document)) {
    const found = players(route, root);
    const playing = new Set(found.filter((media) => !media.paused && !media.ended));
    // The position of a playing player first: a muted dock follows the main one
    const lead = found.find((media) => playing.has(media)) || found[0];
    const time = lead && Number.isFinite(lead.currentTime) ? lead.currentTime : 0;
    return { route, time, playing, root };
}

/**
 * Give the players of the held route their position back once the new
 * file is loaded, and play again those that were playing. A player that
 * was replaced (audio became video) gets the position, not the playing
 * state.
 *
 * @returns {number} how many players were given their position
 */
export function resumePlayback(held) {
    if (!held || !held.root) return 0;
    const found = players(held.route, held.root);
    for (const media of found) {
        const resume = () => {
            const end = Number.isFinite(media.duration) && media.duration > 0 ? media.duration : Infinity;
            if (held.time > 0) {
                try { media.currentTime = Math.min(held.time, end); } catch (e) { /* not seekable yet */ }
            }
            if (held.playing.has(media)) {
                const started = media.play();
                if (started && typeof started.catch === 'function') started.catch(() => {});
            }
        };
        if (media.readyState >= 1) resume();
        else media.addEventListener('loadedmetadata', resume, { once: true });
    }
    return found.length;
}

/**
 * After a player of `route` reported an error: the current revision from
 * `fetchRevision` (null for a recording without audio), when it differs
 * from the one the players have; else undefined. One question at a time
 * per route.
 */
const asking = new Set();
export async function replacedRevision(route, current, fetchRevision) {
    if (asking.has(route)) return undefined;
    asking.add(route);
    try {
        const latest = await fetchRevision();
        if (latest === undefined || latest === (current ?? null)) return undefined;
        return latest;
    } catch (e) {
        return undefined;
    } finally {
        asking.delete(route);
    }
}
