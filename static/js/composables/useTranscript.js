/**
 * Transcript composable
 * Handles transcript viewing and editing functionality
 */

import { ref, computed } from 'vue';
import { apiRequest } from '../utils/apiClient.js';

export function useTranscript() {
    // State
    const selectedTab = ref('summary');
    const isEditingTranscript = ref(false);
    const editedTranscription = ref('');
    const isEditingSummary = ref(false);
    const editedSummary = ref('');
    const isEditingNotes = ref(false);
    const editedNotes = ref('');
    const isInlineEditingTitle = ref(false);
    const editedTitle = ref('');
    const isSavingChanges = ref(false);
    const transcriptSearchQuery = ref('');
    const highlightedText = ref('');

    // Methods
    const setTab = (tab) => {
        selectedTab.value = tab;
    };

    const startEditingTranscript = (recording) => {
        isEditingTranscript.value = true;
        editedTranscription.value = recording.transcription || '';
    };

    const cancelEditingTranscript = () => {
        isEditingTranscript.value = false;
        editedTranscription.value = '';
    };

    const saveTranscript = async (recordingId) => {
        isSavingChanges.value = true;

        try {
            const data = await apiRequest(`/api/recordings/${recordingId}/transcript`, {
                method: 'PUT',
                body: JSON.stringify({
                    transcription: editedTranscription.value
                })
            });

            isEditingTranscript.value = false;
            return data.recording;
        } catch (error) {
            throw error;
        } finally {
            isSavingChanges.value = false;
        }
    };

    const startEditingSummary = (recording) => {
        isEditingSummary.value = true;
        editedSummary.value = recording.summary || '';
    };

    const cancelEditingSummary = () => {
        isEditingSummary.value = false;
        editedSummary.value = '';
    };

    const saveSummary = async (recordingId) => {
        isSavingChanges.value = true;

        try {
            const data = await apiRequest(`/api/recordings/${recordingId}/summary`, {
                method: 'PUT',
                body: JSON.stringify({
                    summary: editedSummary.value
                })
            });

            isEditingSummary.value = false;
            return data.recording;
        } catch (error) {
            throw error;
        } finally {
            isSavingChanges.value = false;
        }
    };

    const startEditingNotes = (recording) => {
        isEditingNotes.value = true;
        editedNotes.value = recording.notes || '';
    };

    const cancelEditingNotes = () => {
        isEditingNotes.value = false;
        editedNotes.value = '';
    };

    const saveNotes = async (recordingId) => {
        isSavingChanges.value = true;

        try {
            const data = await apiRequest(`/api/recordings/${recordingId}/notes`, {
                method: 'PUT',
                body: JSON.stringify({
                    notes: editedNotes.value
                })
            });

            isEditingNotes.value = false;
            return data.recording;
        } catch (error) {
            throw error;
        } finally {
            isSavingChanges.value = false;
        }
    };

    const startEditingTitle = (recording) => {
        isInlineEditingTitle.value = true;
        editedTitle.value = recording.title || '';
    };

    const cancelEditingTitle = () => {
        isInlineEditingTitle.value = false;
        editedTitle.value = '';
    };

    const saveTitle = async (recordingId) => {
        isSavingChanges.value = true;

        try {
            const data = await apiRequest(`/api/recordings/${recordingId}`, {
                method: 'PUT',
                body: JSON.stringify({
                    title: editedTitle.value
                })
            });

            isInlineEditingTitle.value = false;
            return data.recording;
        } catch (error) {
            throw error;
        } finally {
            isSavingChanges.value = false;
        }
    };

    const searchInTranscript = (text, query) => {
        if (!query) {
            highlightedText.value = text;
            return text;
        }

        const regex = new RegExp(`(${query})`, 'gi');
        highlightedText.value = text.replace(regex, '<mark>$1</mark>');
        return highlightedText.value;
    };

    const exportTranscript = async (recordingId, format) => {
        try {
            const response = await fetch(`/api/recordings/${recordingId}/export/${format}`);
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `transcript.${format}`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);
        } catch (error) {
            throw error;
        }
    };

    return {
        // State
        selectedTab,
        isEditingTranscript,
        editedTranscription,
        isEditingSummary,
        editedSummary,
        isEditingNotes,
        editedNotes,
        isInlineEditingTitle,
        editedTitle,
        isSavingChanges,
        transcriptSearchQuery,
        highlightedText,

        // Methods
        setTab,
        startEditingTranscript,
        cancelEditingTranscript,
        saveTranscript,
        startEditingSummary,
        cancelEditingSummary,
        saveSummary,
        startEditingNotes,
        cancelEditingNotes,
        saveNotes,
        startEditingTitle,
        cancelEditingTitle,
        saveTitle,
        searchInTranscript,
        exportTranscript
    };
}
