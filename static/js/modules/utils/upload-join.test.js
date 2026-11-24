import { describe, it, expect } from 'vitest';
import {
    compareForJoin, sortQueuedForJoin, moveQueuedItem, placeQueuedItem,
    totalJoinDuration, newJoinGroupId, isVideoFileName,
} from './upload-join.js';

const item = (clientId, name, lastModified, status = 'queued', duration = null) =>
    ({ clientId, status, duration, file: { name, lastModified } });

const ids = (queue) => queue.map(i => i.clientId);

describe('compareForJoin', () => {
    it('orders by modified time first', () => {
        const a = item('a', 'z.mp3', 1), b = item('b', 'a.mp3', 2);
        expect([b, a].sort(compareForJoin).map(i => i.clientId)).toEqual(['a', 'b']);
    });
    it('breaks ties by name with numbers compared as numbers', () => {
        const q = [item('10', 'part10.m4a', 5), item('2', 'part2.m4a', 5), item('1', 'Part1.m4a', 5)];
        expect(q.sort(compareForJoin).map(i => i.clientId)).toEqual(['1', '2', '10']);
    });
});

describe('sortQueuedForJoin', () => {
    it('sorts only the queued items and leaves the others in place', () => {
        const q = [item('late', 'b.mp3', 9), item('up', 'x.mp3', 0, 'uploading'), item('early', 'a.mp3', 1)];
        expect(ids(sortQueuedForJoin(q))).toEqual(['early', 'up', 'late']);
    });
});

describe('moveQueuedItem', () => {
    const q = [item('a', 'a', 1), item('busy', 'x', 0, 'ready'), item('b', 'b', 2), item('c', 'c', 3)];
    it('moves among queued items, skipping others', () => {
        expect(ids(moveQueuedItem(q, 'b', -1))).toEqual(['b', 'busy', 'a', 'c']);
        expect(ids(moveQueuedItem(q, 'a', 1))).toEqual(['b', 'busy', 'a', 'c']);
    });
    it('does nothing past either end or for unknown items', () => {
        expect(moveQueuedItem(q, 'a', -1)).toBe(q);
        expect(moveQueuedItem(q, 'c', 1)).toBe(q);
        expect(moveQueuedItem(q, 'nope', 1)).toBe(q);
    });
});

describe('placeQueuedItem', () => {
    it('drops an item at a position among the queued items', () => {
        const q = [item('a', 'a', 1), item('b', 'b', 2), item('c', 'c', 3)];
        expect(ids(placeQueuedItem(q, 'c', 0))).toEqual(['c', 'a', 'b']);
        expect(ids(placeQueuedItem(q, 'a', 2))).toEqual(['b', 'c', 'a']);
        expect(placeQueuedItem(q, 'a', 0)).toBe(q);
    });
});

describe('totalJoinDuration', () => {
    it('sums known durations', () => {
        expect(totalJoinDuration([item('a', 'a', 1, 'queued', 60), item('b', 'b', 2, 'queued', 30.5)])).toBe(90.5);
    });
      it('is null while any duration is unknown or for no items', () => {
          expect(totalJoinDuration([item('a', 'a', 1, 'queued', 60), item('b', 'b', 2)])).toBeNull();
          expect(totalJoinDuration([])).toBeNull();
      });
      it('sums the kept span of a part that has its own range', () => {
          const cut = item('a', 'a', 1, 'queued', 60);
          cut.trimOn = true;
          cut.trimStart = 10;
          cut.trimEnd = 40;
          const whole = item('b', 'b', 2, 'queued', 30);
          expect(totalJoinDuration([cut, whole])).toBe(60);
      });
      it('does not count a kept span past the file', () => {
          const cut = item('a', 'a', 1, 'queued', 300);
          cut.trimOn = true;
          cut.trimStart = 60;
          cut.trimEnd = 301220;
          expect(totalJoinDuration([cut])).toBe(240);
      });
});

describe('newJoinGroupId and isVideoFileName', () => {
    it('makes ids the server accepts', () => {
        for (let i = 0; i < 20; i++) expect(newJoinGroupId()).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
        expect(newJoinGroupId()).not.toBe(newJoinGroupId());
    });
    it('recognises video extensions', () => {
        expect(isVideoFileName('talk.MP4')).toBe(true);
        expect(isVideoFileName('talk.m4a')).toBe(false);
        expect(isVideoFileName(undefined)).toBe(false);
    });
});
