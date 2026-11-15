import { describe, expect, it } from 'vitest';
import {
    MAX_UNIT_SEGMENTS, RENDER_ALL_SEGMENTS, bubbleUnits, charsPerLine, createTranscriptWindow,
    estimateUnitHeight, runUnits, segmentUnits, unitOfSegment,
} from './transcript-window.js';

const seg = (i, speaker = 'A', words = 8) => ({ sentence: Array(words).fill('word').join(' '), startTime: i * 2, speaker, originalIndex: i });

// Runs the way processedTranscription builds them: consecutive segments of one speaker.
const runsOf = (speakers) => {
    const runs = [];
    speakers.forEach((speaker, i) => {
        const last = runs[runs.length - 1];
        if (!last || last.speaker !== speaker) runs.push({ speaker, color: `c-${speaker}`, startTime: i * 2, runOffset: i, segments: [] });
        runs[runs.length - 1].segments.push(seg(i, speaker));
    });
    return runs;
};

describe('runUnits', () => {
    it('makes one unit of a turn and keeps the turn\'s fields', () => {
        const units = runUnits(runsOf(['A', 'A', 'B', 'A']));
        expect(units.map((u) => [u.first, u.count, u.speaker])).toEqual([[0, 2, 'A'], [2, 1, 'B'], [3, 1, 'A']]);
        expect(units[0].color).toBe('c-A');
        expect(units[0].startTime).toBe(0);
        expect(units.map((u) => u.unit)).toEqual([0, 1, 2]);
        expect(new Set(units.map((u) => u.key)).size).toBe(3);
        expect(units[1].segments[0].originalIndex).toBe(2);
    });

    it('cuts a long turn into units that start at their own first segment', () => {
        const units = runUnits(runsOf(Array(250).fill('A').concat(['B'])), 100);
        expect(units.map((u) => [u.first, u.count, u.continued])).toEqual([[0, 100, false], [100, 100, true], [200, 50, true], [250, 1, false]]);
        expect(units[1].startTime).toBe(200);
        expect(units[1].speaker).toBe('A');
        expect(units[2].segments[49].originalIndex).toBe(249);
    });

    it('accepts no runs', () => {
        expect(runUnits(undefined)).toEqual([]);
        expect(runUnits([])).toEqual([]);
    });
});

describe('bubbleUnits', () => {
    const rows = [
        { speaker: 'A', color: 'c1', isMe: false, bubbles: [{ sentence: 'a' }, { sentence: 'b' }] },
        { speaker: 'Me', color: 'c2', isMe: true, bubbles: [{ sentence: 'c' }] },
        { speaker: 'A', color: 'c1', isMe: false, bubbles: [{ sentence: 'd' }, { sentence: 'e' }, { sentence: 'f' }] },
    ];

    it('numbers the bubbles through the whole transcript', () => {
        const units = bubbleUnits(rows);
        expect(units.map((u) => [u.first, u.count, u.isMe])).toEqual([[0, 2, false], [2, 1, true], [3, 3, false]]);
        expect(units.flatMap((u) => u.bubbles.map((b) => b.index))).toEqual([0, 1, 2, 3, 4, 5]);
        expect(units[2].bubbles[1].sentence).toBe('e');
    });

    it('cuts a long row', () => {
        const units = bubbleUnits(rows, 2);
        expect(units.map((u) => [u.first, u.count])).toEqual([[0, 2], [2, 1], [3, 2], [5, 1]]);
        expect(units[3].bubbles[0].index).toBe(5);
        expect(units[3].color).toBe('c1');
    });
});

describe('segmentUnits', () => {
    it('makes one unit per segment', () => {
        const units = segmentUnits([{ sentence: 'a', startTime: 0 }, { sentence: 'b', startTime: 3 }]);
        expect(units.map((u) => [u.unit, u.first, u.count, u.segment.sentence])).toEqual([[0, 0, 1, 'a'], [1, 1, 1, 'b']]);
        expect(segmentUnits(undefined)).toEqual([]);
    });
});

describe('unitOfSegment', () => {
    it('finds the unit of a segment', () => {
        const units = runUnits(runsOf(['A', 'A', 'B', 'A', 'A', 'A']));
        expect([0, 1, 2, 3, 5].map((i) => unitOfSegment(units, i))).toEqual([0, 0, 1, 2, 2]);
        expect(unitOfSegment(units, 6)).toBe(-1);
        expect(unitOfSegment(units, -1)).toBe(-1);
        expect(unitOfSegment([], 0)).toBe(-1);
    });
});

describe('estimates', () => {
    it('grow with the text and shrink with the width', () => {
        const short = segmentUnits([{ sentence: 'short' }])[0];
        const long = segmentUnits([{ sentence: 'x'.repeat(900) }])[0];
        expect(estimateUnitHeight(long, 80)).toBeGreaterThan(estimateUnitHeight(short, 80));
        expect(estimateUnitHeight(long, 40)).toBeGreaterThan(estimateUnitHeight(long, 80));
        const run = runUnits(runsOf(['A', 'A', 'A']))[0];
        expect(estimateUnitHeight(run, 80)).toBeGreaterThan(3 * 42);
        const row = bubbleUnits([{ bubbles: [{ sentence: 'x'.repeat(300) }, { sentence: 'y' }] }])[0];
        expect(estimateUnitHeight(row, 60)).toBeGreaterThan(estimateUnitHeight(row, 200));
    });

    it('take the characters per line from the width', () => {
        expect(charsPerLine(800, 'run')).toBeGreaterThan(charsPerLine(400, 'run'));
        expect(charsPerLine(0, 'run')).toBeGreaterThanOrEqual(20);
        expect(charsPerLine(800, 'bubbles')).toBeLessThan(charsPerLine(800, 'run'));
    });
});

describe('createTranscriptWindow', () => {
    const alternating = (n) => runUnits(runsOf(Array.from({ length: n }, (_, i) => (i % 2 ? 'B' : 'A'))));

    it('renders a short transcript whole', () => {
        const win = createTranscriptWindow();
        win.reset(alternating(RENDER_ALL_SEGMENTS), 80);
        expect(win.whole).toBe(true);
        expect(win.range(5000, 800)).toEqual({ start: 0, end: RENDER_ALL_SEGMENTS });
        expect(win.layout(win.range(0, 800))).toEqual({ before: 0, after: 0 });
    });

    it('renders only the units near the view of a long transcript', () => {
        const win = createTranscriptWindow();
        win.reset(alternating(20000), 80);
        expect(win.whole).toBe(false);
        expect(win.segments).toBe(20000);
        const top = win.range(0, 800);
        expect(top.start).toBe(0);
        expect(top.end).toBeGreaterThan(5);
        expect(top.end).toBeLessThan(60);
        const y = win.offset(10000);
        const middle = win.range(y, 800);
        expect(middle.start).toBeLessThan(10000);
        expect(middle.end).toBeGreaterThan(10000);
        expect(middle.end - middle.start).toBeLessThan(80);
        const { before, after } = win.layout(middle);
        expect(before).toBe(win.offset(middle.start));
        expect(before + after).toBeLessThan(win.total());
        const end = win.range(win.total(), 800);
        expect(end.end).toBe(20000);
        expect(win.layout(end).after).toBe(0);
    });

    it('keeps a long turn windowed', () => {
        const win = createTranscriptWindow();
        const units = runUnits(runsOf(Array(5000).fill('A')));
        expect(units.length).toBe(5000 / MAX_UNIT_SEGMENTS);
        win.reset(units, 80);
        const range = win.range(win.offset(25), 800);
        expect(range.end - range.start).toBeLessThanOrEqual(4);
    });

    it('takes the measured heights from the tops of the rendered units', () => {
        const win = createTranscriptWindow();
        win.reset(alternating(1000), 80);
        const before = win.offset(12);
        // Units 10 and 11 are 100 and 30 pixels tall on the page
        expect(win.measure(10, [500, 600], 630)).toBe(true);
        expect(win.height(10)).toBe(100);
        expect(win.height(11)).toBe(30);
        expect(win.offset(12)).not.toBe(before);
        expect(win.offset(12) - win.offset(10)).toBe(130);
        // The same tops again change nothing
        expect(win.measure(10, [500, 600], 630)).toBe(false);
        // A unit that is not laid out (no height) keeps its estimate
        const kept = win.height(20);
        expect(win.measure(20, [700], 700)).toBe(false);
        expect(win.height(20)).toBe(kept);
    });

    it('gives the position that shows a segment', () => {
        const win = createTranscriptWindow();
        win.reset(alternating(1000), 80);
        expect(win.reveal(0, 900)).toBe(0);
        expect(win.reveal(500, 900)).toBe(win.offset(500) - 300);
        expect(win.reveal(5000, 900)).toBe(null);
        // Inside a long unit: in proportion to the segment's place in it
        const long = createTranscriptWindow();
        long.reset(runUnits(runsOf(Array(400).fill('A'))), 80);
        expect(long.reveal(150, 900)).toBe(long.offset(1) + long.height(1) / 2 - 300);
    });

    it('starts over with other units', () => {
        const win = createTranscriptWindow();
        win.reset(alternating(1000), 80);
        win.measure(0, [0], 500);
        win.reset(alternating(400), 80);
        expect(win.count).toBe(400);
        expect(win.height(0)).toBeLessThan(500);
        win.reset([], 80);
        expect(win.range(0, 800)).toEqual({ start: 0, end: 0 });
        expect(win.layout({ start: 0, end: 0 })).toEqual({ before: 0, after: 0 });
    });
});
