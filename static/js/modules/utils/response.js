/**
 * Reading a fetch Response that is supposed to carry JSON.
 *
 * Callers used to do `const data = await response.json()` before checking
 * `response.ok`. When the server answers with an HTML page instead — a CSRF
 * rejection, a redirect to the login form after the session expired, or a
 * 500 error page — that parse throws, and the parse error is what the user
 * sees: "Failed to start summary reprocessing: Unexpected token '<', "
 * (issue #388). It names neither the real problem nor anything to do about it.
 */

import { isCsrfRejection } from '../csrf.js';

/**
 * Parse a JSON response, or throw an Error whose message a human can act on.
 *
 * @param {Response} response  the fetch response
 * @param {Function} t         the i18n lookup; messages are localized
 * @param {string} fallbackKey i18n key used when the server said nothing useful
 * @returns {Promise<Object>}  the parsed body
 */
export async function readJsonResponse(response, t, fallbackKey = 'errors.requestFailed') {
    const _t = t || ((key) => key);
    // Read as text first. A second .json() on a consumed body throws, and the
    // raw text is what the CSRF heuristic needs anyway.
    let text = '';
    try {
        text = await response.text();
    } catch (e) {
        throw new Error(_t('errors.responseUnreadable'));
    }

    let data = null;
    try {
        data = text ? JSON.parse(text) : null;
    } catch (e) {
        data = null;   // Not JSON. Almost always an HTML error page.
    }

    if (response.ok) {
        if (data === null) throw new Error(_t('errors.responseNotJson'));
        return data;
    }

    // Server said no. Work out what actually happened, most specific first.
    if (isCsrfRejection(response.status, text)) {
        const err = new Error(_t('errors.sessionExpiredReload'));
        err.isCsrfRejection = true;
        throw err;
    }
    if (response.status === 401 || response.status === 403 || response.redirected) {
        throw new Error(_t('errors.sessionExpiredReload'));
    }
    if (data && data.error) {
        throw new Error(data.error);        // A real JSON error from the app.
    }
    if (response.status >= 500) {
        throw new Error(_t('errors.serverErrorWithStatus', { status: response.status }));
    }
    throw new Error(_t(fallbackKey, { status: response.status }));
}
