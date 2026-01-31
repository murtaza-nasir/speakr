/**
 * Folders Management Composable
 * Handles folder operations for recordings
 */

const { computed, ref } = Vue;

export function useFolders({
    recordings,
    availableFolders,
    selectedRecording,
    showToast,
    setGlobalError
}) {
    // Computed
    const getRecordingFolder = (recording) => {
        if (!recording || !recording.folder) return null;
        return recording.folder;
    };

    const getAvailableFoldersForRecording = () => {
        if (!availableFolders.value) return [];
        return availableFolders.value;
    };

    // Methods
    const assignFolderToRecording = async (recordingId, folderId) => {
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

            const response = await fetch(`/api/recordings/${recordingId}/folder`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ folder_id: folderId || null })
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'Failed to update folder');
            }

            const updatedRecording = await response.json();

            // Update local recording data
            const recordingInList = recordings.value.find(r => r.id === recordingId);
            if (recordingInList) {
                recordingInList.folder_id = updatedRecording.folder_id;
                recordingInList.folder = updatedRecording.folder;
            }

            // Update selectedRecording if it matches
            if (selectedRecording.value && selectedRecording.value.id === recordingId) {
                selectedRecording.value.folder_id = updatedRecording.folder_id;
                selectedRecording.value.folder = updatedRecording.folder;
            }

            // Update folder recording counts
            if (availableFolders.value) {
                availableFolders.value.forEach(f => {
                    const count = recordings.value.filter(r => r.folder_id === f.id).length;
                    f.recording_count = count;
                });
            }

            if (folderId) {
                const folder = availableFolders.value?.find(f => f.id === folderId);
                showToast(`Moved to folder "${folder?.name || 'Unknown'}"`, 'fa-folder', 2000, 'success');
            } else {
                showToast('Removed from folder', 'fa-folder-minus', 2000, 'success');
            }

            return updatedRecording;

        } catch (error) {
            console.error('Error updating folder:', error);
            setGlobalError(`Failed to update folder: ${error.message}`);
            return null;
        }
    };

    const removeRecordingFromFolder = async (recordingId) => {
        return assignFolderToRecording(recordingId, null);
    };

    const bulkAssignFolder = async (recordingIds, folderId) => {
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

            const response = await fetch('/api/recordings/bulk/folder', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    recording_ids: recordingIds,
                    folder_id: folderId || null
                })
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'Failed to update folders');
            }

            const result = await response.json();

            // Update local recording data
            recordingIds.forEach(id => {
                const recording = recordings.value.find(r => r.id === id);
                if (recording) {
                    recording.folder_id = folderId || null;
                    recording.folder = folderId ? availableFolders.value?.find(f => f.id === folderId) : null;
                }
            });

            // Update folder recording counts
            if (availableFolders.value) {
                availableFolders.value.forEach(f => {
                    const count = recordings.value.filter(r => r.folder_id === f.id).length;
                    f.recording_count = count;
                });
            }

            if (folderId) {
                const folder = availableFolders.value?.find(f => f.id === folderId);
                showToast(`${result.updated_count} recording(s) moved to "${folder?.name || 'Unknown'}"`, 'fa-folder', 2000, 'success');
            } else {
                showToast(`${result.updated_count} recording(s) removed from folder`, 'fa-folder-minus', 2000, 'success');
            }

            return result;

        } catch (error) {
            console.error('Error bulk updating folders:', error);
            setGlobalError(`Failed to update folders: ${error.message}`);
            return null;
        }
    };

    return {
        // Methods
        getRecordingFolder,
        getAvailableFoldersForRecording,
        assignFolderToRecording,
        removeRecordingFromFolder,
        bulkAssignFolder
    };
}
