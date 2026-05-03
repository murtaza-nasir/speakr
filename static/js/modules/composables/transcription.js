/**
 * Transcription editing composable
 * Handles ASR editor, text editor, and segment management
 */

// Module-scoped scroll-position memory for the ASR editor. Keyed by recording
// id so users editing a long transcript can close and reopen the modal within
// the same session and land back where they left off. Cleared implicitly when
// the page reloads.
const _asrEditorScrollMemory = new Map();

// Pending segment index to scroll to when the editor next opens. Used by the
// double-click-on-simple-view-segment path. null means "restore last scroll".
let _asrEditorPendingScrollIndex = null;

export function useTranscription(state, utils) {
    const {
        showTextEditorModal, showAsrEditorModal, selectedRecording,
        editingTranscriptionContent, editingSegments, availableSpeakers,
        recordings, dropdownPositions, openAsrDropdownIndex,
        asrEditorRef, asrEditorSaveFlash, editorAutosave
    } = state;

    const { showToast, setGlobalError, nextTick } = utils;

    // =========================================
    // Text Editor Modal
    // =========================================

    const openTranscriptionEditor = () => {
        if (!selectedRecording.value || !selectedRecording.value.transcription) {
            return;
        }

        // Check if transcription is JSON (ASR format)
        try {
            const parsed = JSON.parse(selectedRecording.value.transcription);
            if (Array.isArray(parsed)) {
                openAsrEditorModal();
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
        editingTranscriptionContent.value = selectedRecording.value.transcription || '';
        showTextEditorModal.value = true;
    };

    const closeTextEditorModal = () => {
        showTextEditorModal.value = false;
        editingTranscriptionContent.value = '';
    };

    const saveTranscription = async () => {
        if (!selectedRecording.value) return;
        await saveTranscriptionContent(editingTranscriptionContent.value);
        closeTextEditorModal();
    };

    // =========================================
    // ASR Editor Modal
    // =========================================

    // Helper to pause outer audio player when opening modals with their own player
    const pauseOuterAudioPlayer = () => {
        const outerAudio = document.querySelector('#rightMainColumn audio') || document.querySelector('#rightMainColumn video') ||
                          document.querySelector('.detail-view audio:not(.fixed audio)') || document.querySelector('.detail-view video:not(.fixed video)');
        if (outerAudio && !outerAudio.paused) {
            outerAudio.pause();
        }
    };

    const openAsrEditorModal = async () => {
        if (!selectedRecording.value) return;

        // Pause outer audio player to avoid conflicts with modal's player
        pauseOuterAudioPlayer();

        try {
            const segments = JSON.parse(selectedRecording.value.transcription);

            // Populate available speakers from THIS recording only
            const speakersInTranscript = [...new Set(segments.map(s => s.speaker))].sort();
            availableSpeakers.value = speakersInTranscript;

            editingSegments.value = segments.map((s, i) => ({
                ...s,
                id: i,
                showSuggestions: false,
                filteredSpeakers: [...speakersInTranscript]
            }));

            showAsrEditorModal.value = true;

            // Reset autosave state for the new editing session.
            _resetAutosave();
            _primeAutosaveAfterOpen();

            // Reset virtual scroll state for fresh modal render. After it
            // initialises, either scroll to a specific segment (when the
            // modal was opened from a double-click on a simple-view row) or
            // restore the last scroll position for this recording.
            if (utils.resetAsrEditorScroll) {
                utils.resetAsrEditorScroll();
            }
            const recordingId = selectedRecording.value.id;
            const targetIndex = _asrEditorPendingScrollIndex;
            _asrEditorPendingScrollIndex = null;
            await nextTick();
            // requestAnimationFrame defers past the virtualScroll's own
            // post-mount initialisation tick.
            requestAnimationFrame(() => {
                if (targetIndex != null) {
                    if (utils.scrollAsrEditorToIndex) {
                        utils.scrollAsrEditorToIndex(targetIndex);
                    }
                } else {
                    const saved = _asrEditorScrollMemory.get(recordingId);
                    if (saved != null && utils.setAsrEditorScrollTop) {
                        utils.setAsrEditorScrollTop(saved);
                    }
                }
            });
        } catch (e) {
            console.error("Could not parse transcription as JSON for ASR editor:", e);
            setGlobalError("This transcription is not in the correct format for the ASR editor.");
        }
    };

    // Open the editor and scroll to a specific segment index. Used by the
    // double-click-on-simple-view-row affordance.
    const openAsrEditorAtSegment = (segmentIndex) => {
        _asrEditorPendingScrollIndex = segmentIndex;
        return openAsrEditorModal();
    };

    const closeAsrEditorModal = () => {
        // Cancel any pending autosave so we don't issue a write after close.
        _resetAutosave();

        // Save scroll position so reopening the same recording within this
        // session lands the user back where they were.
        if (selectedRecording.value && asrEditorRef && asrEditorRef.value) {
            _asrEditorScrollMemory.set(
                selectedRecording.value.id,
                asrEditorRef.value.scrollTop
            );
        }

        // Pause any playing modal audio before closing
        const modalAudio = document.querySelector('.fixed.z-50 audio') || document.querySelector('.fixed.z-50 video');
        if (modalAudio) {
            modalAudio.pause();
        }
        // Reset modal audio state (keep main player independent)
        if (utils.resetModalAudioState) {
            utils.resetModalAudioState();
        }

        showAsrEditorModal.value = false;
        editingSegments.value = [];
    };

    const saveAsrTranscription = async (keepOpen = false) => {
        if (!selectedRecording.value) return;

        // Remove extra UI fields and save the rest
        const contentToSave = JSON.stringify(editingSegments.value.map(({ id, showSuggestions, filteredSpeakers, ...rest }) => rest));

        await saveTranscriptionContent(contentToSave);

        if (keepOpen) {
            // Briefly flash a "Saved" indicator so the user knows the write
            // succeeded without the modal closing.
            if (asrEditorSaveFlash) {
                asrEditorSaveFlash.value = true;
                setTimeout(() => { asrEditorSaveFlash.value = false; }, 1800);
            }
        } else {
            closeAsrEditorModal();
        }
    };

    // Ctrl+S / Cmd+S handler -- saves without closing while the editor modal
    // is open. preventDefault stops the browser's "save page" dialog.
    const handleAsrEditorKeydown = (event) => {
        if (!showAsrEditorModal.value) return;
        const isSaveShortcut = (event.ctrlKey || event.metaKey) && (event.key === 's' || event.key === 'S');
        if (isSaveShortcut) {
            event.preventDefault();
            saveAsrTranscription(true);
        }
    };
    if (typeof window !== 'undefined') {
        window.addEventListener('keydown', handleAsrEditorKeydown);
    }

    // Autosave -- when the user has opted in via the Preferences tab, debounce
    // a save 2s after the last edit. The debounce prevents thrashing the API
    // on every keystroke. The "primed" flag suppresses the initial firing
    // when segments are first hydrated on modal open.
    let _autosaveTimer = null;
    let _autosavePrimed = false;

    const _resetAutosave = () => {
        if (_autosaveTimer) {
            clearTimeout(_autosaveTimer);
            _autosaveTimer = null;
        }
        _autosavePrimed = false;
    };

    const _primeAutosaveAfterOpen = () => {
        // Defer priming until after the modal hydration tick so we don't
        // mistake the initial editingSegments assignment for a user edit.
        setTimeout(() => { _autosavePrimed = true; }, 500);
    };

    if (typeof Vue !== 'undefined' && Vue.watch) {
        Vue.watch(
            editingSegments,
            () => {
                if (!showAsrEditorModal.value) return;
                if (!editorAutosave || !editorAutosave.value) return;
                if (!_autosavePrimed) return;
                if (_autosaveTimer) clearTimeout(_autosaveTimer);
                _autosaveTimer = setTimeout(() => {
                    _autosaveTimer = null;
                    saveAsrTranscription(true);
                }, 2000);
            },
            { deep: true }
        );
    }

    // =========================================
    // Segment Management
    // =========================================

    const adjustTime = (index, field, amount) => {
        if (editingSegments.value[index]) {
            editingSegments.value[index][field] = Math.max(0,
                editingSegments.value[index][field] + amount
            );
        }
    };

    const filterSpeakerSuggestions = (index) => {
        const segment = editingSegments.value[index];
        if (segment) {
            const query = segment.speaker?.toLowerCase() || '';
            if (query === '') {
                segment.filteredSpeakers = [...availableSpeakers.value];
            } else {
                segment.filteredSpeakers = availableSpeakers.value.filter(
                    speaker => speaker.toLowerCase().includes(query)
                );
            }
        }
    };

    // O(1) dropdown management using single ref instead of O(n) forEach
    const openSpeakerSuggestions = (index) => {
        if (editingSegments.value[index]) {
            // Simply set the open index - O(1) instead of O(n) forEach
            openAsrDropdownIndex.value = index;
            filterSpeakerSuggestions(index);
            updateDropdownPosition(index);
        }
    };

    const closeSpeakerSuggestions = (index) => {
        // Only close if this index is currently open
        if (openAsrDropdownIndex.value === index) {
            openAsrDropdownIndex.value = null;
        }
    };

    const closeAllSpeakerSuggestions = () => {
        // O(1) instead of O(n) - just set to null
        openAsrDropdownIndex.value = null;
    };

    // Helper to check if a dropdown is open (for template v-if)
    const isDropdownOpen = (index) => {
        return openAsrDropdownIndex.value === index;
    };

    const getDropdownPosition = (index) => {
        const pos = dropdownPositions.value[index];
        if (pos) {
            const style = {
                left: pos.left + 'px',
                width: pos.width + 'px'
            };

            // When opening upward, anchor from bottom so dropdown grows upward
            if (pos.openUpward) {
                style.bottom = pos.bottom + 'px';
                style.top = 'auto';
            } else {
                style.top = pos.top + 'px';
                style.bottom = 'auto';
            }

            // Apply calculated max height
            if (pos.maxHeight) {
                style.maxHeight = pos.maxHeight + 'px';
            }
            return style;
        }
        return { top: '0px', left: '0px' };
    };

    const updateDropdownPosition = (index) => {
        nextTick(() => {
            // Find row by data attribute to work correctly with virtual scrolling
            const row = document.querySelector(`.asr-editor-table tbody tr[data-segment-index="${index}"]`);
            if (row) {
                const cell = row.querySelector('td:first-child');
                if (cell) {
                    const rect = cell.getBoundingClientRect();
                    const viewportHeight = window.innerHeight;

                    // Calculate available space above and below
                    const spaceBelow = viewportHeight - rect.bottom - 10;
                    const spaceAbove = rect.top - 10;

                    // Determine max height based on available space (cap at 192px which is max-h-48)
                    const maxDropdownHeight = 192;

                    let top, bottom, openUpward, maxHeight;

                    if (spaceBelow >= maxDropdownHeight || spaceBelow >= spaceAbove) {
                        // Open downward
                        top = rect.bottom + 2;
                        bottom = null;
                        openUpward = false;
                        maxHeight = Math.min(spaceBelow, maxDropdownHeight);
                    } else {
                        // Open upward - anchor from bottom so dropdown grows upward
                        openUpward = true;
                        maxHeight = Math.min(spaceAbove, maxDropdownHeight);
                        // Bottom is distance from viewport bottom to the top of the cell
                        bottom = viewportHeight - rect.top + 2;
                        top = null;
                    }

                    dropdownPositions.value[index] = {
                        top: top,
                        bottom: bottom,
                        left: rect.left,
                        width: rect.width,
                        openUpward: openUpward,
                        maxHeight: maxHeight
                    };
                }
            }
        });
    };

    const selectSpeaker = (index, speaker) => {
        if (editingSegments.value[index]) {
            editingSegments.value[index].speaker = speaker;
            closeSpeakerSuggestions(index);
        }
    };

    const addSegment = () => {
        const lastSegment = editingSegments.value[editingSegments.value.length - 1];
        const newStart = lastSegment ? lastSegment.end_time : 0;

        editingSegments.value.push({
            speaker: availableSpeakers.value[0] || 'Speaker 1',
            start_time: newStart,
            end_time: newStart + 5,
            sentence: '',
            id: editingSegments.value.length,
            showSuggestions: false,
            filteredSpeakers: [...availableSpeakers.value]
        });
    };

    const removeSegment = (index) => {
        editingSegments.value.splice(index, 1);
        // Re-index segments
        editingSegments.value.forEach((seg, i) => {
            seg.id = i;
        });
    };

    const addSegmentBelow = (index) => {
        const currentSegment = editingSegments.value[index];
        const nextSegment = editingSegments.value[index + 1];

        const newStart = currentSegment.end_time;
        const newEnd = nextSegment ? nextSegment.start_time : newStart + 5;

        editingSegments.value.splice(index + 1, 0, {
            speaker: currentSegment.speaker,
            start_time: newStart,
            end_time: newEnd,
            sentence: '',
            id: index + 1,
            showSuggestions: false,
            filteredSpeakers: [...availableSpeakers.value]
        });

        // Re-index segments
        editingSegments.value.forEach((seg, i) => {
            seg.id = i;
        });
    };

    const seekToSegmentTime = (time) => {
        // Find audio elements and use the one in a visible modal (z-50)
        const mediaElements = document.querySelectorAll('.fixed.z-50 audio, .fixed.z-50 video');
        const audioElement = mediaElements.length > 0 ? mediaElements[mediaElements.length - 1] : null;
        if (audioElement) {
            audioElement.currentTime = time;
            audioElement.play();
        }
    };

    const autoResizeTextarea = (event) => {
        const textarea = event.target;
        textarea.style.height = 'auto';
        textarea.style.height = textarea.scrollHeight + 'px';
    };

    // =========================================
    // Save Transcription Content
    // =========================================

    const saveTranscriptionContent = async (content) => {
        if (!selectedRecording.value) return;

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${selectedRecording.value.id}/update_transcription`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ transcription: content })
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to update transcription');

            // Update recording
            selectedRecording.value.transcription = content;

            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1) {
                recordings.value[index].transcription = content;
            }

            showToast('Transcription updated successfully!', 'fa-check-circle');
        } catch (error) {
            setGlobalError(`Failed to save transcription: ${error.message}`);
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
            if (!response.ok) throw new Error(data.error || 'Failed to update summary');

            // Update recording
            selectedRecording.value.summary = summary;

            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1) {
                recordings.value[index].summary = summary;
            }

            showToast('Summary saved!', 'fa-check-circle');
        } catch (error) {
            setGlobalError(`Failed to save summary: ${error.message}`);
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
            showToast('Notes saved (in browser only)', 'fa-check-circle');
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
            if (!response.ok) throw new Error(data.error || 'Failed to update notes');

            // Update recording
            selectedRecording.value.notes = notes;

            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1) {
                recordings.value[index].notes = notes;
            }

            showToast('Notes saved!', 'fa-check-circle');
        } catch (error) {
            setGlobalError(`Failed to save notes: ${error.message}`);
        }
    };

    return {
        // Text editor
        openTranscriptionEditor,
        openTextEditorModal,
        closeTextEditorModal,
        saveTranscription,

        // ASR editor
        openAsrEditorModal,
        openAsrEditorAtSegment,
        closeAsrEditorModal,
        saveAsrTranscription,

        // Segment management
        adjustTime,
        filterSpeakerSuggestions,
        openSpeakerSuggestions,
        closeSpeakerSuggestions,
        closeAllSpeakerSuggestions,
        isDropdownOpen,
        getDropdownPosition,
        updateDropdownPosition,
        selectSpeaker,
        addSegment,
        removeSegment,
        addSegmentBelow,
        seekToSegmentTime,
        autoResizeTextarea,

        // Save
        saveTranscriptionContent,
        saveSummary,
        saveNotes
    };
}
