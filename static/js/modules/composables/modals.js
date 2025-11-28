/**
 * Modal management composable
 * Handles opening, closing, and saving modal dialogs
 */

export function useModals(state, utils) {
    const {
        showEditModal, showDeleteModal, showEditTagsModal,
        showReprocessModal, showResetModal, showShareModal,
        showSharesListModal, showTextEditorModal, showAsrEditorModal,
        showEditSpeakersModal, showAddSpeakerModal, showEditTextModal,
        showShareDeleteModal, showUnifiedShareModal, showColorSchemeModal,
        showSystemAudioHelpModal, editingRecording, recordingToDelete, recordingToReset,
        selectedRecording, recordings, selectedNewTagId, tagSearchFilter,
        availableTags, currentView, totalRecordings, toasts, uploadQueue, allJobs
    } = state;

    const { showToast, setGlobalError } = utils;

    // =========================================
    // Edit Recording Modal
    // =========================================

    const openEditModal = (recording) => {
        editingRecording.value = { ...recording };
        showEditModal.value = true;
    };

    const cancelEdit = () => {
        showEditModal.value = false;
        editingRecording.value = null;
    };

    const saveEdit = async () => {
        if (!editingRecording.value) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/api/recordings/${editingRecording.value.id}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    title: editingRecording.value.title,
                    participants: editingRecording.value.participants,
                    meeting_date: editingRecording.value.meeting_date,
                    notes: editingRecording.value.notes
                })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to save changes');

            // Update local data
            const index = recordings.value.findIndex(r => r.id === editingRecording.value.id);
            if (index !== -1) {
                recordings.value[index] = { ...recordings.value[index], ...editingRecording.value };
            }
            if (selectedRecording.value && selectedRecording.value.id === editingRecording.value.id) {
                selectedRecording.value = { ...selectedRecording.value, ...editingRecording.value };
            }

            showToast('Recording updated!', 'fa-check-circle');
            showEditModal.value = false;
            editingRecording.value = null;
        } catch (error) {
            setGlobalError(`Failed to save changes: ${error.message}`);
        }
    };

    // =========================================
    // Delete Recording Modal
    // =========================================

    const confirmDelete = (recording) => {
        recordingToDelete.value = recording;
        showDeleteModal.value = true;
    };

    const cancelDelete = () => {
        showDeleteModal.value = false;
        recordingToDelete.value = null;
    };

    const deleteRecording = async () => {
        if (!recordingToDelete.value) return;
        const deletedId = recordingToDelete.value.id;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${deletedId}`, {
                method: 'DELETE',
                headers: { 'X-CSRFToken': csrfToken }
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to delete recording');

            // Remove from recordings list
            recordings.value = recordings.value.filter(r => r.id !== deletedId);
            totalRecordings.value--;

            // Remove from upload queue if present (frontend tracking)
            if (uploadQueue?.value) {
                uploadQueue.value = uploadQueue.value.filter(item => item.recordingId !== deletedId);
            }

            // Remove from backend job queue if present (backend processing tracking)
            // This is critical - without this, deleted recordings remain in processing queue
            if (allJobs?.value) {
                allJobs.value = allJobs.value.filter(job => job.recording_id !== deletedId);
            }

            // Clear selected recording if it's the one being deleted
            if (selectedRecording.value?.id === deletedId) {
                selectedRecording.value = null;
                currentView.value = 'upload';
            }

            showToast('Recording deleted.', 'fa-trash');
            showDeleteModal.value = false;
            recordingToDelete.value = null;
        } catch (error) {
            setGlobalError(`Failed to delete recording: ${error.message}`);
        }
    };

    // =========================================
    // Archive Recording
    // =========================================

    const archiveRecording = async (recording) => {
        if (!recording) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/api/recordings/${recording.id}/archive`, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken }
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to archive recording');

            recording.is_archived = true;
            recording.audio_deleted_at = data.audio_deleted_at;

            // Update in recordings list
            const index = recordings.value.findIndex(r => r.id === recording.id);
            if (index !== -1) {
                recordings.value[index].is_archived = true;
                recordings.value[index].audio_deleted_at = data.audio_deleted_at;
            }

            showToast('Recording archived (audio deleted)', 'fa-archive');
        } catch (error) {
            setGlobalError(`Failed to archive recording: ${error.message}`);
        }
    };

    // =========================================
    // Edit Tags Modal
    // =========================================

    const openEditTagsModal = () => {
        selectedNewTagId.value = '';
        tagSearchFilter.value = '';
        showEditTagsModal.value = true;
    };

    const closeEditTagsModal = () => {
        showEditTagsModal.value = false;
    };

    const addTagToRecording = async (tagId) => {
        if (!selectedRecording.value || !tagId) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/api/recordings/${selectedRecording.value.id}/tags`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ tag_id: tagId })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to add tag');

            // Find the tag object
            const tag = availableTags.value.find(t => t.id === tagId);
            if (tag) {
                if (!selectedRecording.value.tags) {
                    selectedRecording.value.tags = [];
                }
                selectedRecording.value.tags.push(tag);
            }

            // Update in recordings list
            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1 && tag) {
                if (!recordings.value[index].tags) {
                    recordings.value[index].tags = [];
                }
                recordings.value[index].tags.push(tag);
            }

            selectedNewTagId.value = '';
            showToast('Tag added!', 'fa-tag');
        } catch (error) {
            setGlobalError(`Failed to add tag: ${error.message}`);
        }
    };

    const removeTagFromRecording = async (tagId) => {
        if (!selectedRecording.value || !tagId) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/api/recordings/${selectedRecording.value.id}/tags/${tagId}`, {
                method: 'DELETE',
                headers: { 'X-CSRFToken': csrfToken }
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to remove tag');

            // Remove from selected recording
            if (selectedRecording.value.tags) {
                selectedRecording.value.tags = selectedRecording.value.tags.filter(t => t.id !== tagId);
            }

            // Update in recordings list
            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1 && recordings.value[index].tags) {
                recordings.value[index].tags = recordings.value[index].tags.filter(t => t.id !== tagId);
            }

            showToast('Tag removed!', 'fa-tag');
        } catch (error) {
            setGlobalError(`Failed to remove tag: ${error.message}`);
        }
    };

    // =========================================
    // Reset Modal
    // =========================================

    const openResetModal = (recording) => {
        recordingToReset.value = recording;
        showResetModal.value = true;
    };

    const cancelReset = () => {
        showResetModal.value = false;
        recordingToReset.value = null;
    };

    const resetRecording = async () => {
        if (!recordingToReset.value) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${recordingToReset.value.id}/reset_status`, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken }
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to reset recording');

            // Update recording status
            const index = recordings.value.findIndex(r => r.id === recordingToReset.value.id);
            if (index !== -1) {
                recordings.value[index].status = 'PENDING';
                recordings.value[index].transcription = '';
                recordings.value[index].summary = '';
            }

            if (selectedRecording.value?.id === recordingToReset.value.id) {
                selectedRecording.value.status = 'PENDING';
                selectedRecording.value.transcription = '';
                selectedRecording.value.summary = '';
            }

            showToast('Recording reset for reprocessing.', 'fa-redo');
            showResetModal.value = false;
            recordingToReset.value = null;
        } catch (error) {
            setGlobalError(`Failed to reset recording: ${error.message}`);
        }
    };

    // =========================================
    // System Audio Help Modal
    // =========================================

    const openSystemAudioHelpModal = () => {
        showSystemAudioHelpModal.value = true;
    };

    const closeSystemAudioHelpModal = () => {
        showSystemAudioHelpModal.value = false;
    };

    // =========================================
    // Toast Management
    // =========================================

    const dismissToast = (id) => {
        toasts.value = toasts.value.filter(t => t.id !== id);
    };

    // Aliases for template compatibility
    const editRecording = openEditModal;
    const editRecordingTags = openEditTagsModal;

    return {
        // Edit modal
        openEditModal,
        editRecording,
        cancelEdit,
        saveEdit,

        // Delete modal
        confirmDelete,
        cancelDelete,
        deleteRecording,

        // Archive
        archiveRecording,

        // Tags modal
        openEditTagsModal,
        editRecordingTags,
        closeEditTagsModal,
        addTagToRecording,
        removeTagFromRecording,

        // Reset modal
        openResetModal,
        cancelReset,
        resetRecording,

        // System audio help
        openSystemAudioHelpModal,
        closeSystemAudioHelpModal,

        // Toast
        dismissToast
    };
}
