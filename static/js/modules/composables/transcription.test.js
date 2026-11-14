import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { useTranscription } from './transcription.js';

const ref = (value) => ({ value });

function setup({ transcription, saveOk = true } = {}) {
    const opened = [];
    const errors = [];
    const requests = [];
    globalThis.window = {};
    globalThis.document = { querySelector: () => null };
    globalThis.fetch = async (url, options) => {
        requests.push({ url, body: JSON.parse(options.body) });
        return { ok: saveOk, json: async () => (saveOk ? { success: true } : { error: 'No access' }) };
    };
    const recording = { id: 5, transcription };
    const state = {
        showTextEditorModal: ref(false),
        selectedRecording: ref(recording),
        viewingTranslation: ref(false),
        editingTranscriptionContent: ref(''),
        recordings: ref([recording]),
    };
    const utils = { showToast() {}, setGlobalError: (m) => errors.push(m), openAsrEditorModal: () => opened.push('segments') };
    return { state, api: useTranscription(state, utils), opened, errors, requests };
}

describe('opening the transcript editor', () => {
    let saved;
    beforeEach(() => { saved = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch }; });
    afterEach(() => { Object.assign(globalThis, saved); });

    it('opens the segment editor for a transcript with segments', () => {
        const { state, api, opened } = setup({ transcription: JSON.stringify([{ speaker: 'A', sentence: 'hi' }]) });
        api.openTranscriptionEditor();
        expect(opened).toEqual(['segments']);
        expect(state.showTextEditorModal.value).toBe(false);
    });

    it('opens the text editor for plain text and for other JSON', () => {
        for (const transcription of ['Speaker 1: hello', '{"text": "hello"}']) {
            const { state, api, opened } = setup({ transcription });
            api.openTranscriptionEditor();
            expect(opened).toEqual([]);
            expect(state.showTextEditorModal.value).toBe(true);
            expect(state.editingTranscriptionContent.value).toBe(transcription);
        }
    });

    it('saves the text and closes', async () => {
        const { state, api, requests } = setup({ transcription: 'old' });
        api.openTranscriptionEditor();
        state.editingTranscriptionContent.value = 'new';
        await api.saveTranscription();
        expect(requests).toEqual([{ url: '/recording/5/update_transcription', body: { transcription: 'new' } }]);
        expect(state.selectedRecording.value.transcription).toBe('new');
        expect(state.showTextEditorModal.value).toBe(false);
    });

    it('stays open with the text when the save fails', async () => {
        const { state, api, errors } = setup({ transcription: 'old', saveOk: false });
        api.openTranscriptionEditor();
        state.editingTranscriptionContent.value = 'new';
        await api.saveTranscription();
        expect(errors.length).toBe(1);
        expect(state.showTextEditorModal.value).toBe(true);
        expect(state.editingTranscriptionContent.value).toBe('new');
        expect(await api.saveTranscriptionContent('x')).toBe(false);
    });
});
