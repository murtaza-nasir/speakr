/**
 * Transcription editing composable
 * Handles ASR editor, text editor, and segment management
 */

import { splitAsrSegment, supportsAsrSegmentSplit } from '../utils/asr-segment-split.js';
import { useAsrSpectrogram } from './asrSpectrogram.js';
import { useAsrSegmentTranscription } from './asrSegmentTranscription.js';
import { useAsrWorkplace } from './asrWorkplace.js';

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
        asrEditorRef, asrEditorSaveFlash, asrEditorHighlightIndex, editorAutosave
    } = state;

    const { showToast, setGlobalError, nextTick } = utils;
    let workplace;

    const spectrum = useAsrSpectrogram(state, {...utils, isSelectedSegment: segment => workplace?.selected.value === segment});

    const splitSelection = Vue.ref(null);
    const clearSplitSelection = () => { splitSelection.value = null; };
    const segmentAsr = useAsrSegmentTranscription(state, {...utils,
        beforeApply: clearSplitSelection, canEditDraft: () => !workplace?.locked.value,
        isSelectedSegment: segment => workplace?.selected.value === segment
    });
    const splitMessage = (key) => window.i18n?.t(`asrEditor.${key}`) || key;

    const captureSplitSelection = (index, event) => {
        const segment = editingSegments.value[index];
        const textarea = event.target;
        if (!segment || textarea.value !== segment.sentence) {
            clearSplitSelection();
            return;
        }
        splitSelection.value = {
            segment, text: segment.sentence,
            start: textarea.selectionStart, end: textarea.selectionEnd
        };
    };

    const getSplitResult = (index) => {
        if (!supportsAsrSegmentSplit()) return { error: 'unsupported' };
        const selection = splitSelection.value;
        const segment = editingSegments.value[index];
        if (!selection || selection.segment !== segment || selection.text !== segment?.sentence) {
            return { error: 'cursor' };
        }
        return splitAsrSegment(segment, selection.start, selection.end, spectrum.spectrogramBoundary(segment));
    };

    const canSplitSegment = (index) => !workplace.locked.value && !workplace.splitPreview.value && !getSplitResult(index).error;
    const splitErrorMessage = error => error === 'unsupported' ? 'splitUnsupportedBrowser'
        : error === 'time' ? 'splitInvalidTime' : 'splitChooseCursor';
    const splitSegmentTitle = (index) => {
        const error = getSplitResult(index).error;
        return splitMessage(error ? splitErrorMessage(error) : 'splitAtCursor');
    };

    const splitSegmentAtCursor = async (index) => {
        if (workplace.locked.value) return false;
        const result = getSplitResult(index);
        if (result.error) {
            showToast(splitMessage(splitErrorMessage(result.error)), 'fa-info-circle');
            return false;
        }
        workplace.splitPreview.value = {
            segment: editingSegments.value[index], snapshot: workplace.serialize([editingSegments.value[index]]),
            parts: result.parts, exact: spectrum.spectrogramBoundary(editingSegments.value[index]) !== undefined
        };
        return true;
    };

    const confirmAsrSplit = async () => {
        const preview = workplace.splitPreview.value;
        if (!preview || workplace.locked.value || workplace.serialize([preview.segment]) !== preview.snapshot) return false;
        const index = editingSegments.value.indexOf(preview.segment);
        if (index < 0) return false;
        const parts = preview.parts.map(part => ({
            ...part, showSuggestions: false, filteredSpeakers: [...availableSpeakers.value]
        }));
        parts.forEach(part => { if (part.speaker !== preview.segment.speaker) delete part.speaker_id; });
        spectrum.closeSpectrogram();
        utils.workspace?.split(preview.segment, parts);
        editingSegments.value.splice(index, 1, ...parts);
        workplace.splitPreview.value = null;
        editingSegments.value.forEach((seg, i) => { seg.id = i; });
        clearSplitSelection();
        closeAllSpeakerSuggestions();
        if (asrEditorHighlightIndex) asrEditorHighlightIndex.value = null;
        const second = editingSegments.value[index + 1];
        await workplace.select(index + 1);
        await nextTick();
        if (!showAsrEditorModal.value || editingSegments.value[index + 1] !== second) return true;
        utils.scrollAsrEditorToIndex?.(index + 1);
        await nextTick();
        if (!showAsrEditorModal.value || editingSegments.value[index + 1] !== second) return true;
        const input = document.querySelector('[data-testid="asr-detail"] [data-testid="asr-segment-speaker"]');
        input?.focus();
        input?.select();
        return true;
    };

    const handleSplitKeydown = (index, event) => {
        if (!showAsrEditorModal.value || workplace.splitPreview.value || state.showEditSpeakersModal?.value || event.isComposing || event.keyCode === 229 ||
            event.key !== 'Enter' || !(event.ctrlKey || event.metaKey)) return;
        event.preventDefault();
        captureSplitSelection(index, event);
        return splitSegmentAtCursor(index);
    };

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
        if (await saveTranscriptionContent(editingTranscriptionContent.value)) closeTextEditorModal();
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

    let openingGeneration = 0;
    const openAsrEditorModal = async (entry = 'editor') => {
        if (!selectedRecording.value) return;
        if (showAsrEditorModal.value) {
            if (entry === 'speakers') utils.workspace?.back();
            else utils.workspace?.openEditor(_asrEditorPendingScrollIndex ?? workplace.selectedIndex.value);
            _asrEditorPendingScrollIndex = null;
            return;
        }
        const openingToken = ++openingGeneration;
        clearSplitSelection();
        closeAllSpeakerSuggestions();

        // Pause outer audio player to avoid conflicts with modal's player
        pauseOuterAudioPlayer();

        try {
            const segments = JSON.parse(selectedRecording.value.transcription);
            if (!Array.isArray(segments)) throw new Error('Not an array');

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
            const openingId = selectedRecording.value.id;
            const initialized = await utils.workspace?.start(entry === 'speakers' ? 'speakers' : 'editor');
            if (initialized === false || openingToken !== openingGeneration || !showAsrEditorModal.value || selectedRecording.value?.id !== openingId) return;
            const targetIndex = _asrEditorPendingScrollIndex;
            workplace.start(targetIndex);


            // Reset virtual scroll state for fresh modal render. After it
            // initialises, either scroll to a specific segment (when the
            // modal was opened from a double-click on a simple-view row) or
            // restore the last scroll position for this recording.
            if (utils.resetAsrEditorScroll) {
                utils.resetAsrEditorScroll();
            }
            const recordingId = selectedRecording.value.id;
            _asrEditorPendingScrollIndex = null;
            await nextTick();
            // requestAnimationFrame defers past the virtualScroll's own
            // post-mount initialisation tick.
            requestAnimationFrame(() => {
                if (!showAsrEditorModal.value || selectedRecording.value?.id !== recordingId) return;
                if (targetIndex != null) {
                    if (utils.scrollAsrEditorToIndex) {
                        utils.scrollAsrEditorToIndex(targetIndex);
                    }
                } else {
                    const saved = _asrEditorScrollMemory.get(recordingId);
                    if (saved != null && utils.setAsrEditorScrollTop) {
                        utils.setAsrEditorScrollTop(saved);
                    } else utils.scrollAsrEditorToIndex?.(workplace.selectedIndex.value);
                }
            });
        } catch (e) {
            if (openingToken !== openingGeneration) return;
            console.error("Could not parse transcription as JSON for ASR editor:", e);
            showAsrEditorModal.value = false;
            utils.workspace?.stop();
            setGlobalError(e.message || "This transcription is not in the correct format for the ASR editor.");
        }
    };

    // Open the editor and scroll to a specific segment index. Used by the
    // double-click-on-simple-view-row affordance. Also briefly highlights
    // the target row so the user can see where they landed.
    const openAsrEditorAtSegment = (segmentIndex) => {
        _asrEditorPendingScrollIndex = segmentIndex;
        if (asrEditorHighlightIndex) {
            asrEditorHighlightIndex.value = segmentIndex;
            // Hold the highlight for ~3s so the user has time to register
            // the target row before it fades. The CSS handles the visual
            // transition when the class is removed.
            setTimeout(() => {
                if (asrEditorHighlightIndex.value === segmentIndex) {
                    asrEditorHighlightIndex.value = null;
                }
            }, 3000);
        }
        return openAsrEditorModal();
    };

    const closeAsrEditorModal = () => workplace.requestClose();
    const finishAsrEditorClose = () => {
        openingGeneration++;
        workplace.stop();
        utils.workspace?.stop();
        spectrum.closeSpectrogram();
        segmentAsr.closeSegmentTranscription();
        clearSplitSelection();
        closeAllSpeakerSuggestions();
        // Clear any pending row highlight.
        if (asrEditorHighlightIndex) asrEditorHighlightIndex.value = null;

        // Save scroll position so reopening the same recording within this
        // session lands the user back where they were.
        if (selectedRecording.value && asrEditorRef && asrEditorRef.value) {
            _asrEditorScrollMemory.set(
                selectedRecording.value.id,
                asrEditorRef.value.scrollTop
            );
        }

        // Pause any playing modal audio before closing
        const modalAudio = document.querySelector('[data-testid="asr-editor"] audio, [data-testid="asr-editor"] video');
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

    const saveAsrTranscription = (keepOpen = false) => workplace.save(keepOpen);

    // Ctrl+S / Cmd+S handler -- saves without closing while the editor modal
    // is open. preventDefault stops the browser's "save page" dialog.
    const handleAsrEditorKeydown = (event) => {
        if (!showAsrEditorModal.value || state.showEditSpeakersModal?.value || workplace.closeDecision?.value) return;
        const isSaveShortcut = (event.ctrlKey || event.metaKey) && (event.key === 's' || event.key === 'S');
        if (isSaveShortcut) {
            event.preventDefault();
            saveAsrTranscription(true);
        }
    };
    if (typeof window !== 'undefined') {
        window.addEventListener('keydown', handleAsrEditorKeydown);
    }

    // =========================================
    // Segment Management
    // =========================================

    const adjustTime = (index, field, amount) => {
        if (workplace.locked.value) return;
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
        if (workplace.locked.value) return;
        if (editingSegments.value[index]) {
            if (editingSegments.value[index].speaker !== speaker) delete editingSegments.value[index].speaker_id;
            editingSegments.value[index].speaker = speaker;
            closeSpeakerSuggestions(index);
        }
    };

    const selectNewSegment = async index => {
        const segment = editingSegments.value[index];
        if (utils.workspace?.active()) await utils.workspace.openEditor(index);
        else await workplace.select(index);
        await nextTick();
        if (showAsrEditorModal.value && workplace.selected.value === segment && !workplace.locked.value)
            document.querySelector('[data-testid="asr-segment-text"]')?.focus();
    };
    const addSegment = () => {
        if (workplace.locked.value) return;
        clearSplitSelection();
        closeAllSpeakerSuggestions();
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
        selectNewSegment(editingSegments.value.length - 1);
    };

    const removeSegment = (index) => {
        if (workplace.locked.value || !editingSegments.value[index]) return;
        const wasSelected = workplace.selected.value === editingSegments.value[index];
        clearSplitSelection();
        closeAllSpeakerSuggestions();
        editingSegments.value.splice(index, 1);
        if (wasSelected) {
            workplace.selected.value = null;
            if (editingSegments.value.length) workplace.select(Math.min(index, editingSegments.value.length - 1));
            else { spectrum.closeSpectrogram(); segmentAsr.closeSegmentTranscription(); }
        }
        // Re-index segments
        editingSegments.value.forEach((seg, i) => {
            seg.id = i;
        });
    };

    const addSegmentBelow = (index) => {
        if (workplace.locked.value || !editingSegments.value[index]) return;
        clearSplitSelection();
        closeAllSpeakerSuggestions();
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
        selectNewSegment(index + 1);
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

    const saveTranscriptionContent = async (content, recordingId = selectedRecording.value?.id, isCurrent = () => true) => {
        if (recordingId == null) return false;
        if (selectedRecording.value?.incognito) {
            selectedRecording.value.transcription = content;
            const index = recordings.value.findIndex(r => r.id === recordingId);
            if (index >= 0) recordings.value[index] = {...recordings.value[index], transcription: content};
            sessionStorage.setItem('speakr_incognito_recording', JSON.stringify(selectedRecording.value));
            return {transcription: content};
        }
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${recordingId}/update_transcription`, {
                method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
                body: JSON.stringify({ transcription: content })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to update transcription');
            const stored = data.recording?.transcription ?? content;
            const index = recordings.value.findIndex(r => r.id === recordingId);
            // Replace the list entry: it may alias a selected object from a newer session.
            if (index !== -1) recordings.value[index] = {...recordings.value[index], transcription: stored};
            if (isCurrent() && selectedRecording.value?.id === recordingId) {
                selectedRecording.value.transcription = stored;
                showToast(splitMessage('saved'), 'fa-check-circle');
            }
            return {transcription: stored};
        } catch (error) {
            if (isCurrent()) setGlobalError(`Failed to save transcription: ${error.message}`);
            return false;
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

    workplace = useAsrWorkplace(state, utils, {...spectrum, ...segmentAsr}, {
        clearSplitSelection,
        fingerprint: payload => utils.workspace?.fingerprint(payload) ?? payload,
        needsSave: automatic => utils.workspace?.needsSave(automatic) || false,
        persist: (payload, id, isCurrent, automatic) => utils.workspace?.persist(payload, id, isCurrent, automatic) ?? saveTranscriptionContent(payload, id, isCurrent),
        close: finishAsrEditorClose, remove: removeSegment
    });
    const keyMap = new WeakMap(); let keyCounter = 0;
    const asrSegmentKey = index => {
        const segment = editingSegments.value[index];
        if (!segment) return index;
        if (!keyMap.has(segment)) keyMap.set(segment, ++keyCounter);
        return keyMap.get(segment);
    };
    const editAsrSpeaker = (index, value) => selectSpeaker(index, value);
    const asrSpeakerOptions = Vue.computed(() => [...new Set([
        ...availableSpeakers.value,
        ...editingSegments.value.map(segment => segment.speaker),
        ...(workplace.splitPreview.value?.parts.map(part => part.speaker) || [])
    ].filter(name => typeof name === 'string' && name.trim()))].sort());

    return {
        ...spectrum,
        ...segmentAsr,
        ...workplace,
        confirmAsrSplit, cancelAsrSplit: () => { workplace.splitPreview.value = null; },
        asrSegmentKey, editAsrSpeaker, asrSpeakerOptions,
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
        captureSplitSelection,
        canSplitSegment,
        splitSegmentTitle,
        splitSegmentAtCursor,
        handleSplitKeydown,
        seekToSegmentTime,
        autoResizeTextarea,

        // Save
        saveTranscriptionContent,
        saveSummary,
        saveNotes
    };
}
