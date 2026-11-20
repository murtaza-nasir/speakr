/**
 * Recordings that wait in this browser for an upload (IndexedDB
 * SpeakrFailedUploads, modules/db/failed-uploads.js), and what ties them to
 * the copies kept for crash recovery. Pure functions, used by
 * composables/upload.js, app.modular.js and the offline recorder
 * (static/js/offline-recorder.js).
 *
 * A recording made in the app has up to three copies in the browser while
 * it has not reached the server: the crash-recovery session (SpeakrRecordings,
 * keyed by its startTime), the record of a failed upload, and, with
 * ENABLE_SERVER_RECORDING_CHUNKS, a session on the server whose id is
 * remembered in localStorage. The record of a failed upload keeps the start
 * time and the server session id, so that the upload that finally succeeds
 * removes the other copies as well and no copy is offered or uploaded a
 * second time (#650).
 *
 * Each record also keeps the id of the account it was made under (ownerId).
 * A record of another account is never uploaded to the account signed in now
 * (a shared browser, a session that ended without a sign-out).
 */

/** The localStorage entry the app writes at each online load for the offline recorder. */
export const OFFLINE_CONTEXT_KEY = 'speakr.offlineRecorder';

/** The languages the offline recorder can show (the files in static/locales/). */
export const OFFLINE_LOCALES = ['en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh'];

const asId = (value) => (value === null || value === undefined || value === '' ? '' : String(value));

/**
 * The fields of a failed upload's record, from the queue item and the error.
 * The file itself is added by storeFailedUpload.
 */
export function failedUploadRecord(fileItem, error, ownerId) {
    const file = fileItem.file || {};
    return {
        file: fileItem.file,
        fileName: file.name,
        fileSize: file.size,
        clientId: fileItem.clientId,
        notes: fileItem.notes,
        tags: fileItem.tags,
        asrOptions: fileItem.asrOptions,
        folderId: fileItem.folder_id || null,
        error: error && error.message ? error.message : String(error || ''),
        fromRecorder: !!fileItem.fromInProgressRecording,
        recordingSessionStartTime: fileItem.recordingSessionStartTime || null,
        serverSessionId: fileItem.serverSessionId || null,
        duration: Number.isFinite(fileItem.duration) ? fileItem.duration : null,
        ownerId: asId(ownerId) || null,
    };
}

/**
 * Whether a record may be uploaded by the account signed in now.
 * A record without an owner was saved by a release before this one and is
 * uploaded as it was then. Without a known current account nothing owned is.
 */
export function belongsTo(record, currentUserId) {
    const owner = asId(record && record.ownerId);
    if (!owner) return true;
    const current = asId(currentUserId);
    return current !== '' && owner === current;
}

/** Records split into those of the account signed in now and those of others. */
export function partitionByOwner(records, currentUserId) {
    const mine = [];
    const others = [];
    for (const record of records || []) {
        if (!record) continue;
        (belongsTo(record, currentUserId) ? mine : others).push(record);
    }
    return { mine, others };
}

/**
 * What resurfaceFailedUploads does with a record, given the upload queue.
 *   'skip'    the same upload is in the queue and waits or is under way
 *             (status queued, ready, uploading, pending, the last meaning
 *             that it has reached the server): it is not sent again;
 *   'replace' the queue holds the same upload as failed: that item is
 *             replaced by one built from the record;
 *   'add'     the queue does not hold it.
 */
export function resurfaceAction(record, queue) {
    const existing = (queue || []).find((item) => item && item.clientId === record.clientId);
    if (!existing) return 'add';
    if (['uploading', 'pending', 'ready', 'queued'].includes(existing.status)) return 'skip';
    return 'replace';
}

/** The queue item for a record (status ready: startProcessingQueue sends it). */
export function queueItemFromRecord(record, file) {
    return {
        file,
        notes: record.notes || '',
        tags: record.tags || [],
        asrOptions: record.asrOptions || {},
        folder_id: record.folderId || null,
        status: 'ready',
        recordingId: null,
        clientId: record.clientId,
        error: null,
        duration: Number.isFinite(record.duration) ? record.duration : null,
        willAutoSummarize: false,
        // The stored tags and ASR options are kept; the current form state does not replace them.
        preserveOptions: true,
        // A recording made in this browser: sent with from_recorder, and its
        // crash-recovery copy and server session go once it is uploaded.
        fromInProgressRecording: !!record.fromRecorder,
        recordingSessionStartTime: record.recordingSessionStartTime || null,
        serverSessionId: record.serverSessionId || null,
        _failedUploadId: record.id,
        _retryCount: record.retryCount || 0,
        // Sent again after a reload: the app stays where the person is.
        _resurfaced: true,
    };
}

/**
 * Whether a copy offered for recovery is already held by a record that
 * waits for its upload: the local crash-recovery session with this start
 * time, or the server session with this id. Such a copy is not offered; the
 * record is uploaded instead, once.
 */
export function coveredByPendingUpload(records, { startTime = null, sessionId = null } = {}) {
    return (records || []).some((record) => record && (
        (startTime && record.recordingSessionStartTime && record.recordingSessionStartTime === startTime)
        || (sessionId && record.serverSessionId && record.serverSessionId === sessionId)));
}

/** The language for the offline recorder: one of OFFLINE_LOCALES, else English. */
export function offlineLocale(value) {
    if (typeof value !== 'string' || !value) return 'en';
    if (OFFLINE_LOCALES.includes(value)) return value;
    const lower = value.toLowerCase();
    if (lower === 'pt-br' || lower.startsWith('pt')) return 'pt-BR';
    const base = lower.split(/[-_]/)[0];
    return OFFLINE_LOCALES.includes(base) ? base : 'en';
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** How long the entry lasts after the last signed-in online load, without REMEMBER_COOKIE_DAYS. */
export const DEFAULT_OFFLINE_DAYS = 30;

const positive = (value) => {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : null;
};

/**
 * The entry the app writes for the offline recorder at each signed-in online
 * load: the account the recordings made offline are saved for, the language
 * of its page, whether incognito mode is the default (then nothing is
 * recorded offline, as nothing of an incognito recording is kept), when it
 * expires, and the limits of a recording. It holds no name, address or
 * recording. It is removed at sign-out.
 *
 * expiresAt: `days` (REMEMBER_COOKIE_DAYS, default 30) after this load;
 * the entry is written again, with a new date, at every signed-in load.
 *
 * limits, each null when unknown:
 *   maxSeconds        RECORDING_MAX_HOURS of the in-app recorder;
 *   maxRecorderBytes  the size at which the in-app recorder stops (maxRecordingMB);
 *   maxUploadBytes    the server's upload limit for a file (max_file_size_mb).
 */
export function offlineContext({
    userId, locale, incognitoDefault = false, now = Date.now(), days = DEFAULT_OFFLINE_DAYS,
    maxSeconds = null, maxRecorderBytes = null, maxUploadBytes = null,
} = {}) {
    const id = asId(userId);
    if (!id) return null;
    const lifetime = positive(days) || DEFAULT_OFFLINE_DAYS;
    return {
        userId: id,
        locale: offlineLocale(locale),
        incognitoDefault: !!incognitoDefault,
        savedAt: now,
        expiresAt: now + lifetime * DAY_MS,
        limits: {
            maxSeconds: positive(maxSeconds),
            maxRecorderBytes: positive(maxRecorderBytes),
            maxUploadBytes: positive(maxUploadBytes),
        },
    };
}

/**
 * Read the entry back; null when there is none, it is not well formed, or it
 * has expired at `now`. An entry without expiresAt (written by the first
 * release of the recorder) lasts DEFAULT_OFFLINE_DAYS from savedAt.
 */
export function parseOfflineContext(raw, now = Date.now()) {
    if (!raw) return null;
    let value;
    try {
        value = typeof raw === 'string' ? JSON.parse(raw) : raw;
    } catch (e) {
        return null;
    }
    if (!value || typeof value !== 'object' || !asId(value.userId)) return null;
    const savedAt = Number(value.savedAt) || 0;
    const expiresAt = positive(value.expiresAt) || savedAt + DEFAULT_OFFLINE_DAYS * DAY_MS;
    if (!(now < expiresAt)) return null;
    const limits = value.limits && typeof value.limits === 'object' ? value.limits : {};
    return {
        userId: asId(value.userId),
        locale: offlineLocale(value.locale),
        incognitoDefault: !!value.incognitoDefault,
        savedAt,
        expiresAt,
        limits: {
            maxSeconds: positive(limits.maxSeconds),
            maxRecorderBytes: positive(limits.maxRecorderBytes),
            maxUploadBytes: positive(limits.maxUploadBytes),
        },
    };
}
