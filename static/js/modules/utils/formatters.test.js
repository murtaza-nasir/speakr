/**
 * Unit tests for the pure formatting helpers in formatters.js.
 *
 * Several helpers call `toLocaleDateString` with an undefined locale, so the
 * exact rendered string depends on the host locale/timezone. For those we
 * assert timezone-robust properties (presence of the year, emptiness, or
 * pass-through of invalid input) rather than an exact string, and we use
 * mid-month/mid-year dates so a UTC<->local shift can never cross a day,
 * month, or year boundary.
 */

import { describe, it, expect } from 'vitest';
import {
    formatFileSize,
    formatDisplayDate,
    formatShortDate,
    formatStatus,
    getStatusClass,
    formatTime,
    formatDuration,
    formatProcessingDuration,
} from './formatters.js';

describe('formatFileSize', () => {
    it('returns "0 Bytes" for null, undefined and zero', () => {
        expect(formatFileSize(null)).toBe('0 Bytes');
        expect(formatFileSize(undefined)).toBe('0 Bytes');
        expect(formatFileSize(0)).toBe('0 Bytes');
    });

    it('treats negative input as zero', () => {
        expect(formatFileSize(-1024)).toBe('0 Bytes');
    });

    it('formats byte-range values without a decimal', () => {
        expect(formatFileSize(500)).toBe('500 Bytes');
        expect(formatFileSize(1023)).toBe('1023 Bytes');
    });

    it('formats kilobytes, megabytes and gigabytes', () => {
        expect(formatFileSize(1024)).toBe('1 KB');
        expect(formatFileSize(1536)).toBe('1.5 KB');
        expect(formatFileSize(1048576)).toBe('1 MB');
        expect(formatFileSize(1073741824)).toBe('1 GB');
        expect(formatFileSize(1099511627776)).toBe('1 TB');
    });

    it('rounds to two decimal places', () => {
        expect(formatFileSize(1234567)).toBe('1.18 MB');
    });
});

describe('formatDisplayDate', () => {
    it('returns an empty string for falsy input', () => {
        expect(formatDisplayDate('')).toBe('');
        expect(formatDisplayDate(null)).toBe('');
        expect(formatDisplayDate(undefined)).toBe('');
    });

    it('returns the original string for unparseable input', () => {
        expect(formatDisplayDate('not a date')).toBe('not a date');
    });

    it('returns the original string for an invalid date-only string', () => {
        expect(formatDisplayDate('2024-13-45')).toBe('2024-13-45');
    });

    it('renders a valid date and includes the year', () => {
        const result = formatDisplayDate('2024-06-15T12:00:00');
        expect(typeof result).toBe('string');
        expect(result).toContain('2024');
    });

    it('handles a date-only string', () => {
        const result = formatDisplayDate('2024-06-15');
        expect(result).toContain('2024');
    });
});

describe('formatShortDate', () => {
    it('returns an empty string for falsy input', () => {
        expect(formatShortDate('')).toBe('');
        expect(formatShortDate(null)).toBe('');
    });

    it('returns the original string for unparseable input', () => {
        expect(formatShortDate('nonsense')).toBe('nonsense');
    });

    it('includes a 2-digit year for dates outside the current year', () => {
        const result = formatShortDate('2019-06-15T12:00:00');
        expect(result).toContain('19');
    });

    it('omits the 4-digit year for current-year dates', () => {
        const year = new Date().getFullYear();
        const result = formatShortDate(`${year}-06-15T12:00:00`);
        expect(result).not.toContain(String(year));
        expect(result.length).toBeGreaterThan(0);
    });
});

describe('formatStatus', () => {
    const t = (key) => key;

    it('returns an empty string for falsy status or COMPLETED', () => {
        expect(formatStatus('', t)).toBe('');
        expect(formatStatus(null, t)).toBe('');
        expect(formatStatus('COMPLETED', t)).toBe('');
    });

    it('maps known statuses through the translation function', () => {
        expect(formatStatus('PENDING', t)).toBe('status.queued');
        expect(formatStatus('QUEUED', t)).toBe('status.queued');
        expect(formatStatus('PROCESSING', t)).toBe('status.processing');
        expect(formatStatus('TRANSCRIBING', t)).toBe('status.transcribing');
        expect(formatStatus('SUMMARIZING', t)).toBe('status.summarizing');
        expect(formatStatus('FAILED', t)).toBe('status.failed');
        expect(formatStatus('UPLOADING', t)).toBe('status.uploading');
    });

    it('title-cases unknown statuses', () => {
        expect(formatStatus('WEIRD', t)).toBe('Weird');
        expect(formatStatus('custom', t)).toBe('Custom');
    });
});

describe('getStatusClass', () => {
    it('maps each known status to its CSS class', () => {
        expect(getStatusClass('PENDING')).toBe('status-pending');
        expect(getStatusClass('QUEUED')).toBe('status-pending');
        expect(getStatusClass('PROCESSING')).toBe('status-processing');
        expect(getStatusClass('SUMMARIZING')).toBe('status-summarizing');
        expect(getStatusClass('COMPLETED')).toBe('');
        expect(getStatusClass('FAILED')).toBe('status-failed');
    });

    it('defaults unknown statuses to status-pending', () => {
        expect(getStatusClass('SOMETHING')).toBe('status-pending');
        expect(getStatusClass(undefined)).toBe('status-pending');
    });
});

describe('formatTime', () => {
    it('formats zero as 00:00', () => {
        expect(formatTime(0)).toBe('00:00');
    });

    it('zero-pads minutes and seconds', () => {
        expect(formatTime(5)).toBe('00:05');
        expect(formatTime(65)).toBe('01:05');
    });

    it('formats whole minutes', () => {
        expect(formatTime(600)).toBe('10:00');
        expect(formatTime(3599)).toBe('59:59');
    });

    it('lets minutes exceed 60 (no hour rollover)', () => {
        expect(formatTime(3661)).toBe('61:01');
    });
});

describe('formatDuration', () => {
    it('returns N/A for null, undefined and negative values', () => {
        expect(formatDuration(null)).toBe('N/A');
        expect(formatDuration(undefined)).toBe('N/A');
        expect(formatDuration(-5)).toBe('N/A');
    });

    it('renders sub-second values with two decimals', () => {
        expect(formatDuration(0)).toBe('0.00 seconds');
        expect(formatDuration(0.5)).toBe('0.50 seconds');
    });

    it('renders seconds below a minute', () => {
        expect(formatDuration(30)).toBe('30 sec');
        expect(formatDuration(59)).toBe('59 sec');
    });

    it('rounds to the nearest whole second', () => {
        expect(formatDuration(29.4)).toBe('29 sec');
        expect(formatDuration(59.6)).toBe('1 min');
    });

    it('renders minutes and seconds', () => {
        expect(formatDuration(60)).toBe('1 min');
        expect(formatDuration(90)).toBe('1 min 30 sec');
    });

    it('renders hours and omits seconds when hours are present', () => {
        expect(formatDuration(3600)).toBe('1 hr');
        expect(formatDuration(3661)).toBe('1 hr 1 min');
        expect(formatDuration(7200)).toBe('2 hr');
    });
});

describe('formatProcessingDuration', () => {
    it('returns null for null and undefined but not for zero', () => {
        expect(formatProcessingDuration(null)).toBeNull();
        expect(formatProcessingDuration(undefined)).toBeNull();
        expect(formatProcessingDuration(0)).toBe('0s');
    });

    it('renders seconds below a minute', () => {
        expect(formatProcessingDuration(30)).toBe('30s');
        expect(formatProcessingDuration(59)).toBe('59s');
    });

    it('renders whole minutes without a seconds part', () => {
        expect(formatProcessingDuration(60)).toBe('1m');
        expect(formatProcessingDuration(120)).toBe('2m');
    });

    it('renders minutes and seconds', () => {
        expect(formatProcessingDuration(90)).toBe('1m 30s');
        expect(formatProcessingDuration(125)).toBe('2m 5s');
    });
});
