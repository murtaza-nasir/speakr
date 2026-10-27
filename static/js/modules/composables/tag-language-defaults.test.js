/**
 * applyTagDefaults applies a tag's default language for every connector;
 * speaker counts stay diarization-only. useUpload only touches `.value`, so
 * plain `{ value }` objects stand in for refs and Vue is stubbed minimally.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useUpload } from './upload.js';

const ref = (value) => ({ value });

const csTag = {
    id: 1, name: 'cs', default_language: 'cs', default_min_speakers: 2, default_max_speakers: 4
};

function makeState({ diarization }) {
    const explicit = {
        selectedTagIds: ref([1]),
        availableTags: ref([csTag]),
        uploadLanguage: ref('en'),
        uploadMinSpeakers: ref(null),
        uploadMaxSpeakers: ref(null),
        uploadHotwords: ref(''),
        uploadInitialPrompt: ref(''),
        connectorSupportsDiarization: ref(diarization),
    };
    // Every other state ref is irrelevant to applyTagDefaults.
    return new Proxy(explicit, { get: (target, key) => target[key] ?? ref(null) });
}

beforeEach(() => {
    globalThis.Vue = {
        ref, markRaw: (v) => v, nextTick: vi.fn(),
        computed: (fn) => ({ get value() { return fn(); } }),
    };
});

afterEach(() => {
    delete globalThis.Vue;
});

describe('applyTagDefaults', () => {
    it('applies the tag language when the connector does not diarize', () => {
        const state = makeState({ diarization: false });
        useUpload(state, {}).applyTagDefaults();
        expect(state.uploadLanguage.value).toBe('cs');
        expect(state.uploadMinSpeakers.value).toBeNull();
        expect(state.uploadMaxSpeakers.value).toBeNull();
    });

    it('applies language and speaker counts when the connector diarizes', () => {
        const state = makeState({ diarization: true });
        useUpload(state, {}).applyTagDefaults();
        expect(state.uploadLanguage.value).toBe('cs');
        expect(state.uploadMinSpeakers.value).toBe(2);
        expect(state.uploadMaxSpeakers.value).toBe(4);
    });
});
