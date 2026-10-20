import { describe, expect, it } from 'vitest';

import { buildMatcher, findMatches, replaceAllIn, replaceAt } from './find-replace.js';

describe('find and replace in transcript text (#440)', () => {
    it('finds literal text, ignoring case by default', () => {
        const m = buildMatcher('acme corp.');
        expect(findMatches(['ACME Corp. and acme corp.', 'acme corpx'], m)).toEqual([
            { index: 0, start: 0, end: 10 },
            { index: 0, start: 15, end: 25 },
        ]);
    });

    it('matches case when asked', () => {
        expect(findMatches(['Kubernetes kubernetes'], buildMatcher('kubernetes', { matchCase: true })))
            .toEqual([{ index: 0, start: 11, end: 21 }]);
    });

    it('matches whole words only when asked, in any script', () => {
        const m = buildMatcher('ana', { wholeWord: true });
        expect(findMatches(['Ana and Anabel', 'banana', 'Ana.'], m).map((x) => x.index)).toEqual([0, 2]);
        expect(findMatches(['Müller Müllers'], buildMatcher('Müller', { wholeWord: true }))).toHaveLength(1);
    });

    it('replaces every match with the literal replacement', () => {
        const m = buildMatcher('Speaker');
        expect(replaceAllIn('speaker one, Speaker two', m, 'Dana $1')).toEqual({ text: 'Dana $1 one, Dana $1 two', count: 2 });
    });

    it('finds nothing for an empty search', () => {
        expect(buildMatcher('')).toBe(null);
        expect(findMatches(['abc'], null)).toEqual([]);
        expect(replaceAllIn('abc', null, 'x')).toEqual({ text: 'abc', count: 0 });
    });

    it('replaces one match by position', () => {
        expect(replaceAt('a cat sat', 2, 5, 'dog')).toBe('a dog sat');
    });
});
