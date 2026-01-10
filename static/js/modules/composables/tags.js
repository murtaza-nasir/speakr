/**
 * Tags Management Composable
 * Handles tag operations for recordings
 */

const { computed, ref } = Vue;

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

    // --- Tag Drag-and-Drop State for Edit Modal ---
    const modalDraggedTagIndex = ref(null);
    const modalDragOverTagIndex = ref(null);

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

    // --- Modal Tag Reordering ---

    const reorderModalTags = async (fromIndex, toIndex) => {
        if (!editingRecording.value || !editingRecording.value.tags) return;

        // Reorder locally first for immediate visual feedback
        const tags = [...editingRecording.value.tags];
        const [removed] = tags.splice(fromIndex, 1);
        tags.splice(toIndex, 0, removed);
        editingRecording.value.tags = tags;

        // Update in recordings list
        const recordingInList = recordings.value.find(r => r.id === editingRecording.value.id);
        if (recordingInList && recordingInList !== editingRecording.value) {
            recordingInList.tags = [...tags];
        }

        // Update selectedRecording if it matches
        if (selectedRecording.value && selectedRecording.value.id === editingRecording.value.id) {
            selectedRecording.value.tags = [...tags];
        }

        // Persist to backend
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const tagIds = tags.map(t => t.id);

            const response = await fetch(`/api/recordings/${editingRecording.value.id}/tags/reorder`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ tag_ids: tagIds })
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'Failed to reorder tags');
            }

            showToast('Tags reordered', 'fa-arrows-alt', 1500, 'success');

        } catch (error) {
            console.error('Error reordering tags:', error);
            setGlobalError(`Failed to save tag order: ${error.message}`);
        }
    };

    // === MOUSE DRAG HANDLERS (Modal) ===
    const handleModalTagDragStart = (index, event) => {
        modalDraggedTagIndex.value = index;
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', index.toString());
    };

    const handleModalTagDragOver = (index, event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
        modalDragOverTagIndex.value = index;
    };

    const handleModalTagDrop = (targetIndex, event) => {
        event.preventDefault();
        if (modalDraggedTagIndex.value !== null && modalDraggedTagIndex.value !== targetIndex) {
            reorderModalTags(modalDraggedTagIndex.value, targetIndex);
        }
        modalDraggedTagIndex.value = null;
        modalDragOverTagIndex.value = null;
    };

    const handleModalTagDragEnd = () => {
        modalDraggedTagIndex.value = null;
        modalDragOverTagIndex.value = null;
    };

    // === TOUCH HANDLERS (Modal - Mobile) ===
    let modalTouchStartIndex = null;

    const handleModalTagTouchStart = (index, event) => {
        modalTouchStartIndex = index;
        modalDraggedTagIndex.value = index;
    };

    const handleModalTagTouchMove = (event) => {
        if (modalTouchStartIndex === null) return;
        event.preventDefault();

        const touch = event.touches[0];
        const elementBelow = document.elementFromPoint(touch.clientX, touch.clientY);
        const tagElement = elementBelow?.closest('[data-modal-tag-index]');

        if (tagElement) {
            const targetIndex = parseInt(tagElement.dataset.modalTagIndex);
            modalDragOverTagIndex.value = targetIndex;
        }
    };

    const handleModalTagTouchEnd = () => {
        if (modalTouchStartIndex !== null && modalDragOverTagIndex.value !== null &&
            modalTouchStartIndex !== modalDragOverTagIndex.value) {
            reorderModalTags(modalTouchStartIndex, modalDragOverTagIndex.value);
        }
        modalTouchStartIndex = null;
        modalDraggedTagIndex.value = null;
        modalDragOverTagIndex.value = null;
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
        removeTagFromRecording,

        // Modal Tag Drag-and-Drop
        modalDraggedTagIndex,
        modalDragOverTagIndex,
        handleModalTagDragStart,
        handleModalTagDragOver,
        handleModalTagDrop,
        handleModalTagDragEnd,
        handleModalTagTouchStart,
        handleModalTagTouchMove,
        handleModalTagTouchEnd
    };
}
