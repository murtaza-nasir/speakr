import { describe, expect, it } from 'vitest';
import { createFollow, createPlaybackIndex, isScrollKey } from './follow-playback.js';

describe('createPlaybackIndex', () => {
    const segments = [{ startTime: 0 }, { startTime: 2.5 }, { startTime: 4 }, { startTime: 9 }];

    it('gives the last segment that has begun', () => {
        const index = createPlaybackIndex(segments);
        expect(index.at(0)).toBe(0);
        expect(index.at(2.49)).toBe(0);
        expect(index.at(2.5)).toBe(1);
        expect(index.at(8.9)).toBe(2);
        expect(index.at(1000)).toBe(3);
    });

    it('gives -1 before the first segment and for no time', () => {
        const index = createPlaybackIndex([{ startTime: 3 }, { startTime: 5 }]);
        expect(index.at(1)).toBe(-1);
        expect(index.at(NaN)).toBe(-1);
        expect(index.at(undefined)).toBe(-1);
        expect(createPlaybackIndex([]).at(4)).toBe(-1);
        expect(createPlaybackIndex(null).at(4)).toBe(-1);
    });

    it('passes over segments without a time', () => {
        const index = createPlaybackIndex([{ startTime: 0 }, { sentence: 'no time' }, { startTime: undefined }, { startTime: 6 }]);
        expect(index.at(5)).toBe(0);
        expect(index.at(6)).toBe(3);
    });

    it('finds a segment among 20,000', () => {
        const many = Array.from({ length: 20000 }, (_, i) => ({ startTime: i * 0.5 }));
        const index = createPlaybackIndex(many);
        expect(index.at(7000.25)).toBe(14000);
        expect(index.at(9999.9)).toBe(19999);
    });
});

describe('isScrollKey', () => {
    it('knows the keys that move a list', () => {
        for (const key of ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ']) expect(isScrollKey({ key })).toBe(true);
        for (const key of ['a', 'Enter', 'Tab', 'Escape']) expect(isScrollKey({ key })).toBe(false);
    });

    it('leaves shortcuts alone', () => {
        expect(isScrollKey({ key: 'End', ctrlKey: true })).toBe(false);
        expect(isScrollKey({ key: 'ArrowDown', altKey: true })).toBe(false);
        expect(isScrollKey(null)).toBe(false);
    });
});

describe('createFollow', () => {
    it('brings each newly playing segment into view', () => {
        const follow = createFollow();
        expect(follow.following).toBe(true);
        expect(follow.paused).toBe(false);
        expect(follow.playing(3)).toBe(3);
        expect(follow.playing(3)).toBe(null);
        expect(follow.playing(4)).toBe(4);
        expect(follow.paused).toBe(false);
    });

    it('stops when the reader moves the transcript, and offers to follow again', () => {
        const follow = createFollow();
        follow.playing(3);
        follow.moved();
        expect(follow.following).toBe(false);
        expect(follow.paused).toBe(true);
        expect(follow.playing(4)).toBe(null);
        expect(follow.playing(5)).toBe(null);
        expect(follow.resume()).toBe(5);
        expect(follow.paused).toBe(false);
        expect(follow.playing(6)).toBe(6);
    });

    it('offers nothing while no segment is playing', () => {
        const follow = createFollow();
        follow.moved();
        expect(follow.paused).toBe(false);
        expect(follow.resume()).toBe(null);
        expect(follow.playing(-1)).toBe(null);
        expect(follow.playing(undefined)).toBe(null);
        expect(follow.current).toBe(-1);
        expect(follow.playing(0)).toBe(0);
    });
});
