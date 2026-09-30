/**
 * Bulk Selection Composable
 * Handles multi-select functionality for recordings
 */

import { applyListClick } from '../utils/selection-range.js';

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
    // The last recording clicked in selection mode; Shift-click selects
    // the span from here to the clicked recording.
    let selectionAnchorId = null;

    const enterSelectionMode = () => {
        selectionMode.value = true;
        selectedRecordingIds.value = new Set();
        selectionAnchorId = null;
    };

    const exitSelectionMode = () => {
        selectionMode.value = false;
        selectedRecordingIds.value = new Set();
        selectionAnchorId = null;
    };

    /**
     * A click on a recording in the sidebar list. Ctrl/Cmd-click toggles one
     * recording and Shift-click selects a span, entering selection mode when
     * needed; in selection mode a plain click toggles as before. Returns
     * false when the click should open the recording instead.
     * orderedIds: the recording ids in the order the list shows them.
     */
    const handleRecordingClick = (recording, event, orderedIds) => {
        const result = applyListClick({
            selected: selectedRecordingIds.value,
            orderedIds: orderedIds || [],
            anchorId: selectionAnchorId,
            openId: selectedRecording.value ? selectedRecording.value.id : null,
            id: recording.id,
            ctrl: !!(event && (event.ctrlKey || event.metaKey)),
            shift: !!(event && event.shiftKey),
            selectionMode: selectionMode.value,
        });
        if (!result) return false;
        if (result.enterMode) selectionMode.value = true;
        selectedRecordingIds.value = result.selected;
        selectionAnchorId = result.anchorId;
        return true;
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
        handleRecordingClick,
        selectAll,
        clearSelection,

        // Keyboard
        initSelectionKeyboardListeners,
        cleanupSelectionKeyboardListeners
    };
}
