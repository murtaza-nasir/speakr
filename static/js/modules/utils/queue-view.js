/**
 * View model of the processing queue: how the items of the unified list
 * (uploads and jobs, app.modular.js) are grouped, counted and worded for the
 * indicator and the panel.
 *
 * Pure functions, no Vue and no DOM. The rule for what a row shows comes
 * from queue-data.js: a number only where a real one exists (the uploaded
 * share of a file, finished parts of a total, a fraction a service
 * measured); otherwise a moving bar with the time since the step started.
 * Nothing here estimates.
 */

export const QUEUE_GROUPS = ['active', 'waiting', 'failed', 'done'];

const ACTIVE = ['uploading', 'transcribing', 'summarizing', 'translating'];
const WAITING = ['queued', 'ready'];
const FAILED = ['failed', 'upload_failed'];

/** The group of an item: active, waiting, failed or done. */
export function queueGroup(item) {
    const status = item ? item.status : null;
    if (ACTIVE.includes(status)) return 'active';
    if (WAITING.includes(status)) return 'waiting';
    if (FAILED.includes(status)) return 'failed';
    return 'done';
}

/** The items by group, each group in the order of the list. */
export function groupQueueItems(items) {
    const groups = { active: [], waiting: [], failed: [], done: [] };
    for (const item of items || []) groups[queueGroup(item)].push(item);
    return groups;
}

/**
 * The counts behind the indicator line and its state:
 *   busy    something is processed or waits
 *   failed  nothing runs and at least one item failed
 *   done    only finished items are left
 *   empty   no items
 */
export function queueSummary(groups) {
    const processing = groups.active.length;
    const queued = groups.waiting.length;
    const failed = groups.failed.length;
    const finished = groups.done.length;
    const state = processing || queued ? 'busy' : failed ? 'failed' : finished ? 'done' : 'empty';
    return { processing, queued, failed, finished, state };
}

/**
 * The parts of the indicator line, in order: [{ key, count, tone }]. Finished
 * items are named only when nothing runs or waits; failures always, in the
 * error tone.
 */
export function summaryParts(summary) {
    const parts = [];
    if (summary.processing) parts.push({ key: 'processing', count: summary.processing, tone: 'plain' });
    if (summary.queued) parts.push({ key: 'queued', count: summary.queued, tone: 'plain' });
    if (!parts.length && summary.finished) parts.push({ key: 'finished', count: summary.finished, tone: 'plain' });
    if (summary.failed) parts.push({ key: 'failed', count: summary.failed, tone: 'error' });
    return parts;
}

const STAGE_KEYS = {
    converting: 'progressQueue.converting',
    assembling: 'progressQueue.stage.assembling',
    joining: 'progressQueue.joining',
    transcribing: 'status.transcribing',
    titling: 'progressQueue.stage.titling',
    summarizing: 'status.summarizing',
    translating: 'progressQueue.translating',
    indexing: 'progressQueue.stage.indexing',
    processing: 'status.processing',
};

const QUEUED_KEYS = {
    convert: 'progressQueue.queuedFor.conversion',
    transcription: 'progressQueue.queuedFor.transcription',
    summary: 'progressQueue.queuedFor.summary',
    translation: 'progressQueue.queuedFor.translation',
};

const DETAIL_KEYS = {
    waiting: 'progressQueue.detail.waiting',
    preparing: 'progressQueue.detail.preparing',
    aligning: 'progressQueue.detail.aligning',
    diarizing: 'progressQueue.detail.diarizing',
};

/**
 * The stage of a row in words, as translation keys:
 *   { key, params, detailKey, messageKey }
 * `key` is the stage ("Transcribing", "Queued #2 for transcription",
 * "Canceling…"), `detailKey` the step a transcription service reports inside
 * the stage ("aligning"), shown as a quiet suffix. An upload row that waits
 * for something else carries its own sentence in `item.progressMessage`.
 */
export function stageLabel(item) {
    if (!item) return { key: 'status.processing', params: null, detailKey: null };
    if (item.cancelRequested) return { key: 'progressQueue.canceling', params: null, detailKey: null };
    const status = item.status;
    if (status === 'uploading') {
        return { key: item.serverProcessing ? 'progressQueue.stage.checking' : 'status.uploading', params: null, detailKey: null };
    }
    if (status === 'ready') return { key: 'progressQueue.waiting', params: null, detailKey: null };
    if (status === 'queued') {
        const queue = item.jobType === 'translate_transcription' ? 'translation' : item.queueName;
        const key = item.jobId ? QUEUED_KEYS[queue] : null;
        if (key && item.queuePosition) return { key, params: { position: item.queuePosition }, detailKey: null };
        return { key: 'status.queued', params: null, detailKey: null };
    }
    if (status === 'completed') return { key: 'progressQueue.done', params: null, detailKey: null };
    if (status === 'upload_failed') return { key: 'progressQueue.uploadFailed', params: null, detailKey: null };
    if (status === 'failed') return { key: 'progressQueue.failed', params: null, detailKey: null };
    const code = item.stageCode && STAGE_KEYS[item.stageCode] ? item.stageCode
        : status === 'translating' ? 'translating'
        : status === 'summarizing' ? 'summarizing'
        : item.stage && STAGE_KEYS[item.stage] ? item.stage : 'transcribing';
    // The service's own "transcribing" says nothing the stage does not
    const detailKey = code === 'transcribing' ? (DETAIL_KEYS[item.stageDetail] || null) : null;
    return { key: STAGE_KEYS[code], params: null, detailKey };
}

/**
 * What the progress part of a row shows:
 *   { kind: 'percent', percent }          a bar and "64%" (upload bytes, a measured fraction)
 *   { kind: 'count', percent, done, total } a bar and "3 of 8" (parts)
 *   { kind: 'moving', since }             a moving bar; `since` is the start
 *                                         of the step (ms) for the elapsed
 *                                         time, or null when it is not known
 *   { kind: 'none' }                      no bar (waiting, finished, failed)
 */
export function rowProgress(item, parseInstant = Date.parse) {
    if (!item || queueGroup(item) !== 'active') return { kind: 'none' };
    if (item.status === 'uploading') {
        if (item.serverProcessing) return { kind: 'moving', since: item.serverProcessingAt || null };
        const percent = Math.min(100, Math.max(0, Math.round(Number(item.progress) || 0)));
        return { kind: 'percent', percent };
    }
    const info = item.progressInfo;
    if (info && info.kind === 'chunks') return { kind: 'count', percent: info.percent, done: info.done, total: info.total };
    if (info && info.kind === 'fraction') return { kind: 'percent', percent: info.percent };
    const started = item.startedAt ? parseInstant(item.startedAt) : null;
    return { kind: 'moving', since: Number.isFinite(started) ? started : null };
}

/** Elapsed time as m:ss, or h:mm:ss from one hour. */
export function formatElapsed(totalSeconds) {
    const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = String(seconds % 60).padStart(2, '0');
    return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

/**
 * How long ago a row finished, as a translation key with its count:
 * just now (under a minute), minutes, then hours.
 */
export function finishedAgo(finishedMs, nowMs) {
    if (!Number.isFinite(finishedMs)) return null;
    const minutes = Math.floor(Math.max(0, nowMs - finishedMs) / 60000);
    if (minutes < 1) return { key: 'progressQueue.justNow', count: 0 };
    if (minutes < 60) return { key: 'progressQueue.minutesAgo', count: minutes };
    return { key: 'progressQueue.hoursAgo', count: Math.floor(minutes / 60) };
}

/** The icon (Font Awesome) and the color tone of a row's mark. */
export function rowMark(item) {
    const group = queueGroup(item);
    if (group === 'failed') return { icon: 'fa-xmark', tone: 'error' };
    if (group === 'done') return { icon: 'fa-check', tone: 'done' };
    if (group === 'waiting') return { icon: 'fa-clock', tone: 'wait' };
    if (item.status === 'uploading') return { icon: 'fa-arrow-up-from-bracket', tone: 'info' };
    if (item.status === 'summarizing') return { icon: 'fa-file-lines', tone: 'ok' };
    if (item.status === 'translating') return { icon: 'fa-language', tone: 'ok' };
    const code = item.stageCode || item.stage;
    if (code === 'titling' || code === 'summarizing') return { icon: 'fa-file-lines', tone: 'ok' };
    if (code === 'converting') return { icon: 'fa-wave-square', tone: 'proc' };
    if (code === 'joining' || code === 'assembling') return { icon: 'fa-object-group', tone: 'proc' };
    return { icon: 'fa-microphone', tone: 'proc' };
}

/**
 * Whether a cancel of this item is asked about first: only a running first
 * transcription, where the cancel leaves a recording without a transcript.
 * From the title on, the transcript of the run is stored and is kept.
 */
export function cancelNeedsConfirm(item) {
    if (!item || !item.jobId || !item.isNewUpload) return false;
    if (queueGroup(item) !== 'active') return false;
    return ['converting', 'transcribing', 'processing'].includes(item.stageCode);
}
