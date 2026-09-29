import { describe, it, expect } from 'vitest';
import { annotateTone, buildBadge } from './tone.js';

const states = { Thankfulness: ['thankful', '🙏', 0.5], Enthusiasm: ['enthusiastic', '⚡', 0.6] };
const tone = {
    model: { states },
    windows: [
        { start: 0, end: 10, scores: { Thankfulness: 0.1, Enthusiasm: 0.2 } },
        { start: 10, end: 20, scores: { Thankfulness: 0.9, Enthusiasm: 0.3 },
          standout: { state: 'Thankfulness', label: 'thankful', emoji: '🙏', strength: 'strong' } },
    ],
};
const seg = (startTime) => ({ startTime, sentence: 'x' });

describe('annotateTone', () => {
    it('leaves segments untouched without tone data', () => {
        const segments = [seg(1), seg(12)];
        const before = JSON.stringify(segments);
        expect(annotateTone(segments, null)).toBe(segments);
        expect(annotateTone(segments, { windows: [] })).toBe(segments);
        expect(JSON.stringify(segments)).toBe(before);
    });

    it('badges only the first line of a standout window', () => {
        const segments = [seg(1), seg(11), seg(15)];
        annotateTone(segments, tone);
        expect(segments[0].toneBadge).toBeUndefined();
        expect(segments[1].toneBadge.emoji).toBe('🙏');
        expect(segments[2].toneBadge).toBeUndefined();
    });

    it('still finds the window when a line was split at a new time', () => {
        // The original line started at 10; after a split the first half starts at 10.4 and
        // the second at 16. Only the first half carries the badge, and none is lost.
        const segments = [seg(10.4), seg(16)];
        annotateTone(segments, tone);
        expect(segments[0].toneBadge.label).toBe('Thankful');
        expect(segments[1].toneBadge).toBeUndefined();
    });

    it('ignores segments without a numeric start', () => {
        const segments = [{ sentence: 'no time' }];
        annotateTone(segments, tone);
        expect(segments[0].toneBadge).toBeUndefined();
    });
});

describe('buildBadge', () => {
    it('ranks rows against each state floor and words the aria label honestly', () => {
        const badge = buildBadge(tone.windows[1], tone);
        expect(badge.rows[0].head).toBe('Thankfulness');
        expect(badge.range).toBe('0:10–0:20');
        expect(badge.aria).toContain('Estimated from the voice');
    });

    it('has no rows when the model gave no states', () => {
        expect(buildBadge(tone.windows[1], { windows: tone.windows }).rows).toEqual([]);
    });
});
