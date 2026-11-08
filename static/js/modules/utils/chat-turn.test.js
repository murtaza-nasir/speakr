/**
 * Unit tests for responseWithQuestion in chat-turn.js: the turns a
 * per-response Word download sends.
 */

import { describe, it, expect } from 'vitest';
import { responseWithQuestion } from './chat-turn.js';

const q1 = { role: 'user', content: 'What was decided?' };
const a1 = { role: 'assistant', content: 'The **budget**.', thinking: 'reasoning' };
const q2 = { role: 'user', content: 'And the date?' };
const a2 = { role: 'assistant', content: 'Friday.', sources: [{ n: 1, recording_id: 3, title: 'Standup' }] };

describe('responseWithQuestion', () => {
    it('returns the answer with the question before it', () => {
        expect(responseWithQuestion([q1, a1, q2, a2], 1)).toEqual([q1, a1]);
        expect(responseWithQuestion([q1, a1, q2, a2], 3)).toEqual([q2, a2]);
    });

    it('returns the message objects themselves, with thinking and sources', () => {
        const [question, answer] = responseWithQuestion([q1, a1], 1);
        expect(question).toBe(q1);
        expect(answer).toBe(a1);
        expect(responseWithQuestion([q2, a2], 1)[1].sources).toEqual(a2.sources);
    });

    it('skips user turns without text and other answers', () => {
        const empty = { role: 'user', content: '' };
        const error = { role: 'assistant', content: 'Error: timeout' };
        expect(responseWithQuestion([q1, empty, error, a2], 3)).toEqual([q1, a2]);
        expect(responseWithQuestion([q1, { role: 'user', content: 5 }, a2], 2)).toEqual([q1, a2]);
    });

    it('returns the answer alone when no question comes before it', () => {
        expect(responseWithQuestion([a1], 0)).toEqual([a1]);
        expect(responseWithQuestion([{ role: 'system', content: 'note' }, a1], 1)).toEqual([a1]);
    });

    it('returns nothing for a user turn, an empty answer or a bad index', () => {
        expect(responseWithQuestion([q1, a1], 0)).toEqual([]);
        expect(responseWithQuestion([q1, { role: 'assistant', content: '' }], 1)).toEqual([]);
        expect(responseWithQuestion([q1, a1], 5)).toEqual([]);
        expect(responseWithQuestion(null, 0)).toEqual([]);
    });
});
