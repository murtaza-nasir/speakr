import { describe, expect, it } from 'vitest';

import { anchorAt, createHeightIndex, estimateRowHeight, scrollTopFor, scrollTopToReveal, visibleRange } from './virtual-list.js';

const index = (heights) => {
    const h = createHeightIndex();
    h.reset(heights.length, (i) => heights[i]);
    return h;
};

describe('row offsets', () => {
    it('sums the heights', () => {
        const h = index([40, 60, 40, 100]);
        expect([0, 1, 2, 3, 4].map((i) => h.offset(i))).toEqual([0, 40, 100, 140, 240]);
        expect(h.total()).toBe(240);
        expect(h.count).toBe(4);
    });

    it('finds the row at a position', () => {
        const h = index([40, 60, 40, 100]);
        expect(h.indexAt(-5)).toBe(0);
        expect(h.indexAt(0)).toBe(0);
        expect(h.indexAt(39)).toBe(0);
        expect(h.indexAt(40)).toBe(1);
        expect(h.indexAt(139)).toBe(2);
        expect(h.indexAt(140)).toBe(3);
        expect(h.indexAt(9999)).toBe(3);
        expect(index([]).indexAt(10)).toBe(0);
    });

    it('recomputes from the changed row on', () => {
        const h = index([40, 40, 40, 40]);
        expect(h.total()).toBe(160);
        expect(h.set(1, 100)).toBe(60);
        expect(h.offset(1)).toBe(40);
        expect(h.offset(2)).toBe(140);
        expect(h.total()).toBe(220);
        expect(h.set(1, 100.2)).toBe(0);
        expect(h.set(9, 10)).toBe(0);
        expect(h.set(0, 10)).toBe(-30);
        expect(h.offset(3)).toBe(150);
    });

    it('starts over with another number of rows', () => {
        const h = index([10, 10]);
        h.reset(3, () => 20);
        expect(h.total()).toBe(60);
        h.reset(0, () => 0);
        expect(h.total()).toBe(0);
    });

    it('handles 20,000 rows with measurements in the middle quickly', () => {
        const h = createHeightIndex();
        h.reset(20000, () => 44);
        const t0 = performance.now();
        for (let k = 0; k < 2000; k++) {
            h.set(10000 + (k % 40), 44 + (k % 7) * 10);
            h.indexAt(440000 + k);
            h.offset(10020);
        }
        const took = performance.now() - t0;
        console.log(`[asr virtual list] 2,000 measure + lookup rounds on 20,000 rows: ${took.toFixed(1)} ms`);
        expect(took).toBeLessThan(2000);
        expect(h.total()).toBeGreaterThan(20000 * 44);
    });
});

describe('the rendered range', () => {
    it('covers the viewport plus the overscan', () => {
        const h = index(new Array(100).fill(50));
        expect(visibleRange(h, 0, 200, 2)).toEqual({ start: 0, end: 7 });
        expect(visibleRange(h, 1000, 200, 2)).toEqual({ start: 18, end: 27 });
        expect(visibleRange(h, 4900, 200, 2)).toEqual({ start: 96, end: 100 });
        expect(visibleRange(index([]), 0, 200)).toEqual({ start: 0, end: 0 });
    });

    it('stays bounded whatever the number of rows', () => {
        const h = createHeightIndex();
        h.reset(20000, () => 44);
        const r = visibleRange(h, 400000, 800, 6);
        expect(r.end - r.start).toBeLessThanOrEqual(Math.ceil(800 / 44) + 1 + 12);
    });
});

describe('keeping the view in place', () => {
    it('moves the scroll position by what rows above the viewport grew', () => {
        const h = index(new Array(50).fill(40));
        const anchor = anchorAt(h, 410);
        expect(anchor).toEqual({ row: 10, within: 10 });
        h.set(3, 100);
        h.set(8, 20);
        expect(scrollTopFor(h, anchor)).toBe(410 + 60 - 20);
    });

    it('does not move for changes below the viewport', () => {
        const h = index(new Array(50).fill(40));
        const anchor = anchorAt(h, 410);
        h.set(30, 400);
        expect(scrollTopFor(h, anchor)).toBe(410);
    });

    it('survives rows going away', () => {
        const h = index(new Array(50).fill(40));
        const anchor = anchorAt(h, 1900);
        h.reset(10, () => 40);
        expect(scrollTopFor(h, anchor)).toBe(9 * 40 + 20);
        h.reset(0, () => 0);
        expect(scrollTopFor(h, anchor)).toBe(0);
    });
});

describe('revealing a row', () => {
    const h = index(new Array(50).fill(40));
    it('leaves a visible row alone', () => {
        expect(scrollTopToReveal(h, 5, 100, 400)).toBeNull();
    });
    it('scrolls up to a row above', () => {
        expect(scrollTopToReveal(h, 1, 400, 400)).toBe(32);
    });
    it('scrolls down just far enough for a row below', () => {
        expect(scrollTopToReveal(h, 20, 0, 400)).toBe(20 * 40 + 40 + 8 - 400);
    });
    it('ignores a row that does not exist', () => {
        expect(scrollTopToReveal(h, 99, 0, 400)).toBeNull();
    });
});

describe('height estimate', () => {
    it('grows with the text', () => {
        expect(estimateRowHeight(10, 80)).toBe(44);
        expect(estimateRowHeight(200, 80)).toBe(20 + 3 * 24);
        expect(estimateRowHeight(0, 80)).toBe(44);
        expect(estimateRowHeight(100, 0)).toBe(20 + 13 * 24);
    });
});
