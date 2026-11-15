import { describe, it, expect } from 'vitest';
import {
    clockOffsetMs, elapsedSeconds, jobProgress, jobStage, parseServerInstant, queueFieldsFromJob,
} from './queue-data.js';

const running = (extra = {}) => ({
    id: 7, job_status: 'processing', job_type: 'transcribe', queue_type: 'transcription', ...extra,
});

describe('parseServerInstant', () => {
    it('reads a timestamp with the UTC marker', () => {
        expect(parseServerInstant('2026-11-15T09:30:05Z')).toBe(Date.UTC(2026, 10, 15, 9, 30, 5));
    });
    it('reads a naive timestamp of an older server as UTC', () => {
        expect(parseServerInstant('2026-11-15T09:30:05')).toBe(Date.UTC(2026, 10, 15, 9, 30, 5));
        expect(parseServerInstant('2026-11-15T09:30:05.250000')).toBe(Date.UTC(2026, 10, 15, 9, 30, 5, 250));
    });
    it('reads an explicit offset as written', () => {
        expect(parseServerInstant('2026-11-15T10:30:05+01:00')).toBe(Date.UTC(2026, 10, 15, 9, 30, 5));
    });
    it('is null for nothing and for text that is no date', () => {
        expect(parseServerInstant(null)).toBeNull();
        expect(parseServerInstant('')).toBeNull();
        expect(parseServerInstant('soon')).toBeNull();
    });
});

describe('jobProgress', () => {
    it('is null without a real number', () => {
        expect(jobProgress(running({ progress: null }))).toBeNull();
        expect(jobProgress(running())).toBeNull();
        expect(jobProgress(running({ job_type: 'summarize', progress: null }))).toBeNull();
    });
    it('never invents a number from the stage', () => {
        for (const job_type of ['convert', 'merge', 'transcribe', 'summarize', 'reprocess_summary']) {
            expect(jobProgress(running({ job_type }))).toBeNull();
            expect(queueFieldsFromJob(running({ job_type })).progress).toBeNull();
            expect(queueFieldsFromJob(running({ job_type })).indeterminate).toBe(true);
        }
    });
    it('reads chunks finished of a total', () => {
        expect(jobProgress(running({ progress: { done: 3, total: 8 }, progress_kind: 'chunks' })))
            .toEqual({ kind: 'chunks', done: 3, total: 8, percent: 38 });
    });
    it('reads a measured fraction', () => {
        expect(jobProgress(running({ progress: { fraction: 0.42 }, progress_kind: 'fraction' })))
            .toEqual({ kind: 'fraction', fraction: 0.42, percent: 42 });
    });
    it('keeps the number inside its range', () => {
        expect(jobProgress(running({ progress: { fraction: 1.4 } })).percent).toBe(100);
        expect(jobProgress(running({ progress: { done: 9, total: 4 } })).done).toBe(4);
        expect(jobProgress(running({ progress: { done: 1, total: 0 } }))).toBeNull();
    });
    it('is null for a job that is not running', () => {
        expect(jobProgress({ job_status: 'queued', progress: { done: 1, total: 2 } })).toBeNull();
        expect(jobProgress({ job_status: 'completed', progress: { done: 2, total: 2 } })).toBeNull();
    });
    it('reads the chunk counts of a translation from a server without the progress field', () => {
        const job = running({ job_type: 'translate_transcription', progress_done: 12, progress_total: 18 });
        expect(jobProgress(job)).toEqual({ kind: 'chunks', done: 12, total: 18, percent: 67 });
        // The same counts on a transcription were an estimate there: not shown.
        expect(jobProgress(running({ progress_done: 40, progress_total: 100 }))).toBeNull();
    });
});

describe('jobStage', () => {
    it('uses the code the server sends', () => {
        expect(jobStage(running({ stage: 'titling' }))).toBe('titling');
    });
    it('derives the code for a server that sends none', () => {
        expect(jobStage({ job_status: 'queued', job_type: 'transcribe' })).toBe('queued');
        expect(jobStage(running({ job_type: 'convert' }))).toBe('converting');
        expect(jobStage(running({ job_type: 'merge' }))).toBe('joining');
        expect(jobStage(running({ job_type: 'reprocess_summary', queue_type: 'summary' }))).toBe('summarizing');
        expect(jobStage(running({ job_type: 'translate_transcription' }))).toBe('translating');
        expect(jobStage({ job_status: 'failed', job_type: 'transcribe' })).toBe('failed');
    });
});

describe('elapsedSeconds', () => {
    const now = Date.UTC(2026, 10, 15, 9, 31, 10);
    it('counts from started_at', () => {
        expect(elapsedSeconds(running({ started_at: '2026-11-15T09:30:05Z' }), now)).toBe(65);
        expect(elapsedSeconds(running({ started_at: '2026-11-15T09:30:05' }), now)).toBe(65);
    });
    it('is never negative', () => {
        expect(elapsedSeconds(running({ started_at: '2026-11-15T09:40:00Z' }), now)).toBe(0);
    });
    it('is null for a job that is not running or has no start', () => {
        expect(elapsedSeconds(running(), now)).toBeNull();
        expect(elapsedSeconds({ job_status: 'queued', started_at: '2026-11-15T09:30:05Z' }, now)).toBeNull();
    });
    it('follows the server clock when the browser clock is off', () => {
        const browserNow = now - 5 * 60 * 1000;                    // five minutes behind
        const offset = clockOffsetMs('2026-11-15T09:31:10Z', browserNow);
        expect(offset).toBe(5 * 60 * 1000);
        expect(elapsedSeconds(running({ started_at: '2026-11-15T09:30:05Z' }), browserNow + offset)).toBe(65);
        expect(clockOffsetMs(undefined, browserNow)).toBe(0);
    });
});

describe('queueFieldsFromJob', () => {
    it('describes a waiting job', () => {
        const fields = queueFieldsFromJob({
            job_status: 'queued', job_type: 'transcribe', queue_type: 'transcription', stage: 'queued',
            queue: 'transcription', queue_position: 3, position: 3, can_cancel: true,
            created_at: '2026-11-15T09:00:00Z', started_at: null,
        });
        expect(fields).toMatchObject({
            stageCode: 'queued', queuePosition: 3, queueName: 'transcription', progress: null,
            indeterminate: false, canCancel: true, canRetry: false, canRemove: false, cancelRequested: false,
        });
    });
    it('describes a running job with a number', () => {
        const fields = queueFieldsFromJob(running({
            stage: 'transcribing', stage_detail: 'aligning', progress: { fraction: 0.5 },
            started_at: '2026-11-15T09:30:05Z', can_cancel: true,
        }));
        expect(fields).toMatchObject({
            stageCode: 'transcribing', stageDetail: 'aligning', progress: 50, indeterminate: false,
            queuePosition: null, startedAt: '2026-11-15T09:30:05Z', canCancel: true,
        });
    });
    it('offers no second cancel while one is under way', () => {
        const fields = queueFieldsFromJob(running({ can_cancel: true, cancel_requested: true }));
        expect(fields.cancelRequested).toBe(true);
        expect(fields.canCancel).toBe(false);
    });
    it('describes finished and failed jobs', () => {
        const done = queueFieldsFromJob({
            job_status: 'completed', job_type: 'transcribe', finished_at: '2026-11-15T09:45:00Z',
        });
        expect(done).toMatchObject({ canRemove: true, canRetry: false, finishedAt: '2026-11-15T09:45:00Z' });
        const failed = queueFieldsFromJob({
            job_status: 'failed', job_type: 'transcribe', completed_at: '2026-11-15T09:45:00',
        });
        expect(failed).toMatchObject({ canRemove: true, canRetry: true, finishedAt: '2026-11-15T09:45:00' });
    });
    it('offers no cancel on an older server', () => {
        expect(queueFieldsFromJob(running()).canCancel).toBe(false);
    });
});
