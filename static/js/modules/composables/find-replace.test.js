import { afterEach, describe, expect, it, vi } from 'vitest';

import { useFindReplace } from './find-replace.js';

const ref = (value) => ({ value });

function editor({ segments = null, text = '', utils = {} } = {}) {
    const state = {
        findReplace: { open: true, find: '', replace: '', matchCase: false, wholeWord: false, current: -1 },
        showAsrEditorModal: ref(!!segments),
        showTextEditorModal: ref(!segments),
        editingSegments: ref((segments || []).map((sentence) => ({ speaker: 'A', sentence }))),
        editingTranscriptionContent: ref(text),
        asrEditorHighlightIndex: ref(null),
    };
    const toasts = [];
    const api = useFindReplace(state, { showToast: (m) => toasts.push(m), t: (k, p) => (p ? `${k}:${p.count}` : k), ...utils });
    return { state, api, toasts };
}

describe('find and replace in the transcript editors (#440)', () => {
    afterEach(() => vi.unstubAllGlobals());

    it('opens the matched segment before selecting text in its detail panel', async () => {
        let selected = -1;
        const field = { blur: vi.fn(), focus: vi.fn(), setSelectionRange: vi.fn() };
        vi.stubGlobal('document', { querySelector: vi.fn(() => selected === 1 ? field : null) });
        const openFindReplaceSegment = vi.fn(async (index) => { selected = index; });
        const { state, api } = editor({ segments: ['other', 'the match'], utils: { openFindReplaceSegment } });
        state.findReplace.find = 'match';
        api.goToMatch(1);
        await Promise.resolve();
        await Promise.resolve();
        expect(openFindReplaceSegment).toHaveBeenCalledWith(1);
        expect(field.setSelectionRange).toHaveBeenCalledWith(4, 9);
    });

    it('does not replace a locked draft or an unapplied ASR proposal', () => {
        const { state, api } = editor({ segments: ['cat cat'], utils: { canReplaceTranscript: () => false } });
        Object.assign(state.findReplace, { find: 'cat', replace: 'dog', current: 0 });
        api.replaceCurrentMatch();
        api.replaceAllMatches();
        expect(state.editingSegments.value[0].sentence).toBe('cat cat');
    });
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
