import { describe, expect, it } from 'vitest';

import { MIN_KEEP, fullRange, handleAt, moveEdge, previewRanges, removesSomething, trimEffect } from './trim.js';

// Trim mode of the timeline (#660): the rules the editor applies before it asks the server.
describe('moveEdge', () => {
    const range = { start: 10, end: 50 };

    it('moves one edge and leaves the other', () => {
        expect(moveEdge(range, 'start', 12.3456, 60)).toEqual({ start: 12.346, end: 50 });
        expect(moveEdge(range, 'end', 40, 60)).toEqual({ start: 10, end: 40 });
    });

    it('keeps the range inside the audio', () => {
        expect(moveEdge(range, 'start', -5, 60).start).toBe(0);
        expect(moveEdge(range, 'end', 99, 60).end).toBe(60);
    });

    it('keeps at least MIN_KEEP between the edges', () => {
        expect(moveEdge(range, 'start', 70, 60)).toEqual({ start: 50 - MIN_KEEP, end: 50 });
        expect(moveEdge(range, 'end', 0, 60)).toEqual({ start: 10, end: 10 + MIN_KEEP });
    });

    it('ignores a time that is not a number', () => {
        expect(moveEdge(range, 'start', NaN, 60)).toEqual(range);
    });
});

describe('fullRange and removesSomething', () => {
    it('starts with the whole recording, which removes nothing', () => {
        const range = fullRange(90.12345);
        expect(range).toEqual({ start: 0, end: 90.123 });
        expect(removesSomething(range, 90.12345)).toBe(false);
    });

    it('removes something once an edge moved more than the tolerance', () => {
        expect(removesSomething({ start: 0.04, end: 90 }, 90)).toBe(false);
        expect(removesSomething({ start: 0.5, end: 90 }, 90)).toBe(true);
        expect(removesSomething({ start: 0, end: 80 }, 90)).toBe(true);
    });
});

describe('handleAt', () => {
    it('finds the handle within the slop and prefers the nearer one', () => {
        expect(handleAt(103, 100, 300, 8)).toBe('start');
        expect(handleAt(295, 100, 300, 8)).toBe('end');
        expect(handleAt(200, 100, 300, 8)).toBe(null);
        expect(handleAt(104, 100, 106, 8)).toBe('end');
    });

    it('takes the end when both handles are at one place and the pointer is right of them', () => {
        expect(handleAt(102, 100, 100, 8)).toBe('end');
        expect(handleAt(98, 100, 100, 8)).toBe('start');
    });
});

describe('trimEffect', () => {
    const segments = [
        { start_time: 0, end_time: 1.5 },
        { start_time: 1.5, end_time: 3 },
        { start_time: 3, end_time: 5 },
        { start_time: 5.5, end_time: 7.5 },
        { start_time: 8, end_time: 10 },
        { start_time: 1.97, end_time: 2.01 },
    ];

    it('counts the segments kept, clipped and removed as the server cuts them', () => {
        expect(trimEffect(segments, { start: 2, end: 7 })).toEqual({ kept: 1, clipped: 2, removed: 3 });
    });
});

describe('previewRanges', () => {
    it('plays a few seconds after the start and before the end', () => {
        expect(previewRanges({ start: 10, end: 60 }, 3)).toEqual([{ start: 10, end: 13 }, { start: 57, end: 60 }]);
    });

    it('plays a short range once', () => {
        expect(previewRanges({ start: 10, end: 14 }, 3)).toEqual([{ start: 10, end: 14 }]);
        expect(previewRanges({ start: 10, end: 10 }, 3)).toEqual([]);
    });
});
