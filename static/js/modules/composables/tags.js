/**
 * Tags Management Composable
 * Handles tag operations for recordings
 */

const { computed } = Vue;

export function useTags({
    recordings,
    availableTags,
    selectedRecording,
    showEditTagsModal,
    editingRecording,
    tagSearchFilter,
    showToast,
    setGlobalError
}) {
    // State (using passed refs from parent)

    // Computed
    const getRecordingTags = (recording) => {
        if (!recording || !recording.tags) return [];
        return recording.tags;
    };

    const getAvailableTagsForRecording = (recording) => {
        if (!recording || !availableTags.value) return [];
        const recordingTagIds = getRecordingTags(recording).map(tag => tag.id);
        return availableTags.value.filter(tag => !recordingTagIds.includes(tag.id));
    };

    const filteredAvailableTagsForModal = computed(() => {
        if (!editingRecording.value) return [];
        const availableTagsForRec = getAvailableTagsForRecording(editingRecording.value);
        if (!tagSearchFilter.value) return availableTagsForRec;

        const filter = tagSearchFilter.value.toLowerCase();
        return availableTagsForRec.filter(tag =>
            tag.name.toLowerCase().includes(filter)
        );
    });

    // Methods
    const editRecordingTags = (recording) => {
        editingRecording.value = recording;
        tagSearchFilter.value = '';
        showEditTagsModal.value = true;
    };

    const closeEditTagsModal = () => {
        showEditTagsModal.value = false;
        editingRecording.value = null;
        tagSearchFilter.value = '';
    };

    const addTagToRecording = async (tagId) => {
        if (!tagId || !editingRecording.value) return;

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

            const response = await fetch(`/api/recordings/${editingRecording.value.id}/tags`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ tag_id: tagId })
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'Failed to add tag');
            }

            // Update local recording data
            const tagToAdd = availableTags.value.find(tag => tag.id == tagId);
            if (tagToAdd) {
                // Check if tag already exists to prevent duplicates
                const tagExists = editingRecording.value.tags?.some(t => t.id === tagToAdd.id);
                if (!tagExists) {
                    if (!editingRecording.value.tags) {
                        editingRecording.value.tags = [];
                    }
                    editingRecording.value.tags.push(tagToAdd);
                }

                // Also update in recordings list (only if different object)
                const recordingInList = recordings.value.find(r => r.id === editingRecording.value.id);
                if (recordingInList && recordingInList !== editingRecording.value) {
                    const tagExistsInList = recordingInList.tags?.some(t => t.id === tagToAdd.id);
                    if (!tagExistsInList) {
                        if (!recordingInList.tags) {
                            recordingInList.tags = [];
                        }
                        recordingInList.tags.push(tagToAdd);
                    }
                }

                // Update selectedRecording if it matches (only if different object)
                if (selectedRecording.value &&
                    selectedRecording.value.id === editingRecording.value.id &&
                    selectedRecording.value !== editingRecording.value) {
                    const tagExistsInSelected = selectedRecording.value.tags?.some(t => t.id === tagToAdd.id);
                    if (!tagExistsInSelected) {
                        if (!selectedRecording.value.tags) {
                            selectedRecording.value.tags = [];
                        }
                        selectedRecording.value.tags.push(tagToAdd);
                    }
                }
            }

            showToast('Tag added successfully', 'fa-check-circle', 2000, 'success');

        } catch (error) {
            console.error('Error adding tag to recording:', error);
            setGlobalError(`Failed to add tag: ${error.message}`);
        }
    };

    const removeTagFromRecording = async (tagId) => {
        if (!editingRecording.value) return;

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

            const response = await fetch(`/api/recordings/${editingRecording.value.id}/tags/${tagId}`, {
                method: 'DELETE',
                headers: {
                    'X-CSRFToken': csrfToken
                }
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'Failed to remove tag');
            }

            // Update local recording data
            editingRecording.value.tags = editingRecording.value.tags.filter(tag => tag.id !== tagId);

            // Also update in recordings list
            const recordingInList = recordings.value.find(r => r.id === editingRecording.value.id);
            if (recordingInList && recordingInList !== editingRecording.value && recordingInList.tags) {
                recordingInList.tags = recordingInList.tags.filter(tag => tag.id !== tagId);
            }

            // Update selectedRecording if it matches
            if (selectedRecording.value && selectedRecording.value.id === editingRecording.value.id && selectedRecording.value.tags) {
                selectedRecording.value.tags = selectedRecording.value.tags.filter(tag => tag.id !== tagId);
            }

            showToast('Tag removed successfully', 'fa-check-circle', 2000, 'success');

        } catch (error) {
            console.error('Error removing tag from recording:', error);
            setGlobalError(`Failed to remove tag: ${error.message}`);
        }
    };

    return {
        // Computed
        filteredAvailableTagsForModal,

        // Methods
        getRecordingTags,
        getAvailableTagsForRecording,
        editRecordingTags,
        closeEditTagsModal,
        addTagToRecording,
        removeTagFromRecording
    };
}
