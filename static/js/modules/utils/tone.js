/**
 * Voice tone on transcript lines.
 *
 * An external scorer supplies `recording.tone` (see src/services/tone.py): windows of speech, each
 * with per-state scores and, when something stood out, a `standout`. This module turns that into a
 * small badge on the FIRST line of each standout window. Lines find their window by time, so a
 * line that was split or merged still resolves. With no tone data every function returns its input
 * untouched, so recordings without tone render exactly as before.
 */

const STORAGE_KEY = 'speakr.tone.show';

export function loadShowTone() {
    try {
        return localStorage.getItem(STORAGE_KEY) !== 'off';
    } catch (e) {
        return true;
    }
}

export function saveShowTone(show) {
    try {
        localStorage.setItem(STORAGE_KEY, show ? 'on' : 'off');
    } catch (e) { /* private mode: the choice just isn't remembered */ }
}

function clock(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function titleCase(text) {
    return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Top three states for a window, each with a bar 0..1 measured against that state's "notable" floor. */
function topStates(window, states) {
    if (!states) return [];
    return Object.entries(states)
        .filter(([head]) => typeof window.scores[head] === 'number')
        .map(([head, [label, emoji, floor]]) => ({
            head, label: titleCase(label), emoji, ratio: window.scores[head] / floor,
            bar: Math.max(0, Math.min(1, window.scores[head] / (floor * 2)))
        }))
        .sort((a, b) => b.ratio - a.ratio)
        .slice(0, 3);
}

export function buildBadge(window, tone) {
    const s = window.standout;
    const range = `${clock(window.start)}–${clock(window.end)}`;
    const label = titleCase(s.label);
    return {
        emoji: s.emoji,
        label,
        strength: s.strength,
        range,
        rows: topStates(window, tone.model && tone.model.states),
        aria: `${label}, ${s.strength}, ${range}`
    };
}

/**
 * Sets `toneBadge` on the first segment of each standout window. Mutates and returns `segments`.
 * `segments` must be in time order with a numeric `startTime`.
 */
export function annotateTone(segments, tone) {
    const windows = tone && Array.isArray(tone.windows) ? tone.windows : null;
    if (!windows || !windows.length) return segments;
    let cursor = 0;
    const badged = new Set();
    for (const segment of segments) {
        const at = segment.startTime;
        if (typeof at !== 'number') continue;
        while (cursor < windows.length && windows[cursor].end <= at) cursor++;
        const window = windows[cursor];
        if (window && window.start <= at && window.standout && !badged.has(cursor)) {
            segment.toneBadge = buildBadge(window, tone);
            badged.add(cursor);
        }
    }
    return segments;
}

/**
 * Keeps a badge's popover inside the pane that scrolls the transcript: if it would run past the
 * right edge, it aligns to the badge's right edge instead. Delegated on `document` so it needs no
 * hooks in the templates, and it does nothing when there are no badges.
 */
function clippingAncestor(node) {
    for (let el = node.parentElement; el; el = el.parentElement) {
        const overflow = getComputedStyle(el).overflowX;
        if (overflow !== 'visible') return el;
    }
    return null;
}

export function placePopover(event) {
    const badge = event.target && event.target.closest ? event.target.closest('.tone-badge') : null;
    const pop = badge && badge.querySelector('.tone-pop');
    if (!pop) return;
    badge.classList.remove('tone-flip');
    const pane = clippingAncestor(badge);
    const limit = pane ? pane.getBoundingClientRect().right : window.innerWidth;
    if (pop.getBoundingClientRect().right > limit - 8) badge.classList.add('tone-flip');
}

if (typeof document !== 'undefined') {
    document.addEventListener('mouseover', placePopover);
    document.addEventListener('focusin', placePopover);
}
