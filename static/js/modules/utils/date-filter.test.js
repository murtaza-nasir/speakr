import { describe, expect, it } from 'vitest';

import { DATE_FILTER_MAX, DATE_FILTER_MIN, isValidDateInput } from './date-filter.js';

describe('isValidDateInput', () => {
    it('accepts a real day inside the range', () => {
        for (const value of ['2026-10-31', '2024-02-29', DATE_FILTER_MIN, DATE_FILTER_MAX]) {
            expect(isValidDateInput(value)).toBe(true);
        }
    });

    it('refuses a day that does not exist, a value outside the range and anything else', () => {
        for (const value of ['9999-99-99', '0000-00-00', '2026-13-40', '2025-02-29', '1899-12-31',
            '2101-01-01', '20255-01-01', 'not-a-date', '', null, undefined, 20261031]) {
            expect(isValidDateInput(value)).toBe(false);
        }
    });
});
