/**
 * What the signed-in app leaves for the offline recorder (offline-recorder.html,
 * answered by the service worker when a page cannot be loaded), at each
 * online load of the main page:
 *
 *   1. the localStorage entry speakr.offlineRecorder: the id of the account
 *      signed in, the interface language and whether incognito mode is the
 *      default (utils/pending-uploads.js, offlineContext). The recorder saves
 *      its recordings for that account, and records nothing without the
 *      entry. It is removed at sign-out (service-worker-client.js).
 *   2. a PREPARE_OFFLINE_RECORDER message to the service worker, which then
 *      keeps the recorder's files of this release, and the locale file of
 *      this language, in its cache (static/sw.js).
 *
 * Neither holds a name, an address, a recording or a token.
 */

import { OFFLINE_CONTEXT_KEY, offlineContext } from '../utils/pending-uploads.js';

/**
 * @param {{userId: string|number, locale?: string, incognitoDefault?: boolean}} facts
 * @param {{storage?: Storage, serviceWorker?: ServiceWorkerContainer}} [env] - for the tests
 * @returns {object|null} the entry written, or null when there is no account
 */
export function rememberForOfflineRecorder(facts, env = {}) {
    const storage = env.storage || (typeof localStorage !== 'undefined' ? localStorage : null);
    const sw = env.serviceWorker
        || (typeof navigator !== 'undefined' && navigator.serviceWorker ? navigator.serviceWorker : null);
    const context = offlineContext(facts || {});
    try {
        if (storage) {
            if (context) storage.setItem(OFFLINE_CONTEXT_KEY, JSON.stringify(context));
            else storage.removeItem(OFFLINE_CONTEXT_KEY);
        }
    } catch (e) {
        // Storage unavailable: the recorder then records nothing.
    }
    if (context && sw && sw.controller) {
        try {
            sw.controller.postMessage({ type: 'PREPARE_OFFLINE_RECORDER', locale: context.locale });
        } catch (e) { /* no worker: nothing to prepare */ }
    }
    return context;
}
