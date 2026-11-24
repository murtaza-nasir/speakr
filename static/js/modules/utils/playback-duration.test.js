import { describe, expect, it } from 'vitest';
import { playedDuration, seekableTime } from './playback-duration.js';

describe('playedDuration', () => {
    it('replaces the whole-file length with the kept span', () => {
        expect(playedDuration(300, 240)).toBe(240);
        expect(playedDuration(0, 240)).toBe(240);
    });

    it('keeps the length already shown when the next reading is empty', () => {
        expect(playedDuration(240, 0)).toBe(240);
        expect(playedDuration(240, null)).toBe(240);
        expect(playedDuration(240, undefined)).toBe(240);
    });
});

describe('seekableTime', () => {
    it('stops a seek at the file when the bar still shows the whole upload', () => {
        expect(seekableTime(250, 300, 240)).toBeCloseTo(239.95, 5);
    });

    it('leaves a time inside the kept span where it is', () => {
        expect(seekableTime(70, 240, 240)).toBe(70);
    });

    it('does not extend a seek with a missing media length', () => {
        expect(seekableTime(10, 240, Infinity)).toBe(10);
        expect(seekableTime(Number.NaN, 240, 240)).toBeNull();
    });
});
