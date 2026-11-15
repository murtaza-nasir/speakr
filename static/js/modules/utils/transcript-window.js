/**
 * The transcript of the recording page as a windowed list.
 *
 * Pure logic, no DOM and no Vue. The transcript is cut into units, the
 * blocks the views lay out one under the other: a speaker turn of the simple
 * view, a row of bubbles, or one segment of a transcript without speakers.
 * Only the units near the viewport are rendered; a spacer above and one
 * below stand for the rest. Unit positions come from the height index of the
 * segment editor (asr-editor/virtual-list.js): an estimate from the length
 * of the text until a unit has been rendered, its measured height afterwards.
 *
 * composables/transcript-view.js connects this to the page.
 */

import { createHeightIndex, estimateRowHeight, visibleRange } from '../asr-editor/virtual-list.js';

// A turn with more segments than this is rendered as several units, each
// with its own speaker label, so a monologue of any length stays windowed.
export const MAX_UNIT_SEGMENTS = 100;
// A transcript up to this many segments is rendered whole, as before.
export const RENDER_ALL_SEGMENTS = 300;

const lengthOf = (item) => (item && typeof item.sentence === 'string' ? item.sentence.length : 0);

/**
 * The speaker turns of a transcript: consecutive segments of one speaker as
 * one run, { speaker, color, speakerId, startTime, runOffset, segments },
 * each segment with its index in the transcript as originalIndex. What the
 * simple view renders (processedTranscription.simpleSegmentRuns).
 */
export function segmentRuns(segments) {
    const runs = [];
    let last;
    (segments || []).forEach((segment, index) => {
        if (!runs.length || segment.speakerId !== last) {
            runs.push({
                speaker: segment.speaker,
                color: segment.color,
                speakerId: segment.speakerId,
                startTime: segment.startTime,
                runOffset: index,
                segments: [],
            });
        }
        runs[runs.length - 1].segments.push({ ...segment, originalIndex: index });
        last = segment.speakerId;
    });
    return runs;
}

/** Units of the simple view with speakers, from processedTranscription.simpleSegmentRuns. */
export function runUnits(runs, max = MAX_UNIT_SEGMENTS) {
    const units = [];
    (runs || []).forEach((run) => {
        const segments = run.segments || [];
        for (let at = 0; at < segments.length; at += max) {
            const part = at === 0 && segments.length <= max ? segments : segments.slice(at, at + max);
            const first = part[0].originalIndex;
            units.push({
                ...run,
                kind: 'run',
                unit: units.length,
                key: `run-${first}-${part.length}`,
                first,
                count: part.length,
                continued: at > 0,
                startTime: at === 0 ? run.startTime : part[0].startTime,
                segments: part,
            });
        }
    });
    return units;
}

/** Units of the bubble view, from processedTranscription.bubbleRows. Every bubble gets its segment index. */
export function bubbleUnits(rows, max = MAX_UNIT_SEGMENTS) {
    const units = [];
    let next = 0;
    (rows || []).forEach((row) => {
        const bubbles = (row.bubbles || []).map((bubble, i) => ({ ...bubble, index: next + i }));
        for (let at = 0; at < bubbles.length; at += max) {
            const part = at === 0 && bubbles.length <= max ? bubbles : bubbles.slice(at, at + max);
            units.push({
                ...row,
                kind: 'bubbles',
                unit: units.length,
                key: `row-${next + at}-${part.length}`,
                first: next + at,
                count: part.length,
                bubbles: part,
            });
        }
        next += bubbles.length;
    });
    return units;
}

/** Units of a transcript without speakers: one per segment. */
export function segmentUnits(segments) {
    return (segments || []).map((segment, index) => ({
        kind: 'segment',
        unit: index,
        key: `seg-${index}-${segment.startTime}`,
        first: index,
        count: 1,
        segment,
    }));
}

/** Characters that fit on a line of text in a list of this width. */
export function charsPerLine(width, kind) {
    const text = Math.max(160, (width || 0) - (kind === 'bubbles' ? 48 : 16));
    return Math.max(20, Math.floor(text / 7.6));
}

/** Height of a unit before it has been rendered. */
export function estimateUnitHeight(unit, perLine) {
    if (unit.kind === 'bubbles') {
        // Bubbles follow each other on a line and wrap
        const chars = unit.bubbles.reduce((sum, bubble) => sum + lengthOf(bubble) + 6, 0);
        return 8 + Math.max(1, Math.ceil(chars / Math.max(8, perLine))) * 42;
    }
    const row = { lineHeight: 26, padding: 16, min: 42 };
    if (unit.kind === 'segment') return estimateRowHeight(lengthOf(unit.segment), perLine, row);
    return 48 + unit.segments.reduce((sum, segment) => sum + estimateRowHeight(lengthOf(segment), perLine, row), 0);
}

/** Index of the unit that holds a segment, or -1. `units` are in order of `first`. */
export function unitOfSegment(units, segmentIndex) {
    let lo = 0;
    let hi = units.length - 1;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const unit = units[mid];
        if (segmentIndex < unit.first) hi = mid - 1;
        else if (segmentIndex >= unit.first + Math.max(unit.count, 1)) lo = mid + 1;
        else return mid;
    }
    return -1;
}

/**
 * The window over a list of units.
 *
 *   reset(units, perLine)        new units or another width: estimates again
 *   range(y, viewport, overscan) units to render for a view that begins y
 *                                pixels below the top of the list
 *   layout(range)                { before, after }: the heights of the spacers
 *   measure(start, tops, end)    the rendered units' tops (and the bottom of
 *                                the last); returns true when a height changed
 *   reveal(segmentIndex, viewport)  the y that shows a segment a third down
 */
export function createTranscriptWindow() {
    const heights = createHeightIndex();
    let units = [];
    let segments = 0;

    return {
        get count() { return units.length; },
        get segments() { return segments; },
        get whole() { return segments <= RENDER_ALL_SEGMENTS; },
        units: () => units,

        reset(list, perLine) {
            units = list || [];
            segments = units.reduce((sum, unit) => sum + unit.count, 0);
            heights.reset(units.length, (i) => estimateUnitHeight(units[i], perLine));
        },

        range(y, viewport, overscan = 600) {
            if (segments <= RENDER_ALL_SEGMENTS) return { start: 0, end: units.length };
            const top = Math.max(0, y - overscan);
            return visibleRange(heights, top, Math.max(1, y + viewport + overscan - top), 1);
        },

        layout({ start, end }) {
            return { before: heights.offset(start), after: Math.max(0, heights.total() - heights.offset(end)) };
        },

        measure(start, tops, end) {
            let changed = false;
            for (let i = 0; i < tops.length; i++) {
                const height = (i + 1 < tops.length ? tops[i + 1] : end) - tops[i];
                if (height > 0 && heights.set(start + i, height)) changed = true;
            }
            return changed;
        },

        unitOf: (segmentIndex) => unitOfSegment(units, segmentIndex),
        offset: (unit) => heights.offset(unit),
        height: (unit) => heights.height(unit),
        total: () => heights.total(),

        reveal(segmentIndex, viewport) {
            const i = unitOfSegment(units, segmentIndex);
            if (i < 0) return null;
            const unit = units[i];
            const within = unit.count > 1 ? ((segmentIndex - unit.first) / unit.count) * heights.height(i) : 0;
            return Math.max(0, heights.offset(i) + within - viewport / 3);
        },
    };
}
