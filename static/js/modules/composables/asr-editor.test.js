import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAsrEditor } from './asr-editor.js';
import { createDraftStore, hashText } from '../asr-editor/draft.js';

const ref = (value) => ({ value });

const SEGMENTS = [
    { speaker: 'SPEAKER_00', sentence: 'Good morning', start_time: 0, end_time: 2 },
    { speaker: 'SPEAKER_01', sentence: 'Morning', start_time: 2, end_time: 3 },
    { speaker: 'SPEAKER_00', sentence: 'Shall we start', start_time: 3, end_time: 5 },
];

function memoryStorage() {
    const data = new Map();
    return {
        data,
        getItem: (k) => (data.has(k) ? data.get(k) : null),
        setItem: (k, v) => data.set(k, String(v)),
        removeItem: (k) => data.delete(k),
    };
}

function setup({ segments = SEGMENTS, saveOk = true, confirm = true, autosave = false, translation = false, serverSegments = null } = {}) {
    const classes = new Set();
    const requests = [];
    const asked = [];
    globalThis.window = { confirm: (message) => { asked.push(message); return confirm; }, addEventListener() {} };
    globalThis.document = {
        querySelector: () => null,
        documentElement: { classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), remove: (name) => classes.delete(name) } },
    };
    globalThis.fetch = async (url, options = {}) => {
        requests.push({ url, body: options.body ? JSON.parse(options.body) : null });
        if (url.endsWith('/update_transcript')) {
            const stored = serverSegments || JSON.parse(options.body).transcript_data;
            return { ok: true, json: async () => ({ recording: { transcription: JSON.stringify(stored), participants: 'Dana Lee' } }) };
        }
        return { ok: true, json: async () => (url === '/speakers' ? [] : { success: true, suggestions: {} }) };
    };
    const text = JSON.stringify(segments);
    const recording = { id: 7, transcription: text, transcription_translation: translation ? text : null };
    const saved = [];
    const errors = [];
    const state = {
        showAsrEditorModal: ref(false),
        selectedRecording: ref(recording),
        viewingTranslation: ref(translation),
        recordings: ref([recording]),
        asrEditorSession: ref(null),
        asrEditorDirty: ref(false),
        asrEditorRev: ref(0),
        asrEditorSaveFlash: ref(false),
        asrEditorSaving: ref(false),
        editorAutosave: ref(autosave),
        speakerColorMap: ref({ SPEAKER_00: 'speaker-color-1', SPEAKER_01: 'speaker-color-2' }),
    };
    const utils = { showToast() {}, setGlobalError: (m) => errors.push(m) };
    const api = useAsrEditor(state, utils, {
        saveTranscriptionContent: async (content) => { saved.push(content); if (saveOk) recording.transcription = content; return saveOk; },
    });
    const drafts = createDraftStore(globalThis.localStorage);
    return { state, api, saved, errors, requests, asked, classes, recording, drafts, model: () => state.asrEditorSession.value.model };
}

describe('the segment editor session', () => {
    let globals;
    beforeEach(() => {
        globals = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch, localStorage: globalThis.localStorage };
        globalThis.localStorage = memoryStorage();
        vi.useFakeTimers();
    });
    afterEach(() => {
        vi.useRealTimers();
        Object.assign(globalThis, globals);
    });

    it('opens on the transcript and closes without asking when nothing changed', async () => {
        const { state, api, asked, model } = setup();
        await api.openAsrEditorModal();
        expect(state.showAsrEditorModal.value).toBe(true);
        expect(model().length).toBe(3);
        expect(state.asrEditorSession.value.colorMap).toEqual({ SPEAKER_00: 'speaker-color-1', SPEAKER_01: 'speaker-color-2' });
        expect(state.asrEditorDirty.value).toBe(false);
        expect(api.closeAsrEditorModal()).toBe(true);
        expect(asked).toEqual([]);
        expect(state.showAsrEditorModal.value).toBe(false);
        expect(state.asrEditorSession.value).toBeNull();
    });

    it('does not open on text that is not a list of segments', async () => {
        const { state, api, errors } = setup({ segments: { text: 'hello' } });
        await api.openAsrEditorModal();
        expect(state.showAsrEditorModal.value).toBe(false);
        expect(errors.length).toBe(1);
    });

    it('tracks unsaved changes and asks before discarding them', async () => {
        const { state, api, asked, model } = setup({ confirm: false });
        await api.openAsrEditorModal();
        model().setText(0, 'Good morning all');
        expect(state.asrEditorDirty.value).toBe(true);
        expect(state.asrEditorRev.value).toBe(1);
        expect(api.closeAsrEditorModal()).toBe(false);
        expect(asked).toEqual(['asrEditor.discardConfirm']);
        expect(state.showAsrEditorModal.value).toBe(true);
        model().undo();
        expect(state.asrEditorDirty.value).toBe(false);
        expect(api.closeAsrEditorModal()).toBe(true);
    });

    it('saves the whole transcript and stays open or closes', async () => {
        const { state, api, saved, model } = setup();
        await api.openAsrEditorModal();
        model().split(0, 4);
        expect(await api.saveAsrTranscription(true)).toBe(true);
        expect(JSON.parse(saved[0]).map((s) => s.sentence)).toEqual(['Good', 'morning', 'Morning', 'Shall we start']);
        expect(state.asrEditorDirty.value).toBe(false);
        expect(state.asrEditorSaveFlash.value).toBe(true);
        expect(state.showAsrEditorModal.value).toBe(true);
        expect(model().canUndo).toBe(true);
        model().setText(0, 'Hello');
        expect(await api.saveAsrTranscription(false)).toBe(true);
        expect(saved.length).toBe(2);
        expect(state.showAsrEditorModal.value).toBe(false);
    });

    it('stays open and unsaved when the save fails', async () => {
        const { state, api, model } = setup({ saveOk: false });
        await api.openAsrEditorModal();
        model().setText(0, 'Hello');
        expect(await api.saveAsrTranscription(false)).toBe(false);
        expect(state.showAsrEditorModal.value).toBe(true);
        expect(state.asrEditorDirty.value).toBe(true);
        expect(state.asrEditorSaving.value).toBe(false);
    });

    it('sends accepted voice matches through update_transcript, under their labels', async () => {
        const stored = SEGMENTS.map((s) => (s.speaker === 'SPEAKER_00' ? { ...s, speaker: 'Dana Lee', speaker_id: 4 } : s));
        const { state, api, saved, requests, recording, model } = setup({ serverSegments: stored });
        await api.openAsrEditorModal();
        model().renameSpeaker('SPEAKER_00', 'dana lee', { match: { label: 'SPEAKER_00', name: 'dana lee' } });
        expect(await api.saveAsrTranscription(true)).toBe(true);
        const request = requests.find((r) => r.url === '/recording/7/update_transcript');
        expect(request.body.speaker_map).toEqual({ SPEAKER_00: { name: 'dana lee', isMe: false } });
        expect(request.body.transcript_data.map((s) => s.speaker)).toEqual(['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_00']);
        expect(saved).toEqual([]);
        // The stored spelling and link come back into the editor and the recording
        expect(model().toJSON()[0]).toMatchObject({ speaker: 'Dana Lee', speaker_id: 4 });
        expect(JSON.parse(recording.transcription)[0].speaker).toBe('Dana Lee');
        expect(recording.participants).toBe('Dana Lee');
        expect(state.asrEditorDirty.value).toBe(false);
        expect(model().acceptedMatches()).toEqual([]);
        // The next save has no match to record: the plain route
        model().setText(1, 'Morning all');
        await api.saveAsrTranscription(true);
        expect(saved.length).toBe(1);
    });

    it('does not record voice matches when a translation is edited', async () => {
        const { api, saved, requests, model } = setup({ translation: true });
        await api.openAsrEditorModal();
        model().renameSpeaker('SPEAKER_00', 'Dana Lee', { match: { label: 'SPEAKER_00', name: 'Dana Lee' } });
        await api.saveAsrTranscription(true);
        expect(saved.length).toBe(1);
        expect(requests.some((r) => r.url.endsWith('/update_transcript'))).toBe(false);
        expect(requests.some((r) => r.url.includes('/speakers/suggestions/'))).toBe(false);
    });

    it('keeps a local draft of the edits, offers it on the same transcript and drops it on save', async () => {
        const { state, api, drafts, recording, model } = setup();
        const base = hashText(recording.transcription);
        await api.openAsrEditorModal();
        model().setText(1, 'Morning everyone');
        expect(drafts.load(7, 'transcription', base)).toBeNull();
        vi.advanceTimersByTime(1000);
        expect(drafts.load(7, 'transcription', base).log.length).toBe(1);

        // The page is reloaded: another session on the same saved transcript
        state.asrEditorSession.value = null;
        await api.openAsrEditorModal();
        const session = state.asrEditorSession.value;
        expect(session.draft.value.log.length).toBe(1);
        expect(model().get(1).sentence).toBe('Morning');
        session.restoreDraft();
        expect(session.draft.value).toBeNull();
        expect(model().get(1).sentence).toBe('Morning everyone');
        expect(state.asrEditorDirty.value).toBe(true);

        await api.saveAsrTranscription(true);
        vi.advanceTimersByTime(2000);
        expect(globalThis.localStorage.data.size).toBe(0);
        // Edits after the save are a draft on the newly saved transcript
        model().setText(0, 'Hello');
        vi.advanceTimersByTime(1000);
        expect(hashText(recording.transcription)).not.toBe(base);
        expect(drafts.load(7, 'transcription', hashText(recording.transcription)).log.length).toBe(1);
    });

    it('removes the draft when the edits are discarded', async () => {
        const { api, model } = setup({ confirm: true });
        await api.openAsrEditorModal();
        model().setText(1, 'Morning everyone');
        vi.advanceTimersByTime(1000);
        expect(globalThis.localStorage.data.size).toBe(1);
        expect(api.closeAsrEditorModal()).toBe(true);
        expect(globalThis.localStorage.data.size).toBe(0);
    });

    it('drops the offer of an older draft at the first new edit', async () => {
        const { state, api, drafts, recording, model } = setup();
        drafts.save(7, 'transcription', hashText(recording.transcription), [{ label: 'text', steps: [{ k: 'set', i: 0, f: 'sentence', a: 'Good morning', b: 'Draft' }] }]);
        await api.openAsrEditorModal();
        expect(state.asrEditorSession.value.draft.value).not.toBeNull();
        model().setText(2, 'Shall we begin');
        expect(state.asrEditorSession.value.draft.value).toBeNull();
    });

    it('saves by itself two seconds after an edit when autosave is on', async () => {
        const { api, saved, model } = setup({ autosave: true });
        await api.openAsrEditorModal();
        model().setText(0, 'Hello');
        vi.advanceTimersByTime(1500);
        model().setText(0, 'Hello there');
        vi.advanceTimersByTime(1500);
        expect(saved.length).toBe(0);
        await vi.advanceTimersByTimeAsync(600);
        expect(saved.length).toBe(1);
        expect(JSON.parse(saved[0])[0].sentence).toBe('Hello there');
    });

    it('marks the page only for a long transcript', async () => {
        const long = Array.from({ length: 600 }, (_, i) => ({ speaker: 'A', sentence: `s${i}`, start_time: i, end_time: i + 1 }));
        const a = setup({ segments: long });
        await a.api.openAsrEditorModal();
        expect(a.classes.has('asr-editor-long')).toBe(true);
        a.api.closeAsrEditorModal();
        expect(a.classes.has('asr-editor-long')).toBe(false);
        const b = setup();
        await b.api.openAsrEditorModal();
        expect(b.classes.has('asr-editor-long')).toBe(false);
    });

    it('opens at a segment when asked', async () => {
        const { state, api } = setup();
        await api.openAsrEditorAtSegment(2);
        expect(state.asrEditorSession.value.initialIndex).toBe(2);
        api.closeAsrEditorModal();
        await api.openAsrEditorModal();
        expect(state.asrEditorSession.value.initialIndex).toBeNull();
    });
});
