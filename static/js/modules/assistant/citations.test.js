/**
 * Citation numbering in the shared Ambrose chat (phase 2b): segment markers
 * and links to other recordings share one numbering, which continues through
 * the chat.
 */
import { describe, expect, it } from 'vitest';
import { historySources, numberAnswer, plainAnswer, priorNumbers } from './citations.js';

const seg = (marker, index, quote, extra = {}) => ({
    n: 1, marker, segment_index: index, start_time: 754.2, end_time: 761, speaker: 'Omar', quote, verified: true, ...extra,
});
const travel = seg('[S1 "budget freeze"]', 1, 'budget freeze');
const hiring = seg('[S4 "hiring stays open"]', 4, 'hiring stays open', { start_time: 30, speaker: 'Dana' });

describe('numberAnswer', () => {
    it('numbers segment markers and library links in the order they appear', () => {
        const content = 'Travel [S1 "budget freeze"]; see also [Budget review @ 2:05](/recordings/73?t=125) and [S4 "hiring stays open"].';
        const { text, occurrences, sources } = numberAnswer(content, [travel, hiring], null);
        expect(occurrences.map((o) => [o.n, o.kind])).toEqual([[1, 'segment'], [2, 'library'], [3, 'segment']]);
        expect(occurrences[1]).toMatchObject({ recording_id: 73, seconds: 125, title: 'Budget review' });
        expect(text).not.toContain('[S1');
        expect(text).not.toContain('/recordings/73');
        expect(sources.map((s) => s.n)).toEqual([1, 2, 3]);
    });

    it('reuses a number for the same quote or the same recording', () => {
        const content = 'A [S1 "budget freeze"] B [Budget review](/recordings/73) C [S1  "Budget  freeze"] D [Budget review @ 9:00](/recordings/73?t=540)';
        const respaced = seg('[S1  "Budget  freeze"]', 1, 'Budget  freeze');
        const { occurrences, sources } = numberAnswer(content, [travel, respaced], null);
        expect(occurrences.map((o) => o.n)).toEqual([1, 2, 1, 2]);
        expect(sources).toHaveLength(2);
    });

    it('continues the numbering of earlier answers', () => {
        const first = { role: 'assistant', content: 'x', sources: [{ n: 1, kind: 'segment', segment_index: 1, quote: 'budget freeze' },
            { n: 2, kind: 'library', recording_id: 73, title: 'Budget review' }] };
        const messages = [{ role: 'user', content: 'q' }, first, { role: 'user', content: 'more?' }];
        const prior = priorNumbers(messages, 3);
        expect(prior.next).toBe(3);
        const { occurrences } = numberAnswer('New [S4 "hiring stays open"], old [S1 "budget freeze"], [Budget review](/recordings/73), [Other](/recordings/9)',
            [hiring, travel], prior);
        expect(occurrences.map((o) => o.n)).toEqual([3, 1, 2, 4]);
    });

    it('leaves a marker without its source, and ordinary links, as text', () => {
        const content = 'Plain [S9 "nothing"] and [a site](https://example.test) and [1].';
        const { text, occurrences } = numberAnswer(content, [], null);
        expect(text).toBe(content);
        expect(occurrences).toEqual([]);
    });
});

describe('plainAnswer and historySources', () => {
    it('writes citations as [n] with the sources under the label', () => {
        const content = 'Travel [S1 "budget freeze"], also [Budget review @ 2:05](/recordings/73?t=125).';
        expect(plainAnswer(content, [travel], null, 'Sources')).toBe(
            'Travel [1], also [2].\n\nSources\n1. 12:34 Omar: “budget freeze”\n2. Budget review');
    });

    it('sends back only what the server reads', () => {
        const { sources } = numberAnswer('[S1 "budget freeze"] [Budget review](/recordings/73)', [travel], null);
        expect(historySources(sources)).toEqual([
            { n: 1, segment_index: 1, start_time: 754.2, speaker: 'Omar', quote: 'budget freeze' },
            { n: 2, recording_id: 73, title: 'Budget review' },
        ]);
        expect(historySources([])).toBeUndefined();
    });
});
