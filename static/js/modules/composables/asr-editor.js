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
import { confirmDialog } from '../utils/dialogs.js';
import { createSummaryFollowUp } from '../utils/summary-follow-up.js';
import { formatExact } from '../asr-editor/helpers.js';
import { sendTrim, waitForTrim } from '../utils/recording-trim.js';

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

const ACTION_TOAST_MS = 8000;

/**
 * The message and the arguments of an editor toast. With an action, the
 * toast names it (e.g. Undo) and a click on the toast runs it. The toast
 * shows text only, so speaker names need no escaping.
 */
export function editorToast(message, { action = null, actionLabel = '' } = {}) {
    const text = String(message == null ? '' : message);
    if (typeof action !== 'function') return { message: text, duration: undefined, onClick: null };
    return {
        message: { text, actionLabel: String(actionLabel || '') },
        duration: ACTION_TOAST_MS,
        onClick: action,
    };
}

export function useAsrEditor(state, utils, { saveTranscriptionContent }) {
    const {
        showAsrEditorModal, selectedRecording, viewingTranslation, recordings,
        asrEditorSession, asrEditorDirty, asrEditorRev, asrEditorSaveFlash, asrEditorSaving,
        asrEditorRegenerateSummary, editorAutosave, speakerColorMap, audioDuration,
    } = state;
    const { showToast, setGlobalError } = utils;
    const nextTick = (typeof Vue !== 'undefined' && Vue.nextTick) || null;
    // After a save that queued a summary regeneration (shared with the Identify Speakers dialog)
    const summaryFollowUp = createSummaryFollowUp({
        recordings, selectedRecording, showToast, t, nextTick, onDone: () => { if (utils.onChatComplete) utils.onChatComplete(); },
    });
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
                const { message: body, duration, onClick } = editorToast(message, options);
                showToast(body, 'fa-check-circle', duration, 'info', onClick);
            },
            restoreDraft,
            discardDraft,
            // Trim mode of the timeline asks here (#660)
            requestTrim: (range, options) => requestTrim(session, range, options),
            findStep: null,
            pane: null,
            // Edits of this session were saved without regenerating the summary
            unsummarized: false,
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
                    if (current() === session && model.dirty) saveAsrTranscription(true, { auto: true });
                }, AUTOSAVE_DELAY);
            }
        });

        asrEditorSession.value = session;
        if (asrEditorDirty) asrEditorDirty.value = false;
        // Off whenever the editor opens: the Identify Speakers dialog keeps no preference to share
        if (asrEditorRegenerateSummary) asrEditorRegenerateSummary.value = false;
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

    let discardAsked = false;

    /**
     * Close the editor. Unsaved changes are discarded only after the user
     * agrees (unless `force`): the question is the app's own dialog, so this
     * resolves to true once the editor is closed and to false when it stays.
     */
    const closeAsrEditorModal = async ({ force = false } = {}) => {
        const session = current();
        if (session && !force && session.model.dirty) {
            if (discardAsked) return false;
            discardAsked = true;
            let agreed = false;
            try {
                agreed = await confirmDialog({
                    message: t('asrEditor.discardConfirm'),
                    confirmLabel: t('common.discard'),
                    danger: true,
                });
            } finally {
                discardAsked = false;
            }
            // Saved or closed while the question was open: nothing to discard
            if (!agreed || current() !== session) return false;
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
     * True when the summary can be regenerated after a save from this
     * session: the transcript itself is edited (not a translation) and the
     * recording is stored on the server (not incognito). The footer shows
     * its checkbox only then.
     */
    const canRegenerateSummary = (session = current()) => !!session && session.field === 'transcription'
        && !!selectedRecording.value && !selectedRecording.value.incognito;

    /**
     * Save the transcript. The whole transcript is sent: the server stores it
     * as one JSON text. When voice matches were accepted, or the summary is
     * to be regenerated, it goes through the route of the Identify Speakers
     * dialog, which also records the matches and queues the summary.
     *
     * The summary is regenerated when its checkbox in the footer is on, the
     * save is the user's (Save, Save and close, Ctrl+S; never autosave), and
     * there are changes: unsaved ones, or ones this session saved earlier
     * without regenerating. Returns true when it was saved.
     */
    const saveAsrTranscription = async (keepOpen = false, { auto = false } = {}) => {
        const session = current();
        if (!session || !selectedRecording.value) return false;
        if (asrEditorSaving && asrEditorSaving.value) return false;
        clearTimeout(autosaveTimer);
        autosaveTimer = null;

        const model = session.model;
        model.endBurst();
        const revAtSave = model.rev;
        const wasDirty = model.dirty;
        const stored = model.toJSON();
        const withMatches = session.field === 'transcription' && !selectedRecording.value.incognito
            ? withAcceptedMatches(stored, model.segments.map((s) => s.id), model.acceptedMatches()) : null;
        const regenerate = !auto && !!(asrEditorRegenerateSummary && asrEditorRegenerateSummary.value)
            && canRegenerateSummary(session) && (wasDirty || session.unsummarized);

        if (asrEditorSaving) asrEditorSaving.value = true;
        let savedText = null;
        let serverSegments = null;
        let summaryQueued = false;
        try {
            shareColors(session);
            if (withMatches || regenerate) {
                const data = await saveTranscriptWithSpeakers(session.recordingId, {
                    transcriptData: withMatches ? withMatches.transcriptData : stored,
                    speakerMap: withMatches ? withMatches.speakerMap : {},
                    regenerateSummary: regenerate,
                }, { fallbackMessage: t('errors.updateTranscriptFailed') });
                summaryQueued = regenerate && !!data.summary_queued;
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

        // A failed save returned above: nothing was queued. Here the transcript
        // is stored; the summary follows it, or is still to be regenerated.
        if (summaryQueued) {
            session.unsummarized = false;
            summaryFollowUp.started(session.recordingId);
        } else if (wasDirty) {
            session.unsummarized = true;
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

    // ---- trim (#660) -------------------------------------------------------------

    /**
     * Whether the timeline offers Trim: the stored transcript (not a
     * translation) of a finished recording with audio, for a user who may
     * edit it, as on the server.
     */
    const canTrimRecording = (session = current()) => {
        const recording = selectedRecording.value;
        return !!session && session.field === 'transcription' && !!recording && !recording.incognito
            && !recording.audio_deleted_at && recording.status === 'COMPLETED' && recording.can_edit !== false;
    };

    // The players of the page load the trimmed audio again. The address
    // gets a version: the browser's media cache keeps the old file under
    // the plain one. Vue leaves the attribute alone, as its bound value is
    // unchanged.
    const reloadPlayers = (recordingId) => {
        if (typeof document === 'undefined') return;
        const version = Date.now();
        for (const media of document.querySelectorAll('audio, video')) {
            const src = media.getAttribute('src') || '';
            if (src === `/audio/${recordingId}` || src.startsWith(`/audio/${recordingId}?`)) {
                try {
                    media.setAttribute('src', `/audio/${recordingId}?v=${version}`);
                    media.load();
                } catch (e) { /* not loaded yet */ }
            }
        }
    };

    const refreshRecording = async (recordingId) => {
        try {
            const response = await fetch(`/api/recordings/${recordingId}`);
            if (!response.ok) return;
            const full = await response.json();
            if (selectedRecording.value && selectedRecording.value.id === recordingId) selectedRecording.value = full;
            const index = recordings.value.findIndex((r) => r.id === recordingId);
            if (index !== -1) recordings.value[index] = { ...recordings.value[index], ...full };
            // The player keeps the stored length of a recording it has open: the new one
            if (audioDuration && full.audio_duration && selectedRecording.value && selectedRecording.value.id === recordingId) {
                audioDuration.value = full.audio_duration;
            }
            if (nextTick) await nextTick();
            reloadPlayers(recordingId);
        } catch (error) {
            console.error('Could not load the trimmed recording:', error);
        }
    };

    /**
     * Trim the open recording to `range` ({ start, end }, seconds). The
     * edits must be saved first: the server trims the stored transcript.
     * After the user agrees, the trim is queued, the editor closes and the
     * recording is loaded again when it is done. Resolves to true when the
     * trim was queued.
     */
    async function requestTrim(session, range, { regenerateSummary = false, effect = null } = {}) {
        if (current() !== session || !selectedRecording.value) return false;
        if (session.model.dirty) {
            showToast(t('asrEditor.trim.saveFirst'), 'fa-exclamation-circle', 4000, 'error');
            return false;
        }
        const recordingId = session.recordingId;
        const agreed = await confirmDialog({
            title: t('asrEditor.trim.confirmTitle'),
            message: t('asrEditor.trim.confirmMessage', {
                start: formatExact(range.start), end: formatExact(range.end),
                removed: effect ? effect.removed : 0, clipped: effect ? effect.clipped : 0,
            }),
            confirmLabel: t('asrEditor.trim.apply'),
            danger: true,
        });
        if (!agreed || current() !== session) return false;
        const sent = await sendTrim(fetch, recordingId, range, {
            regenerateSummary, fallback: t('asrEditor.trim.failed'),
        });
        if (!sent.ok) {
            showToast(sent.error || t('asrEditor.trim.failed'), 'fa-exclamation-circle', 6000, 'error');
            return false;
        }
        await closeAsrEditorModal({ force: true });
        showToast(t('asrEditor.trim.started'), 'fa-scissors');
        waitForTrim(fetch, recordingId).then(async (result) => {
            if (result.state === 'completed') {
                await refreshRecording(recordingId);
                showToast(t('asrEditor.trim.done'), 'fa-check-circle');
                if (result.summary_queued) summaryFollowUp.started(recordingId);
            } else if (result.state === 'failed') {
                showToast(result.error || t('asrEditor.trim.failed'), 'fa-exclamation-circle', 6000, 'error');
            }
        });
        return true;
    }

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
        canRegenerateSummary,
        canTrimRecording,
    };
}
