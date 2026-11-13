import { describe, expect, it } from 'vitest';
import { compareReleaseKeys, releaseVersion, releasesToShow } from './whats-new.js';

const ALL = ['0_10_20', '0_10_21', '0_10_22', '0_11_0'];

describe('releasesToShow', () => {
    it('shows every release after the one last seen, newest first', () => {
        expect(releasesToShow(ALL, '0_10_22', '0_10_20')).toEqual(['0_10_22', '0_10_21']);
    });
    it('shows only the running release when the previous one was seen', () => {
        expect(releasesToShow(ALL, '0_10_21', '0_10_20')).toEqual(['0_10_21']);
    });
    it('shows nothing when the running release was seen', () => {
        expect(releasesToShow(ALL, '0_10_21', '0_10_21')).toEqual([]);
    });
    it('never shows notes of a release newer than the running one', () => {
        expect(releasesToShow(ALL, '0_10_21', null)).toEqual(['0_10_21', '0_10_20']);
    });
    it('shows everything up to the running release to an account that never saw the dialog', () => {
        expect(releasesToShow(ALL, '0_11_0', null)).toEqual(['0_11_0', '0_10_22', '0_10_21', '0_10_20']);
        expect(releasesToShow(ALL, '0_11_0', 'garbage')).toHaveLength(4);
    });
    it('shows nothing after a downgrade', () => {
        expect(releasesToShow(ALL, '0_10_20', '0_10_22')).toEqual([]);
    });
    it('caps the number of releases', () => {
        expect(releasesToShow(ALL, '0_11_0', null, 2)).toEqual(['0_11_0', '0_10_22']);
    });
    it('compares numerically, not as text', () => {
        expect(compareReleaseKeys('0_10_9', '0_10_20')).toBeLessThan(0);
        expect(releasesToShow(['0_10_9', '0_10_20', 'x'], '0_10_20', null)).toEqual(['0_10_20', '0_10_9']);
    });
    it('needs a running release', () => {
        expect(releasesToShow(ALL, null, null)).toEqual([]);
    });
});

describe('releaseVersion', () => {
    it('turns a key into a version', () => {
        expect(releaseVersion('0_10_21')).toBe('0.10.21');
    });
});
