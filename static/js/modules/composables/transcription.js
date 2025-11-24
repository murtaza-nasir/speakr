/**
 * Transcription editing composable
 * Handles ASR editor, text editor, and segment management
 */

export function useTranscription(state, utils) {
    const {
        showTextEditorModal, showAsrEditorModal, selectedRecording,
        editingTranscriptionContent, editingSegments, availableSpeakers,
        recordings, dropdownPositions
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

    const openAsrEditorModal = async () => {
        if (!selectedRecording.value) return;
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
        } catch (e) {
            console.error("Could not parse transcription as JSON for ASR editor:", e);
            setGlobalError("This transcription is not in the correct format for the ASR editor.");
        }
    };

    const closeAsrEditorModal = () => {
        showAsrEditorModal.value = false;
        editingSegments.value = [];
    };

    const saveAsrTranscription = async () => {
        if (!selectedRecording.value) return;

        // Remove extra UI fields and save the rest
        const contentToSave = JSON.stringify(editingSegments.value.map(({ id, showSuggestions, filteredSpeakers, ...rest }) => rest));

        await saveTranscriptionContent(contentToSave);
        closeAsrEditorModal();
    };

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

    const filterSpeakers = (index) => {
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

    const openSpeakerSuggestions = (index) => {
        if (editingSegments.value[index]) {
            // Close other dropdowns
            editingSegments.value.forEach((seg, i) => {
                if (i !== index) seg.showSuggestions = false;
            });

            editingSegments.value[index].showSuggestions = true;
            filterSpeakers(index);
            updateDropdownPosition(index);
        }
    };

    const closeSpeakerSuggestions = (index) => {
        if (editingSegments.value[index]) {
            editingSegments.value[index].showSuggestions = false;
        }
    };

    const closeAllSpeakerSuggestions = () => {
        editingSegments.value.forEach(seg => {
            seg.showSuggestions = false;
        });
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
            const rows = document.querySelectorAll('.asr-editor-table tbody tr');
            if (rows[index]) {
                const cell = rows[index].querySelector('td:first-child');
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
        const audioElements = document.querySelectorAll('.fixed.z-50 audio');
        const audioElement = audioElements.length > 0 ? audioElements[audioElements.length - 1] : null;
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
        closeAsrEditorModal,
        saveAsrTranscription,

        // Segment management
        adjustTime,
        filterSpeakers,
        openSpeakerSuggestions,
        closeSpeakerSuggestions,
        closeAllSpeakerSuggestions,
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
