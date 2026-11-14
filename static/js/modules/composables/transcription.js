/**
 * Transcription editing composable
 * Handles the plain text editor and saving of transcript, summary and notes.
 * The segment editor lives in asr-editor.js (opened from here through
 * utils.openAsrEditorModal).
 */

// Guarded: the module can run before i18n.js has loaded.
const t = (key, params) => (window.i18n ? window.i18n.t(key, params) : key);

export function useTranscription(state, utils) {
    const {
        showTextEditorModal, selectedRecording,
        viewingTranslation,
        editingTranscriptionContent,
        recordings
    } = state;

    const { showToast, setGlobalError } = utils;

    // =========================================
    // Text Editor Modal
    // =========================================

    const displayedTranscription = () => viewingTranslation.value
        ? selectedRecording.value?.transcription_translation
        : selectedRecording.value?.transcription;

    const openTranscriptionEditor = () => {
        if (!selectedRecording.value) return;
        const source = displayedTranscription();
        if (!source) return;

        // Check if transcription is JSON (ASR format)
        try {
            const parsed = JSON.parse(source);
            if (Array.isArray(parsed)) {
                utils.openAsrEditorModal();
            } else {
                openTextEditorModal();
            }
        } catch (e) {
            // Not JSON, use text editor
            openTextEditorModal();
        }
    };

    const openTextEditorModal = () => {
        if (!selectedRecording.value) return;
        const source = displayedTranscription();
        editingTranscriptionContent.value = source || '';
        showTextEditorModal.value = true;
    };

    const closeTextEditorModal = () => {
        showTextEditorModal.value = false;
        editingTranscriptionContent.value = '';
    };

    const saveTranscription = async () => {
        if (!selectedRecording.value) return;
        // A failed save keeps the editor open, so the text is not lost.
        if (await saveTranscriptionContent(editingTranscriptionContent.value)) closeTextEditorModal();
    };

    // =========================================
    // Save Transcription Content
    // =========================================

    // Resolves to true when the transcript was saved, false when it was not
    // (the error is shown here).
    const saveTranscriptionContent = async (content) => {
        if (!selectedRecording.value) return false;

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const payload = viewingTranslation.value
                ? { transcription_translation: content }
                : { transcription: content };
            const response = await fetch(`/recording/${selectedRecording.value.id}/update_transcription`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify(payload)
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || t('errors.updateTranscriptionFailed'));

            // Update recording
            if (viewingTranslation.value) {
                selectedRecording.value.transcription_translation = content;
                const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
                if (index !== -1) {
                    recordings.value[index].transcription_translation = content;
                }
            } else {
                selectedRecording.value.transcription = content;
                const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
                if (index !== -1) {
                    recordings.value[index].transcription = content;
                }
            }

            showToast(t('messages.transcriptionUpdated'), 'fa-check-circle');
            return true;
        } catch (error) {
            setGlobalError(t('errors.saveTranscriptionFailedDetail', { error: error.message }));
            return false;
        }
    };

    // =========================================
    // Save Summary
    // =========================================

    const saveSummary = async (summary) => {
        if (!selectedRecording.value) return;

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const payload = {
                id: selectedRecording.value.id,
                title: selectedRecording.value.title,
                participants: selectedRecording.value.participants,
                notes: selectedRecording.value.notes,
                summary: summary,
                meeting_date: selectedRecording.value.meeting_date
            };
            const response = await fetch('/save', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify(payload)
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || t('errors.updateSummaryFailed'));

            // Update recording
            selectedRecording.value.summary = summary;

            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1) {
                recordings.value[index].summary = summary;
            }

            showToast(t('messages.summarySaved'), 'fa-check-circle');
        } catch (error) {
            setGlobalError(t('errors.saveSummaryFailedDetail', { error: error.message }));
        }
    };

    // =========================================
    // Save Notes
    // =========================================

    const saveNotes = async (notes) => {
        if (!selectedRecording.value) return;

        // Handle incognito recordings - save to sessionStorage only
        if (selectedRecording.value.incognito) {
            selectedRecording.value.notes = notes;
            // Update sessionStorage
            try {
                const stored = sessionStorage.getItem('speakr_incognito_recording');
                if (stored) {
                    const data = JSON.parse(stored);
                    data.notes = notes;
                    sessionStorage.setItem('speakr_incognito_recording', JSON.stringify(data));
                }
            } catch (e) {
                console.error('[Incognito] Failed to save notes to sessionStorage:', e);
            }
            showToast(t('messages.notesSavedBrowserOnly'), 'fa-check-circle');
            return;
        }

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/api/recordings/${selectedRecording.value.id}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ notes })
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || t('errors.updateNotesFailed'));

            // Update recording
            selectedRecording.value.notes = notes;

            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1) {
                recordings.value[index].notes = notes;
            }

            showToast(t('messages.notesUpdated'), 'fa-check-circle');
        } catch (error) {
            setGlobalError(t('errors.saveNotesFailedDetail', { error: error.message }));
        }
    };

    return {
        // Text editor
        openTranscriptionEditor,
        openTextEditorModal,
        closeTextEditorModal,
        saveTranscription,

        // Save
        saveTranscriptionContent,
        saveSummary,
        saveNotes
    };
}
