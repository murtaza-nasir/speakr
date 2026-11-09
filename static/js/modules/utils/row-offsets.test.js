import { describe, it, expect } from 'vitest';
import { buildOffsets, indexAtOffset, applyMeasurements } from './row-offsets.js';

describe('row offsets', () => {
    it('uses the estimate for rows that were not measured', () => {
        const offsets = buildOffsets(3, 44, new Map([[1, 100]]));
        expect(Array.from(offsets)).toEqual([0, 44, 144, 188]);
    });

    it('finds the row at a position', () => {
        const offsets = buildOffsets(4, 10, new Map([[1, 30]]));
        // rows: [0,10) [10,40) [40,50) [50,60)
        expect(indexAtOffset(offsets, 0)).toBe(0);
        expect(indexAtOffset(offsets, 9.9)).toBe(0);
        expect(indexAtOffset(offsets, 10)).toBe(1);
        expect(indexAtOffset(offsets, 39)).toBe(1);
        expect(indexAtOffset(offsets, 45)).toBe(2);
        expect(indexAtOffset(offsets, 500)).toBe(3);
        expect(indexAtOffset(offsets, -5)).toBe(0);
        expect(indexAtOffset(buildOffsets(0, 10, new Map()), 5)).toBe(0);
    });

    it('reports growth above the scroll position only', () => {
        const measured = new Map();
        const offsets = buildOffsets(5, 44, measured);
        // scrolled to row 2 (y = 88): rows 0 and 1 end at or above it
        const { changed, shiftAbove } = applyMeasurements(measured, offsets, 44, 88,
            [[0, 60], [1, 44], [3, 90]]);
        expect(changed).toBe(true);
        expect(shiftAbove).toBe(16);
        expect(measured.get(3)).toBe(90);
        expect(measured.has(1)).toBe(false);
    });

    it('ignores changes below half a pixel', () => {
        const measured = new Map([[0, 44]]);
        const offsets = buildOffsets(1, 44, measured);
        expect(applyMeasurements(measured, offsets, 44, 0, [[0, 44.3]]).changed).toBe(false);
    });
});
