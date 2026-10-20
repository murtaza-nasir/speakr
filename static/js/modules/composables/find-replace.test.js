import { describe, expect, it } from 'vitest';

import { useFindReplace } from './find-replace.js';

const ref = (value) => ({ value });

function editor({ segments = null, text = '' } = {}) {
    const state = {
        findReplace: { open: true, find: '', replace: '', matchCase: false, wholeWord: false, current: -1 },
        showAsrEditorModal: ref(!!segments),
        showTextEditorModal: ref(!segments),
        editingSegments: ref((segments || []).map((sentence) => ({ speaker: 'A', sentence }))),
        editingTranscriptionContent: ref(text),
        asrEditorHighlightIndex: ref(null),
    };
    const toasts = [];
    const api = useFindReplace(state, { showToast: (m) => toasts.push(m), t: (k, p) => (p ? `${k}:${p.count}` : k) });
    return { state, api, toasts };
}

describe('find and replace in the transcript editors (#440)', () => {
    it('counts matches across segments', () => {
        const { state, api } = editor({ segments: ['Acme agreed', 'acme and ACME', 'none'] });
        state.findReplace.find = 'acme';
        expect(api.findReplaceMatches.value.map((m) => m.index)).toEqual([0, 1, 1]);
        expect(api.findReplaceLabel.value).toBe('3');
    });

    it('replaces every match in the segments', () => {
        const { state, api, toasts } = editor({ segments: ['Acme agreed', 'acme and ACME'] });
        Object.assign(state.findReplace, { find: 'acme', replace: 'Akme' });
        api.replaceAllMatches();
        expect(state.editingSegments.value.map((s) => s.sentence)).toEqual(['Akme agreed', 'Akme and Akme']);
        expect(state.editingSegments.value[0].speaker).toBe('A');
        expect(toasts).toEqual(['findReplace.replacedCount:3']);
    });

    it('replaces one match at a time, moving to the next', () => {
        const { state, api } = editor({ segments: ['cat cat', 'cat'] });
        Object.assign(state.findReplace, { find: 'cat', replace: 'dog' });
        api.goToMatch(1);
        expect(state.findReplace.current).toBe(0);
        api.replaceCurrentMatch();
        expect(state.editingSegments.value[0].sentence).toBe('dog cat');
        expect(api.findReplaceLabel.value).toBe('1 / 2');
        api.replaceCurrentMatch();
        api.replaceCurrentMatch();
        expect(state.editingSegments.value.map((s) => s.sentence)).toEqual(['dog dog', 'dog']);
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
