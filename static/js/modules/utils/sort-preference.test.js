/**
 * Unit tests for resolveInitialSort in sort-preference.js.
 *
 * Precedence is: the browser's saved choice, then the account default, then
 * 'created_at'. Unknown values never win.
 */

import { describe, it, expect } from 'vitest';
import { resolveInitialSort } from './sort-preference.js';

describe('resolveInitialSort', () => {
    it('keeps the historical default when nothing is set', () => {
        expect(resolveInitialSort(null, undefined)).toBe('created_at');
        expect(resolveInitialSort(null, 'created_at')).toBe('created_at');
    });

    it('uses the account default when the browser has no saved choice', () => {
        expect(resolveInitialSort(null, 'meeting_date')).toBe('meeting_date');
    });

    it('lets the browser\'s saved choice override the account default', () => {
        expect(resolveInitialSort('created_at', 'meeting_date')).toBe('created_at');
        expect(resolveInitialSort('meeting_date', 'created_at')).toBe('meeting_date');
    });

    it('ignores values it does not know', () => {
        expect(resolveInitialSort('title', 'meeting_date')).toBe('meeting_date');
        expect(resolveInitialSort('meeting_date', 'title')).toBe('meeting_date');
        expect(resolveInitialSort('x', 'y')).toBe('created_at');
    });
});
