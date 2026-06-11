/**
 * CSRF token helpers for code paths that bypass the fetch interceptor
 * in csrf-refresh.js.
 *
 * Flask-WTF's default WTF_CSRF_TIME_LIMIT is one hour, so the token in
 * the page's meta tag goes stale during long recordings or laptop
 * sleep (the 45-minute setInterval refresh in csrf-refresh.js does not
 * fire in throttled / suspended background tabs). The fetch interceptor
 * recovers from this transparently, but XMLHttpRequest callers (e.g.
 * the upload path in composables/upload.js, which needs XHR for upload
 * progress events) read the meta tag directly and would send the
 * expired token, getting a 400 back. These helpers give such callers
 * the same refresh-before-send / retry-on-rejection behaviour.
 */

/** Read the current CSRF token from the page's meta tag (or null). */
export function getCsrfTokenFromMeta() {
    if (typeof document === 'undefined') return null;
    return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || null;
}

/**
 * Fetch a fresh CSRF token from the server.
 *
 * Prefers the shared CSRFManager from csrf-refresh.js (so its cached
 * token stays in sync and concurrent refreshes are deduplicated), with
 * a direct call to /api/csrf-token as fallback for contexts where
 * csrf-refresh.js is not loaded. Throws on failure.
 */
export async function fetchFreshCsrfToken() {
    const manager = (typeof window !== 'undefined') ? window.csrfManager : null;
    if (manager && typeof manager.refreshToken === 'function') {
        return await manager.refreshToken();
    }

    // Use the original (un-intercepted) fetch if csrf-refresh.js saved
    // one, to match its own refresh behaviour.
    const doFetch = ((typeof window !== 'undefined') && window.originalFetch) || fetch;
    const response = await doFetch('/api/csrf-token', {
        method: 'GET',
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' }
    });
    if (!response.ok) {
        throw new Error(`Failed to refresh CSRF token: ${response.status}`);
    }
    const data = await response.json();
    if (!data.csrf_token) {
        throw new Error('No CSRF token in response');
    }
    // Keep the meta tag current for any other code that reads it.
    const metaTag = (typeof document !== 'undefined')
        ? document.querySelector('meta[name="csrf-token"]')
        : null;
    if (metaTag) {
        metaTag.setAttribute('content', data.csrf_token);
    }
    return data.csrf_token;
}

/**
 * Best-effort fresh token for an outgoing upload. A refresh failure
 * (offline, server hiccup) must not block the upload attempt itself,
 * so this falls back to whatever the meta tag currently holds.
 */
export async function getUploadCsrfToken() {
    try {
        return await fetchFreshCsrfToken();
    } catch (error) {
        console.warn('[CSRF] Token refresh failed, falling back to meta tag token:', error);
        return getCsrfTokenFromMeta();
    }
}

/**
 * Heuristic for "the server rejected our CSRF token", mirroring the
 * detection in csrf-refresh.js's fetch interceptor: a 400/403 whose
 * body (JSON error message or HTML error page) mentions csrf/token.
 */
export function isCsrfRejection(status, responseText) {
    if (status !== 400 && status !== 403) return false;
    const text = (responseText || '').toLowerCase();
    return text.includes('csrf') || text.includes('token');
}
