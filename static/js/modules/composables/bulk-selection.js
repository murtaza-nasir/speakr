/**
 * Bulk Selection Composable
 * Handles multi-select functionality for recordings
 */

const { computed } = Vue;

export function useBulkSelection({
    selectionMode,
    selectedRecordingIds,
    recordings,
    selectedRecording,
    currentView
}) {
    // Computed
    const selectedCount = computed(() => selectedRecordingIds.value.size);

    const selectedRecordings = computed(() => {
        return recordings.value.filter(r => selectedRecordingIds.value.has(r.id));
    });

    const allVisibleSelected = computed(() => {
        if (recordings.value.length === 0) return false;
        return recordings.value.every(r => selectedRecordingIds.value.has(r.id));
    });

    const isSelected = (id) => {
        return selectedRecordingIds.value.has(id);
    };

    // Methods
    const enterSelectionMode = () => {
        selectionMode.value = true;
        selectedRecordingIds.value = new Set();
    };

    const exitSelectionMode = () => {
        selectionMode.value = false;
        selectedRecordingIds.value = new Set();
    };

    const toggleSelection = (id) => {
        const newSet = new Set(selectedRecordingIds.value);
        if (newSet.has(id)) {
            newSet.delete(id);
        } else {
            newSet.add(id);
        }
        selectedRecordingIds.value = newSet;
    };

    const selectAll = () => {
        const newSet = new Set();
        recordings.value.forEach(r => newSet.add(r.id));
        selectedRecordingIds.value = newSet;
    };

    const clearSelection = () => {
        selectedRecordingIds.value = new Set();
    };

    // Keyboard handler for selection mode
    const handleSelectionKeyboard = (event) => {
        if (!selectionMode.value) return;

        // Escape to exit selection mode
        if (event.key === 'Escape') {
            exitSelectionMode();
            event.preventDefault();
        }

        // Ctrl/Cmd + A to select all
        if ((event.ctrlKey || event.metaKey) && event.key === 'a') {
            // Only if not in an input field
            if (document.activeElement.tagName !== 'INPUT' &&
                document.activeElement.tagName !== 'TEXTAREA' &&
                !document.activeElement.isContentEditable) {
                event.preventDefault();
                selectAll();
            }
        }
    };

    // Initialize keyboard listener
    const initSelectionKeyboardListeners = () => {
        document.addEventListener('keydown', handleSelectionKeyboard);
    };

    const cleanupSelectionKeyboardListeners = () => {
        document.removeEventListener('keydown', handleSelectionKeyboard);
    };

    return {
        // Computed
        selectedCount,
        selectedRecordings,
        allVisibleSelected,

        // Methods
        isSelected,
        enterSelectionMode,
        exitSelectionMode,
        toggleSelection,
        selectAll,
        clearSelection,

        // Keyboard
        initSelectionKeyboardListeners,
        cleanupSelectionKeyboardListeners
    };
}
