import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
    DEFAULT_STRINGS, interpolate, translator, formatDuration, formatSize, recorderAvailability, pendingRows,
    pickMimeType, recordingFileName, offlineClientId, whenServerIsBack, returnTarget, serverAnswered,
} from './offline-recorder-model.js';
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
    const context = (fields) => parseOfflineContext(JSON.stringify({ userId: '3', locale: 'en', ...fields }));

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
