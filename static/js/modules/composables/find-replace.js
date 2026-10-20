/**
 * Find and replace in the transcript editors (#440).
 *
 * Works on the open editor: the sentences of the segment editor, or the text
 * of the plain transcript editor. Changes are made to the editor's copy, so
 * Cancel still discards them.
 */

import { buildMatcher, findMatches, replaceAllIn, replaceAt } from '../utils/find-replace.js';

const computedOr = (fn) => (typeof Vue !== 'undefined' && Vue.computed)
    ? Vue.computed(fn)
    : { get value() { return fn(); } };

export function useFindReplace(state, utils) {
    const {
        findReplace, showAsrEditorModal, showTextEditorModal,
        editingSegments, editingTranscriptionContent, asrEditorHighlightIndex
    } = state;
    const { showToast, nextTick = (fn) => fn && fn(), t = (key) => key } = utils;

    const openEditor = () => {
        if (showAsrEditorModal && showAsrEditorModal.value) return 'segments';
        if (showTextEditorModal && showTextEditorModal.value) return 'text';
        return null;
    };

    const editorTexts = () => {
        const editor = openEditor();
        if (editor === 'segments') return editingSegments.value.map((s) => s.sentence || '');
        if (editor === 'text') return [editingTranscriptionContent.value || ''];
        return [];
    };

    const matcher = () => buildMatcher(findReplace.find, findReplace);
    const findReplaceMatches = computedOr(() => findMatches(editorTexts(), matcher()));

    const findReplaceLabel = computedOr(() => {
        const total = findReplaceMatches.value.length;
        if (!findReplace.find) return '';
        if (!total) return t('findReplace.noMatches');
        return findReplace.current >= 0 ? `${findReplace.current + 1} / ${total}` : String(total);
    });

    const setText = (index, text) => {
        if (openEditor() === 'segments') editingSegments.value[index].sentence = text;
        else editingTranscriptionContent.value = text;
    };

    // Show the match in the editor: scroll its row into view and select it.
    const revealMatch = (match) => {
        if (!match || typeof document === 'undefined') return;
        if (openEditor() === 'segments') {
            if (asrEditorHighlightIndex) asrEditorHighlightIndex.value = match.index;
            if (utils.scrollAsrEditorToIndex) utils.scrollAsrEditorToIndex(match.index);
        }
        nextTick(() => {
            const field = openEditor() === 'segments'
                ? document.querySelector(`[data-segment-index="${match.index}"] textarea`)
                : document.querySelector('[data-transcript-textarea]');
            if (!field) return;
            field.blur();
            field.setSelectionRange(match.start, match.end);
            field.focus({ preventScroll: openEditor() === 'segments' });
        });
    };

    const goToMatch = (step = 1) => {
        const total = findReplaceMatches.value.length;
        if (!total) {
            findReplace.current = -1;
            return;
        }
        const from = findReplace.current < 0 && step < 0 ? 0 : findReplace.current;
        findReplace.current = ((from + step) % total + total) % total;
        revealMatch(findReplaceMatches.value[findReplace.current]);
    };

    // Replace the current match, then show the one after it.
    const replaceCurrentMatch = () => {
        const matches = findReplaceMatches.value;
        if (!matches.length) return;
        if (findReplace.current < 0 || findReplace.current >= matches.length) {
            goToMatch(1);
            return;
        }
        const match = matches[findReplace.current];
        const text = editorTexts()[match.index];
        setText(match.index, replaceAt(text, match.start, match.end, findReplace.replace));
        const remaining = findReplaceMatches.value.length;
        if (!remaining) {
            findReplace.current = -1;
            return;
        }
        findReplace.current = Math.min(findReplace.current, remaining - 1);
        revealMatch(findReplaceMatches.value[findReplace.current]);
    };

    const replaceAllMatches = () => {
        const m = matcher();
        let total = 0;
        editorTexts().forEach((text, index) => {
            const result = replaceAllIn(text, m, findReplace.replace);
            if (result.count) {
                setText(index, result.text);
                total += result.count;
            }
        });
        findReplace.current = -1;
        if (asrEditorHighlightIndex) asrEditorHighlightIndex.value = null;
        if (total && showToast) showToast(t('findReplace.replacedCount', { count: total }), 'fa-check-circle');
    };

    const openFindReplace = () => {
        findReplace.open = true;
        nextTick(() => {
            const input = typeof document !== 'undefined' && document.querySelector('[data-find-input]');
            if (input) {
                input.focus();
                input.select();
            }
        });
    };

    const closeFindReplace = () => {
        findReplace.open = false;
        findReplace.current = -1;
        if (asrEditorHighlightIndex) asrEditorHighlightIndex.value = null;
    };

    // A new search starts again from the first match
    if (typeof Vue !== 'undefined' && Vue.watch) {
        Vue.watch(() => [findReplace.find, findReplace.matchCase, findReplace.wholeWord],
            () => { findReplace.current = -1; });
        // Each editor opens with the bar closed
        Vue.watch(() => openEditor(), () => closeFindReplace());
    }

    // Ctrl+F / Cmd+F (and Ctrl+H) open the bar while an editor is open: the
    // browser's own search does not reach text inside the editor's fields.
    const handleFindShortcut = (event) => {
        if (!openEditor()) return;
        const key = (event.key || '').toLowerCase();
        if ((event.ctrlKey || event.metaKey) && (key === 'f' || key === 'h')) {
            event.preventDefault();
            openFindReplace();
        }
    };
    if (typeof window !== 'undefined') {
        window.addEventListener('keydown', handleFindShortcut);
    }

    return {
        findReplaceMatches, findReplaceLabel, goToMatch, replaceCurrentMatch,
        replaceAllMatches, openFindReplace, closeFindReplace
    };
}
