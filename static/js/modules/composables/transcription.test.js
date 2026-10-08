import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useTranscription } from './transcription.js';

const ref = value => ({ value });
const source = () => ({ speaker: 'Анна', sentence: 'Привет. Добрый день.', start_time: 10, end_time: 18 });
const event = (text, p, extra = {}) => ({
    target: { value: text, selectionStart: p, selectionEnd: p }, ...extra
});

let state, utils, editor, focus, select, watchers, keydown;
beforeEach(() => {
    watchers = [];
    vi.stubGlobal('Vue', { ref, computed: getter => ({get value(){return getter();}}), watch: (_ref, callback) => watchers.push(callback) });
    keydown = null;
    vi.stubGlobal('window', {
        addEventListener: (_name, handler) => { keydown = handler; },
        i18n: { t: key => key }
    });
    vi.stubGlobal('document', {
        querySelector: vi.fn(() => null), querySelectorAll: vi.fn(() => [])
    });
    vi.stubGlobal('requestAnimationFrame', callback => callback());
    focus = vi.fn(); select = vi.fn();
    state = Object.fromEntries([
        'showTextEditorModal', 'showAsrEditorModal', 'selectedRecording',
        'editingTranscriptionContent', 'editingSegments', 'availableSpeakers',
        'recordings', 'dropdownPositions', 'openAsrDropdownIndex', 'asrEditorRef',
        'asrEditorSaveFlash', 'asrEditorHighlightIndex', 'editorAutosave'
    ].map(key => [key, ref(null)]));
    state.showAsrEditorModal.value = true;
    state.editingSegments.value = [source(), { ...source(), start_time: 20, end_time: 28 }];
    state.availableSpeakers.value = ['Анна', 'Борис'];
    state.dropdownPositions.value = {};
    state.selectedRecording.value = { id: 9, transcription: JSON.stringify(state.editingSegments.value) };
    state.recordings.value = [state.selectedRecording.value];
    state.asrEditorRef.value = { querySelector: vi.fn(() => ({ focus, select })), scrollTop: 0 };
    state.editorAutosave.value = false;
    utils = { showToast: vi.fn(), setGlobalError: vi.fn(), nextTick: vi.fn(async () => {}), scrollAsrEditorToIndex: vi.fn() };
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ success: true }) })));
    editor = useTranscription(state, utils);
    editor.start(0);
    document.querySelector.mockImplementation(selector => selector.includes('asr-segment-speaker') ? {focus,select} : null);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('ASR split interaction', () => {
    it('offers existing and new draft names without changing saved transcription', () => {
        const before = state.selectedRecording.value.transcription;
        editor.editAsrSpeaker(1, 'Вера');
        expect(editor.asrSpeakerOptions.value).toEqual(['Анна', 'Борис', 'Вера']);
        state.editingSegments.value[0].speaker = '  ';
        expect(editor.asrSpeakerOptions.value).toEqual(['Анна', 'Борис', 'Вера']);
        expect(state.selectedRecording.value.transcription).toBe(before);
    });

    it('offers a new speaker in both split fields before confirmation', async () => {
        editor.captureSplitSelection(0, event(source().sentence, 8));
        await editor.splitSegmentAtCursor(0);
        editor.splitPreview.value.parts[1].speaker = 'Вера';
        expect(editor.asrSpeakerOptions.value).toEqual(['Анна', 'Борис', 'Вера']);
        expect(state.editingSegments.value).toHaveLength(2);
    });

    it('captures caret, inserts adjacent parts, closes dropdown and focuses the second speaker', async () => {
        const neighbour = state.editingSegments.value[1];
        state.openAsrDropdownIndex.value = 1;
        editor.captureSplitSelection(0, event(source().sentence, 8));
        expect(editor.canSplitSegment(0)).toBe(true);
        expect(editor.canSplitSegment(1)).toBe(false);
        expect(await editor.splitSegmentAtCursor(0)).toBe(true);
        expect(state.editingSegments.value).toHaveLength(2);
        expect(await editor.confirmAsrSplit()).toBe(true);
        expect(state.editingSegments.value.map(s => s.sentence)).toEqual(['Привет.', 'Добрый день.', neighbour.sentence]);
        expect(state.editingSegments.value[2]).toBe(neighbour);
        expect(state.editingSegments.value.map(s => s.id)).toEqual([0, 1, 2]);
        expect(state.openAsrDropdownIndex.value).toBeNull();
        expect(utils.scrollAsrEditorToIndex).toHaveBeenCalledWith(1);
        expect(focus).toHaveBeenCalledOnce();
        expect(select).toHaveBeenCalledOnce();
        expect(editor.canSplitSegment(0)).toBe(false);
        expect(fetch).not.toHaveBeenCalled();
    });

    it('does not use a stale caret when the sentence changed', async () => {
        editor.captureSplitSelection(0, event(source().sentence, 8));
        state.editingSegments.value[0].sentence = 'Другой текст';
        expect(await editor.splitSegmentAtCursor(0)).toBe(false);
        expect(state.editingSegments.value).toHaveLength(2);
    });

    it.each(['addSegment', 'addSegmentBelow', 'removeSegment'])('invalidates selection after %s', method => {
        editor.captureSplitSelection(0, event(source().sentence, 8));
        editor[method](1);
        expect(editor.canSplitSegment(0)).toBe(false);
    });

    it('uses segment identity instead of reusing the original numeric index', () => {
        editor.captureSplitSelection(0, event(source().sentence, 8));
        state.editingSegments.value.unshift(source());
        expect(editor.canSplitSegment(0)).toBe(false);
    });

    it('revalidates the timestamps when the action is invoked', async () => {
        editor.captureSplitSelection(0, event(source().sentence, 8));
        state.editingSegments.value[0].end_time = null;
        expect(await editor.splitSegmentAtCursor(0)).toBe(false);
        expect(state.editingSegments.value).toHaveLength(2);
        expect(utils.showToast).toHaveBeenCalledWith('asrEditor.splitInvalidTime', 'fa-info-circle');
    });

    it.each(['ctrlKey', 'metaKey'])('splits on Enter with %s', async key => {
        const e = event(source().sentence, 8, { key: 'Enter', [key]: true, preventDefault: vi.fn() });
        await editor.handleSplitKeydown(0, e);
        expect(state.editingSegments.value).toHaveLength(2);
        await editor.confirmAsrSplit();
        expect(e.preventDefault).toHaveBeenCalledOnce();
        expect(state.editingSegments.value).toHaveLength(3);
    });

    it.each([{ key: 'Enter' }, { key: 'Enter', ctrlKey: true, isComposing: true }, { key: 's', ctrlKey: true }])
        ('leaves ordinary Enter, IME and Save to their normal handlers: %j', extra => {
            const e = event(source().sentence, 8, { ...extra, preventDefault: vi.fn() });
            editor.handleSplitKeydown(0, e);
            expect(e.preventDefault).not.toHaveBeenCalled();
            expect(state.editingSegments.value).toHaveLength(2);
        });

    it('rejects range selection without dropping selected text', async () => {
        const e = event(source().sentence, 8);
        e.target.selectionEnd = 12;
        editor.captureSplitSelection(0, e);
        expect(await editor.splitSegmentAtCursor(0)).toBe(false);
        expect(state.editingSegments.value[0].sentence).toBe(source().sentence);
    });

    it('does not move focus to another session after a deferred render', async () => {
        utils.nextTick.mockImplementationOnce(async () => { state.showAsrEditorModal.value = false; });
        editor.captureSplitSelection(0, event(source().sentence, 8));
        await editor.splitSegmentAtCursor(0);
        await editor.confirmAsrSplit();
        expect(focus).not.toHaveBeenCalled();
    });

    it('saves both parts and the changed speaker without transient fields, including Ctrl+S', async () => {
        editor.captureSplitSelection(0, event(source().sentence, 8));
        await editor.splitSegmentAtCursor(0);
        await editor.confirmAsrSplit();
        state.editingSegments.value[1].speaker = 'Борис';
        await editor.saveAsrTranscription(true);
        const saved = JSON.parse(JSON.parse(fetch.mock.calls[0][1].body).transcription);
        expect(saved[0]).toEqual({ speaker: 'Анна', sentence: 'Привет.', start_time: 10, end_time: 12.95 });
        expect(saved[1]).toEqual({ speaker: 'Борис', sentence: 'Добрый день.', start_time: 12.95, end_time: 18 });
        expect(JSON.parse(state.selectedRecording.value.transcription)).toEqual(saved);
        const e = { key: 's', ctrlKey: true, preventDefault: vi.fn() };
        keydown(e);
        await Promise.resolve(); await Promise.resolve();
        expect(e.preventDefault).toHaveBeenCalledOnce();
        expect(fetch).toHaveBeenCalledTimes(1); // A clean Ctrl+S does not create another write/webhook.
    });

    it('autosaves an immediate split during the initial hydration grace period', async () => {
        vi.useFakeTimers();
        state.editorAutosave.value = true;
        await editor.openAsrEditorModal();
        editor.captureSplitSelection(0, event(source().sentence, 8));
        await editor.splitSegmentAtCursor(0);
        await editor.confirmAsrSplit();
        watchers.forEach(callback => callback());
        await vi.advanceTimersByTimeAsync(1999);
        expect(fetch).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(1);
        expect(fetch).toHaveBeenCalledOnce();
        expect(JSON.parse(JSON.parse(fetch.mock.calls[0][1].body).transcription)).toHaveLength(3);
    });

    it('does not mutate a reopened session through a shared recording-list object after a late save', async () => {
        let finish;
        fetch.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
        state.editingSegments.value[0].sentence = 'Old pending save';
        const pending = editor.saveAsrTranscription(true);
        const fresh = JSON.stringify([{...source(), sentence:'New session source'}]);
        editor.stop();
        state.selectedRecording.value.transcription = fresh;
        state.editingSegments.value = JSON.parse(fresh);
        editor.start(0);
        const oldStored = JSON.stringify([{...source(), sentence:'Old pending save'}]);
        finish({ok:true,json:async()=>({recording:{transcription:oldStored}})});
        await pending;
        expect(state.selectedRecording.value.transcription).toBe(fresh);
        expect(state.editingSegments.value[0].sentence).toBe('New session source');
        expect(state.recordings.value[0].transcription).toBe(oldStored);
        expect(editor.asrSaveState.value).toBe('saved');
    });
});
