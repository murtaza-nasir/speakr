import { describe, it, expect } from 'vitest';
import { computeUploadTimeout, UPLOAD_TIMEOUT_CONSTANTS } from './upload-timeout.js';

const { BASE_TIMEOUT_MS, MINUTE_MS, BYTES_PER_10MB } = UPLOAD_TIMEOUT_CONSTANTS;

describe('computeUploadTimeout', () => {
    it('returns the base floor for an empty or zero-size upload', () => {
        expect(computeUploadTimeout(0)).toBe(BASE_TIMEOUT_MS);
    });

    it('falls back to the base floor for invalid sizes', () => {
        expect(computeUploadTimeout(undefined)).toBe(BASE_TIMEOUT_MS);
        expect(computeUploadTimeout(null)).toBe(BASE_TIMEOUT_MS);
        expect(computeUploadTimeout(NaN)).toBe(BASE_TIMEOUT_MS);
        expect(computeUploadTimeout(-5)).toBe(BASE_TIMEOUT_MS);
    });

    it('adds one minute per 10 MB on top of the base floor', () => {
        // 10 MB -> base + 1 minute
        expect(computeUploadTimeout(BYTES_PER_10MB)).toBe(BASE_TIMEOUT_MS + MINUTE_MS);
        // 100 MB -> base + 10 minutes
        expect(computeUploadTimeout(10 * BYTES_PER_10MB)).toBe(BASE_TIMEOUT_MS + 10 * MINUTE_MS);
    });

    it('scales monotonically with size and never drops below the floor', () => {
        const small = computeUploadTimeout(1 * 1024 * 1024); // 1 MB
        const large = computeUploadTimeout(500 * 1024 * 1024); // 500 MB
        expect(small).toBeGreaterThanOrEqual(BASE_TIMEOUT_MS);
        expect(large).toBeGreaterThan(small);
    });

    it('gives a multi-hour recording a sane (not infinite) ceiling', () => {
        // ~300 MB recording: base 10 min + 30 min = 40 min, well under an hour.
        const fortyMinutes = 40 * MINUTE_MS;
        expect(computeUploadTimeout(300 * 1024 * 1024)).toBe(fortyMinutes);
    });
});
