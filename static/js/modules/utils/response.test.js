import { describe, it, expect } from 'vitest';
import { readJsonResponse } from './response.js';

// Minimal stand-in for fetch's Response: the reader only uses these.
const res = ({ ok = true, status = 200, body = '', redirected = false }) => ({
    ok, status, redirected,
    text: async () => body,
});

// Pass the key straight through so assertions read as the key, not prose.
const t = (key, params) => (params ? `${key}:${JSON.stringify(params)}` : key);

describe('readJsonResponse', () => {
    it('returns the parsed body on success', async () => {
        const data = await readJsonResponse(res({ body: '{"id":7,"status":"SUMMARIZING"}' }), t);
        expect(data).toEqual({ id: 7, status: 'SUMMARIZING' });
    });

    it('reports an HTML error page as an expired session, not a parse error', async () => {
        // This is issue #388: the user saw `Unexpected token '<', "` because
        // .json() was called on a login page before checking response.ok.
        const html = '<!doctype html><html><body>CSRF token expired</body></html>';
        await expect(readJsonResponse(res({ ok: false, status: 400, body: html }), t))
            .rejects.toThrow('errors.sessionExpiredReload');
    });

    it('marks a CSRF rejection so callers can retry after refreshing', async () => {
        const html = '<!doctype html><html><body>The CSRF token is missing.</body></html>';
        await expect(readJsonResponse(res({ ok: false, status: 400, body: html }), t))
            .rejects.toMatchObject({ isCsrfRejection: true });
    });

    it('never surfaces a raw JSON parse error', async () => {
        const html = '<!doctype html><h1>500 Internal Server Error</h1>';
        try {
            await readJsonResponse(res({ ok: false, status: 500, body: html }), t);
            throw new Error('should have thrown');
        } catch (e) {
            expect(e.message).not.toMatch(/Unexpected token/);
            expect(e.message).not.toMatch(/JSON/i);
        }
    });

    it('prefers the application error message when the server sent JSON', async () => {
        await expect(readJsonResponse(res({ ok: false, status: 400, body: '{"error":"No valid transcription available"}' }), t))
            .rejects.toThrow('No valid transcription available');
    });

    it('treats a 401 as an expired session', async () => {
        await expect(readJsonResponse(res({ ok: false, status: 401, body: '' }), t))
            .rejects.toThrow('errors.sessionExpiredReload');
    });

    it('treats a redirect to the login page as an expired session', async () => {
        await expect(readJsonResponse(res({ ok: false, status: 200, redirected: true, body: '<html>login</html>' }), t))
            .rejects.toThrow('errors.sessionExpiredReload');
    });

    it('reports a 5xx with its status', async () => {
        await expect(readJsonResponse(res({ ok: false, status: 503, body: '' }), t))
            .rejects.toThrow('errors.serverErrorWithStatus:{"status":503}');
    });

    it('uses the caller fallback key for anything else', async () => {
        await expect(readJsonResponse(res({ ok: false, status: 418, body: '' }), t, 'errors.reprocessSummaryFailed'))
            .rejects.toThrow('errors.reprocessSummaryFailed:{"status":418}');
    });

    it('rejects a 200 whose body is not JSON', async () => {
        await expect(readJsonResponse(res({ ok: true, status: 200, body: '<html>oops</html>' }), t))
            .rejects.toThrow('errors.responseNotJson');
    });

    it('survives a body that cannot be read at all', async () => {
        const broken = { ok: true, status: 200, redirected: false,
                         text: async () => { throw new Error('network died'); } };
        await expect(readJsonResponse(broken, t)).rejects.toThrow('errors.responseUnreadable');
    });

    it('works without an i18n function', async () => {
        await expect(readJsonResponse(res({ ok: false, status: 401, body: '' }), null))
            .rejects.toThrow('errors.sessionExpiredReload');
    });
});
