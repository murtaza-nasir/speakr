/**
 * Geometry of the segment editor's timeline: lanes, the view, hit testing,
 * snapping and what a drag does to the times.
 *
 * Pure: no Vue, no DOM and no canvas (timeline.js draws and listens). A
 * lane holds the segments of one speaker in the order of the editing model,
 * which is sorted by start time, so every lookup is a binary search.
 *
 * Times are seconds, x is CSS pixels from the left of the canvas, and a view
 * is { t0, pps }: the time at the left edge and pixels per second.
 */

export const MIN_LENGTH = 0.05;     // shortest segment a drag can produce, seconds
export const TOUCH = 0.03;          // neighbours closer than this count as touching
export const MERGE_PPS = 4;         // below this zoom, blocks are drawn merged
export const MAX_PPS = 400;
export const SNAP_PX = 8;
export const CREATE_MIN = 0.1;      // a shorter drag on empty lane space creates nothing

const fin = Number.isFinite;

export const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
export const round3 = (v) => Math.round(v * 1000) / 1000;

// ---- lanes -------------------------------------------------------------------

/** First index whose start is t or more. */
export function lowerBound(segs, t) {
    let lo = 0;
    let hi = segs.length;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (segs[mid].start_time < t) lo = mid + 1; else hi = mid;
    }
    return lo;
}

/** First index whose start is after t. */
export function upperBound(segs, t) {
    let lo = 0;
    let hi = segs.length;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (segs[mid].start_time <= t) lo = mid + 1; else hi = mid;
    }
    return lo;
}

/** Index of a segment in its lane, or -1. */
export function indexInLane(segs, seg) {
    let i = lowerBound(segs, seg.start_time);
    while (i < segs.length && segs[i] !== seg && segs[i].start_time === seg.start_time) i++;
    return segs[i] === seg ? i : segs.indexOf(seg);
}

/**
 * The order of the lanes after a change. Lanes keep their place while their
 * speaker has segments; a speaker without segments loses its lane; a new
 * speaker is added at the end, except that one new name beside one name that
 * went away is a rename and takes that lane.
 */
export function nextLaneOrder(previous, present) {
    const now = new Set(present);
    const before = new Set(previous);
    const gone = previous.filter((name) => !now.has(name));
    const added = present.filter((name) => !before.has(name));
    if (gone.length === 1 && added.length === 1) return previous.map((name) => (name === gone[0] ? added[0] : name));
    return previous.filter((name) => now.has(name)).concat(added);
}

/**
 * The segments by lane. `segments` is the model's array (sorted by start);
 * segments without both times are left out (the transcript pane lists them
 * as problems). `order` is the lane order (speaker names); speakers not in
 * it get lanes at the end. `problemAt(i)` is the model's problem mask.
 *
 * Returns { lanes: [{ name, segs, bad }], laneOf: Map(name -> lane index),
 * maxLength, end }: `bad[k]` is nonzero when segs[k] has a problem,
 * `maxLength` the longest segment (how far back a search must look) and
 * `end` the latest end.
 */
export function buildLanes(segments, order, problemAt = null) {
    const lanes = [];
    const laneOf = new Map();
    const laneFor = (name) => {
        let index = laneOf.get(name);
        if (index === undefined) {
            index = lanes.length;
            laneOf.set(name, index);
            lanes.push({ name, segs: [], bad: [] });
        }
        return index;
    };
    for (const name of order) laneFor(name);
    let maxLength = 0;
    let end = 0;
    for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        if (!fin(seg.start_time) || !fin(seg.end_time)) continue;
        const lane = lanes[laneFor(seg.speaker)];
        lane.segs.push(seg);
        lane.bad.push(problemAt ? (problemAt(i) ? 1 : 0) : 0);
        const length = seg.end_time - seg.start_time;
        if (length > maxLength) maxLength = length;
        if (seg.end_time > end) end = seg.end_time;
    }
    return { lanes, laneOf, maxLength, end };
}

// ---- the view ------------------------------------------------------------------

export const minPps = (width, duration) => width / Math.max(duration, 0.001);

/** Keep a view inside the recording. Changes and returns it. */
export function clampView(view, width, duration) {
    view.pps = clamp(view.pps, Math.min(minPps(width, duration), MAX_PPS), MAX_PPS);
    view.t0 = clamp(view.t0, 0, Math.max(0, duration - width / view.pps));
    return view;
}

/** Zoom by a factor, keeping the time under x where it is. */
export function zoomAt(view, factor, x, width, duration) {
    const t = view.t0 + x / view.pps;
    view.pps = clamp(view.pps * factor, Math.min(minPps(width, duration), MAX_PPS), MAX_PPS);
    view.t0 = t - x / view.pps;
    return clampView(view, width, duration);
}

/** Scroll so that a segment is in view; true when the view moved. */
export function revealInView(view, start, end, width, duration) {
    const t1 = view.t0 + width / view.pps;
    const span = width / view.pps;
    if (end < view.t0 || start > t1 || (start < view.t0 && end - start < span)) {
        view.t0 = (start + end) / 2 - span / 2;
        clampView(view, width, duration);
        return true;
    }
    return false;
}

const STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200];

/** Seconds between labelled ruler ticks at a zoom (at least 76 px apart). */
export function rulerStep(pps) {
    for (const step of STEPS) if (step * pps >= 76) return step;
    return STEPS[STEPS.length - 1];
}

/** The window of the overview bar: { x, w } in pixels. */
export function overviewWindow(view, width, duration, out = { x: 0, w: 0 }) {
    const w = Math.max(8, Math.min(width, (width / view.pps / duration) * width));
    out.x = clamp((view.t0 / duration) * width, 0, width - w);
    out.w = w;
    return out;
}

// ---- hit testing ---------------------------------------------------------------

/**
 * The block under x in a lane and the part of it: 'l' and 'r' are the edges
 * (also a few pixels outside the block), 'body' the rest. Null on empty lane
 * space. Touch gets wider edges.
 */
export function hitTest(segs, x, view, maxLength, isTouch = false) {
    const t = view.t0 + x / view.pps;
    const outside = isTouch ? 18 : 5;
    const tolerance = outside / view.pps;
    let best = null;
    let bestDistance = Infinity;
    const lowest = t - maxLength - tolerance;
    for (let i = upperBound(segs, t + tolerance) - 1; i >= 0 && segs[i].start_time >= lowest; i--) {
        const seg = segs[i];
        const x0 = (seg.start_time - view.t0) * view.pps;
        let x1 = (seg.end_time - view.t0) * view.pps;
        if (x1 - x0 < 3) x1 = x0 + 3;
        if (x >= x0 && x <= x1) {
            const inside = Math.min(isTouch ? 8 : 6, (x1 - x0) * 0.3);
            return { seg, index: i, zone: x - x0 < inside ? 'l' : x1 - x < inside ? 'r' : 'body' };
        }
        const left = x0 - x;
        const right = x - x1;
        if (left > 0 && left <= outside && left < bestDistance) { bestDistance = left; best = { seg, index: i, zone: 'l' }; }
        if (right > 0 && right <= outside && right < bestDistance) { bestDistance = right; best = { seg, index: i, zone: 'r' }; }
    }
    return best;
}

// ---- snapping ------------------------------------------------------------------

/**
 * The nearest snap target within 8 px of t: the playhead, an edge of any
 * other segment, or a quiet point of the audio. `env` is { lanes, maxLength,
 * pps, playhead, quietNear(t, tolerance) }; `skipA` and `skipB` are the
 * segments being dragged. Returns { t, kind } or null; kind is 'playhead',
 * 'edge' or 'quiet'.
 */
export function findSnap(env, t, skipA = null, skipB = null) {
    const reach = SNAP_PX / env.pps;
    let best = null;
    let bestDistance = reach;
    const consider = (value, kind, limit) => {
        const distance = Math.abs(value - t);
        if (distance < bestDistance && distance < limit) { bestDistance = distance; best = { t: value, kind }; }
    };
    if (fin(env.playhead)) consider(env.playhead, 'playhead', reach);
    for (const lane of env.lanes) {
        const segs = lane.segs;
        for (let i = lowerBound(segs, t - env.maxLength - reach), end = upperBound(segs, t + reach); i < end; i++) {
            const seg = segs[i];
            if (seg === skipA || seg === skipB) continue;
            consider(seg.start_time, 'edge', reach);
            consider(seg.end_time, 'edge', reach);
        }
    }
    if (env.quietNear) {
        const quiet = env.quietNear(t, reach * 0.75);
        if (fin(quiet)) consider(quiet, 'quiet', reach * 0.75);
    }
    return best;
}

// ---- drags ---------------------------------------------------------------------
//
// Each drag has a state object made when it starts and a function that takes
// the pointer's time and writes the previewed times into the state. The
// model is not touched until the drag ends; `changesOf` then lists what
// model.retime() is to store.

/** Start dragging an edge ('l' or 'r') of segs[index]. */
export function beginResize(segs, index, edge) {
    const seg = segs[index];
    const prev = segs[index - 1] || null;
    const next = segs[index + 1] || null;
    let neighbour = null;
    if (edge === 'r' && next && Math.abs(next.start_time - seg.end_time) <= TOUCH) neighbour = next;
    if (edge === 'l' && prev && Math.abs(prev.end_time - seg.start_time) <= TOUCH) neighbour = prev;
    return {
        kind: 'resize', seg, edge, prev, next, neighbour,
        start: seg.start_time, end: seg.end_time,
        neighbourStart: neighbour ? neighbour.start_time : 0, neighbourEnd: neighbour ? neighbour.end_time : 0,
        linked: false, snap: null,
    };
}

/**
 * Move the dragged edge to time t. A touching neighbour's edge follows
 * unless `alt`; without a following neighbour the edge stops at the next
 * block of the lane unless `alt`. `ctrl` turns snapping off.
 */
export function resizeTo(state, t, { alt = false, ctrl = false } = {}, env) {
    const seg = state.seg;
    const neighbour = state.neighbour;
    const linked = !!neighbour && !alt;
    let snap = null;
    if (!ctrl) {
        snap = findSnap(env, t, seg, linked ? neighbour : null);
        if (snap) t = snap.t;
    }
    let lo;
    let hi;
    if (state.edge === 'r') {
        lo = seg.start_time + MIN_LENGTH;
        hi = linked ? neighbour.end_time - MIN_LENGTH : (alt || !state.next ? env.duration : state.next.start_time);
        if (hi < lo) hi = lo;
    } else {
        hi = seg.end_time - MIN_LENGTH;
        lo = linked ? neighbour.start_time + MIN_LENGTH : (alt || !state.prev ? 0 : state.prev.end_time);
        if (lo > hi) lo = hi;
    }
    t = round3(clamp(clamp(t, lo, hi), 0, Math.max(env.duration, lo)));
    if (snap && Math.abs(t - snap.t) > 0.0006) snap = null;
    state.start = state.edge === 'r' ? seg.start_time : t;
    state.end = state.edge === 'r' ? t : seg.end_time;
    if (neighbour) {
        state.neighbourStart = linked && state.edge === 'r' ? t : neighbour.start_time;
        state.neighbourEnd = linked && state.edge === 'l' ? t : neighbour.end_time;
    }
    state.linked = linked;
    state.snap = snap;
    return state;
}

/** Start moving segs[index] of lane `lane`; `grab` is the time under the pointer minus the start. */
export function beginMove(segs, index, lane, grab) {
    const seg = segs[index];
    return {
        kind: 'move', seg, fromLane: lane, lane, grab,
        prev: segs[index - 1] || null, next: segs[index + 1] || null,
        start: seg.start_time, end: seg.end_time, snap: null,
    };
}

/**
 * Move the block so that it starts at `start`, in lane `lane`. It keeps its
 * length, snaps with either edge, and stops at its neighbours in the lane
 * unless `alt`. With `keepTime` only the lane changes (a drag straight up or
 * down changes the speaker and nothing else).
 */
export function moveTo(state, start, lane, { alt = false, ctrl = false, keepTime = false } = {}, env) {
    const seg = state.seg;
    const length = seg.end_time - seg.start_time;
    if (keepTime) {
        state.start = seg.start_time;
        state.end = seg.end_time;
        state.lane = lane;
        state.snap = null;
        return state;
    }
    let s = start;
    let snap = null;
    if (!ctrl) {
        const a = findSnap(env, s, seg);
        const b = findSnap(env, s + length, seg);
        if (a && (!b || Math.abs(a.t - s) <= Math.abs(b.t - s - length))) { s = a.t; snap = a; }
        else if (b) { s = b.t - length; snap = b; }
    }
    if (!alt) {
        let lo = 0;
        let hi = env.duration - length;
        if (lane === state.fromLane) {
            if (state.prev) lo = state.prev.end_time;
            if (state.next) hi = state.next.start_time - length;
        } else {
            const segs = env.lanes[lane].segs;
            const i = upperBound(segs, s + length / 2) - 1;
            if (i >= 0) lo = segs[i].end_time;
            if (segs[i + 1]) hi = segs[i + 1].start_time - length;
        }
        if (lo <= hi) s = clamp(s, lo, hi);
    }
    s = round3(clamp(s, 0, Math.max(0, env.duration - length)));
    const e = round3(s + length);
    if (snap && Math.abs(s - snap.t) > 0.0006 && Math.abs(e - snap.t) > 0.0006) snap = null;
    state.start = s;
    state.end = e;
    state.lane = lane;
    state.snap = snap;
    return state;
}

/** Start drawing a new segment in lane `lane` from time `from`. */
export function beginCreate(segs, lane, from, env) {
    const i = upperBound(segs, from) - 1;
    // The latest end before `from`: usually the block before it, but with
    // overlapping blocks an earlier one may end later. A block that starts
    // more than the longest length before `lo` cannot.
    let lo = 0;
    for (let j = i; j >= 0 && segs[j].start_time + env.maxLength > lo; j--) {
        if (segs[j].end_time > lo && segs[j].end_time <= from + 1e-6) lo = segs[j].end_time;
    }
    return {
        kind: 'create', lane, from, to: from,
        lo, hi: segs[i + 1] ? segs[i + 1].start_time : env.duration,
        start: from, end: from, snap: null,
    };
}

/** Drag the open end of a new segment to time t (it stays between its neighbours unless `alt`). */
export function createTo(state, t, { alt = false, ctrl = false } = {}, env) {
    let snap = null;
    if (!ctrl) {
        snap = findSnap(env, t);
        if (snap) t = snap.t;
    }
    if (!alt) t = clamp(t, state.lo, Math.max(state.lo, state.hi));
    t = round3(clamp(t, 0, env.duration));
    if (snap && Math.abs(t - snap.t) > 0.0006) snap = null;
    state.to = t;
    state.start = Math.min(state.from, t);
    state.end = Math.max(state.from, t);
    state.snap = snap;
    return state;
}

/**
 * What a finished resize or move stores: the list for model.retime()
 * (empty when nothing changed). `laneName(i)` gives a lane's speaker.
 */
export function changesOf(state, laneName = null) {
    const out = [];
    const seg = state.seg;
    if (state.kind === 'resize') {
        if (state.start !== seg.start_time || state.end !== seg.end_time) out.push({ id: seg.id, start: state.start, end: state.end });
        const n = state.neighbour;
        if (n && (state.neighbourStart !== n.start_time || state.neighbourEnd !== n.end_time)) {
            out.push({ id: n.id, start: state.neighbourStart, end: state.neighbourEnd });
        }
    } else if (state.kind === 'move') {
        const moved = state.start !== seg.start_time;
        const relaned = state.lane !== state.fromLane;
        if (moved || relaned) {
            const change = { id: seg.id, start: state.start, end: state.end };
            if (relaned && laneName) change.speaker = laneName(state.lane);
            out.push(change);
        }
    }
    return out;
}
