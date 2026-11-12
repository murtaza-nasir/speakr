import { describe, expect, it } from 'vitest';
import { CITE_PLACEHOLDER_RE, formatClock, markCitations, numberedSources, plainCitations, upsertSource }
    from './chat-citations.js';

const A = { n: 1, marker: '[S3 "read-only"]', start_time: 98.4, speaker: 'Priya Raman', quote: 'read-only', verified: true };
const B = { n: 2, marker: '[S7 "five weeks"]', start_time: 3725, speaker: null, quote: 'five weeks', verified: false };
const A2 = { ...A, marker: '[S3  "read-only"]' };   // same source, other spacing

describe('chat citations', () => {
    it('keeps one source per marker', () => {
        const list = [];
        upsertSource(list, A);
        upsertSource(list, A);
        upsertSource(list, A2);
        upsertSource(list, { n: 3 });
        expect(list).toHaveLength(2);
        expect(numberedSources(list).map((s) => s.n)).toEqual([1]);
    });

    it('turns only known markers into placeholders', () => {
        const text = 'Read-only [S3 "read-only"], again [S3  "read-only"], see [1] and [S9 "x"].';
        const marked = markCitations(text, [A, A2]);
        expect([...marked.matchAll(CITE_PLACEHOLDER_RE)].map((m) => m[1])).toEqual(['1', '1']);
        expect(marked).toContain('see [1] and [S9 "x"].');
    });

    it('writes plain text with a numbered sources list', () => {
        const text = plainCitations('A [S3 "read-only"] and B [S7 "five weeks"].', [A, B], 'Sources');
        expect(text).toBe('A [1] and B [2].\n\nSources\n1. 1:38 Priya Raman: “read-only”\n2. 1:02:05: “five weeks”');
    });

    it('leaves text without sources alone, timestamps included', () => {
        expect(plainCitations('At [00:07:35] they agreed.', [], 'Sources')).toBe('At [00:07:35] they agreed.');
        expect(markCitations('At [00:07:35].', undefined)).toBe('At [00:07:35].');
    });

    it('formats clock times', () => {
        expect(formatClock(0)).toBe('0:00');
        expect(formatClock(98.9)).toBe('1:38');
        expect(formatClock(3725)).toBe('1:02:05');
    });
});
