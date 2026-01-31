/**
 * Bulk Operations Composable
 * Handles bulk API operations for multiple recordings
 */

const { ref, computed } = Vue;

export function useBulkOperations({
    selectedRecordingIds,
    selectedRecordings,
    recordings,
    selectedRecording,
    bulkActionInProgress,
    availableTags,
    availableFolders,
    showToast,
    setGlobalError,
    startReprocessingPoll
}) {
    // Modal state
    const showBulkDeleteModal = ref(false);
    const showBulkTagModal = ref(false);
    const showBulkReprocessModal = ref(false);
    const showBulkFolderModal = ref(false);
    const bulkTagAction = ref('add'); // 'add' or 'remove'
    const bulkTagSelectedId = ref('');
    const bulkReprocessType = ref('summary'); // 'transcription' or 'summary'

    // Get CSRF token
    const getCsrfToken = () => {
        return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
    };

    // Helper to get selected IDs as array
    const getSelectedIds = () => {
        return Array.from(selectedRecordingIds.value);
    };

    // =========================================
    // Bulk Delete
    // =========================================

    const openBulkDeleteModal = () => {
        showBulkDeleteModal.value = true;
    };

    const closeBulkDeleteModal = () => {
        showBulkDeleteModal.value = false;
    };

    const executeBulkDelete = async () => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        bulkActionInProgress.value = true;
        closeBulkDeleteModal();

        try {
            const response = await fetch('/api/recordings/bulk', {
                method: 'DELETE',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({ recording_ids: ids })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to delete recordings');
            }

            // Remove deleted recordings from local state
            const deletedIds = new Set(data.deleted_ids || ids);
            recordings.value = recordings.value.filter(r => !deletedIds.has(r.id));

            // Clear selected recording if it was deleted
            if (selectedRecording.value && deletedIds.has(selectedRecording.value.id)) {
                selectedRecording.value = null;
            }

            // Remove deleted IDs from selection
            deletedIds.forEach(id => selectedRecordingIds.value.delete(id));

            const count = deletedIds.size;
            showToast(`${count} recording${count !== 1 ? 's' : ''} deleted`, 'fa-trash', 3000, 'success');
        } catch (error) {
            console.error('Bulk delete error:', error);
            setGlobalError(`Failed to delete recordings: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    // =========================================
    // Bulk Tag Operations
    // =========================================

    const openBulkTagModal = (action = 'add') => {
        bulkTagAction.value = action;
        bulkTagSelectedId.value = '';
        showBulkTagModal.value = true;
    };

    const closeBulkTagModal = () => {
        showBulkTagModal.value = false;
        bulkTagSelectedId.value = '';
    };

    const executeBulkTag = async () => {
        const ids = getSelectedIds();
        const tagId = bulkTagSelectedId.value;
        const action = bulkTagAction.value;

        // Validate before making API call
        if (ids.length === 0) {
            console.warn('No recordings selected for bulk tag operation');
            return;
        }
        if (!tagId && tagId !== 0) {
            console.warn('No tag selected for bulk tag operation');
            return;
        }

        bulkActionInProgress.value = true;
        closeBulkTagModal();

        try {
            const response = await fetch('/api/recordings/bulk-tags', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    tag_id: parseInt(tagId),
                    action: action
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || `Failed to ${action} tag`);
            }

            // Update local state
            const tag = availableTags.value.find(t => t.id == tagId);
            if (tag) {
                const affectedIds = new Set(data.affected_ids || ids);
                recordings.value.forEach(recording => {
                    if (affectedIds.has(recording.id)) {
                        if (!recording.tags) recording.tags = [];

                        if (action === 'add') {
                            // Add tag if not already present
                            if (!recording.tags.find(t => t.id === tag.id)) {
                                recording.tags.push(tag);
                            }
                        } else {
                            // Remove tag
                            recording.tags = recording.tags.filter(t => t.id !== tag.id);
                        }
                    }
                });

                // Update selected recording if affected
                if (selectedRecording.value && affectedIds.has(selectedRecording.value.id)) {
                    if (!selectedRecording.value.tags) selectedRecording.value.tags = [];

                    if (action === 'add') {
                        if (!selectedRecording.value.tags.find(t => t.id === tag.id)) {
                            selectedRecording.value.tags.push(tag);
                        }
                    } else {
                        selectedRecording.value.tags = selectedRecording.value.tags.filter(t => t.id !== tag.id);
                    }
                }
            }

            const count = data.affected_ids?.length || ids.length;
            const actionText = action === 'add' ? 'added to' : 'removed from';
            showToast(`Tag ${actionText} ${count} recording${count !== 1 ? 's' : ''}`, 'fa-tags', 3000, 'success');
        } catch (error) {
            console.error('Bulk tag error:', error);
            setGlobalError(`Failed to ${action} tag: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    // =========================================
    // Bulk Reprocess
    // =========================================

    const openBulkReprocessModal = () => {
        bulkReprocessType.value = 'summary';
        showBulkReprocessModal.value = true;
    };

    const closeBulkReprocessModal = () => {
        showBulkReprocessModal.value = false;
    };

    const executeBulkReprocess = async () => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        bulkActionInProgress.value = true;
        closeBulkReprocessModal();

        try {
            const response = await fetch('/api/recordings/bulk-reprocess', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    type: bulkReprocessType.value
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to queue reprocessing');
            }

            // Update status for queued recordings
            const queuedIds = new Set(data.queued_ids || ids);
            const newStatus = bulkReprocessType.value === 'transcription' ? 'PROCESSING' : 'SUMMARIZING';

            recordings.value.forEach(recording => {
                if (queuedIds.has(recording.id)) {
                    recording.status = newStatus;
                    // Start polling for each
                    if (startReprocessingPoll) {
                        startReprocessingPoll(recording.id);
                    }
                }
            });

            if (selectedRecording.value && queuedIds.has(selectedRecording.value.id)) {
                selectedRecording.value.status = newStatus;
            }

            const count = queuedIds.size;
            const typeText = bulkReprocessType.value === 'transcription' ? 'Transcription' : 'Summary';
            showToast(`${typeText} reprocessing queued for ${count} recording${count !== 1 ? 's' : ''}`, 'fa-sync-alt', 3000, 'success');
        } catch (error) {
            console.error('Bulk reprocess error:', error);
            setGlobalError(`Failed to queue reprocessing: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    // =========================================
    // Bulk Toggle (Inbox/Highlight)
    // =========================================

    const bulkToggleInbox = async (value = null) => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        // If no value specified, toggle based on majority
        if (value === null) {
            const inboxCount = selectedRecordings.value.filter(r => r.is_inbox).length;
            value = inboxCount < ids.length / 2;
        }

        bulkActionInProgress.value = true;

        try {
            const response = await fetch('/api/recordings/bulk-toggle', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    field: 'inbox',
                    value: value
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to update inbox status');
            }

            // Update local state
            const affectedIds = new Set(data.affected_ids || ids);
            recordings.value.forEach(recording => {
                if (affectedIds.has(recording.id)) {
                    recording.is_inbox = value;
                }
            });

            if (selectedRecording.value && affectedIds.has(selectedRecording.value.id)) {
                selectedRecording.value.is_inbox = value;
            }

            const count = affectedIds.size;
            const actionText = value ? 'added to' : 'removed from';
            showToast(`${count} recording${count !== 1 ? 's' : ''} ${actionText} inbox`, 'fa-inbox', 3000, 'success');
        } catch (error) {
            console.error('Bulk toggle inbox error:', error);
            setGlobalError(`Failed to update inbox status: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    const bulkToggleHighlight = async (value = null) => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        // If no value specified, toggle based on majority
        if (value === null) {
            const highlightCount = selectedRecordings.value.filter(r => r.is_highlighted).length;
            value = highlightCount < ids.length / 2;
        }

        bulkActionInProgress.value = true;

        try {
            const response = await fetch('/api/recordings/bulk-toggle', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    field: 'highlight',
                    value: value
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to update highlight status');
            }

            // Update local state
            const affectedIds = new Set(data.affected_ids || ids);
            recordings.value.forEach(recording => {
                if (affectedIds.has(recording.id)) {
                    recording.is_highlighted = value;
                }
            });

            if (selectedRecording.value && affectedIds.has(selectedRecording.value.id)) {
                selectedRecording.value.is_highlighted = value;
            }

            const count = affectedIds.size;
            const actionText = value ? 'highlighted' : 'unhighlighted';
            showToast(`${count} recording${count !== 1 ? 's' : ''} ${actionText}`, 'fa-star', 3000, 'success');
        } catch (error) {
            console.error('Bulk toggle highlight error:', error);
            setGlobalError(`Failed to update highlight status: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    // =========================================
    // Bulk Folder Assignment
    // =========================================

    const bulkAssignFolder = async (folderId) => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        bulkActionInProgress.value = true;
        showBulkFolderModal.value = false;

        try {
            const response = await fetch('/api/recordings/bulk/folder', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    folder_id: folderId
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to update folders');
            }

            // Update local state
            const folder = folderId ? availableFolders.value.find(f => f.id === folderId) : null;
            recordings.value.forEach(recording => {
                if (ids.includes(recording.id)) {
                    recording.folder_id = folderId;
                    recording.folder = folder;
                }
            });

            // Update selected recording if affected
            if (selectedRecording.value && ids.includes(selectedRecording.value.id)) {
                selectedRecording.value.folder_id = folderId;
                selectedRecording.value.folder = folder;
            }

            // Update folder recording counts
            if (availableFolders.value) {
                availableFolders.value.forEach(f => {
                    const count = recordings.value.filter(r => r.folder_id === f.id).length;
                    f.recording_count = count;
                });
            }

            const count = data.updated_count || ids.length;
            if (folderId) {
                showToast(`${count} recording${count !== 1 ? 's' : ''} moved to "${folder?.name || 'folder'}"`, 'fa-folder', 3000, 'success');
            } else {
                showToast(`${count} recording${count !== 1 ? 's' : ''} removed from folder`, 'fa-folder-minus', 3000, 'success');
            }
        } catch (error) {
            console.error('Bulk folder assignment error:', error);
            setGlobalError(`Failed to update folders: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    return {
        // Modal state
        showBulkDeleteModal,
        showBulkTagModal,
        showBulkReprocessModal,
        showBulkFolderModal,
        bulkTagAction,
        bulkTagSelectedId,
        bulkReprocessType,

        // Bulk Delete
        openBulkDeleteModal,
        closeBulkDeleteModal,
        executeBulkDelete,

        // Bulk Tag
        openBulkTagModal,
        closeBulkTagModal,
        executeBulkTag,

        // Bulk Reprocess
        openBulkReprocessModal,
        closeBulkReprocessModal,
        executeBulkReprocess,

        // Bulk Toggle
        bulkToggleInbox,
        bulkToggleHighlight,

        // Bulk Folder
        bulkAssignFolder
    };
}
