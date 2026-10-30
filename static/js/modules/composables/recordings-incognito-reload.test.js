/**
 * An incognito recording lasts until its tab closes (#528).
 *
 * A reload used to delete it: loadRecordings re-selects the last saved
 * recording before the incognito entry is restored, and selectRecording
 * cleared the stored copy without asking. Only an accepted switch prompt
 * discards it now.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useRecordings } from './recordings.js';
import * as IncognitoStorage from '../db/incognito-storage.js';

const ref = (value) => ({ value });

const incognito = () => ({ id: 'incognito', incognito: true, transcription: 'keep me' });

function setup(overrides = {}) {
    const base = {
        recordings: ref([]), selectedRecording: ref(null), currentView: ref('recording'),
        isRecording: ref(false), audioBlobURL: ref(null), speakerColorMap: ref({}),
        incognitoRecording: ref(null), availableSpeakers: ref([]), uploadQueue: ref([]),
        ...overrides
    };
    // Any other state the composable reads is a plain ref holding null.
    const state = new Proxy(base, { get: (t, k) => (k in t ? t[k] : (t[k] = ref(null))) });
    const utils = { setGlobalError: vi.fn(), showToast: vi.fn() };
    return { state, composable: useRecordings(state, utils, {}) };
}

function memoryStorage() {
    const data = new Map();
    return {
        getItem: (k) => (data.has(k) ? data.get(k) : null),
        setItem: (k, v) => data.set(k, String(v)),
        removeItem: (k) => data.delete(k)
    };
}

beforeEach(() => {
    globalThis.window = {};
    globalThis.sessionStorage = memoryStorage();
    globalThis.localStorage = memoryStorage();
    globalThis.confirm = vi.fn();
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 7, title: 'Saved' }) });
});

afterEach(() => {
    vi.restoreAllMocks();
    for (const name of ['window', 'sessionStorage', 'localStorage', 'confirm', 'fetch']) delete globalThis[name];
});

describe('incognito recording across a reload', () => {
    it('keeps the stored transcript when the reload re-selects a saved recording', async () => {
        IncognitoStorage.saveIncognitoRecording(incognito());
        const { state, composable } = setup();

        await composable.selectRecording({ id: 7, title: 'Saved' });

        expect(IncognitoStorage.hasIncognitoRecording()).toBe(true);
        expect(globalThis.confirm).not.toHaveBeenCalled();
        expect(state.selectedRecording.value.id).toBe(7);
    });

    it('keeps the transcript when the switch prompt is declined', async () => {
        IncognitoStorage.saveIncognitoRecording(incognito());
        const { state, composable } = setup({ incognitoRecording: ref(incognito()), selectedRecording: ref(incognito()) });
        globalThis.confirm.mockReturnValue(false);

        await composable.selectRecording({ id: 7 });

        expect(state.selectedRecording.value.id).toBe('incognito');
        expect(IncognitoStorage.hasIncognitoRecording()).toBe(true);
    });

    it('discards the transcript when the switch prompt is accepted', async () => {
        IncognitoStorage.saveIncognitoRecording(incognito());
        const { state, composable } = setup({ incognitoRecording: ref(incognito()), selectedRecording: ref(incognito()) });
        globalThis.confirm.mockReturnValue(true);

        await composable.selectRecording({ id: 7 });

        expect(IncognitoStorage.hasIncognitoRecording()).toBe(false);
        expect(state.incognitoRecording.value).toBe(null);
    });
});
