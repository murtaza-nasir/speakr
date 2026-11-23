/**
 * Trim mode of the segment editor's timeline (#660): the range to keep, its
 * handles and what a trim does to the transcript. The server cuts the audio
 * and the transcript the same way (src/services/recording_trim.py); this is
 * what the editor shows before it is asked.
 */

// The shortest audio a trim keeps, in seconds (MIN_KEPT_SECONDS on the server)
export const MIN_KEEP = 1;
// A segment clipped shorter than this is dropped (MIN_SEGMENT_SECONDS)
const MIN_SEGMENT = 0.05;
// Start or end this close to the edges of the audio count as the edges (EDGE_TOLERANCE)
const EDGE = 0.05;
// Seconds heard at each cut by Preview
export const PREVIEW_SECONDS = 3;

const round3 = (t) => Math.round(t * 1000) / 1000;

/** The whole recording: where trim mode starts. */
export function fullRange(duration) {
    return { start: 0, end: round3(Math.max(0, Number(duration) || 0)) };
}

/**
 * The range with one edge ('start' or 'end') moved to `t`, kept inside the
 * audio and at least MIN_KEEP long (or the whole audio when it is shorter).
 */
export function moveEdge(range, edge, t, duration) {
    const total = Math.max(0, Number(duration) || 0);
    const keep = Math.min(MIN_KEEP, total);
    const time = Number.isFinite(t) ? t : (edge === 'start' ? range.start : range.end);
    if (edge === 'start') {
        const start = Math.min(Math.max(0, time), Math.max(0, range.end - keep));
        return { start: round3(start), end: range.end };
    }
    const end = Math.max(Math.min(total, time), Math.min(total, range.start + keep));
    return { start: range.start, end: round3(end) };
}

/** Whether the range removes anything (the server refuses a trim that does not). */
export function removesSomething(range, duration) {
    const total = Number(duration) || 0;
    return range.start > EDGE || range.end < total - EDGE;
}

/**
 * The edge whose handle is under x (canvas pixels): 'start', 'end' or null.
 * The nearer one wins when both are within `slop`.
 */
export function handleAt(x, startX, endX, slop) {
    const ds = Math.abs(x - startX);
    const de = Math.abs(x - endX);
    if (ds > slop && de > slop) return null;
    if (ds === de) return x > endX ? 'end' : 'start';
    return ds < de ? 'start' : 'end';
}

/**
 * What a trim to the range does to the segments: how many are kept as they
 * are (moved in time), clipped at a cut, or removed.
 */
export function trimEffect(segments, range) {
    let kept = 0;
    let clipped = 0;
    let removed = 0;
    for (const seg of segments || []) {
        const s = Number(seg.start_time);
        const e = Number(seg.end_time);
        if (!Number.isFinite(s) || !Number.isFinite(e)) { kept++; continue; }
        if (e <= range.start || s >= range.end) { removed++; continue; }
        const across = s < range.start || e > range.end;
        if (across && Math.min(e, range.end) - Math.max(s, range.start) < MIN_SEGMENT) { removed++; continue; }
        if (across) clipped++; else kept++;
    }
    return { kept, clipped, removed };
}

/**
 * The stretches Preview plays: a few seconds after the start and a few
 * before the end, one stretch when they meet.
 */
export function previewRanges(range, seconds = PREVIEW_SECONDS) {
    const { start, end } = range;
    if (!(end > start)) return [];
    if (end - start <= seconds * 2) return [{ start, end }];
    return [{ start, end: start + seconds }, { start: end - seconds, end }];
}
