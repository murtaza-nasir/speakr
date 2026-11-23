/**
 * Data layer of the processing queue: what one row of
 * GET /api/recordings/job-queue-status means for the panel.
 *
 * Pure functions, no Vue and no DOM, so the panel can be built and tested
 * against them. The rule for progress: a number is shown only where the
 * server holds a real one (chunks finished of a total, or a fraction a
 * service measured). Otherwise the panel shows a moving bar with the time
 * since the job started. Nothing here estimates.
 */

const NAIVE_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;

/**
 * Milliseconds since the epoch for a server timestamp, or null.
 * The queue answer marks UTC with a Z; an older server sends the same
 * instant without it, and that form is read as UTC too.
 */
export function parseServerInstant(value) {
    if (typeof value !== 'string' || !value) return null;
    const ms = Date.parse(NAIVE_ISO.test(value) ? `${value}Z` : value);
    return Number.isNaN(ms) ? null : ms;
}

const DEFAULT_STAGES = {
    convert: 'converting',
    stitch: 'assembling',
    merge: 'joining',
    transcribe: 'transcribing',
    reprocess_transcription: 'transcribing',
    summarize: 'summarizing',
    reprocess_summary: 'summarizing',
    translate_transcription: 'translating',
    index_chunks: 'indexing',
    trim: 'trimming',
};

/**
 * The stage code of a job: queued, converting, assembling, joining,
 * transcribing, titling, summarizing, translating, indexing, completed or
 * failed. The server sends it as `stage`; for a server that does not, the
 * code follows from the status and the job type.
 */
export function jobStage(job) {
    if (!job) return null;
    if (typeof job.stage === 'string' && job.stage) return job.stage;
    if (job.job_status !== 'processing') return job.job_status || null;
    if (job.queue_type === 'summary' && !DEFAULT_STAGES[job.job_type]) return 'summarizing';
    return DEFAULT_STAGES[job.job_type] || 'processing';
}

/**
 * The real progress of a running job, or null when the server has no number:
 *   { kind: 'chunks', done, total, percent }
 *   { kind: 'fraction', fraction, percent }
 * `percent` is a whole number from 0 to 100 for the width of a bar.
 */
export function jobProgress(job) {
    if (!job || job.job_status !== 'processing') return null;
    const sent = job.progress;
    if (sent && typeof sent === 'object') {
        if (Number.isFinite(sent.fraction)) {
            const fraction = Math.min(1, Math.max(0, sent.fraction));
            return { kind: 'fraction', fraction, percent: Math.round(fraction * 100) };
        }
        if (Number.isFinite(sent.total) && sent.total > 0) {
            const done = Math.min(sent.total, Math.max(0, sent.done || 0));
            return { kind: 'chunks', done, total: sent.total, percent: Math.round((100 * done) / sent.total) };
        }
        return null;
    }
    // A server without the `progress` field: chunk counts of a translation.
    if (sent === undefined && job.job_type === 'translate_transcription' && job.progress_total > 0) {
        const done = Math.min(job.progress_total, Math.max(0, job.progress_done || 0));
        return {
            kind: 'chunks', done, total: job.progress_total,
            percent: Math.round((100 * done) / job.progress_total),
        };
    }
    return null;
}

/**
 * Whole seconds since the job started, for the label beside a moving bar;
 * null for a job that is not running or has no start time. `nowMs` is the
 * current time, corrected with clockOffsetMs() when the server sent its time.
 */
export function elapsedSeconds(job, nowMs = Date.now()) {
    if (!job || job.job_status !== 'processing') return null;
    const started = parseServerInstant(job.started_at);
    if (started === null) return null;
    return Math.max(0, Math.floor((nowMs - started) / 1000));
}

/**
 * Server clock minus browser clock in milliseconds, from the `server_time`
 * of the queue answer; 0 when the answer has none. Added to Date.now()
 * before elapsedSeconds(), a browser with a wrong clock still shows the
 * right elapsed time.
 */
export function clockOffsetMs(serverTime, receivedAtMs = Date.now()) {
    const server = parseServerInstant(serverTime);
    return server === null ? 0 : server - receivedAtMs;
}

/**
 * The fields the panel reads for one job row, beside title and messages:
 * stage, progress, times, queue place and what can be done with the row.
 */
export function queueFieldsFromJob(job) {
    const progress = jobProgress(job);
    const waiting = job.job_status === 'queued';
    const running = job.job_status === 'processing';
    const position = job.queue_position ?? job.position ?? null;
    return {
        stageCode: jobStage(job),
        stageDetail: running ? (job.stage_detail || null) : null,
        progressInfo: progress,
        progress: progress ? progress.percent : null,
        indeterminate: running && !progress,
        queuePosition: waiting ? position : null,
        queueName: waiting ? (job.queue || job.queue_type || null) : null,
        createdAt: job.created_at || null,
        startedAt: job.started_at || null,
        finishedAt: job.finished_at || (['completed', 'failed'].includes(job.job_status) ? job.completed_at : null) || null,
        cancelRequested: !!job.cancel_requested,
        canCancel: job.can_cancel === true && !job.cancel_requested,
        canRetry: job.job_status === 'failed',
        canRemove: ['completed', 'failed'].includes(job.job_status),
    };
}

/**
 * What the answer to DELETE /api/recordings/jobs/<id> means for a job that
 * waited or ran:
 *   200 canceled          the job waited and is gone
 *   202 cancel_requested  the job runs and stops at its next safe point
 *   404                   the job is already gone (a second cancel)
 *   409 job_already_finished  it ended first: nothing to cancel, no error to show
 * Anything else is a refusal (`ok` false).
 */
export function cancelResult(status, data = {}) {
    const body = data || {};
    if (status === 202 || body.cancel_requested) {
        return { ok: true, canceled: false, cancelRequested: true };
    }
    if (status === 200) {
        return { ok: true, canceled: true, cancelRequested: false };
    }
    if (status === 404) {
        return { ok: true, canceled: true, cancelRequested: false };
    }
    if (status === 409 && body.code === 'job_already_finished') {
        return { ok: true, canceled: false, cancelRequested: false, finished: true };
    }
    return { ok: false, canceled: false, cancelRequested: false };
}

/**
 * Of the running jobs whose cancel was accepted (`pending`: job id ->
 * recording id), those the worker has stopped: their row has left the
 * queue answer, or is there as finished because the cancel came too late.
 * They are removed from `pending`; the ids of their recordings are returned
 * so the caller reloads them.
 */
export function settledCancels(pending, jobs) {
    const still = new Map((jobs || []).map(job => [job.id, job]));
    const recordingIds = [];
    for (const [jobId, recordingId] of [...pending]) {
        const job = still.get(jobId);
        if (job && ['queued', 'processing'].includes(job.job_status)) continue;
        pending.delete(jobId);
        if (recordingId !== null && recordingId !== undefined) recordingIds.push(recordingId);
    }
    return recordingIds;
}
