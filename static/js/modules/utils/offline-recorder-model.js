/**
 * The decisions of the offline recorder page (static/offline-recorder.html,
 * static/js/offline-recorder.js), without the DOM: texts, what the page
 * offers, what it lists and when it goes back to the app. Tested by
 * offline-recorder-model.test.js.
 */

// No import: the page loads every module with this release's ?v= (the
// service worker keeps those URLs), and a static import would ask for the
// module without it. The helpers of utils/pending-uploads.js are passed in.

/**
 * The English texts, as in the offlineRecorder section of
 * static/locales/en.json (a test keeps the two equal). They are used when no
 * locale file is in the cache.
 */
export const DEFAULT_STRINGS = {
    pageTitle: 'Offline - Speakr',
    title: 'You are offline',
    lead: 'Speakr cannot be reached. You can record here: the recording is kept in this browser and uploaded when Speakr is back.',
    leadPlain: 'Speakr cannot be reached. This page is loaded again when the connection is back.',
    notSignedIn: 'Recording offline is possible once Speakr has been opened online while you are signed in.',
    incognito: 'Incognito mode is on by default, so nothing is recorded offline: a recording made offline would be kept in this browser.',
    noMicrophone: 'This browser can\'t record on this page.',
    microphoneDenied: 'Speakr may not use the microphone. Allow it in the browser\'s site settings, then try again.',
    record: 'Record',
    stop: 'Stop and save',
    recording: 'Recording…',
    saving: 'Saving…',
    saved: 'Saved in this browser. It is uploaded when Speakr is back.',
    saveFailed: 'Couldn\'t keep the recording in this browser. It was downloaded instead.',
    saveLost: 'Couldn\'t keep or download the recording.',
    pendingTitle: 'Waiting to upload',
    itemName: 'Recording of {{date}}',
    delete: 'Delete',
    deleteQuestion: 'Delete this recording from this browser? This can\'t be undone.',
    deleteConfirm: 'Delete',
    deleteCancel: 'Keep',
    deleted: 'Recording deleted.',
    backWhileRecording: 'Speakr is reachable again. Stop the recording to save it; it is then uploaded.',
    backOpening: 'Speakr is reachable again. Opening Speakr…',
    tryAgain: 'Try again',
    leaveWarning: 'The recording is not saved yet.',
};

const lookup = (table, key) => {
    if (!table) return undefined;
    let node = table;
    for (const part of key.split('.')) {
        if (!node || typeof node !== 'object') return undefined;
        node = node[part];
    }
    return typeof node === 'string' && node ? node : undefined;
};

/** Fill {{name}} placeholders. */
export function interpolate(text, params) {
    return String(text).replace(/\{\{\s*(\w+)\s*\}\}/g, (match, name) => (
        params && Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match));
}

/**
 * A t(key, params) over a locale table and the English one. A key of the
 * page is looked up as offlineRecorder.<key>; a key with a dot as given
 * (fileSize.megabytes). The English texts above are the last fallback.
 */
export function translator(table, english) {
    return (key, params) => {
        const full = key.includes('.') ? key : `offlineRecorder.${key}`;
        const text = lookup(table, full) || lookup(english, full)
            || (key.includes('.') ? undefined : DEFAULT_STRINGS[key]) || key;
        return interpolate(text, params);
    };
}

/** 0:07, 12:30, 1:02:03; a dash when unknown. */
export function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '–';
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const two = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${two(m)}:${two(s)}` : `${m}:${two(s)}`;
}

/** A size with the units of the fileSize section of the locale files. */
export function formatSize(bytes, t) {
    const n = Number(bytes) || 0;
    const units = [['terabytes', 1024 ** 4], ['gigabytes', 1024 ** 3], ['megabytes', 1024 ** 2], ['kilobytes', 1024]];
    for (const [unit, size] of units) {
        if (n >= size) {
            const value = n / size;
            return t(`fileSize.${unit}`, { count: value >= 10 ? Math.round(value) : Math.round(value * 10) / 10 });
        }
    }
    return t('fileSize.bytes', { count: n });
}

/**
 * What the page offers, from the entry the app left (speakr.offlineRecorder,
 * read with parseOfflineContext of utils/pending-uploads.js) and the
 * browser's abilities.
 *   { canRecord: true }
 *   { canRecord: false, reason: 'notSignedIn' | 'incognito' | 'noMicrophone' }
 */
export function recorderAvailability(context, { mediaDevices = true, mediaRecorder = true, indexedDB = true } = {}) {
    if (!context) return { canRecord: false, reason: 'notSignedIn', context: null };
    if (context.incognitoDefault) return { canRecord: false, reason: 'incognito', context };
    if (!mediaDevices || !mediaRecorder || !indexedDB) return { canRecord: false, reason: 'noMicrophone', context };
    return { canRecord: true, reason: null, context };
}

/**
 * The rows of "Waiting to upload": the records of this account, newest
 * first. Nothing but the file's own facts; records of another account are
 * not listed.
 */
export function pendingRows(records, userId, { t, formatDate, belongsTo }) {
    if (!userId) return [];
    return (records || [])
        .filter((record) => record && belongsTo(record, userId))
        .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))
        .map((record) => {
            const recorded = record.fromRecorder || record.source === 'offline';
            return {
                id: record.id,
                name: recorded && record.timestamp ? t('itemName', { date: formatDate(record.timestamp) }) : (record.fileName || ''),
                duration: formatDuration(record.duration),
                size: formatSize(record.fileSize, t),
            };
        });
}

/** The first container the browser can record audio in. */
export function pickMimeType(isTypeSupported) {
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
    if (typeof isTypeSupported !== 'function') return '';
    return candidates.find((type) => {
        try { return isTypeSupported(type); } catch (e) { return false; }
    }) || '';
}

/** The file name, as the in-app recorder names its files (composables/audio.js). */
export function recordingFileName(date, mimeType) {
    const stamp = date.toISOString().replace(/[:.]/g, '-');
    const ext = /mp4/.test(mimeType || '') ? 'm4a' : (/ogg/.test(mimeType || '') ? 'ogg' : 'webm');
    return `recording-${stamp}.${ext}`;
}

/** An id for client_upload_id (the server accepts [A-Za-z0-9_-]{8,80}). */
export function offlineClientId(now, random) {
    const tail = String(random || '').replace(/[^a-z0-9]/gi, '').slice(0, 12) || '0';
    return `offline-${now}-${tail}`;
}

/** When the server answers again: back to the app, or wait until the recording is saved. */
export function whenServerIsBack({ recording = false, saving = false } = {}) {
    return recording || saving ? 'wait' : 'open';
}

/**
 * Where the page goes when the server is back: the page that was asked for
 * (the worker answers this page in its place), or the app when this file
 * was opened by its own address.
 */
export function returnTarget(pathname) {
    return typeof pathname === 'string' && pathname.startsWith('/static/') ? '/' : null;
}

/** A proxy's 502 or 504 means Speakr behind it is still away. */
export function serverAnswered(status) {
    return Number.isFinite(status) && status !== 502 && status !== 504;
}
