/**
 * Tests for the CSRF helpers used by XHR code paths that bypass the
 * fetch interceptor in csrf-refresh.js (the upload path in
 * composables/upload.js). Covers token refresh via the shared
 * CSRFManager, the direct /api/csrf-token fallback, the best-effort
 * fallback to the meta-tag token, and the CSRF-rejection heuristic.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
    getCsrfTokenFromMeta,
    fetchFreshCsrfToken,
    getUploadCsrfToken,
    isCsrfRejection
} from './csrf.js';

describe('csrf helpers', () => {
    let originalDocument;
    let originalWindow;
    let metaTag;

    const mockMeta = (token) => {
        metaTag = {
            _content: token,
            getAttribute: vi.fn(() => metaTag._content),
            setAttribute: vi.fn((name, val) => { metaTag._content = val; }),
        };
        global.document = {
            querySelector: vi.fn((sel) =>
                sel === 'meta[name="csrf-token"]' ? metaTag : null),
        };
    };

    beforeEach(() => {
        originalDocument = global.document;
        originalWindow = global.window;
        global.window = {};
        mockMeta('stale-token');
    });

    afterEach(() => {
        global.document = originalDocument;
        global.window = originalWindow;
        vi.restoreAllMocks();
    });

    describe('getCsrfTokenFromMeta', () => {
        it('reads the token from the meta tag', () => {
            expect(getCsrfTokenFromMeta()).toBe('stale-token');
        });

        it('returns null when the meta tag is missing', () => {
            global.document = { querySelector: vi.fn(() => null) };
            expect(getCsrfTokenFromMeta()).toBe(null);
        });
    });

    describe('fetchFreshCsrfToken', () => {
        it('prefers window.csrfManager.refreshToken when available', async () => {
            const refreshToken = vi.fn(async () => 'manager-token');
            global.window.csrfManager = { refreshToken };

            expect(await fetchFreshCsrfToken()).toBe('manager-token');
            expect(refreshToken).toHaveBeenCalledTimes(1);
        });

        it('falls back to /api/csrf-token and updates the meta tag', async () => {
            global.window.originalFetch = vi.fn(async () => ({
                ok: true,
                json: async () => ({ csrf_token: 'fresh-token' }),
            }));

            expect(await fetchFreshCsrfToken()).toBe('fresh-token');
            expect(global.window.originalFetch).toHaveBeenCalledWith(
                '/api/csrf-token',
                expect.objectContaining({ method: 'GET', credentials: 'same-origin' })
            );
            expect(metaTag.setAttribute).toHaveBeenCalledWith('content', 'fresh-token');
        });

        it('throws when the endpoint responds non-OK', async () => {
            global.window.originalFetch = vi.fn(async () => ({ ok: false, status: 500 }));
            await expect(fetchFreshCsrfToken()).rejects.toThrow('500');
        });

        it('throws when the response has no csrf_token', async () => {
            global.window.originalFetch = vi.fn(async () => ({
                ok: true,
                json: async () => ({}),
            }));
            await expect(fetchFreshCsrfToken()).rejects.toThrow('No CSRF token');
        });
    });

    describe('getUploadCsrfToken', () => {
        it('returns the freshly fetched token on success', async () => {
            global.window.csrfManager = { refreshToken: vi.fn(async () => 'fresh-token') };
            expect(await getUploadCsrfToken()).toBe('fresh-token');
        });

        it('falls back to the meta tag token when refresh fails', async () => {
            vi.spyOn(console, 'warn').mockImplementation(() => {});
            global.window.csrfManager = {
                refreshToken: vi.fn(async () => { throw new Error('offline'); }),
            };
            expect(await getUploadCsrfToken()).toBe('stale-token');
        });
    });

    describe('isCsrfRejection', () => {
        it('matches 400/403 with a CSRF-flavoured body', () => {
            expect(isCsrfRejection(400, '{"error": "The CSRF token has expired."}')).toBe(true);
            expect(isCsrfRejection(403, '<html><body>CSRF validation failed</body></html>')).toBe(true);
            expect(isCsrfRejection(400, 'The CSRF session token is missing.')).toBe(true);
        });

        it('rejects other statuses regardless of body', () => {
            expect(isCsrfRejection(500, 'csrf')).toBe(false);
            expect(isCsrfRejection(200, 'csrf token')).toBe(false);
            expect(isCsrfRejection(413, 'token')).toBe(false);
        });

        it('rejects 400s that are not CSRF-related', () => {
            expect(isCsrfRejection(400, '{"error": "No file selected"}')).toBe(false);
            expect(isCsrfRejection(400, '')).toBe(false);
            expect(isCsrfRejection(400, null)).toBe(false);
        });
    });
});
