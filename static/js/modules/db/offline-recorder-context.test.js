import { describe, it, expect, vi } from 'vitest';
import { rememberForOfflineRecorder } from './offline-recorder-context.js';

const fakeStorage = () => {
    const data = {};
    return {
        data,
        setItem: vi.fn((k, v) => { data[k] = v; }),
        removeItem: vi.fn((k) => { delete data[k]; }),
    };
};

describe('rememberForOfflineRecorder', () => {
    it('writes the account, the language and the incognito default, and asks the worker to keep the recorder', () => {
        const storage = fakeStorage();
        const postMessage = vi.fn();
        const context = rememberForOfflineRecorder({ userId: 7, locale: 'fr-CA', incognitoDefault: false },
            { storage, serviceWorker: { controller: { postMessage } } });
        expect(context).toMatchObject({ userId: '7', locale: 'fr', incognitoDefault: false });
        expect(JSON.parse(storage.data['speakr.offlineRecorder'])).toMatchObject({ userId: '7', locale: 'fr', incognitoDefault: false });
        expect(Object.keys(JSON.parse(storage.data['speakr.offlineRecorder'])).sort())
            .toEqual(['expiresAt', 'incognitoDefault', 'limits', 'locale', 'savedAt', 'userId']);
        expect(postMessage).toHaveBeenCalledWith({ type: 'PREPARE_OFFLINE_RECORDER', locale: 'fr' });
    });

    it('writes the expiry and the limits the app passes', () => {
        const storage = fakeStorage();
        rememberForOfflineRecorder({ userId: 7, days: 10, maxSeconds: 3600, maxRecorderBytes: 5, maxUploadBytes: 9 }, { storage });
        const entry = JSON.parse(storage.data['speakr.offlineRecorder']);
        expect(entry.expiresAt - entry.savedAt).toBe(10 * 24 * 60 * 60 * 1000);
        expect(entry.limits).toEqual({ maxSeconds: 3600, maxRecorderBytes: 5, maxUploadBytes: 9 });
    });

    it('removes the entry and asks nothing without an account', () => {
        const storage = fakeStorage();
        storage.data['speakr.offlineRecorder'] = '{"userId":"1"}';
        const postMessage = vi.fn();
        expect(rememberForOfflineRecorder({ userId: '' }, { storage, serviceWorker: { controller: { postMessage } } })).toBeNull();
        expect(storage.data).toEqual({});
        expect(postMessage).not.toHaveBeenCalled();
    });

    it('works without a worker in control and without storage', () => {
        expect(rememberForOfflineRecorder({ userId: 1 }, { storage: fakeStorage(), serviceWorker: { controller: null } })).toMatchObject({ userId: '1' });
        const refusing = { setItem: () => { throw new Error('SecurityError'); }, removeItem: () => {} };
        expect(() => rememberForOfflineRecorder({ userId: 1 }, { storage: refusing })).not.toThrow();
    });
});
