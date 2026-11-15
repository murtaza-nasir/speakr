/**
 * Segment transcript editor: the editing session of the open recording.
 *
 * Opening the editor creates a session: the editing model (not reactive, see
 * modules/asr-editor/model.js) and what the transcript pane needs around it.
 * The pane component (modules/asr-editor/pane.js) renders it; this composable
 * opens, saves and closes it, keeps the local draft and loads the voice-match
 * suggestions.
 */

import { createAsrModel } from '../asr-editor/model.js';
import { createDraftStore, hashText } from '../asr-editor/draft.js';
import { saveTranscriptWithSpeakers, withAcceptedMatches } from '../utils/speaker-save.js';

// Scroll position per recording, so reopening the editor in the same page
// session returns to where it was.
const scrollMemory = new Map();
// Segment to show when the editor next opens (double-click on a transcript row).
let pendingIndex = null;

const DRAFT_DELAY = 800;
const AUTOSAVE_DELAY = 2000;
// From this many segments on, the dialog's backdrop blur is off while the
// editor is open (see .asr-editor-long in styles.css).
const LONG_TRANSCRIPT = 500;
const LONG_CLASS = 'asr-editor-long';

// Guarded: the module can run before i18n.js has loaded.
const t = (key, params) => (window.i18n ? window.i18n.t(key, params) : key);

const plain = (value) => ({ value });

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const escapeHtml = (text) => String(text == null ? '' : text).replace(/[&<>"']/g, (c) => ESCAPES[c]);
const ACTION_TOAST_MS = 8000;

/**
 * The HTML and the arguments of an editor toast. With an action, the toast
 * names it (e.g. Undo) and a click on the toast runs it. Speaker names are
 * user text: they are escaped.
 */
export function editorToast(message, { action = null, actionLabel = '' } = {}) {
    const text = escapeHtml(message);
    if (typeof action !== 'function') return { html: text, duration: undefined, onClick: null };
    return {
        html: `${text} <span class="toast__action">${escapeHtml(actionLabel)}</span>`,
        duration: ACTION_TOAST_MS,
        onClick: action,
    };
}

export function useAsrEditor(state, utils, { saveTranscriptionContent }) {
    const {
        showAsrEditorModal, selectedRecording, viewingTranslation, recordings,
        asrEditorSession, asrEditorDirty, asrEditorRev, asrEditorSaveFlash, asrEditorSaving,
        editorAutosave, speakerColorMap,
    } = state;
    const { showToast, setGlobalError } = utils;
    const shallowRef = (typeof Vue !== 'undefined' && Vue.shallowRef) || plain;
    const markRaw = (typeof Vue !== 'undefined' && Vue.markRaw) || ((x) => x);

    let storage = null;
    try { storage = typeof localStorage !== 'undefined' ? localStorage : null; } catch (e) { storage = null; }
    const drafts = storage ? createDraftStore(storage) : null;

    let draftTimer = null;
    let autosaveTimer = null;
    let draftTooLargeTold = false;

    const clearTimers = () => {
        clearTimeout(draftTimer);
        clearTimeout(autosaveTimer);
        draftTimer = null;
        autosaveTimer = null;
    };

    const current = () => (asrEditorSession ? asrEditorSession.value : null);

    // Helper to pause outer audio player when opening modals with their own player
    const pauseOuterAudioPlayer = () => {
        const outerAudio = document.querySelector('#rightMainColumn audio') || document.querySelector('#rightMainColumn video') ||
                          document.querySelector('.detail-view audio:not(.fixed audio)') || document.querySelector('.detail-view video:not(.fixed video)');
        if (outerAudio && !outerAudio.paused) outerAudio.pause();
    };

    // ---- local draft --------------------------------------------------------

    const writeDraft = (session) => {
        if (!drafts || current() !== session) return;
        const log = session.model.dirty ? session.model.exportLog() : null;
        if (!log || !log.length) {
            drafts.discard(session.recordingId, session.field);
            return;
        }
        const result = drafts.save(session.recordingId, session.field, session.base, log);
        if (result === 'too-large' && !draftTooLargeTold) {
            draftTooLargeTold = true;
            showToast(t('asrEditor.draftTooLarge'), 'fa-info-circle');
        }
    };

    const restoreDraft = () => {
        const session = current();
        const draft = session && session.draft.value;
        if (!draft) return;
        session.draft.value = null;
        try {
            session.model.importLog(draft.log);
            showToast(t('asrEditor.draftRestored'), 'fa-check-circle');
        } catch (error) {
            console.error('Could not restore the transcript draft:', error);
            if (drafts) drafts.discard(session.recordingId, session.field);
            showToast(t('asrEditor.draftUnusable'), 'fa-exclamation-circle', 4000, 'error');
        }
    };

    const discardDraft = () => {
        const session = current();
        if (!session) return;
        session.draft.value = null;
        if (drafts) drafts.discard(session.recordingId, session.field);
    };

    // ---- voice-match suggestions ---------------------------------------------

    // The same sources as the Identify Speakers dialog: voice matches per
    // label and the user's saved speakers.
    const loadSpeakerData = async (session) => {
        try {
            const response = await fetch('/speakers');
            const speakers = response.ok ? await response.json() : [];
            if (current() === session) session.savedSpeakers.value = Array.isArray(speakers) ? speakers : [];
        } catch (error) { /* offline: the picker lists the recording's speakers only */ }
        if (session.field !== 'transcription') return;
        try {
            const response = await fetch(`/speakers/suggestions/${session.recordingId}`);
            if (!response.ok) return;
            const data = await response.json();
            if (current() !== session || !data.success || !data.suggestions) return;
            session.voice.value = Object.fromEntries(
                Object.entries(data.suggestions).filter(([, matches]) => matches && matches.length > 0)
            );
        } catch (error) {
            console.error('Error loading voice suggestions:', error);
        }
    };

    // ---- open / close --------------------------------------------------------

    const openAsrEditorModal = async () => {
        const recording = selectedRecording.value;
        if (!recording) return;
        pauseOuterAudioPlayer();

        const field = viewingTranslation.value ? 'transcription_translation' : 'transcription';
        const source = recording[field];
        let segments;
        try {
            segments = JSON.parse(source);
            if (!Array.isArray(segments)) throw new Error('not a segment list');
        } catch (e) {
            console.error('Could not parse transcription as JSON for ASR editor:', e);
            setGlobalError(t('errors.asrEditorWrongFormat'));
            return;
        }

        clearTimers();
        const model = createAsrModel(segments);
        const base = hashText(source);
        const targetIndex = pendingIndex;
        pendingIndex = null;

        const session = markRaw({
            key: `${recording.id}:${field}:${Date.now()}`,
            model,
            recordingId: recording.id,
            field,
            base,
            colorMap: { ...((speakerColorMap && speakerColorMap.value) || {}) },
            voice: shallowRef({}),
            savedSpeakers: shallowRef([]),
            draft: shallowRef(drafts && !recording.incognito ? drafts.load(recording.id, field, base) : null),
            initialIndex: targetIndex,
            initialScrollTop: targetIndex == null ? (scrollMemory.get(`${recording.id}:${field}`) || 0) : 0,
            audio: () => document.querySelector('[data-asr-editor-audio]'),
            toast: (message, options) => {
                const { html, duration, onClick } = editorToast(message, options);
                showToast(html, 'fa-check-circle', duration, 'info', onClick);
            },
            restoreDraft,
            discardDraft,
            findStep: null,
            pane: null,
        });

        model.onChange((event) => {
            if (current() !== session) return;
            if (asrEditorRev) asrEditorRev.value++;
            if (asrEditorDirty) asrEditorDirty.value = model.dirty;
            if (event.source === 'save') return;
            // A draft that was not restored is replaced by the first new edit.
            if (session.draft.value && event.type !== 'restore') session.draft.value = null;
            if (drafts && !recording.incognito) {
                clearTimeout(draftTimer);
                draftTimer = setTimeout(() => writeDraft(session), DRAFT_DELAY);
            }
            // Autosave (Account, Preferences): two seconds after the last edit.
            if (editorAutosave && editorAutosave.value) {
                clearTimeout(autosaveTimer);
                autosaveTimer = setTimeout(() => {
                    autosaveTimer = null;
                    if (current() === session && model.dirty) saveAsrTranscription(true);
                }, AUTOSAVE_DELAY);
            }
        });

        asrEditorSession.value = session;
        if (asrEditorDirty) asrEditorDirty.value = false;
        draftTooLargeTold = false;
        document.documentElement.classList.toggle(LONG_CLASS, model.length >= LONG_TRANSCRIPT);
        showAsrEditorModal.value = true;
        if (!recording.incognito) loadSpeakerData(session);
    };

    // Open the editor at a segment (double-click on a transcript row). The
    // row is selected and briefly highlighted.
    const openAsrEditorAtSegment = (segmentIndex) => {
        pendingIndex = segmentIndex;
        return openAsrEditorModal();
    };

    /** Close the editor. Unsaved changes are discarded only after the user agrees (unless `force`). */
    const closeAsrEditorModal = ({ force = false } = {}) => {
        const session = current();
        if (session && !force && session.model.dirty) {
            if (!window.confirm(t('asrEditor.discardConfirm'))) return false;
        }
        clearTimers();
        if (session) {
            if (session.pane) scrollMemory.set(`${session.recordingId}:${session.field}`, session.pane.scrollTop());
            // Closing without saving discards the edits, and their draft with them.
            if (drafts && session.model.dirty) drafts.discard(session.recordingId, session.field);
        }

        // Pause any playing modal audio before closing
        const modalAudio = document.querySelector('[data-asr-editor-audio]');
        if (modalAudio) modalAudio.pause();
        // Reset modal audio state (keep main player independent)
        if (utils.resetModalAudioState) utils.resetModalAudioState();

        showAsrEditorModal.value = false;
        asrEditorSession.value = null;
        if (asrEditorDirty) asrEditorDirty.value = false;
        document.documentElement.classList.remove(LONG_CLASS);
        return true;
    };

    // ---- save ------------------------------------------------------------------

    // New speaker names keep the colour the editor gave them.
    const shareColors = (session) => {
        if (!speakerColorMap) return;
        const known = speakerColorMap.value || {};
        const added = Object.entries(session.colorMap).filter(([name]) => !known[name]);
        if (added.length) speakerColorMap.value = { ...known, ...Object.fromEntries(added) };
    };

    const updateRecording = (recordingId, fields) => {
        if (selectedRecording.value && selectedRecording.value.id === recordingId) Object.assign(selectedRecording.value, fields);
        const index = recordings.value.findIndex((r) => r.id === recordingId);
        if (index !== -1 && recordings.value[index] !== selectedRecording.value) Object.assign(recordings.value[index], fields);
    };

    /**
     * Save the transcript. The whole transcript is sent: the server stores it
     * as one JSON text. When voice matches were accepted, it goes through the
     * route of the Identify Speakers dialog, which also records the matches.
     * Returns true when it was saved.
     */
    const saveAsrTranscription = async (keepOpen = false) => {
        const session = current();
        if (!session || !selectedRecording.value) return false;
        if (asrEditorSaving && asrEditorSaving.value) return false;
        clearTimeout(autosaveTimer);
        autosaveTimer = null;

        const model = session.model;
        model.endBurst();
        const revAtSave = model.rev;
        const stored = model.toJSON();
        const withMatches = session.field === 'transcription' && !selectedRecording.value.incognito
            ? withAcceptedMatches(stored, model.segments.map((s) => s.id), model.acceptedMatches()) : null;

        if (asrEditorSaving) asrEditorSaving.value = true;
        let savedText = null;
        let serverSegments = null;
        try {
            shareColors(session);
            if (withMatches) {
                const data = await saveTranscriptWithSpeakers(session.recordingId, {
                    transcriptData: withMatches.transcriptData, speakerMap: withMatches.speakerMap, regenerateSummary: false,
                }, { fallbackMessage: t('errors.updateTranscriptFailed') });
                const recording = data.recording || {};
                savedText = typeof recording.transcription === 'string' ? recording.transcription : JSON.stringify(stored);
                try { serverSegments = JSON.parse(savedText); } catch (e) { serverSegments = null; }
                updateRecording(session.recordingId, {
                    transcription: savedText,
                    ...(recording.participants !== undefined ? { participants: recording.participants } : {}),
                });
                showToast(t('messages.transcriptionUpdated'), 'fa-check-circle');
            } else {
                const content = JSON.stringify(stored);
                const ok = await saveTranscriptionContent(content);
                if (!ok) return false;
                savedText = content;
            }
        } catch (error) {
            setGlobalError(t('errors.saveTranscriptionFailedDetail', { error: error.message }));
            return false;
        } finally {
            if (asrEditorSaving) asrEditorSaving.value = false;
        }

        if (current() === session) {
            if (model.rev === revAtSave) {
                model.markSaved();
                // The names as the server spelled them, and their links to saved speakers.
                if (serverSegments) model.adoptSaved(serverSegments);
                session.base = hashText(savedText);
                clearTimeout(draftTimer);
                if (drafts) drafts.discard(session.recordingId, session.field);
                if (asrEditorDirty) asrEditorDirty.value = false;
            } else {
                // Edited while the request was under way: those edits are not
                // saved yet. The draft base stays the older transcript, so
                // the draft is not offered for the newer one.
                if (drafts) drafts.discard(session.recordingId, session.field);
            }
        }

        if (keepOpen) {
            if (asrEditorSaveFlash) {
                asrEditorSaveFlash.value = true;
                setTimeout(() => { asrEditorSaveFlash.value = false; }, 1800);
            }
        } else if (current() === session && !model.dirty) {
            closeAsrEditorModal({ force: true });
        }
        return true;
    };

    // Ctrl+S / Cmd+S saves without closing while the editor is open.
    // preventDefault stops the browser's "save page" dialog.
    const handleAsrEditorKeydown = (event) => {
        if (!showAsrEditorModal.value) return;
        const isSaveShortcut = (event.ctrlKey || event.metaKey) && (event.key === 's' || event.key === 'S');
        if (isSaveShortcut) {
            event.preventDefault();
            saveAsrTranscription(true);
        }
    };
    if (typeof window !== 'undefined' && window.addEventListener) {
        window.addEventListener('keydown', handleAsrEditorKeydown);
    }

    return {
        openAsrEditorModal,
        openAsrEditorAtSegment,
        closeAsrEditorModal,
        saveAsrTranscription,
    };
}
