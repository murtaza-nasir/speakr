/**
 * The page's side of the service worker (static/sw.js), and what is removed
 * from the browser at sign-out. A classic script, loaded with ?v=<app
 * version> by the signed-in pages and by the sign-in pages.
 *
 * Registration (signed-in pages only; data-register on the script tag):
 *   /sw.js?v=<app version> is registered with scope /. A registration of an
 *   earlier release, at /static/sw.js with scope /static/, is removed first,
 *   so one registration remains. A service worker needs a secure context
 *   (https, localhost or 127.0.0.1); elsewhere nothing is registered and
 *   the app works without it.
 *
 * Sign-out:
 *   1. Before the sign-out form is sent: when this browser holds a recording
 *      that was not finished or uploads that failed (IndexedDB), or an
 *      upload in slices that did not finish (localStorage), the person is
 *      asked, since those copies are deleted at sign-out. Cancel keeps
 *      them and the session.
 *   2. The server answers the sign-out with the cookie speakr_signed_out.
 *      The page that follows (the sign-in page) then clears sessionStorage,
 *      the IndexedDB stores SpeakrRecordings and SpeakrFailedUploads, the
 *      localStorage entry of unfinished uploads in slices (file name, size
 *      and modification time), the localStorage entry of the offline
 *      recorder (the account id it saves recordings for), and every Cache
 *      Storage entry, and tells the worker. The recordings made on the
 *      offline recorder page are records of SpeakrFailedUploads, so they
 *      are counted in step 1 and deleted with the others. Doing this after
 *      the server's answer means that a sign-out the person cancels (the
 *      browser's question about a recording in progress) deletes nothing.
 *
 * window.SpeakrServiceWorker = { register, localCopies, clearAfterSignOut, ready }
 */
(function (root) {
    'use strict';

    const SIGNED_OUT_COOKIE = 'speakr_signed_out';
    // The stores that hold user data: [database, object store].
    const USER_STORES = [['SpeakrRecordings', 'activeRecording'], ['SpeakrFailedUploads', 'failedUploads']];
    // The localStorage entry of uploads in slices that did not finish: per
    // upload the file name, size and modification time with the id of its
    // session, kept for 24 hours so that the upload can be resumed
    // (RESUME_KEY and RESUME_TTL_MS in modules/db/sliced-file-upload.js).
    const SLICED_UPLOADS_KEY = 'speakr.slicedUploads';
    const SLICED_UPLOADS_TTL_MS = 24 * 60 * 60 * 1000;
    // The account and language the offline recorder saves its recordings for
    // (OFFLINE_CONTEXT_KEY in modules/utils/pending-uploads.js). Without it
    // the offline recorder records nothing, so after a sign-out nobody can
    // record offline for the account that signed out.
    const OFFLINE_RECORDER_KEY = 'speakr.offlineRecorder';

    const script = root.document && root.document.currentScript;
    let version = '';
    try {
        version = script ? new URL(script.src, root.location.href).searchParams.get('v') || '' : '';
    } catch (e) {
        version = '';
    }

    const workerUrl = (v) => (v ? `/sw.js?v=${encodeURIComponent(v)}` : '/sw.js');

    const isLeftover = (registration) => {
        try {
            return new URL(registration.scope).pathname !== '/';
        } catch (e) {
            return false;
        }
    };

    /** Register the worker at scope /, after removing a registration at /static/. */
    async function register() {
        const sw = root.navigator && root.navigator.serviceWorker;
        if (!sw) return null;
        try {
            const registrations = await sw.getRegistrations();
            await Promise.all(registrations.filter(isLeftover).map((r) => r.unregister()));
            return await sw.register(workerUrl(version), { scope: '/' });
        } catch (error) {
            console.warn('Service worker registration failed (the app works without it):', error);
            return null;
        }
    }

    // ---- IndexedDB -------------------------------------------------------

    /** Open a database only when it exists; null otherwise. Nothing is created. */
    function openExisting(name) {
        return new Promise((resolve) => {
            let request;
            try {
                request = root.indexedDB.open(name);
            } catch (e) {
                resolve(null);
                return;
            }
            request.onupgradeneeded = () => {
                // The database did not exist: undo its creation.
                try { request.transaction.abort(); } catch (e) { /* ignore */ }
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = (event) => {
                if (event && event.preventDefault) event.preventDefault();
                resolve(null);
            };
            request.onblocked = () => resolve(null);
        });
    }

    function withStore(name, storeName, mode, work) {
        return openExisting(name).then((db) => {
            if (!db) return null;
            if (!db.objectStoreNames.contains(storeName)) {
                db.close();
                return null;
            }
            return new Promise((resolve) => {
                let result = null;
                const transaction = db.transaction([storeName], mode);
                const request = work(transaction.objectStore(storeName));
                request.onsuccess = () => { result = request.result; };
                transaction.oncomplete = () => { db.close(); resolve(result); };
                transaction.onerror = transaction.onabort = () => { db.close(); resolve(null); };
            });
        });
    }

    /** How many uploads in slices this browser could still resume. */
    function unfinishedSlicedUploads() {
        try {
            const entries = JSON.parse(root.localStorage.getItem(SLICED_UPLOADS_KEY) || '{}');
            return Object.values(entries).filter((entry) => (
                entry && entry.sessionId && Date.now() - (entry.savedAt || 0) < SLICED_UPLOADS_TTL_MS)).length;
        } catch (e) {
            return 0;
        }
    }

    /**
     * How many recordings this browser holds that did not reach the server:
     * a recording that was not finished (kept for recovery), the files of
     * failed uploads, and uploads in slices that did not finish.
     */
    async function localCopies() {
        const sliced = unfinishedSlicedUploads();
        if (!root.indexedDB) return sliced;
        const [session, failed] = await Promise.all([
            withStore('SpeakrRecordings', 'activeRecording', 'readonly', (store) => store.get('current')),
            withStore('SpeakrFailedUploads', 'failedUploads', 'readonly', (store) => store.count()),
        ]);
        const unfinished = session && Array.isArray(session.chunks) && session.chunks.length > 0 ? 1 : 0;
        return unfinished + (Number(failed) || 0) + sliced;
    }

    function clearDatabases() {
        if (!root.indexedDB) return Promise.resolve();
        return Promise.all(USER_STORES.map(([name, storeName]) => (
            withStore(name, storeName, 'readwrite', (store) => store.clear()))));
    }

    // ---- Cache Storage and the worker --------------------------------------

    async function clearCaches() {
        if (!root.caches) return;
        const names = await root.caches.keys();
        await Promise.all(names.map((name) => root.caches.delete(name)));
    }

    /** Tell the worker; resolved when it has answered, or after two seconds. */
    function tellWorker() {
        const sw = root.navigator && root.navigator.serviceWorker;
        const worker = sw && sw.controller;
        if (!worker) return Promise.resolve();
        return new Promise((resolve) => {
            const timer = setTimeout(resolve, 2000);
            try {
                const channel = new MessageChannel();
                channel.port1.onmessage = () => { clearTimeout(timer); resolve(); };
                worker.postMessage({ type: 'SIGN_OUT' }, [channel.port2]);
            } catch (e) {
                clearTimeout(timer);
                resolve();
            }
        });
    }

    /** Remove what the signed-out account left in this browser. */
    async function clearAfterSignOut() {
        try { root.sessionStorage.clear(); } catch (e) { /* unavailable */ }
        // Of localStorage only the entry that names files of the account;
        // preferences of the interface stay.
        try { root.localStorage.removeItem(SLICED_UPLOADS_KEY); } catch (e) { /* unavailable */ }
        try { root.localStorage.removeItem(OFFLINE_RECORDER_KEY); } catch (e) { /* unavailable */ }
        const results = await Promise.allSettled([clearDatabases(), tellWorker()]);
        // After the worker, so that nothing it was storing is left behind.
        await clearCaches().catch(() => {});
        return results.every((r) => r.status === 'fulfilled');
    }

    const signedOut = () => {
        try {
            return new RegExp(`(?:^|;\\s*)${SIGNED_OUT_COOKIE}=1(?:;|$)`).test(root.document.cookie);
        } catch (e) {
            return false;
        }
    };

    const forgetSignedOut = () => {
        try {
            root.document.cookie = `${SIGNED_OUT_COOKIE}=; Max-Age=0; path=/; SameSite=Lax`;
        } catch (e) { /* ignore */ }
    };

    // ---- the sign-out form -------------------------------------------------

    const isSignOutForm = (form) => {
        const action = form && form.getAttribute ? form.getAttribute('action') || '' : '';
        return /\/logout\/?$/.test(action);
    };

    const tr = (key, fallback, params) => {
        try {
            const i18n = root.i18n;
            if (i18n && typeof i18n.t === 'function') {
                const value = i18n.t(key, params);
                if (value && value !== key) return value;
            }
        } catch (e) { /* fall back */ }
        return fallback.replace(/\{\{(\w+)\}\}/g, (match, name) => (params && name in params ? params[name] : match));
    };

    function askAboutLocalCopies(count) {
        const options = {
            title: tr('signOut.localCopiesTitle', 'Delete the copies in this browser?'),
            message: count === 1
                ? tr('signOut.localCopiesMessageOne', 'This browser holds 1 recording or upload that did not reach the server. It is deleted from this browser at sign-out.')
                : tr('signOut.localCopiesMessage', 'This browser holds {{count}} recordings or uploads that did not reach the server. They are deleted from this browser at sign-out.', { count }),
            confirmLabel: tr('signOut.deleteAndSignOut', 'Delete and sign out'),
            danger: true,
        };
        if (typeof root.confirmDialog === 'function') return root.confirmDialog(options);
        return Promise.resolve(root.confirm(`${options.title}\n\n${options.message}`));
    }

    const confirmed = new WeakSet();

    function onSubmit(event) {
        const form = event.target;
        if (!isSignOutForm(form)) return;
        if (confirmed.has(form)) {
            // The second pass: the form is sent, and the other listeners
            // (the chat threads' own clearing) run.
            confirmed.delete(form);
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        const submitter = event.submitter || null;
        localCopies()
            .catch(() => 0)
            .then((count) => (count > 0 ? askAboutLocalCopies(count) : true))
            .catch(() => true)
            .then((go) => {
                if (!go) return;
                confirmed.add(form);
                if (typeof form.requestSubmit === 'function') {
                    try {
                        form.requestSubmit(submitter || undefined);
                        return;
                    } catch (e) { /* a submitter outside the form: send it plainly */ }
                }
                confirmed.delete(form);
                form.submit();
            });
    }

    // ---- start ---------------------------------------------------------------

    let ready = Promise.resolve();
    if (root.document) {
        root.document.addEventListener('submit', onSubmit, true);
        if (signedOut()) {
            ready = clearAfterSignOut().catch(() => false).then(forgetSignedOut);
        }
        if (script && script.dataset && 'register' in script.dataset) {
            const start = () => { ready.then(register); };
            if (root.document.readyState === 'complete') start();
            else root.addEventListener('load', start, { once: true });
        }
    }

    root.SpeakrServiceWorker = {
        register,
        localCopies,
        clearAfterSignOut,
        /** Resolved when the clearing after a sign-out, if any, is done. */
        get ready() { return ready; },
        workerUrl,
        isLeftover,
        isSignOutForm,
    };
})(typeof window !== 'undefined' ? window : globalThis);
