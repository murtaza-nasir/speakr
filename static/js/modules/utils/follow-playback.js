/**
 * Following the player in a transcript (the public share page).
 *
 * Pure logic, no DOM and no Vue. While the audio plays, the transcript
 * keeps the playing segment in view. That stops as soon as the reader moves
 * the transcript themselves, so the page never pulls them away from what
 * they are reading; a control then offers to follow again.
 */

/**
 * Which segment is playing at a time: the last one that has begun.
 * `segments` have startTime in seconds, in order; a segment without a time
 * is never the playing one. at(time) gives its index, or -1 before the
 * first.
 */
export function createPlaybackIndex(segments) {
    const index = [];
    const starts = [];
    (segments || []).forEach((segment, i) => {
        const start = segment ? segment.startTime : null;
        if (typeof start === 'number' && isFinite(start)) {
            index.push(i);
            starts.push(start);
        }
    });
    return {
        at(time) {
            if (typeof time !== 'number' || !isFinite(time)) return -1;
            let lo = 0;
            let hi = starts.length - 1;
            let found = -1;
            while (lo <= hi) {
                const mid = (lo + hi) >> 1;
                if (starts[mid] <= time) {
                    found = mid;
                    lo = mid + 1;
                } else {
                    hi = mid - 1;
                }
            }
            return found < 0 ? -1 : index[found];
        },
    };
}

const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ', 'Spacebar']);

/** Whether a key press in the transcript moves it. */
export function isScrollKey(event) {
    if (!event || event.ctrlKey || event.metaKey || event.altKey) return false;
    return SCROLL_KEYS.has(event.key);
}

/**
 * The state of following.
 *
 *   playing(index)   the playing segment is now `index` (-1 for none);
 *                    returns the segment to bring into view, or null
 *   moved()          the reader moved the transcript: stop following
 *   resume()         follow again; returns the segment to bring into view, or null
 *   following        whether the playing segment is kept in view
 *   paused           whether to offer the control: not following, with a
 *                    segment playing
 */
export function createFollow() {
    let following = true;
    let current = -1;
    return {
        get following() { return following; },
        get current() { return current; },
        get paused() { return !following && current >= 0; },
        playing(index) {
            const next = typeof index === 'number' && index >= 0 ? index : -1;
            const changed = next !== current;
            current = next;
            return following && changed && current >= 0 ? current : null;
        },
        moved() {
            following = false;
        },
        resume() {
            following = true;
            return current >= 0 ? current : null;
        },
    };
}
