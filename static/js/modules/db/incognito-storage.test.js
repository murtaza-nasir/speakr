/**
 * Vitest tests for the incognito-mode storage helpers.
 *
 * These functions wrap sessionStorage so an incognito recording lives only for
 * the lifetime of the tab. sessionStorage does not exist in the node test
 * environment, so we install a small in-memory stub (the same in-memory
 * philosophy the other db tests use for fetch/localStorage/IndexedDB).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
    saveIncognitoRecording,
    getIncognitoRecording,
    clearIncognitoRecording,
    hasIncognitoRecording,
    updateIncognitoRecording,
} from './incognito-storage.js';

const INCOGNITO_KEY = 'speakr_incognito_recording';

function installSessionStorage() {
    const store = new Map();
    const api = {
        getItem: vi.fn((k) => (store.has(k) ? store.get(k) : null)),
        setItem: vi.fn((k, v) => { store.set(k, String(v)); }),
        removeItem: vi.fn((k) => { store.delete(k); }),
        clear: vi.fn(() => store.clear()),
        _store: store,
    };
    globalThis.sessionStorage = api;
    return api;
}

describe('incognito-storage', () => {
    let ss;

    beforeEach(() => {
        ss = installSessionStorage();
        // Silence the module's console noise but keep spies inspectable.
        vi.spyOn(console, 'log').mockImplementation(() => {});
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
        delete globalThis.sessionStorage;
    });

    describe('saveIncognitoRecording / getIncognitoRecording', () => {
        it('round-trips an object through sessionStorage', () => {
            const data = { title: 'Note', transcription: 'hello', summary: 's', tags: ['a'] };
            saveIncognitoRecording(data);

            expect(ss.setItem).toHaveBeenCalledWith(INCOGNITO_KEY, JSON.stringify(data));
            expect(getIncognitoRecording()).toEqual(data);
        });

        it('serializes to JSON (string) under the hood', () => {
            saveIncognitoRecording({ n: 1 });
            expect(ss._store.get(INCOGNITO_KEY)).toBe('{"n":1}');
        });

        it('getIncognitoRecording returns null when nothing is stored', () => {
            expect(getIncognitoRecording()).toBeNull();
        });

        it('getIncognitoRecording returns null and logs on corrupt JSON', () => {
            ss._store.set(INCOGNITO_KEY, '{not valid json');
            expect(getIncognitoRecording()).toBeNull();
            expect(console.error).toHaveBeenCalled();
        });

        it('saveIncognitoRecording swallows errors when setItem throws (quota)', () => {
            ss.setItem.mockImplementation(() => { throw new Error('QuotaExceededError'); });
            expect(() => saveIncognitoRecording({ big: 'x' })).not.toThrow();
            expect(console.error).toHaveBeenCalled();
        });
    });

    describe('hasIncognitoRecording', () => {
        it('is false when empty and true after a save', () => {
            expect(hasIncognitoRecording()).toBe(false);
            saveIncognitoRecording({ a: 1 });
            expect(hasIncognitoRecording()).toBe(true);
        });

        it('returns false when sessionStorage throws', () => {
            ss.getItem.mockImplementation(() => { throw new Error('blocked'); });
            expect(hasIncognitoRecording()).toBe(false);
        });
    });

    describe('clearIncognitoRecording', () => {
        it('removes the stored recording', () => {
            saveIncognitoRecording({ a: 1 });
            expect(hasIncognitoRecording()).toBe(true);
            clearIncognitoRecording();
            expect(ss.removeItem).toHaveBeenCalledWith(INCOGNITO_KEY);
            expect(hasIncognitoRecording()).toBe(false);
        });

        it('swallows errors when removeItem throws', () => {
            ss.removeItem.mockImplementation(() => { throw new Error('boom'); });
            expect(() => clearIncognitoRecording()).not.toThrow();
            expect(console.error).toHaveBeenCalled();
        });
    });

    describe('updateIncognitoRecording', () => {
        it('merges updates into the existing record and returns it', () => {
            saveIncognitoRecording({ title: 'Old', transcription: 't', keep: true });
            const updated = updateIncognitoRecording({ title: 'New', extra: 1 });

            expect(updated).toEqual({ title: 'New', transcription: 't', keep: true, extra: 1 });
            // Persisted as well.
            expect(getIncognitoRecording()).toEqual(updated);
        });

        it('returns null when there is nothing to update', () => {
            expect(updateIncognitoRecording({ title: 'New' })).toBeNull();
            expect(hasIncognitoRecording()).toBe(false);
        });

        it('returns null and logs when retrieval throws', () => {
            saveIncognitoRecording({ a: 1 });
            // Make the read inside update blow up after the record exists.
            ss.getItem.mockImplementation(() => { throw new Error('explode'); });
            expect(updateIncognitoRecording({ b: 2 })).toBeNull();
        });
    });
});
