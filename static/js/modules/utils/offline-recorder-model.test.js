import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
    DEFAULT_STRINGS, interpolate, translator, formatDuration, formatSize, recorderAvailability, pendingRows,
    pickMimeType, recordingFileName, offlineClientId, whenServerIsBack, returnTarget, serverAnswered,
    freeBytes, canStart, recordingLimits, limitState, stoppedMessage, MIN_FREE_BYTES, STORAGE_MARGIN_BYTES,
    DEFAULT_MAX_SECONDS, captureMessage, SOURCE_KEY,
} from './offline-recorder-model.js';
import { CaptureError } from './audio-capture.js';
import { belongsTo, parseOfflineContext } from './pending-uploads.js';

const locale = (code) => JSON.parse(readFileSync(fileURLToPath(new URL(`../../../locales/${code}.json`, import.meta.url)), 'utf8'));
const EN = locale('en');

describe('the texts of the offline recorder', () => {
    it('are the English texts of en.json, so the page reads the same without a locale file', () => {
        expect(EN.offlineRecorder).toEqual(DEFAULT_STRINGS);
    });

    it('are translated in every language, with the same placeholders', () => {
        for (const code of ['de', 'es', 'fr', 'pt-BR', 'ru', 'zh']) {
            const table = locale(code).offlineRecorder;
            expect(Object.keys(table).sort(), code).toEqual(Object.keys(DEFAULT_STRINGS).sort());
            for (const [key, text] of Object.entries(DEFAULT_STRINGS)) {
                const holes = (s) => (s.match(/\{\{\w+\}\}/g) || []).sort();
                expect(holes(table[key]), `${code}.${key}`).toEqual(holes(text));
                if (key !== 'pageTitle' && !['delete', 'deleteConfirm'].includes(key)) {
                    // Translated, not copied (titles and short words may coincide).
                    if (text.length > 20) expect(table[key], `${code}.${key}`).not.toBe(text);
                }
            }
        }
    });

    it('uses the language, then English, then the built-in texts', () => {
        const t = translator({ offlineRecorder: { record: 'Aufnehmen' }, fileSize: { kilobytes: '{{count}} KB' } },
            { offlineRecorder: { stop: 'Stop (en file)' } });
        expect(t('record')).toBe('Aufnehmen');
        expect(t('stop')).toBe('Stop (en file)');
        expect(t('saved')).toBe(DEFAULT_STRINGS.saved);
        expect(t('fileSize.kilobytes', { count: 3 })).toBe('3 KB');
        expect(t('nothing.here')).toBe('nothing.here');
        expect(translator(null, null)('itemName', { date: 'today' })).toBe('Recording of today');
    });

    it('fills placeholders and leaves unknown ones', () => {
        expect(interpolate('{{a}} and {{ b }} and {{c}}', { a: 1, b: 'x' })).toBe('1 and x and {{c}}');
    });
});

describe('formatting', () => {
    it('shows a duration as m:ss or h:mm:ss, a dash when unknown', () => {
        expect(formatDuration(0)).toBe('0:00');
        expect(formatDuration(7.9)).toBe('0:07');
        expect(formatDuration(750)).toBe('12:30');
        expect(formatDuration(3723)).toBe('1:02:03');
        expect(formatDuration(null)).toBe('–');
        expect(formatDuration(-1)).toBe('–');
    });

    it('shows a size with the units of the locale file', () => {
        const t = translator(EN, EN);
        expect(formatSize(512, t)).toBe('512 B');
        expect(formatSize(2048, t)).toBe('2 KB');
        expect(formatSize(1.5 * 1024 * 1024, t)).toBe('1.5 MB');
        expect(formatSize(25 * 1024 * 1024, t)).toBe('25 MB');
        expect(formatSize(undefined, t)).toBe('0 B');
    });
});

describe('recorderAvailability', () => {
    const context = (fields) => parseOfflineContext(JSON.stringify({ userId: '3', locale: 'en', savedAt: Date.now(), ...fields }));

    it('records nothing without the entry of a signed-in online visit', () => {
        expect(recorderAvailability(null)).toMatchObject({ canRecord: false, reason: 'notSignedIn' });
    });

    it('records nothing when incognito mode is the default', () => {
        expect(recorderAvailability(context({ incognitoDefault: true }))).toMatchObject({ canRecord: false, reason: 'incognito' });
    });

    it('records nothing without a microphone API, MediaRecorder or IndexedDB', () => {
        for (const missing of ['mediaDevices', 'mediaRecorder', 'indexedDB']) {
            expect(recorderAvailability(context({}), { [missing]: false }), missing).toMatchObject({ canRecord: false, reason: 'noMicrophone' });
        }
    });

    it('records nothing once the entry has expired', () => {
        const raw = JSON.stringify({ userId: '3', locale: 'en', savedAt: 0, expiresAt: 1000 });
        expect(recorderAvailability(parseOfflineContext(raw, 999))).toMatchObject({ canRecord: true });
        expect(recorderAvailability(parseOfflineContext(raw, 1000))).toMatchObject({ canRecord: false, reason: 'notSignedIn' });
    });

    it('records for the account of the entry', () => {
        expect(recorderAvailability(context({}))).toMatchObject({ canRecord: true, reason: null, context: { userId: '3' } });
    });
});

describe('pendingRows', () => {
    const t = translator(EN, EN);
    const formatDate = (ms) => `D${ms}`;
    const records = [
        { id: 1, fileName: 'recording-a.webm', fileSize: 2048, duration: 65, timestamp: 100, source: 'offline', ownerId: '3' },
        { id: 2, fileName: 'talk.mp3', fileSize: 1024 * 1024, duration: null, timestamp: 300, ownerId: '3' },
        { id: 3, fileName: 'theirs.webm', fileSize: 10, timestamp: 200, fromRecorder: true, ownerId: '4' },
        { id: 4, fileName: 'old.webm', fileSize: 10, timestamp: 50, fromRecorder: true },
    ];

    it('lists the records of this account newest first, with name, duration and size', () => {
        expect(pendingRows(records, '3', { t, formatDate, belongsTo })).toEqual([
            { id: 2, name: 'talk.mp3', duration: '–', size: '1 MB' },
            { id: 1, name: 'Recording of D100', duration: '1:05', size: '2 KB' },
            { id: 4, name: 'Recording of D50', duration: '–', size: '10 B' },
        ]);
    });

    it('lists nothing of another account, and nothing without an account', () => {
        expect(pendingRows(records, '4', { t, formatDate, belongsTo }).map((r) => r.id)).toEqual([3, 4]);
        expect(pendingRows(records, '', { t, formatDate, belongsTo })).toEqual([]);
        expect(pendingRows(records, null, { t, formatDate, belongsTo })).toEqual([]);
    });
});

describe('recording', () => {
    it('picks the first container the browser can record', () => {
        expect(pickMimeType((type) => type === 'audio/mp4')).toBe('audio/mp4');
        expect(pickMimeType(() => true)).toBe('audio/webm;codecs=opus');
        expect(pickMimeType(() => false)).toBe('');
        expect(pickMimeType(() => { throw new Error('x'); })).toBe('');
        expect(pickMimeType(undefined)).toBe('');
    });

    it('names the file as the in-app recorder does', () => {
        const date = new Date('2026-11-20T10:11:12.345Z');
        expect(recordingFileName(date, 'audio/webm')).toBe('recording-2026-11-20T10-11-12-345Z.webm');
        expect(recordingFileName(date, 'audio/mp4')).toBe('recording-2026-11-20T10-11-12-345Z.m4a');
        expect(recordingFileName(date, 'audio/ogg')).toBe('recording-2026-11-20T10-11-12-345Z.ogg');
    });

    it('gives an id the server accepts as client_upload_id', () => {
        const id = offlineClientId(1763633472345, '4fzyo82mvyr');
        expect(id).toMatch(/^[A-Za-z0-9_-]{8,80}$/);
        expect(offlineClientId(1, '')).toMatch(/^[A-Za-z0-9_-]{8,80}$/);
        expect(offlineClientId(1, '$$/..')).toMatch(/^[A-Za-z0-9_-]{8,80}$/);
    });
});

describe('back online', () => {
    it('opens the app at once, but not during a recording or while it is saved', () => {
        expect(whenServerIsBack({})).toBe('open');
        expect(whenServerIsBack({ recording: true })).toBe('wait');
        expect(whenServerIsBack({ saving: true })).toBe('wait');
    });

    it('loads the page that was asked for, or the app when the file was opened by its own address', () => {
        expect(returnTarget('/recordings/12')).toBeNull();
        expect(returnTarget('/')).toBeNull();
        expect(returnTarget('/static/offline-recorder.html')).toBe('/');
    });

    it('does not count a proxy\'s 502 or 504 as the server', () => {
        expect(serverAnswered(200)).toBe(true);
        expect(serverAnswered(404)).toBe(true);
        expect(serverAnswered(503)).toBe(true);
        expect(serverAnswered(502)).toBe(false);
        expect(serverAnswered(504)).toBe(false);
        expect(serverAnswered(undefined)).toBe(false);
    });
});

describe('the limits of a recording', () => {
    const MB = 1024 * 1024;
    const t = translator(EN, EN);

    it('reads the free storage from navigator.storage.estimate(), unknown when it gives nothing', () => {
        expect(freeBytes({ quota: 100, usage: 30 })).toBe(70);
        expect(freeBytes({ quota: 10, usage: 30 })).toBe(0);
        expect(freeBytes(null)).toBeNull();
        expect(freeBytes({ quota: 100 })).toBeNull();
    });

    it('starts only with the minimum free, or when the browser cannot say', () => {
        expect(canStart(MIN_FREE_BYTES)).toBe(true);
        expect(canStart(MIN_FREE_BYTES - 1)).toBe(false);
        expect(canStart(0)).toBe(false);
        expect(canStart(null)).toBe(true);
    });

    it('takes RECORDING_MAX_HOURS and the smaller of the two sizes', () => {
        expect(recordingLimits({ maxSeconds: 3600, maxRecorderBytes: 200 * MB, maxUploadBytes: 250 * MB }))
            .toEqual({ maxSeconds: 3600, maxBytes: 200 * MB });
        expect(recordingLimits({ maxSeconds: 3600, maxRecorderBytes: 200 * MB, maxUploadBytes: 100 * MB }).maxBytes).toBe(100 * MB);
        expect(recordingLimits({ maxRecorderBytes: null, maxUploadBytes: 5 })).toEqual({ maxSeconds: DEFAULT_MAX_SECONDS, maxBytes: 5 });
        expect(recordingLimits({})).toEqual({ maxSeconds: DEFAULT_MAX_SECONDS, maxBytes: null });
        expect(recordingLimits(undefined)).toEqual({ maxSeconds: DEFAULT_MAX_SECONDS, maxBytes: null });
    });

    it('counts down the duration, warns at 80 % and stops at all of it', () => {
        expect(limitState({ elapsed: 7, bytes: 7000, maxSeconds: 10 })).toMatchObject({ binding: 'duration', secondsLeft: 3, warn: false, stop: false });
        expect(limitState({ elapsed: 8, bytes: 8000, maxSeconds: 10 })).toMatchObject({ binding: 'duration', warn: true, stop: false });
        expect(limitState({ elapsed: 10, bytes: 10000, maxSeconds: 10 })).toMatchObject({ stop: true, reason: 'duration', secondsLeft: 0 });
    });

    it('turns the size limit into time at the rate so far, and stops when the bytes reach it', () => {
        // 10 kB/s, 1 MB cap, an hour allowed: the size binds.
        const state = limitState({ elapsed: 50, bytes: 500000, maxSeconds: 3600, maxBytes: 1000000 });
        expect(state).toMatchObject({ binding: 'size', warn: false, stop: false });
        expect(state.secondsLeft).toBeCloseTo(50);
        expect(limitState({ elapsed: 80, bytes: 800000, maxSeconds: 3600, maxBytes: 1000000 })).toMatchObject({ binding: 'size', warn: true });
        expect(limitState({ elapsed: 100, bytes: 1000000, maxSeconds: 3600, maxBytes: 1000000 })).toMatchObject({ stop: true, reason: 'size' });
        // With chunks of two seconds the chunk to come counts: no chunk carries it over the limit.
        expect(limitState({ elapsed: 6, bytes: 72000, maxSeconds: 3600, maxBytes: 90000, step: 2 })).toMatchObject({ stop: true, reason: 'size' });
        expect(limitState({ elapsed: 4, bytes: 48000, maxSeconds: 3600, maxBytes: 90000, step: 2 })).toMatchObject({ warn: true, stop: false });
        expect(limitState({ elapsed: 2, bytes: 24000, maxSeconds: 3600, maxBytes: 90000, step: 2 })).toMatchObject({ warn: false });
        // A chunk larger than the average (the first one holds the file header) counts as what may still come
        expect(limitState({ elapsed: 8, bytes: 60000, maxSeconds: 3600, maxBytes: 90000, step: 2, largestChunk: 20000 })).toMatchObject({ stop: true, reason: 'size' });
        expect(limitState({ elapsed: 8, bytes: 50000, maxSeconds: 3600, maxBytes: 90000, step: 2, largestChunk: 20000 })).toMatchObject({ stop: false, warn: true });
        // Before a byte is known the size has no time; the duration is shown.
        expect(limitState({ elapsed: 0, bytes: 0, maxSeconds: 60, maxBytes: 1000 })).toMatchObject({ binding: 'duration', secondsLeft: 60 });
    });

    it('stops before the storage runs out, keeping room for the save and a margin', () => {
        const free = STORAGE_MARGIN_BYTES + 1000000;
        expect(limitState({ elapsed: 10, bytes: 400000, maxSeconds: 3600, free })).toMatchObject({ binding: 'storage', stop: false });
        expect(limitState({ elapsed: 10, bytes: 800000, maxSeconds: 3600, free })).toMatchObject({ binding: 'storage', warn: true, stop: false });
        expect(limitState({ elapsed: 10, bytes: 1000000, maxSeconds: 3600, free })).toMatchObject({ stop: true, reason: 'storage' });
        // Plenty of storage: the duration binds.
        expect(limitState({ elapsed: 10, bytes: 1000, maxSeconds: 60, free: 10 * 1024 * MB })).toMatchObject({ binding: 'duration' });
    });

    it('names the limit reached', () => {
        expect(stoppedMessage('duration', { maxSeconds: 3600 }, t)).toBe('The recording reached the maximum length of 1:00:00. It was stopped and saved.');
        expect(stoppedMessage('size', { maxBytes: 200 * MB }, t)).toBe('The recording reached the maximum size of 200 MB. It was stopped and saved.');
        expect(stoppedMessage('storage', {}, t)).toBe(EN.offlineRecorder.stoppedStorage);
        expect(stoppedMessage(null, {}, t)).toBe('');
    });
});

describe('the source of a recording (#697)', () => {
    const t = translator(EN, EN);
    const nameError = (name, message = name) => Object.assign(new Error(message), { name });

    it('says why a capture failed with the texts of the in-app recorder', () => {
        expect(captureMessage(new CaptureError('shareCancelled'), t)).toBe(EN.toasts.screenShareCancelled);
        expect(captureMessage(new CaptureError('captureFailed', nameError('NotReadableError', 'Could not start video source')), t))
            .toBe(EN.toasts.captureFailed.replace('{{error}}', 'Could not start video source'));
        expect(captureMessage(new CaptureError('microphoneDenied'), t)).toBe(EN.offlineRecorder.microphoneDenied);
        expect(captureMessage(new CaptureError('noMicrophone'), t)).toBe(EN.offlineRecorder.noMicrophone);
        expect(captureMessage(new CaptureError('unavailable'), t)).toBe(EN.recording.systemAudioNotSupported);
        expect(captureMessage(new Error('anything'), t)).toBe(EN.offlineRecorder.noMicrophone);
        expect(captureMessage(null, t)).toBe(EN.offlineRecorder.noMicrophone);
    });

    it('names no help guide for a share without audio, which this page does not have', () => {
        const text = captureMessage(new CaptureError('noAudioTrack'), t);
        expect(text).toBe(EN.offlineRecorder.noAudioTrack);
        expect(EN.errors.noAudioTrackInScreenShare.startsWith(text)).toBe(true);
        expect(text).not.toMatch(/help/i);
        for (const code of ['de', 'es', 'fr', 'pt-BR', 'ru', 'zh']) {
            const table = locale(code);
            const own = (s) => s.replace(/[’']/g, "'");
            expect(own(table.errors.noAudioTrackInScreenShare).startsWith(own(table.offlineRecorder.noAudioTrack)), code).toBe(true);
        }
    });

    it('remembers the choice under the key of the in-app recorder', () => {
        const app = readFileSync(fileURLToPath(new URL('../../app.modular.js', import.meta.url)), 'utf8');
        expect(app).toContain("_stored('" + SOURCE_KEY + "', ['microphone', 'system', 'both'], 'microphone')");
        expect(app).toContain("localStorage.setItem('" + SOURCE_KEY + "', v)");
    });
});
