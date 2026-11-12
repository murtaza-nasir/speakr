/**
 * The recording chat's threads: one Ambrose conversation per recording,
 * kept in this browser tab.
 *
 * The shared AssistantChat (modules/assistant/chat.js) reads and writes the
 * thread of the recording on screen (its `thread` option), and a turn keeps
 * writing to the thread it started in, so an answer that is still arriving
 * when another recording opens stays with its own recording.
 *
 * Finished messages and drafts go to sessionStorage under a key per user:
 * they survive a reload and are gone when the tab closes. An incognito
 * recording's thread is never stored and is dropped when it is left.
 * At most `max` threads are kept; the least recently used idle thread goes
 * first, and a thread that is answering is never dropped.
 */

export const STORAGE_PREFIX = 'speakr.chatThreads.v1.';
export const MAX_THREADS = 20;
export const MAX_STORED_CHARS = 1500000;
export const INCOGNITO_KEY = 'incognito';

// The parts of a turn worth keeping; html is the rendered answer as shown.
const KEPT_FIELDS = ['role', 'content', 'html', 'thinking', 'steps', 'stepsLabel', 'activitySummary',
    'agentStepCount', 'segmentSources', 'sources', 'failed'];

const safeStorage = (getStorage) => {
    try { return getStorage() || null; } catch (_) { return null; }
};

/** The key a recording's thread is kept under, or null for none. */
export function threadKey(recording) {
    if (!recording || recording.id === undefined || recording.id === null) return null;
    if (recording.incognito === true || recording.id === INCOGNITO_KEY) return INCOGNITO_KEY;
    return String(recording.id);
}

/** The finished messages of a thread: an answer still arriving and its question are left out. */
export function finishedMessages(thread) {
    let list = Array.isArray(thread.messages) ? thread.messages.slice() : [];
    if (thread.loading) {
        let lastQuestion = -1;
        list.forEach((m, i) => { if (m && m.role === 'user') lastQuestion = i; });
        if (lastQuestion >= 0) list = list.slice(0, lastQuestion);
    }
    return list.filter((m) => m && !m.streaming).map((m) => {
        const kept = {};
        KEPT_FIELDS.forEach((f) => { if (m[f] !== undefined) kept[f] = m[f]; });
        return kept;
    });
}

/**
 * opts:
 *   reactive         Vue's reactive (an identity function in tests)
 *   storage()        the sessionStorage to use; may throw or return null
 *   userId           the signed-in user; no id means nothing is stored
 *   max, maxChars    limits (defaults above)
 *   now()            clock for least-recently-used order
 */
export function createChatThreads(opts = {}) {
    const reactive = opts.reactive || ((o) => o);
    const getStorage = opts.storage || (() => window.sessionStorage);
    const userId = opts.userId === undefined || opts.userId === null ? '' : String(opts.userId);
    const max = opts.max || MAX_THREADS;
    const maxChars = opts.maxChars || MAX_STORED_CHARS;
    let tick = 0;
    const now = opts.now || (() => ++tick);
    const storageKey = userId ? STORAGE_PREFIX + userId : null;

    const state = reactive({ threads: {}, currentKey: null });
    // Shown when no recording is open; never kept.
    const none = reactive(blank(null, {}));

    function blank(key, recording) {
        return {
            key, recordingId: key === INCOGNITO_KEY ? null : (key === null ? null : recording.id),
            title: (recording && recording.title) || '', incognito: key === INCOGNITO_KEY,
            messages: [], input: '', loading: false, status: '', following: true,
            controller: null, lastUsed: 0, dropped: false,
        };
    }

    const get = (key) => (key === null || key === undefined ? null : state.threads[key] || null);
    const current = () => get(state.currentKey) || none;

    const abort = (thread) => {
        try { if (thread && thread.controller) thread.controller.abort(); } catch (_) { /* gone */ }
    };

    const remove = (key) => {
        const thread = state.threads[key];
        if (!thread) return;
        thread.dropped = true;
        abort(thread);
        delete state.threads[key];
    };

    /** Drop the least recently used idle threads beyond the limit (never the one on screen). */
    const evict = () => {
        const keys = Object.keys(state.threads);
        if (keys.length <= max) return;
        const idle = keys
            .filter((k) => k !== state.currentKey && !state.threads[k].loading)
            .sort((a, b) => state.threads[a].lastUsed - state.threads[b].lastUsed);
        let extra = keys.length - max;
        for (const k of idle) {
            if (extra <= 0) break;
            remove(k);
            extra -= 1;
        }
    };

    // ------------------------------------------------------------ storage
    const serialize = (keys) => JSON.stringify({
        threads: keys.map((k) => {
            const thread = state.threads[k];
            return { key: k, recordingId: thread.recordingId, title: thread.title, lastUsed: thread.lastUsed,
                input: thread.input || '', messages: finishedMessages(thread) };
        }).filter((t) => t.messages.length || t.input),
    });

    /** Write the stored threads; on a full quota, leave out the oldest until it fits. */
    const save = () => {
        if (!storageKey) return false;
        const storage = safeStorage(getStorage);
        if (!storage) return false;
        let keys = Object.keys(state.threads)
            .filter((k) => !state.threads[k].incognito)
            .sort((a, b) => state.threads[b].lastUsed - state.threads[a].lastUsed);
        while (true) {
            let text;
            try { text = serialize(keys); } catch (_) { return false; }
            if (text.length <= maxChars) {
                try { storage.setItem(storageKey, text); return true; } catch (_) { /* quota: try smaller */ }
            }
            if (!keys.length) break;
            keys = keys.slice(0, -1);
        }
        try { storage.removeItem(storageKey); } catch (_) { /* nothing to do */ }
        return false;
    };

    /** Bring back the threads this tab kept for this user; other users' threads are removed. */
    const restore = () => {
        const storage = safeStorage(getStorage);
        if (!storage) return 0;
        try {
            const stale = [];
            for (let i = 0; i < storage.length; i++) {
                const k = storage.key(i);
                if (k && k.startsWith(STORAGE_PREFIX) && k !== storageKey) stale.push(k);
            }
            stale.forEach((k) => storage.removeItem(k));
        } catch (_) { /* storage unavailable */ }
        if (!storageKey) return 0;
        let data = null;
        try { data = JSON.parse(storage.getItem(storageKey) || 'null'); } catch (_) { data = null; }
        if (!data || !Array.isArray(data.threads)) return 0;
        let count = 0;
        data.threads.forEach((saved) => {
            if (!saved || typeof saved.key !== 'string' || saved.key === INCOGNITO_KEY) return;
            if (state.threads[saved.key]) return;
            const thread = blank(saved.key, { id: saved.recordingId, title: saved.title });
            thread.input = typeof saved.input === 'string' ? saved.input : '';
            thread.messages = (Array.isArray(saved.messages) ? saved.messages : [])
                .filter((m) => m && (m.role === 'user' || m.role === 'assistant'))
                .map((m) => Object.assign({ streaming: false, showSteps: false, thinkingExpanded: false,
                    steps: [], segmentSources: [], sources: [] }, m));
            thread.lastUsed = typeof saved.lastUsed === 'number' ? saved.lastUsed : 0;
            state.threads[saved.key] = reactive(thread);
            count += 1;
        });
        // Keep the clock ahead of restored threads.
        const newest = Math.max(0, ...Object.values(state.threads).map((t) => t.lastUsed));
        if (!opts.now) tick = Math.max(tick, newest);
        evict();
        return count;
    };

    // ------------------------------------------------------------ switching
    /**
     * Show the thread of `recording` (null for none). Opening another
     * recording after an incognito one drops the incognito thread (as the
     * incognito recording itself goes). Returns the thread now on screen.
     */
    const select = (recording) => {
        const key = threadKey(recording);
        const previous = state.currentKey;
        if (key !== null && key !== INCOGNITO_KEY && state.threads[INCOGNITO_KEY]) remove(INCOGNITO_KEY);
        state.currentKey = key;
        if (key === null) { save(); return none; }
        let thread = state.threads[key];
        if (!thread) {
            state.threads[key] = reactive(blank(key, recording));
            thread = state.threads[key];
        }
        if (recording && recording.title) thread.title = recording.title;
        thread.lastUsed = now();
        evict();
        if (previous !== key) save();
        return thread;
    };

    /** Clear the thread on screen: stop its answer and empty it. */
    const clearCurrent = () => {
        const thread = current();
        abort(thread);
        thread.messages = [];
        save();
    };

    /** Drop the threads of deleted recordings. */
    const drop = (ids) => {
        (Array.isArray(ids) ? ids : [ids]).forEach((id) => {
            const key = threadKey({ id });
            if (key && key !== INCOGNITO_KEY) remove(key);
        });
        save();
    };

    /** Sign-out: drop every thread and every stored copy. */
    const clearAll = () => {
        Object.keys(state.threads).forEach(remove);
        removeStoredThreads(getStorage);
    };

    /** True while the thread of recording `id` is answering and another recording is on screen. */
    const isAnsweringElsewhere = (id) => {
        const key = threadKey({ id });
        const thread = get(key);
        return !!(thread && thread.loading && key !== state.currentKey);
    };

    return {
        state, current, get, select, save, restore, clearCurrent, drop, clearAll,
        isAnsweringElsewhere, storageKey,
        isCurrent: (thread) => !!thread && thread === current(),
        count: () => Object.keys(state.threads).length,
    };
}

/** Clear every stored thread when a sign-out form is sent (any page). */
export function clearThreadsOnSignOut(clear, doc = document) {
    try {
        doc.addEventListener('submit', (event) => {
            const form = event.target;
            const action = form && form.getAttribute ? form.getAttribute('action') || '' : '';
            if (/\/logout\/?$/.test(action)) clear();
        }, true);
    } catch (_) { /* no document */ }
}

/** Remove every stored thread without a thread store (the Ask page's sign-out). */
export function removeStoredThreads(getStorage = () => window.sessionStorage) {
    const storage = safeStorage(getStorage);
    if (!storage) return;
    try {
        const keys = [];
        for (let i = 0; i < storage.length; i++) {
            const k = storage.key(i);
            if (k && k.startsWith(STORAGE_PREFIX)) keys.push(k);
        }
        keys.forEach((k) => storage.removeItem(k));
    } catch (_) { /* storage unavailable */ }
}
