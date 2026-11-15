import { describe, it, expect } from 'vitest';
import {
    advancedFilterCount, composerHeight, detailDates, formatCompactDate, phoneListShown, sameCalendarDay, skipTarget, speakerAt,
} from './phone-layout.js';

const now = new Date(2026, 10, 15, 12, 0);
const opts = { locale: 'en-US', now };

describe('the dates of the details line', () => {
    it('shows both dates when they are on different days', () => {
        const dates = detailDates(new Date(2026, 10, 12, 10, 2), new Date(2026, 10, 14, 9, 15), opts);
        expect(dates.meeting).toBe('Nov 12, 10:02 AM');
        expect(dates.uploaded).toBe('Nov 14');
        expect(dates.only).toBe('');
    });

    it('shows one date when both are on the same day', () => {
        const dates = detailDates(new Date(2026, 10, 13, 8, 41), new Date(2026, 10, 13, 23, 59), opts);
        expect(dates.meeting).toBe('Nov 13, 8:41 AM');
        expect(dates.uploaded).toBe('');
    });

    it('shows the upload date alone when there is no meeting date', () => {
        const dates = detailDates(null, new Date(2026, 10, 14, 9, 15), opts);
        expect(dates).toEqual({ meeting: '', uploaded: 'Nov 14, 9:15 AM', only: 'uploaded' });
        expect(detailDates(new Date(NaN), new Date(2026, 10, 14, 9, 15), opts).only).toBe('uploaded');
    });

    it('shows the meeting date alone when there is no upload date', () => {
        expect(detailDates(new Date(2026, 10, 12, 10, 2), null, opts)).toEqual({ meeting: 'Nov 12, 10:02 AM', uploaded: '', only: '' });
    });

    it('is empty without dates', () => {
        expect(detailDates(null, undefined, opts)).toEqual({ meeting: '', uploaded: '', only: '' });
    });

    it('adds the year to a date of another year', () => {
        const dates = detailDates(new Date(2025, 11, 30, 15, 0), new Date(2026, 0, 2, 9, 0), opts);
        expect(dates.meeting).toBe('Dec 30, 2025, 3:00 PM');
        expect(dates.uploaded).toBe('Jan 2');
        expect(formatCompactDate(new Date(2024, 4, 3), opts)).toBe('May 3, 2024');
    });

    it('compares days in the calendar, not 24 hours', () => {
        expect(sameCalendarDay(new Date(2026, 10, 12, 23, 59), new Date(2026, 10, 13, 0, 1))).toBe(false);
        expect(sameCalendarDay(new Date(2026, 10, 12, 0, 1), new Date(2026, 10, 12, 23, 59))).toBe(true);
        expect(sameCalendarDay(new Date(NaN), new Date())).toBe(false);
    });

    it('falls back to the default locale for an unknown one', () => {
        expect(formatCompactDate(new Date(2026, 10, 12), { locale: 'not a locale', now })).not.toBe('');
        expect(formatCompactDate(null)).toBe('');
    });
});

describe('the player strip', () => {
    const segments = [{ speaker: 'Dana' }, { speaker: '' }, {}, { speaker: 'Marco' }];

    it('names the speaker of the playing segment', () => {
        expect(speakerAt(segments, 0)).toBe('Dana');
        expect(speakerAt(segments, 3)).toBe('Marco');
    });

    it('names nobody without a speaker or a playing segment', () => {
        expect(speakerAt(segments, 1)).toBe('');
        expect(speakerAt(segments, 2)).toBe('');
        expect(speakerAt(segments, -1)).toBe('');
        expect(speakerAt(segments, null)).toBe('');
        expect(speakerAt(segments, 9)).toBe('');
        expect(speakerAt(null, 0)).toBe('');
    });

    it('skips inside the recording', () => {
        expect(skipTarget(30, -15, 100)).toBe(15);
        expect(skipTarget(30, 15, 100)).toBe(45);
        expect(skipTarget(5, -15, 100)).toBe(0);
        expect(skipTarget(95, 15, 100)).toBe(100);
        expect(skipTarget(NaN, 15, 100)).toBe(15);
        expect(skipTarget(30, 15, 0)).toBe(45);
    });
});

describe('the composer', () => {
    it('is one line high and grows to about five lines', () => {
        expect(composerHeight(20)).toBe(44);
        expect(composerHeight(64)).toBe(64);
        expect(composerHeight(64.2)).toBe(65);
        expect(composerHeight(400)).toBe(124);
        expect(composerHeight(NaN)).toBe(44);
        expect(composerHeight(90, { min: 40, max: 80 })).toBe(80);
    });
});

describe('the list as a screen', () => {
    const base = { isPhone: true, sidebarCollapsed: true, view: 'detail', hasRecording: true };

    it('is never shown as a screen on wider windows', () => {
        expect(phoneListShown({ ...base, isPhone: false, sidebarCollapsed: false })).toBe(false);
    });

    it('is hidden behind an open recording', () => {
        expect(phoneListShown(base)).toBe(false);
    });

    it('is shown when opened over a recording', () => {
        expect(phoneListShown({ ...base, sidebarCollapsed: false })).toBe(true);
    });

    it('is shown when no recording is open', () => {
        expect(phoneListShown({ ...base, hasRecording: false })).toBe(true);
        expect(phoneListShown({ ...base, view: 'upload', hasRecording: false })).toBe(true);
    });

    it('leaves the screen to a recording in progress', () => {
        expect(phoneListShown({ ...base, view: 'recording', hasRecording: false })).toBe(false);
        expect(phoneListShown({ ...base, view: 'recording', sidebarCollapsed: false })).toBe(true);
    });
});

describe('the filters behind the search field', () => {
    it('counts each kind once', () => {
        expect(advancedFilterCount()).toBe(0);
        expect(advancedFilterCount({ tags: [1, 2], speakers: ['a'] })).toBe(2);
        expect(advancedFilterCount({ datePreset: 'today', dateRange: { start: '2026-01-01', end: '' } })).toBe(1);
        expect(advancedFilterCount({ dateRange: { start: '', end: '2026-01-01' }, audioRemoved: true, needsTranscription: true, needsSummary: true, needsSpeakers: true })).toBe(5);
    });
});
