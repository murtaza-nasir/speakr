import { describe, expect, it } from 'vitest';

import { useFindReplace } from './find-replace.js';
import { createAsrModel } from '../asr-editor/model.js';

const ref = (value) => ({ value });

function editor({ segments = null, text = '' } = {}) {
    const model = segments
        ? createAsrModel(segments.map((sentence, i) => ({ speaker: 'A', sentence, start_time: i, end_time: i + 1 })))
        : null;
    const revealed = [];
    const pane = { revealIndex: (index, options) => revealed.push({ index, ...options }), clearHighlight() {} };
    const state = {
        findReplace: { open: true, find: '', replace: '', matchCase: false, wholeWord: false, current: -1 },
        showAsrEditorModal: ref(!!segments),
        showTextEditorModal: ref(!segments),
        asrEditorSession: ref(model ? { model, pane, findStep: null } : null),
        asrEditorRev: ref(0),
        editingTranscriptionContent: ref(text),
    };
    const toasts = [];
    const api = useFindReplace(state, { showToast: (m) => toasts.push(m), t: (k, p) => (p ? `${k}:${p.count}` : k) });
    const sentences = () => model.segments.map((s) => s.sentence);
    return { state, api, toasts, model, sentences, revealed };
}

describe('find and replace in the transcript editors (#440)', () => {
    it('counts matches across segments', () => {
        const { state, api } = editor({ segments: ['Acme agreed', 'acme and ACME', 'none'] });
        state.findReplace.find = 'acme';
        expect(api.findReplaceMatches.value.map((m) => m.index)).toEqual([0, 1, 1]);
        expect(api.findReplaceLabel.value).toBe('3');
    });

    it('replaces every match in the segments', () => {
        const { state, api, toasts, model, sentences } = editor({ segments: ['Acme agreed', 'acme and ACME'] });
        Object.assign(state.findReplace, { find: 'acme', replace: 'Akme' });
        api.replaceAllMatches();
        expect(sentences()).toEqual(['Akme agreed', 'Akme and Akme']);
        expect(model.get(0).speaker).toBe('A');
        expect(toasts).toEqual(['findReplace.replacedCount:3']);
    });

    it('makes Replace all one undo step in the segment editor', () => {
        const { state, api, model, sentences } = editor({ segments: ['cat', 'dog', 'cat cat', 'bird'] });
        Object.assign(state.findReplace, { find: 'cat', replace: 'cow' });
        api.replaceAllMatches();
        expect(sentences()).toEqual(['cow', 'dog', 'cow cow', 'bird']);
        expect(model.undoDepth).toBe(1);
        model.undo();
        expect(sentences()).toEqual(['cat', 'dog', 'cat cat', 'bird']);
        expect(model.canUndo).toBe(false);
    });

    it('shows a match in the transcript pane with its text selected', () => {
        const { state, api, revealed } = editor({ segments: ['none', 'a cat here'] });
        state.findReplace.find = 'cat';
        api.goToMatch(1);
        expect(revealed).toEqual([{ index: 1, highlight: true, textRange: [2, 5] }]);
        expect(state.asrEditorSession.value.findStep).toBe(api.goToMatch);
    });

    it('replaces one match at a time, moving to the next', () => {
        const { state, api, model, sentences } = editor({ segments: ['cat cat', 'cat'] });
        Object.assign(state.findReplace, { find: 'cat', replace: 'dog' });
        api.goToMatch(1);
        expect(state.findReplace.current).toBe(0);
        api.replaceCurrentMatch();
        expect(sentences()[0]).toBe('dog cat');
        expect(api.findReplaceLabel.value).toBe('1 / 2');
        api.replaceCurrentMatch();
        api.replaceCurrentMatch();
        expect(sentences()).toEqual(['dog dog', 'dog']);
        expect(model.undoDepth).toBe(3);
        expect(state.findReplace.current).toBe(-1);
        expect(api.findReplaceLabel.value).toBe('findReplace.noMatches');
    });

    it('works on the plain transcript text', () => {
        const { state, api } = editor({ text: 'Speaker 1: hello\nSpeaker 1: bye' });
        Object.assign(state.findReplace, { find: 'speaker 1', replace: 'Dana', matchCase: false });
        api.replaceAllMatches();
        expect(state.editingTranscriptionContent.value).toBe('Dana: hello\nDana: bye');
    });

    it('wraps around when moving past the last match', () => {
        const { state, api } = editor({ segments: ['a', 'a'] });
        state.findReplace.find = 'a';
        api.goToMatch(-1);
        expect(state.findReplace.current).toBe(1);
        api.goToMatch(1);
        expect(state.findReplace.current).toBe(0);
    });
});
