/**
 * Unit tests for the relative-date helpers in dates.js.
 *
 * Every helper except isSameDay/getDateForSorting reads `new Date()` for "now",
 * so we pin the system clock with fake timers to a fixed mid-month Wednesday
 * (2024-06-12, getDay()===3). All input dates are built with the local
 * `new Date(year, monthIndex, day)` constructor so the comparisons happen in
 * the same timezone as the helpers and stay deterministic in any CI timezone.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
    isSameDay,
    isToday,
    isYesterday,
    isThisWeek,
    isLastWeek,
    isThisMonth,
    isLastMonth,
    getDateForSorting,
} from './dates.js';

// Wednesday, 12 June 2024 at noon. Week (Mon-Sun) = Jun 10 - Jun 16.
const NOW = new Date(2024, 5, 12, 12, 0, 0);

describe('isSameDay', () => {
    it('is true for the same calendar day at different times', () => {
        expect(isSameDay(new Date(2024, 5, 12, 8), new Date(2024, 5, 12, 20))).toBe(true);
    });

    it('is false for different days', () => {
        expect(isSameDay(new Date(2024, 5, 12), new Date(2024, 5, 13))).toBe(false);
    });

    it('is false for the same day number in a different month', () => {
        expect(isSameDay(new Date(2024, 5, 12), new Date(2024, 6, 12))).toBe(false);
    });

    it('is false for the same day/month in a different year', () => {
        expect(isSameDay(new Date(2024, 5, 12), new Date(2023, 5, 12))).toBe(false);
    });
});

describe('relative-to-now helpers', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.setSystemTime(NOW);
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    describe('isToday', () => {
        it('is true for today regardless of time of day', () => {
            expect(isToday(new Date(2024, 5, 12, 0, 0, 1))).toBe(true);
            expect(isToday(new Date(2024, 5, 12, 23, 59))).toBe(true);
        });
        it('is false for yesterday and tomorrow', () => {
            expect(isToday(new Date(2024, 5, 11))).toBe(false);
            expect(isToday(new Date(2024, 5, 13))).toBe(false);
        });
    });

    describe('isYesterday', () => {
        it('is true for the day before now', () => {
            expect(isYesterday(new Date(2024, 5, 11, 15))).toBe(true);
        });
        it('is false for today and two days ago', () => {
            expect(isYesterday(new Date(2024, 5, 12))).toBe(false);
            expect(isYesterday(new Date(2024, 5, 10))).toBe(false);
        });
    });

    describe('isThisWeek', () => {
        it('is true for the Monday and Sunday bounding the current week', () => {
            expect(isThisWeek(new Date(2024, 5, 10))).toBe(true);  // Monday
            expect(isThisWeek(new Date(2024, 5, 16, 12))).toBe(true); // Sunday
        });
        it('is false just outside the week boundaries', () => {
            expect(isThisWeek(new Date(2024, 5, 9, 12))).toBe(false);  // prev Sunday
            expect(isThisWeek(new Date(2024, 5, 17))).toBe(false);     // next Monday
        });
    });

    describe('isLastWeek', () => {
        it('is true for the previous Monday-to-Sunday window (Jun 3 - Jun 9)', () => {
            expect(isLastWeek(new Date(2024, 5, 3))).toBe(true);
            expect(isLastWeek(new Date(2024, 5, 9, 12))).toBe(true);
        });
        it('is false for the current week and two weeks ago', () => {
            expect(isLastWeek(new Date(2024, 5, 10))).toBe(false);
            expect(isLastWeek(new Date(2024, 5, 2))).toBe(false);
        });
    });

    describe('isThisMonth', () => {
        it('is true for any day in the current month/year', () => {
            expect(isThisMonth(new Date(2024, 5, 1))).toBe(true);
            expect(isThisMonth(new Date(2024, 5, 30))).toBe(true);
        });
        it('is false for an adjacent month or the same month in another year', () => {
            expect(isThisMonth(new Date(2024, 4, 30))).toBe(false);
            expect(isThisMonth(new Date(2023, 5, 12))).toBe(false);
        });
    });

    describe('isLastMonth', () => {
        it('is true for any day in the previous month (May 2024)', () => {
            expect(isLastMonth(new Date(2024, 4, 1))).toBe(true);
            expect(isLastMonth(new Date(2024, 4, 31))).toBe(true);
        });
        it('is false for the current month and the same month in another year', () => {
            expect(isLastMonth(new Date(2024, 5, 1))).toBe(false);
            expect(isLastMonth(new Date(2023, 4, 15))).toBe(false);
        });
    });
});

describe('week helpers when "now" is a Sunday (day === 0 edge case)', () => {
    // Sunday, 16 June 2024. getDay()===0 exercises the special Monday-start
    // ternary (`day === 0 ? -6 : 1`) in isThisWeek/isLastWeek.
    beforeEach(() => {
        vi.useFakeTimers();
        vi.setSystemTime(new Date(2024, 5, 16, 12, 0, 0));
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it('treats Sunday as the end of the current week (Jun 10 - Jun 16)', () => {
        expect(isThisWeek(new Date(2024, 5, 10))).toBe(true);  // Monday
        expect(isThisWeek(new Date(2024, 5, 16, 8))).toBe(true); // the Sunday itself
        expect(isThisWeek(new Date(2024, 5, 9))).toBe(false);  // prev Sunday
        expect(isThisWeek(new Date(2024, 5, 17))).toBe(false); // next Monday
    });

    it('treats the prior Mon-Sun window as last week (Jun 3 - Jun 9)', () => {
        expect(isLastWeek(new Date(2024, 5, 3))).toBe(true);
        expect(isLastWeek(new Date(2024, 5, 9, 12))).toBe(true);
        expect(isLastWeek(new Date(2024, 5, 10))).toBe(false);
    });
});

describe('getDateForSorting', () => {
    it('uses meeting_date when sorting by meeting_date', () => {
        const rec = { meeting_date: '2024-06-12', created_at: '2024-01-01' };
        const result = getDateForSorting(rec, 'meeting_date');
        expect(result.getTime()).toBe(new Date('2024-06-12').getTime());
    });

    it('uses created_at for any other sort key', () => {
        const rec = { meeting_date: '2024-06-12', created_at: '2024-01-01' };
        const result = getDateForSorting(rec, 'created_at');
        expect(result.getTime()).toBe(new Date('2024-01-01').getTime());
    });

    it('returns null when the selected date field is missing', () => {
        expect(getDateForSorting({ created_at: null }, 'created_at')).toBeNull();
        expect(getDateForSorting({ meeting_date: null, created_at: '2024-01-01' }, 'meeting_date')).toBeNull();
        expect(getDateForSorting({}, 'created_at')).toBeNull();
    });
});
