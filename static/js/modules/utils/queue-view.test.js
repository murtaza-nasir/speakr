import { describe, it, expect } from 'vitest';
import {
    cancelNeedsConfirm, finishedAgo, formatElapsed, groupQueueItems, queueGroup, queueSummary, rowMark,
    rowProgress, stageLabel, summaryParts,
} from './queue-view.js';
import { parseServerInstant } from './queue-data.js';

const item = (status, extra = {}) => ({ id: `rec_${status}`, status, jobId: 1, ...extra });

describe('groups', () => {
    it('puts each status in its group', () => {
        expect(queueGroup(item('uploading'))).toBe('active');
        expect(queueGroup(item('transcribing'))).toBe('active');
        expect(queueGroup(item('summarizing'))).toBe('active');
        expect(queueGroup(item('translating'))).toBe('active');
        expect(queueGroup(item('queued'))).toBe('waiting');
        expect(queueGroup(item('ready'))).toBe('waiting');
        expect(queueGroup(item('failed'))).toBe('failed');
        expect(queueGroup(item('upload_failed'))).toBe('failed');
        expect(queueGroup(item('completed'))).toBe('done');
    });

    it('keeps the order of the list inside a group', () => {
        const groups = groupQueueItems([
            item('completed', { id: 'a' }), item('transcribing', { id: 'b' }), item('queued', { id: 'c' }),
            item('uploading', { id: 'd' }), item('failed', { id: 'e' }), item('ready', { id: 'f' }),
        ]);
        expect(Object.keys(groups)).toEqual(['active', 'waiting', 'failed', 'done']);
        expect(groups.active.map(i => i.id)).toEqual(['b', 'd']);
        expect(groups.waiting.map(i => i.id)).toEqual(['c', 'f']);
        expect(groups.failed.map(i => i.id)).toEqual(['e']);
        expect(groups.done.map(i => i.id)).toEqual(['a']);
        expect(groupQueueItems(null)).toEqual({ active: [], waiting: [], failed: [], done: [] });
    });
});

describe('the indicator line', () => {
    const summary = (statuses) => queueSummary(groupQueueItems(statuses.map(s => item(s))));

    it('counts what is processed, waits, failed and finished', () => {
        expect(summary(['transcribing', 'uploading', 'queued', 'failed', 'completed'])).toEqual(
            { processing: 2, queued: 1, failed: 1, finished: 1, state: 'busy' });
    });

    it('has a state for busy, failed, done and empty', () => {
        expect(summary(['queued']).state).toBe('busy');
        expect(summary(['failed', 'completed']).state).toBe('failed');
        expect(summary(['completed']).state).toBe('done');
        expect(summary([]).state).toBe('empty');
    });

    it('names processing, queued and failures; finished only when nothing runs', () => {
        expect(summaryParts(summary(['transcribing', 'transcribing', 'queued', 'failed', 'completed']))).toEqual([
            { key: 'processing', count: 2, tone: 'plain' },
            { key: 'queued', count: 1, tone: 'plain' },
            { key: 'failed', count: 1, tone: 'error' },
        ]);
        expect(summaryParts(summary(['completed', 'completed']))).toEqual([{ key: 'finished', count: 2, tone: 'plain' }]);
        expect(summaryParts(summary(['completed', 'failed']))).toEqual([
            { key: 'finished', count: 1, tone: 'plain' }, { key: 'failed', count: 1, tone: 'error' },
        ]);
        expect(summaryParts(summary([]))).toEqual([]);
    });
});

describe('stageLabel', () => {
    it('names each stage the server reports', () => {
        const key = (stageCode, status = 'transcribing') => stageLabel(item(status, { stageCode })).key;
        expect(key('converting')).toBe('progressQueue.converting');
        expect(key('assembling')).toBe('progressQueue.stage.assembling');
        expect(key('joining')).toBe('progressQueue.joining');
        expect(key('transcribing')).toBe('status.transcribing');
        expect(key('titling')).toBe('progressQueue.stage.titling');
        expect(key('summarizing')).toBe('status.summarizing');
        expect(key('translating', 'translating')).toBe('progressQueue.translating');
        expect(key('indexing')).toBe('progressQueue.stage.indexing');
    });

    it('falls back to the status when the stage code is not known', () => {
        expect(stageLabel(item('summarizing', { stageCode: 'something-new' })).key).toBe('status.summarizing');
        expect(stageLabel(item('translating')).key).toBe('progressQueue.translating');
        expect(stageLabel(item('transcribing', { stage: 'joining' })).key).toBe('progressQueue.joining');
        expect(stageLabel(item('transcribing')).key).toBe('status.transcribing');
    });

    it('adds the step of the transcription service as a detail', () => {
        const label = stageLabel(item('transcribing', { stageCode: 'transcribing', stageDetail: 'aligning' }));
        expect(label).toEqual({ key: 'status.transcribing', params: null, detailKey: 'progressQueue.detail.aligning' });
        expect(stageLabel(item('transcribing', { stageCode: 'transcribing', stageDetail: 'transcribing' })).detailKey).toBeNull();
        expect(stageLabel(item('transcribing', { stageCode: 'transcribing', stageDetail: 'unheard-of' })).detailKey).toBeNull();
        expect(stageLabel(item('summarizing', { stageCode: 'summarizing', stageDetail: 'aligning' })).detailKey).toBeNull();
    });

    it('says what a waiting job waits for, with its place', () => {
        expect(stageLabel(item('queued', { queueName: 'transcription', queuePosition: 2 }))).toEqual(
            { key: 'progressQueue.queuedFor.transcription', params: { position: 2 }, detailKey: null });
        expect(stageLabel(item('queued', { queueName: 'convert', queuePosition: 1 })).key).toBe('progressQueue.queuedFor.conversion');
        expect(stageLabel(item('queued', { queueName: 'summary', queuePosition: 3 })).key).toBe('progressQueue.queuedFor.summary');
        expect(stageLabel(item('queued', { queueName: 'summary', queuePosition: 3, jobType: 'translate_transcription' })).key)
            .toBe('progressQueue.queuedFor.translation');
    });

    it('is plain Queued without a place or for an upload', () => {
        expect(stageLabel(item('queued', { queueName: 'transcription', queuePosition: null })).key).toBe('status.queued');
        expect(stageLabel(item('queued', { jobId: null, queueName: 'transcription', queuePosition: 2 })).key).toBe('status.queued');
    });

    it('shows Canceling while the cancel of a running job is pending', () => {
        expect(stageLabel(item('transcribing', { stageCode: 'transcribing', stageDetail: 'aligning', cancelRequested: true })))
            .toEqual({ key: 'progressQueue.canceling', params: null, detailKey: null });
    });

    it('names uploads and ends', () => {
        expect(stageLabel(item('uploading', { jobId: null })).key).toBe('status.uploading');
        expect(stageLabel(item('ready', { jobId: null })).key).toBe('progressQueue.waiting');
        expect(stageLabel(item('completed')).key).toBe('progressQueue.done');
        expect(stageLabel(item('failed')).key).toBe('progressQueue.failed');
        expect(stageLabel(item('upload_failed', { jobId: null })).key).toBe('progressQueue.uploadFailed');
    });
});

describe('rowProgress', () => {
    it('shows the uploaded share of a file', () => {
        expect(rowProgress(item('uploading', { jobId: null, progress: 64 }))).toEqual({ kind: 'percent', percent: 64 });
        expect(rowProgress(item('uploading', { jobId: null, progress: 140 }))).toEqual({ kind: 'percent', percent: 100 });
        expect(rowProgress(item('uploading', { jobId: null }))).toEqual({ kind: 'percent', percent: 0 });
    });

    it('moves without a number while the server checks an uploaded file', () => {
        expect(rowProgress(item('uploading', { jobId: null, progress: 100, serverProcessing: true, serverProcessingAt: 5000 })))
            .toEqual({ kind: 'moving', since: 5000 });
        expect(rowProgress(item('uploading', { jobId: null, progress: 100, serverProcessing: true })))
            .toEqual({ kind: 'moving', since: null });
    });

    it('shows finished parts of a total', () => {
        expect(rowProgress(item('transcribing', { progressInfo: { kind: 'chunks', done: 3, total: 8, percent: 38 } })))
            .toEqual({ kind: 'count', percent: 38, done: 3, total: 8 });
    });

    it('shows a measured fraction as a percent', () => {
        expect(rowProgress(item('transcribing', { progressInfo: { kind: 'fraction', fraction: 0.42, percent: 42 } })))
            .toEqual({ kind: 'percent', percent: 42 });
    });

    it('invents no number: a moving bar with the start of the step', () => {
        const started = '2026-11-15T09:30:05Z';
        expect(rowProgress(item('transcribing', { progressInfo: null, startedAt: started }), parseServerInstant))
            .toEqual({ kind: 'moving', since: Date.UTC(2026, 10, 15, 9, 30, 5) });
        expect(rowProgress(item('summarizing', { progressInfo: null, startedAt: null }), parseServerInstant))
            .toEqual({ kind: 'moving', since: null });
        // The stored percent of an older answer is not a real number for the row
        expect(rowProgress(item('transcribing', { progress: 50, progressInfo: null }), parseServerInstant).kind).toBe('moving');
    });

    it('has no bar for rows that wait or have ended', () => {
        for (const status of ['queued', 'ready', 'completed', 'failed', 'upload_failed']) {
            expect(rowProgress(item(status, { progress: 100 }))).toEqual({ kind: 'none' });
        }
    });
});

describe('times', () => {
    it('formats elapsed time without an estimate', () => {
        expect(formatElapsed(0)).toBe('0:00');
        expect(formatElapsed(9.9)).toBe('0:09');
        expect(formatElapsed(134)).toBe('2:14');
        expect(formatElapsed(3599)).toBe('59:59');
        expect(formatElapsed(3723)).toBe('1:02:03');
        expect(formatElapsed(-5)).toBe('0:00');
        expect(formatElapsed(undefined)).toBe('0:00');
    });

    it('words the time since a row finished', () => {
        const now = 10_000_000;
        expect(finishedAgo(now - 20_000, now)).toEqual({ key: 'progressQueue.justNow', count: 0 });
        expect(finishedAgo(now - 120_000, now)).toEqual({ key: 'progressQueue.minutesAgo', count: 2 });
        expect(finishedAgo(now - 59 * 60_000, now)).toEqual({ key: 'progressQueue.minutesAgo', count: 59 });
        expect(finishedAgo(now - 3 * 3_600_000, now)).toEqual({ key: 'progressQueue.hoursAgo', count: 3 });
        expect(finishedAgo(now + 5000, now)).toEqual({ key: 'progressQueue.justNow', count: 0 });
        expect(finishedAgo(NaN, now)).toBeNull();
    });
});

describe('rowMark', () => {
    it('keeps waiting rows neutral and marks the rest by stage', () => {
        expect(rowMark(item('queued')).tone).toBe('wait');
        expect(rowMark(item('ready')).tone).toBe('wait');
        expect(rowMark(item('uploading')).tone).toBe('info');
        expect(rowMark(item('transcribing', { stageCode: 'transcribing' }))).toEqual({ icon: 'fa-microphone', tone: 'proc' });
        expect(rowMark(item('transcribing', { stageCode: 'converting' })).tone).toBe('proc');
        expect(rowMark(item('transcribing', { stageCode: 'titling' })).tone).toBe('ok');
        expect(rowMark(item('summarizing')).tone).toBe('ok');
        expect(rowMark(item('translating')).tone).toBe('ok');
        expect(rowMark(item('failed')).tone).toBe('error');
        expect(rowMark(item('completed')).tone).toBe('done');
    });
});

describe('cancelNeedsConfirm', () => {
    const first = (extra = {}) => item('transcribing', { isNewUpload: true, stageCode: 'transcribing', ...extra });

    it('asks for a running first transcription or its conversion', () => {
        expect(cancelNeedsConfirm(first())).toBe(true);
        expect(cancelNeedsConfirm(first({ stageCode: 'converting' }))).toBe(true);
    });

    it('does not ask once the transcript of the run is stored', () => {
        expect(cancelNeedsConfirm(first({ stageCode: 'titling' }))).toBe(false);
        expect(cancelNeedsConfirm(first({ stageCode: 'summarizing' }))).toBe(false);
    });

    it('does not ask for a waiting job, a reprocess, a translation or an upload', () => {
        expect(cancelNeedsConfirm(item('queued', { isNewUpload: true, stageCode: 'queued' }))).toBe(false);
        expect(cancelNeedsConfirm(first({ isNewUpload: false }))).toBe(false);
        expect(cancelNeedsConfirm(item('translating', { isNewUpload: false, stageCode: 'translating' }))).toBe(false);
        expect(cancelNeedsConfirm(item('uploading', { jobId: null, isNewUpload: true }))).toBe(false);
        expect(cancelNeedsConfirm(null)).toBe(false);
    });
});
