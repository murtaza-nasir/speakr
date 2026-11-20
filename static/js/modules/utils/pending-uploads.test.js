import { describe, it, expect } from 'vitest';
import {
    OFFLINE_CONTEXT_KEY, OFFLINE_LOCALES, failedUploadRecord, belongsTo, partitionByOwner, resurfaceAction,
    queueItemFromRecord, coveredByPendingUpload, offlineLocale, offlineContext, parseOfflineContext,
} from './pending-uploads.js';
import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const recordingItem = () => ({
    file: { name: 'recording-x.webm', size: 2048, type: 'audio/webm' },
    clientId: 'client-1-abc',
    notes: 'n',
    tags: [{ id: 1 }],
    asrOptions: { language: 'de' },
    folder_id: 4,
    fromInProgressRecording: true,
    recordingSessionStartTime: '2026-11-20T10:00:00.000Z',
    serverSessionId: 'sess-9',
    duration: null,
});

describe('failedUploadRecord', () => {
    it('keeps what ties a failed upload to its recording and account (#650)', () => {
        const record = failedUploadRecord(recordingItem(), new Error('Network error during upload'), 12);
        expect(record).toMatchObject({
            fileName: 'recording-x.webm', fileSize: 2048, clientId: 'client-1-abc', notes: 'n',
            tags: [{ id: 1 }], asrOptions: { language: 'de' }, folderId: 4,
            error: 'Network error during upload', fromRecorder: true,
            recordingSessionStartTime: '2026-11-20T10:00:00.000Z', serverSessionId: 'sess-9',
            duration: null, ownerId: '12',
        });
    });

    it('marks an upload of a file from disk as not recorded here, and leaves the owner empty without an account', () => {
        const record = failedUploadRecord({ file: { name: 'a.mp3', size: 1 }, clientId: 'c-12345678', duration: 61 }, 'x', '');
        expect(record).toMatchObject({ fromRecorder: false, recordingSessionStartTime: null, serverSessionId: null, duration: 61, ownerId: null, error: 'x' });
    });
});

describe('the owner of a record', () => {
    it('uploads a record only for the account it was saved for', () => {
        expect(belongsTo({ ownerId: '3' }, 3)).toBe(true);
        expect(belongsTo({ ownerId: '3' }, '3')).toBe(true);
        expect(belongsTo({ ownerId: '3' }, '4')).toBe(false);
        // Without a known account nothing owned is uploaded.
        expect(belongsTo({ ownerId: '3' }, '')).toBe(false);
        expect(belongsTo({ ownerId: '3' }, null)).toBe(false);
    });

    it('uploads a record of an earlier release, which has no owner, as before', () => {
        expect(belongsTo({}, '4')).toBe(true);
        expect(belongsTo({ ownerId: null }, '4')).toBe(true);
    });

    it('splits the records into those of this account and of others', () => {
        const records = [{ id: 1, ownerId: '1' }, { id: 2, ownerId: '2' }, { id: 3 }, null];
        const { mine, others } = partitionByOwner(records, '1');
        expect(mine.map((r) => r.id)).toEqual([1, 3]);
        expect(others.map((r) => r.id)).toEqual([2]);
    });
});

describe('resurfaceAction', () => {
    it('does not send an upload again that waits or is under way in the queue', () => {
        for (const status of ['queued', 'ready', 'uploading', 'pending']) {
            expect(resurfaceAction({ clientId: 'c1' }, [{ clientId: 'c1', status }]), status).toBe('skip');
        }
    });

    it('replaces a failed item of the same upload and adds one that is not in the queue', () => {
        expect(resurfaceAction({ clientId: 'c1' }, [{ clientId: 'c1', status: 'failed' }])).toBe('replace');
        expect(resurfaceAction({ clientId: 'c1' }, [{ clientId: 'c2', status: 'uploading' }])).toBe('add');
        expect(resurfaceAction({ clientId: 'c1' }, [])).toBe('add');
    });
});

describe('queueItemFromRecord', () => {
    it('gives the retried upload the ties of its record, so that its success removes the other copies', () => {
        const file = { name: 'f' };
        const item = queueItemFromRecord({
            id: 8, clientId: 'c8', notes: 'x', tags: [2], asrOptions: { a: 1 }, folderId: 5, retryCount: 1,
            fromRecorder: true, recordingSessionStartTime: 'T', serverSessionId: 'S', duration: 4.5,
        }, file);
        expect(item).toMatchObject({
            file, status: 'ready', clientId: 'c8', notes: 'x', tags: [2], asrOptions: { a: 1 }, folder_id: 5,
            preserveOptions: true, fromInProgressRecording: true, recordingSessionStartTime: 'T',
            serverSessionId: 'S', duration: 4.5, _failedUploadId: 8, _retryCount: 1, _resurfaced: true,
        });
    });

    it('keeps an upload of a file from disk one without recorder ties', () => {
        const item = queueItemFromRecord({ id: 1, clientId: 'c' }, {});
        expect(item).toMatchObject({ fromInProgressRecording: false, recordingSessionStartTime: null, serverSessionId: null, folder_id: null });
    });
});

describe('coveredByPendingUpload', () => {
    const records = [{ recordingSessionStartTime: 'T1', serverSessionId: null }, { recordingSessionStartTime: null, serverSessionId: 'S1' }];

    it('finds the crash-recovery copy and the server session of a recording that waits as a failed upload', () => {
        expect(coveredByPendingUpload(records, { startTime: 'T1' })).toBe(true);
        expect(coveredByPendingUpload(records, { sessionId: 'S1' })).toBe(true);
    });

    it('offers every other copy for recovery', () => {
        expect(coveredByPendingUpload(records, { startTime: 'T2' })).toBe(false);
        expect(coveredByPendingUpload(records, { sessionId: 'S2' })).toBe(false);
        expect(coveredByPendingUpload(records, {})).toBe(false);
        expect(coveredByPendingUpload([], { startTime: 'T1' })).toBe(false);
        expect(coveredByPendingUpload([{}], { startTime: null, sessionId: null })).toBe(false);
    });
});

describe('the entry for the offline recorder', () => {
    it('is named as the sign-out clearing names it', () => {
        expect(OFFLINE_CONTEXT_KEY).toBe('speakr.offlineRecorder');
    });

    const DAY = 24 * 60 * 60 * 1000;
    const NOLIMITS = { maxSeconds: null, maxRecorderBytes: null, maxUploadBytes: null };

    it('holds the account id, the language, the incognito default, its expiry and the limits, and nothing without an account', () => {
        expect(offlineContext({ userId: 5, locale: 'fr', incognitoDefault: false, now: 10 }))
            .toEqual({ userId: '5', locale: 'fr', incognitoDefault: false, savedAt: 10, expiresAt: 10 + 30 * DAY, limits: NOLIMITS });
        expect(offlineContext({
            userId: 5, locale: 'de', now: 0, days: 7, maxSeconds: 28800, maxRecorderBytes: 200 * 1048576, maxUploadBytes: 250 * 1048576,
        })).toEqual({
            userId: '5', locale: 'de', incognitoDefault: false, savedAt: 0, expiresAt: 7 * DAY,
            limits: { maxSeconds: 28800, maxRecorderBytes: 200 * 1048576, maxUploadBytes: 250 * 1048576 },
        });
        expect(offlineContext({ userId: '', locale: 'fr' })).toBeNull();
        expect(offlineContext({})).toBeNull();
    });

    it('lasts 30 days when REMEMBER_COOKIE_DAYS is unknown or not a number, and drops limits that are not numbers', () => {
        for (const days of [undefined, null, 0, -3, 'x', NaN]) {
            expect(offlineContext({ userId: 1, now: 0, days }).expiresAt, String(days)).toBe(30 * DAY);
        }
        expect(offlineContext({ userId: 1, now: 0, maxSeconds: 'x', maxRecorderBytes: -1, maxUploadBytes: 0 }).limits).toEqual(NOLIMITS);
    });

    it('is read back only when well formed', () => {
        const raw = JSON.stringify(offlineContext({ userId: '5', locale: 'pt-BR', incognitoDefault: true, now: 3, maxSeconds: 60 }));
        expect(parseOfflineContext(raw, 4)).toEqual({
            userId: '5', locale: 'pt-BR', incognitoDefault: true, savedAt: 3, expiresAt: 3 + 30 * DAY,
            limits: { ...NOLIMITS, maxSeconds: 60 },
        });
        for (const bad of [null, '', 'not json', '{}', '{"userId":""}', '[]', '"x"']) {
            expect(parseOfflineContext(bad), String(bad)).toBeNull();
        }
    });

    it('is not read back once it has expired', () => {
        const raw = JSON.stringify(offlineContext({ userId: '5', now: 1000, days: 2 }));
        expect(parseOfflineContext(raw, 1000 + 2 * DAY - 1)).not.toBeNull();
        expect(parseOfflineContext(raw, 1000 + 2 * DAY)).toBeNull();
        expect(parseOfflineContext(raw, 1000 + 40 * DAY)).toBeNull();
    });

    it('gives an entry of the first release, without an expiry, 30 days from its save', () => {
        const raw = JSON.stringify({ userId: '5', locale: 'en', incognitoDefault: false, savedAt: 1000 });
        expect(parseOfflineContext(raw, 1000 + 29 * DAY)).toMatchObject({ userId: '5', expiresAt: 1000 + 30 * DAY, limits: NOLIMITS });
        expect(parseOfflineContext(raw, 1000 + 30 * DAY)).toBeNull();
    });

    it('names one of the languages of static/locales, else English', () => {
        const files = readdirSync(fileURLToPath(new URL('../../../locales/', import.meta.url)))
            .filter((f) => f.endsWith('.json')).map((f) => f.slice(0, -5)).sort();
        expect([...OFFLINE_LOCALES].sort()).toEqual(files);
        expect(offlineLocale('de')).toBe('de');
        expect(offlineLocale('de-AT')).toBe('de');
        expect(offlineLocale('pt')).toBe('pt-BR');
        expect(offlineLocale('pt-PT')).toBe('pt-BR');
        expect(offlineLocale('zh-CN')).toBe('zh');
        expect(offlineLocale('ja')).toBe('en');
        expect(offlineLocale('../x')).toBe('en');
        expect(offlineLocale(undefined)).toBe('en');
    });
});
