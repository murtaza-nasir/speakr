/**
 * Unit tests for createApiClient in api.js.
 *
 * createApiClient is a thin fetch wrapper, but it owns real logic worth
 * pinning: it attaches the CSRF token (read lazily from a ref-like object on
 * every call), sets the JSON content type for json verbs, omits it for
 * multipart form data, serialises bodies, and returns the fetch response. We
 * mock global.fetch and assert the request shape, mirroring the DOM-mock
 * pattern used elsewhere in the suite.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createApiClient } from './api.js';

describe('createApiClient', () => {
    let fetchMock;
    let originalFetch;
    let csrfToken;

    beforeEach(() => {
        fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 200 }));
        originalFetch = global.fetch;
        global.fetch = fetchMock;
        csrfToken = { value: 'token-abc' };
    });

    afterEach(() => {
        global.fetch = originalFetch;
    });

    it('get() sends CSRF + JSON content-type headers and no method', async () => {
        const api = createApiClient(csrfToken);
        const res = await api.get('/items');
        expect(res).toEqual({ ok: true, status: 200 });
        expect(fetchMock).toHaveBeenCalledTimes(1);
        const [url, opts] = fetchMock.mock.calls[0];
        expect(url).toBe('/items');
        expect(opts.method).toBeUndefined();
        expect(opts.headers).toEqual({
            'X-CSRFToken': 'token-abc',
            'Content-Type': 'application/json',
        });
    });

    it('post() uses POST and serialises the body, defaulting to {}', async () => {
        const api = createApiClient(csrfToken);
        await api.post('/create', { name: 'x' });
        let [url, opts] = fetchMock.mock.calls[0];
        expect(url).toBe('/create');
        expect(opts.method).toBe('POST');
        expect(opts.body).toBe(JSON.stringify({ name: 'x' }));
        expect(opts.headers['Content-Type']).toBe('application/json');
        expect(opts.headers['X-CSRFToken']).toBe('token-abc');

        await api.post('/create');
        [, opts] = fetchMock.mock.calls[1];
        expect(opts.body).toBe('{}');
    });

    it('put() uses PUT and serialises the body', async () => {
        const api = createApiClient(csrfToken);
        await api.put('/update/1', { a: 1 });
        const [url, opts] = fetchMock.mock.calls[0];
        expect(url).toBe('/update/1');
        expect(opts.method).toBe('PUT');
        expect(opts.body).toBe(JSON.stringify({ a: 1 }));
        expect(opts.headers['Content-Type']).toBe('application/json');
    });

    it('delete() uses DELETE with CSRF + JSON headers and no body', async () => {
        const api = createApiClient(csrfToken);
        await api.delete('/remove/1');
        const [url, opts] = fetchMock.mock.calls[0];
        expect(url).toBe('/remove/1');
        expect(opts.method).toBe('DELETE');
        expect(opts.body).toBeUndefined();
        expect(opts.headers['X-CSRFToken']).toBe('token-abc');
        expect(opts.headers['Content-Type']).toBe('application/json');
    });

    it('postFormData() sends the form body and omits the content-type header', async () => {
        const api = createApiClient(csrfToken);
        const form = { isForm: true };
        await api.postFormData('/upload', form);
        const [url, opts] = fetchMock.mock.calls[0];
        expect(url).toBe('/upload');
        expect(opts.method).toBe('POST');
        expect(opts.body).toBe(form);
        expect(opts.headers).toEqual({ 'X-CSRFToken': 'token-abc' });
        expect(opts.headers['Content-Type']).toBeUndefined();
    });

    it('reads the CSRF token lazily on each request', async () => {
        const api = createApiClient(csrfToken);
        await api.get('/first');
        csrfToken.value = 'rotated-token';
        await api.get('/second');
        expect(fetchMock.mock.calls[0][1].headers['X-CSRFToken']).toBe('token-abc');
        expect(fetchMock.mock.calls[1][1].headers['X-CSRFToken']).toBe('rotated-token');
    });
});
