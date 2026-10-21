/**
 * Speaker management composable
 * Handles speaker identification, naming, and navigation
 */

export function useSpeakers(state, utils, processedTranscription) {
    const { nextTick } = Vue;
    const {
        showSpeakerModal, speakerModalTab, showAddSpeakerModal, showEditSpeakersModal,
        showEditTextModal, selectedRecording, recordings,
        speakerMap, speakerColorMap, modalSpeakers, regenerateSummaryAfterSpeakerUpdate,
        editingSpeakersList, databaseSpeakers, editingSpeakerSuggestions,
        editSpeakerDropdownPositions, newSpeakerName, newSpeakerIsMe,
        newSpeakerSuggestions, loadingNewSpeakerSuggestions, showNewSpeakerSuggestions,
        editingSegmentIndex, editingSpeakerIndex, editedText, editedTranscriptData,
        isAutoIdentifying, availableSpeakers, editingSegments, currentUserName,
        voiceSuggestions, loadingVoiceSuggestions
    } = state;

    const { showToast, setGlobalError, onChatComplete } = utils;

    // i18n helper — falls back to the provided fallback string if i18n is not loaded
    const t = (key, params, fallback) => window.i18n ? window.i18n.t(key, params) : (fallback || key);
    const tc = (key, count, params) => window.i18n ? window.i18n.tc(key, count, params) : (params && params.count != null ? `${params.count}` : key);

    // Snapshot of the recording's transcription taken when the modal opens.
    // Per-line speaker/text edits are now STAGED in memory (changeSpeaker /
    // saveEditedText mutate selectedRecording.transcription for live preview
    // but don't persist). If the user Cancels with unsaved staged edits, we
    // restore this snapshot so the cancelled edits don't leak into the
    // detail view. On a real save the staged flag is cleared first so no
    // revert happens.
    let originalTranscriptionSnapshot = null;

    // Number of speaker colors available in CSS (must match styles.css and app.modular.js)
    const SPEAKER_COLOR_COUNT = 16;

    // Get speaker color from the shared color map
    // If speaker not in map, assign next available color
    const getSpeakerColor = (speakerId) => {
        if (speakerColorMap.value[speakerId]) {
            return speakerColorMap.value[speakerId];
        }
        // Assign next color to new speaker
        const colorIndex = Object.keys(speakerColorMap.value).length;
        const color = `speaker-color-${(colorIndex % SPEAKER_COLOR_COUNT) + 1}`;
        speakerColorMap.value[speakerId] = color;
        return color;
    };

    // Helper to pause outer audio player when opening modals with their own player
    const pauseOuterAudioPlayer = () => {
        // Find the audio player in the right panel (not in a modal)
        const outerAudio = document.querySelector('#rightMainColumn audio') || document.querySelector('#rightMainColumn video') ||
                          document.querySelector('.detail-view audio:not(.fixed audio)') || document.querySelector('.detail-view video:not(.fixed video)');
        if (outerAudio && !outerAudio.paused) {
            outerAudio.pause();
        }
    };

    // =========================================
    // Speaker Identification Modal
    // =========================================

    // The modal's list, filter, name fields and player live in
    // speaker-modal.js; this file keeps opening, saving, auto-identify and
    // the per-line edits.
    const openSpeakerModal = () => {
        if (!selectedRecording.value) return;
        try {
            if (utils.workspace && Array.isArray(JSON.parse(selectedRecording.value.transcription))) return utils.openWorkspace('speakers');
        } catch { /* Plain text keeps its existing labeling screen. */ }

        // Pause outer audio player to avoid conflicts with modal's player
        pauseOuterAudioPlayer();

        // Snapshot the committed transcription and clear any stale staged
        // edits so this modal session starts from a clean, saved state.
        originalTranscriptionSnapshot = selectedRecording.value.transcription || null;
        editedTranscriptData.value = null;

        // Get the same speaker order used in processedTranscription
        const transcription = selectedRecording.value?.transcription;
        let speakers = [];

        if (transcription) {
            try {
                const transcriptionData = JSON.parse(transcription);
                if (transcriptionData && Array.isArray(transcriptionData)) {
                    // Use the exact same logic as processedTranscription to get speakers
                    speakers = [...new Set(transcriptionData.map(segment => segment.speaker).filter(Boolean))];
                }
            } catch (e) {
                // Fall back to getIdentifiedSpeakers if JSON parsing fails
                speakers = getIdentifiedSpeakers();
            }
        }

        // Initialize speaker map FIRST with colors from shared color map
        // Clear existing map and rebuild it
        speakerMap.value = {};
        speakers.forEach(speaker => {
            speakerMap.value[speaker] = {
                name: '',
                isMe: false,
                color: getSpeakerColor(speaker)
            };
        });

        // Set modalSpeakers AFTER speakerMap is populated (triggers render)
        modalSpeakers.value = speakers;

        isAutoIdentifying.value = false;
        regenerateSummaryAfterSpeakerUpdate.value = true;
        voiceSuggestions.value = {};
        speakerModalTab.value = 'speakers';  // Reset to speakers tab on mobile

        showSpeakerModal.value = true;

        // Load voice-based suggestions if embeddings are available
        loadVoiceSuggestions();
    };

    const getIdentifiedSpeakers = () => {
        // Ensure we have a valid recording and transcription
        if (!selectedRecording.value?.transcription) {
            return [];
        }

        const transcription = selectedRecording.value.transcription;
        let transcriptionData;

        try {
            transcriptionData = JSON.parse(transcription);
        } catch (e) {
            transcriptionData = null;
        }

        // Handle new simplified JSON format (array of segments)
        if (transcriptionData && Array.isArray(transcriptionData)) {
            // JSON format - extract speakers in order of appearance
            const speakersInOrder = [];
            const seenSpeakers = new Set();
            transcriptionData.forEach(segment => {
                if (segment.speaker && !seenSpeakers.has(segment.speaker)) {
                    seenSpeakers.add(segment.speaker);
                    speakersInOrder.push(segment.speaker);
                }
            });
            return speakersInOrder;
        } else if (typeof transcription === 'string') {
            // Plain text format - find speakers in order of appearance
            const speakerRegex = /\[([^\]]+)\]:/g;
            const speakersInOrder = [];
            const seenSpeakers = new Set();
            let match;
            while ((match = speakerRegex.exec(transcription)) !== null) {
                const speaker = match[1].trim();
                if (speaker && !seenSpeakers.has(speaker)) {
                    seenSpeakers.add(speaker);
                    speakersInOrder.push(speaker);
                }
            }
            return speakersInOrder;
        }
        return [];
    };

    const closeSpeakerModal = () => {
        if (utils.workspace?.active()) return utils.closeWorkspace();
        // The modal composable pauses its player when showSpeakerModal turns
        // false. Reset modal audio state (keep main player independent)
        if (utils.resetModalAudioState) {
            utils.resetModalAudioState();
        }

        // If the modal is closing with UNSAVED staged per-line edits (the
        // user hit Cancel/X, not Save), discard them by restoring the
        // snapshot taken on open. A real save clears editedTranscriptData
        // before calling this, so saved edits are never reverted.
        if (editedTranscriptData.value && originalTranscriptionSnapshot != null && selectedRecording.value) {
            selectedRecording.value.transcription = originalTranscriptionSnapshot;
        }
        editedTranscriptData.value = null;
        originalTranscriptionSnapshot = null;

        showSpeakerModal.value = false;
        showAutoIdDropdown.value = false;
        // Clear the speaker map to prevent stale data from persisting
        speakerMap.value = {};
    };

    const saveTranscriptImmediately = async (transcriptData) => {
        if (!selectedRecording.value) return;

        try {
            // Save transcript without closing modal
            const filteredSpeakerMap = Object.entries(speakerMap.value).reduce((acc, [speakerId, speakerData]) => {
                if (speakerData.name && speakerData.name.trim() !== '') {
                    acc[speakerId] = speakerData;
                }
                return acc;
            }, {});

            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${selectedRecording.value.id}/update_transcript`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    transcript_data: transcriptData,
                    speaker_map: filteredSpeakerMap,
                    regenerate_summary: false // Don't regenerate on immediate saves
                })
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to update transcript');

            // Update recordings list and selected recording without closing modal
            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1) {
                recordings.value[index] = data.recording;
            }
            selectedRecording.value = data.recording;
            editedTranscriptData.value = null;

            showToast(t('help.saved'), 'fa-check-circle', 2000, 'success');
        } catch (error) {
            console.error('Save Transcript Error:', error);
            showToast(`Error: ${error.message}`, 'fa-exclamation-circle', 3000, 'error');
        }
    };

    const saveTranscriptEdits = async () => {
        if (!selectedRecording.value || !editedTranscriptData.value) {
            return saveSpeakerNames(); // Fall back to regular speaker name save
        }

        try {
            // Save both speaker names and transcript edits
            const filteredSpeakerMap = Object.entries(speakerMap.value).reduce((acc, [speakerId, speakerData]) => {
                if (speakerData.name && speakerData.name.trim() !== '') {
                    acc[speakerId] = speakerData;
                }
                return acc;
            }, {});

            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${selectedRecording.value.id}/update_transcript`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    transcript_data: editedTranscriptData.value,
                    speaker_map: filteredSpeakerMap,
                    regenerate_summary: regenerateSummaryAfterSpeakerUpdate.value
                })
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to update transcript');

            // The edits are now persisted on the server — clear the staged
            // flag BEFORE closing so closeSpeakerModal doesn't mistake this
            // for a Cancel and revert the just-saved changes.
            editedTranscriptData.value = null;
            closeSpeakerModal();

            // If summary regeneration was requested, update status immediately
            if (regenerateSummaryAfterSpeakerUpdate.value && data.summary_queued) {
                // Update recording status to SUMMARIZING immediately for UI feedback
                const summarizingRecording = { ...data.recording, status: 'SUMMARIZING' };

                const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
                if (index !== -1) {
                    recordings.value[index] = summarizingRecording;
                }
                selectedRecording.value = summarizingRecording;
                editedTranscriptData.value = null;

                showToast(t('help.transcriptUpdated'), 'fa-check-circle');
                showToast(t('help.summaryRegenerationStarted'), 'fa-sync-alt');

                // Poll for summary completion
                pollForSummaryCompletion(selectedRecording.value.id);
            } else {
                const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
                if (index !== -1) {
                    recordings.value[index] = data.recording;
                }
                selectedRecording.value = data.recording;
                editedTranscriptData.value = null;

                showToast(t('help.transcriptUpdated'), 'fa-check-circle');
            }
        } catch (error) {
            console.error('Save Transcript Error:', error);
            showToast(`Error: ${error.message}`, 'fa-exclamation-circle', 3000, 'error');
        }
    };

    const saveSpeakerNames = async () => {
        if (utils.workspace?.active()) return utils.saveWorkspace(false);
        if (!selectedRecording.value) return;
        if (selectedRecording.value.incognito) {
            let content = selectedRecording.value.transcription || '';
            for (const [label, info] of Object.entries(speakerMap.value)) {
                const name = info.name?.trim() || (info.isMe ? currentUserName.value || 'Me' : '');
                if (!name) continue;
                const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                content = content.replace(new RegExp(`\\[\\s*${escaped}\\s*\\]`, 'gi'), () => `[${name}]`);
            }
            if (await utils.persistLocalTranscript(content)) closeSpeakerModal();
            return;
        }

        // If there are transcript edits, save those instead
        if (editedTranscriptData.value) {
            return saveTranscriptEdits();
        }

        // Create a filtered speaker map that excludes entries with blank names
        const filteredSpeakerMap = Object.entries(speakerMap.value).reduce((acc, [speakerId, speakerData]) => {
            if (speakerData.name && speakerData.name.trim() !== '') {
                acc[speakerId] = speakerData;
            }
            return acc;
        }, {});

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${selectedRecording.value.id}/update_speakers`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    speaker_map: filteredSpeakerMap,
                    regenerate_summary: regenerateSummaryAfterSpeakerUpdate.value
                })
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to update speaker names');

            closeSpeakerModal();

            // If summary regeneration was requested, update status immediately
            if (regenerateSummaryAfterSpeakerUpdate.value && data.summary_queued) {
                // Update recording status to SUMMARIZING immediately for UI feedback
                const summarizingRecording = { ...data.recording, status: 'SUMMARIZING' };

                const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
                if (index !== -1) {
                    recordings.value[index] = summarizingRecording;
                }
                selectedRecording.value = summarizingRecording;

                showToast(t('help.speakerNamesUpdated'), 'fa-check-circle');
                showToast(t('help.summaryRegenerationStarted'), 'fa-sync-alt');

                // Poll for summary completion
                pollForSummaryCompletion(selectedRecording.value.id);
            } else {
                // The backend returns the fully updated recording object
                const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
                if (index !== -1) {
                    recordings.value[index] = data.recording;
                }
                selectedRecording.value = data.recording;

                showToast(t('help.speakerNamesUpdated'), 'fa-check-circle');
            }
        } catch (error) {
            setGlobalError(`Failed to save speaker names: ${error.message}`);
        }
    };

    // Poll for summary completion after regeneration
    const pollForSummaryCompletion = async (recordingId) => {
        const maxAttempts = 40; // Poll for up to 2 minutes (40 * 3 seconds)
        let attempts = 0;

        const pollInterval = setInterval(async () => {
            attempts++;

            try {
                // Use lightweight status-only endpoint for polling
                const response = await fetch(`/recording/${recordingId}/status`);
                if (!response.ok) {
                    clearInterval(pollInterval);
                    return;
                }

                const statusData = await response.json();

                // Update status in recordings list
                const index = recordings.value.findIndex(r => r.id === recordingId);
                if (index !== -1) {
                    // Create new object to ensure Vue reactivity
                    recordings.value[index] = {
                        ...recordings.value[index],
                        status: statusData.status
                    };
                }

                // Update selectedRecording with new object reference for reactivity
                if (selectedRecording.value && selectedRecording.value.id === recordingId) {
                    if (utils.workspace?.active()) Object.assign(selectedRecording.value, {status:statusData.status});
                    else selectedRecording.value = {
                        ...selectedRecording.value,
                        status: statusData.status
                    };
                }

                // Check if summarization is complete
                if (statusData.status === 'COMPLETED') {
                    clearInterval(pollInterval);

                    // Now fetch the full recording with the new summary
                    const fullResponse = await fetch(`/api/recordings/${recordingId}`);
                    if (fullResponse.ok) {
                        const fullData = await fullResponse.json();

                        // Update in recordings list first
                        const currentIndex = recordings.value.findIndex(r => r.id === recordingId);
                        if (currentIndex !== -1) {
                            recordings.value[currentIndex] = fullData;
                        }

                        // Always update selectedRecording if it's the current recording
                        if (selectedRecording.value && selectedRecording.value.id === recordingId) {
                            if (utils.workspace?.active()) Object.assign(selectedRecording.value, fullData);
                            else selectedRecording.value = fullData;
                            // Force Vue to detect the change
                            await nextTick();
                        }
                    }

                    showToast(t('help.summaryUpdated'), 'fa-check-circle');
                    // Refresh token budget after LLM operation
                    if (onChatComplete) onChatComplete();
                } else if (statusData.status === 'FAILED' || statusData.status === 'ERROR') {
                    // Stop polling if it failed
                    clearInterval(pollInterval);
                    showToast(t('help.summaryGenerationFailed'), 'fa-exclamation-circle', 3000, 'error');
                } else if (attempts >= maxAttempts) {
                    // Stop polling after max attempts
                    clearInterval(pollInterval);
                    showToast(t('help.summaryGenerationTimedOut'), 'fa-clock', 3000, 'warning');
                }
            } catch (error) {
                console.error('Error polling for summary:', error);
                clearInterval(pollInterval);
            }
        }, 3000); // Poll every 3 seconds
    };

    // =========================================
    // Speaker Suggestions
    // =========================================

    const loadVoiceSuggestions = async () => {
        if (!selectedRecording.value?.id) return;
        if (selectedRecording.value.incognito) return;
        const workspaceToken = utils.workspace?.active() ? utils.workspace.llmSnapshot() : null;
        const recordingId = selectedRecording.value.id;

        loadingVoiceSuggestions.value = true;
        voiceSuggestions.value = {};

        try {
            const response = await fetch(`/speakers/suggestions/${selectedRecording.value.id}`);
            if (!response.ok) throw new Error('Failed to load voice suggestions');

            const data = await response.json();

            if (recordingId !== selectedRecording.value?.id || (workspaceToken && !utils.workspace.llmSessionCurrent(workspaceToken))) return;

            if (data.success && data.suggestions) {
                // Only keep suggestions that have matches
                voiceSuggestions.value = Object.fromEntries(
                    Object.entries(data.suggestions).filter(([_, matches]) => matches && matches.length > 0)
                );
            }
        } catch (error) {
            console.error('Error loading voice suggestions:', error);
            if (recordingId === selectedRecording.value?.id && (!workspaceToken || utils.workspace.llmSessionCurrent(workspaceToken))) voiceSuggestions.value = {};
        } finally {
            if (recordingId === selectedRecording.value?.id && (!workspaceToken || utils.workspace.llmSessionCurrent(workspaceToken))) loadingVoiceSuggestions.value = false;
        }
    };

    const applyVoiceSuggestion = (speakerId, suggestion) => {
        if (utils.workspace?.active() && utils.canEditAsrDraft?.() === false) return;
        if (speakerMap.value[speakerId]) {
            speakerMap.value[speakerId].name = suggestion.name;
            // Don't delete the suggestion - let it reappear if user clears the field
        }
    };

    // Handle "This is Me" checkbox changes
    const handleIsMeChange = (speakerId) => {
        if (utils.workspace?.active() && utils.canEditAsrDraft?.() === false) return;
        if (!speakerMap.value[speakerId]) return;

        if (speakerMap.value[speakerId].isMe) {
            // Checkbox is now checked - set the name to current user's name
            speakerMap.value[speakerId].name = currentUserName.value || 'Me';
        } else {
            // Checkbox is now unchecked - clear the name
            speakerMap.value[speakerId].name = '';
        }
    };

    // =========================================
    // Auto-Identify Speakers
    // =========================================

    // Split button dropdown visibility + click-outside handling
    const showAutoIdDropdown = Vue.ref(false);
    const autoIdSplitBtn = Vue.ref(null);

    const onAutoIdClickOutside = (e) => {
        if (autoIdSplitBtn.value && !autoIdSplitBtn.value.contains(e.target)) {
            showAutoIdDropdown.value = false;
        }
    };
    Vue.watch(showAutoIdDropdown, (open) => {
        if (open) {
            document.addEventListener('click', onAutoIdClickOutside, true);
        } else {
            document.removeEventListener('click', onAutoIdClickOutside, true);
        }
    });

    /**
     * Auto-identify speakers via LLM.
     * @param {boolean} identifyAll - When false (default), only fill speakers with empty names.
     *                                When true, overwrite all speaker names.
     */
    const autoIdentifySpeakers = async (identifyAll = false) => {
        showAutoIdDropdown.value = false;
        if (selectedRecording.value?.incognito) return;
        if (utils.workspace?.active() && (selectedRecording.value.incognito || utils.canEditAsrDraft?.() === false)) return;
        const workspaceToken = utils.workspace?.active() ? utils.workspace.llmSnapshot() : null;

        if (!selectedRecording.value) {
            showToast(t('help.noRecordingSelected'), 'fa-exclamation-circle');
            return;
        }

        isAutoIdentifying.value = true;
        showToast(t('help.startingAutoIdentification'), 'fa-magic');

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${selectedRecording.value.id}/auto_identify_speakers`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    current_speaker_map: speakerMap.value,
                    ...(workspaceToken ? utils.workspace.llmPayload() : {})
                })
            });

            const data = await response.json();
            if (workspaceToken && !utils.workspace.llmCurrent(workspaceToken)) return;
            if (!response.ok) {
                throw new Error(data.error || 'Unknown error occurred during auto-identification.');
            }

            // Check if there's a message (e.g., all speakers already identified)
            if (data.message) {
                showToast(data.message, 'fa-info-circle');
                return;
            }

            // Update speakerMap with the identified names
            let identifiedCount = 0;
            for (const speakerId in data.speaker_map) {
                const identifiedName = data.speaker_map[speakerId];
                if (speakerMap.value[speakerId] && identifiedName && identifiedName.trim() !== '') {
                    // Skip speakers that already have a name unless identifyAll is true
                    if (!identifyAll && speakerMap.value[speakerId].name && speakerMap.value[speakerId].name.trim() !== '') {
                        continue;
                    }
                    speakerMap.value[speakerId].name = identifiedName;
                    identifiedCount++;
                }
            }

            if (identifiedCount > 0) {
                showToast(tc('help.speakersIdentified', identifiedCount, { count: identifiedCount }), 'fa-check-circle');
            } else {
                showToast(t('help.noSpeakersIdentified'), 'fa-info-circle');
            }

            // Refresh token budget after LLM operation
            if (onChatComplete) onChatComplete();

        } catch (error) {
            console.error('Auto Identify Speakers Error:', error);
            showToast(`Error: ${error.message}`, 'fa-exclamation-circle', 5000, 'error');
        } finally {
            if (!workspaceToken || utils.workspace.llmSessionCurrent(workspaceToken)) isAutoIdentifying.value = false;
        }
    };

    // =========================================
    // Apply Suggested Names
    // =========================================

    /** True when any unnamed, non-isMe speaker has a voice-match suggestion */
    const hasAnySuggestions = Vue.computed(() => {
        for (const speakerId of modalSpeakers.value) {
            const data = speakerMap.value[speakerId];
            if (!data || data.isMe) continue;
            if (data.name && data.name.trim() !== '') continue;
            if (voiceSuggestions.value[speakerId] && voiceSuggestions.value[speakerId].length > 0) {
                return true;
            }
        }
        return false;
    });

    /** Apply the best voice match to every speaker whose name is still empty */
    const applySuggestedNames = () => {
        if (utils.workspace?.active() && utils.canEditAsrDraft?.() === false) return;
        let appliedCount = 0;
        for (const speakerId of modalSpeakers.value) {
            const data = speakerMap.value[speakerId];
            if (!data || data.isMe) continue;
            if (data.name && data.name.trim() !== '') continue;

            const voice = voiceSuggestions.value[speakerId];
            if (voice && voice.length > 0) {
                data.name = voice[0].name;
                appliedCount++;
            }
        }

        if (appliedCount > 0) {
            showToast(tc('help.appliedSuggestedNames', appliedCount, { count: appliedCount }), 'fa-check-circle');
        } else {
            showToast(t('help.noSuggestionsToApply'), 'fa-info-circle');
        }
    };

    // =========================================
    // Add Speaker Modal
    // =========================================

    const searchNewSpeaker = async () => {
        const query = newSpeakerName.value;
        if (!query || query.length < 2) {
            newSpeakerSuggestions.value = [];
            return;
        }

        loadingNewSpeakerSuggestions.value = true;
        try {
            const response = await fetch(`/speakers/search?q=${encodeURIComponent(query)}`);
            if (!response.ok) throw new Error('Failed to search speakers');

            const speakers = await response.json();
            newSpeakerSuggestions.value = speakers;
        } catch (error) {
            console.error('Error searching speakers:', error);
            newSpeakerSuggestions.value = [];
        } finally {
            loadingNewSpeakerSuggestions.value = false;
        }
    };

    const selectNewSpeakerSuggestion = (suggestion) => {
        newSpeakerName.value = suggestion.name;
        newSpeakerSuggestions.value = [];
        showNewSpeakerSuggestions.value = false;
    };

    const hideNewSpeakerSuggestionsDelayed = () => {
        setTimeout(() => {
            showNewSpeakerSuggestions.value = false;
            newSpeakerSuggestions.value = [];
        }, 200);
    };

    const openAddSpeakerModal = () => {
        newSpeakerName.value = '';
        newSpeakerIsMe.value = false;
        newSpeakerSuggestions.value = [];
        loadingNewSpeakerSuggestions.value = false;
        showNewSpeakerSuggestions.value = false;
        showAddSpeakerModal.value = true;
    };

    const closeAddSpeakerModal = () => {
        showAddSpeakerModal.value = false;
        newSpeakerName.value = '';
        newSpeakerIsMe.value = false;
        newSpeakerSuggestions.value = [];
        loadingNewSpeakerSuggestions.value = false;
        showNewSpeakerSuggestions.value = false;
    };

    const addNewSpeaker = () => {
        const name = newSpeakerIsMe.value ? (currentUserName.value || 'Me') : newSpeakerName.value.trim();

        if (!newSpeakerIsMe.value && !name) {
            showToast(t('help.pleaseEnterSpeakerName'), 'fa-exclamation-circle');
            return;
        }

        // Generate new speaker ID
        const existingSpeakerNumbers = modalSpeakers.value
            .map(s => {
                const match = s.match(/^SPEAKER_(\d+)$/);
                return match ? parseInt(match[1]) : -1;
            })
            .filter(n => n >= 0);

        const nextNumber = existingSpeakerNumbers.length > 0
            ? Math.max(...existingSpeakerNumbers) + 1
            : modalSpeakers.value.length;

        const newSpeakerId = `SPEAKER_${String(nextNumber).padStart(2, '0')}`;

        // Add to speakerMap FIRST (before modalSpeakers) to avoid render race condition
        speakerMap.value[newSpeakerId] = {
            name: name,
            isMe: newSpeakerIsMe.value,
            color: getSpeakerColor(newSpeakerId)
        };

        // Add to modalSpeakers LAST (triggers re-render, but speakerMap is already populated)
        modalSpeakers.value.push(newSpeakerId);
        utils.workspace?.addSpeaker(newSpeakerId);

        closeAddSpeakerModal();
        showToast(t('help.speakerAdded'), 'fa-check-circle');
    };

    // =========================================
    // Edit Speakers Modal
    // =========================================

    let editNamesToken = 0, editNamesContext = null;
    const namesContextCurrent = context => !!context && context.recordingId === selectedRecording.value?.id &&
        context.asr === !!state.showAsrEditorModal?.value && (!context.asr ||
            (context.draft === editingSegments.value && utils.canEditAsrDraft?.() !== false));
    const openEditSpeakersModal = async () => {
        if (utils.workspace?.active()) return utils.workspace.back();
        const token = ++editNamesToken;
        const context = {recordingId:selectedRecording.value?.id, asr:!!state.showAsrEditorModal?.value, draft:editingSegments.value};
        editNamesContext = context;
        // Close any open suggestions
        editingSegments.value.forEach(seg => seg.showSuggestions = false);
        // Copy current speakers to editing list with original and current properties
        editingSpeakersList.value = availableSpeakers.value.map(s => ({
            original: s,
            current: s
        }));
        // Fetch speakers from database for autocomplete
        try {
            const response = await fetch('/speakers');
            const speakers = await response.json();
            if (token !== editNamesToken || !namesContextCurrent(context)) return;
            // Keep full objects with id and name for autocomplete dropdown
            databaseSpeakers.value = speakers;
        } catch (e) {
            if (token !== editNamesToken || !namesContextCurrent(context)) return;
            console.error('Failed to fetch speakers:', e);
            databaseSpeakers.value = [];
        }
        if (token !== editNamesToken || !namesContextCurrent(context)) return;
        editingSpeakerSuggestions.value = {};
        showEditSpeakersModal.value = true;
    };

    const closeEditSpeakersModal = () => {
        editNamesToken++; editNamesContext = null;
        showEditSpeakersModal.value = false;
        editingSpeakersList.value = [];
    };

    const addEditingSpeaker = () => {
        editingSpeakersList.value.push({ original: '', current: '' });
    };

    const removeEditingSpeaker = (index) => {
        editingSpeakersList.value.splice(index, 1);
    };

    const filterEditingSpeakerSuggestions = (index) => {
        const query = editingSpeakersList.value[index]?.current?.toLowerCase().trim() || '';
        if (query === '') {
            // Show all speakers when field is empty/focused
            editingSpeakerSuggestions.value[index] = [...databaseSpeakers.value];
        } else {
            editingSpeakerSuggestions.value[index] = databaseSpeakers.value.filter(
                s => s.name.toLowerCase().includes(query)
            );
        }
    };

    const selectEditingSpeakerSuggestion = (index, name) => {
        editingSpeakersList.value[index].current = name;
        editingSpeakerSuggestions.value[index] = [];
    };

    const closeEditingSpeakerSuggestions = (index) => {
        editingSpeakerSuggestions.value[index] = [];
    };

    const onEditSpeakerBlur = (index) => {
        // Delay closing to allow clicking on suggestions
        setTimeout(() => {
            closeEditingSpeakerSuggestions(index);
        }, 200);
    };

    const getEditSpeakerDropdownPosition = (index) => {
        // Find the input element for this index and calculate position
        const inputs = document.querySelectorAll('[data-testid="asr-speaker-names"] input');
        if (inputs[index]) {
            const rect = inputs[index].getBoundingClientRect();
            return {
                top: rect.bottom + 2 + 'px',
                left: rect.left + 'px',
                width: rect.width + 'px'
            };
        }
        return { top: '0px', left: '0px', width: '200px' };
    };

    const saveEditingSpeakers = async () => {
        if (!namesContextCurrent(editNamesContext) || selectedRecording.value?.can_edit === false || selectedRecording.value?.is_read_only === true) {
            closeEditSpeakersModal(); return;
        }
        const map = {};
        editingSpeakersList.value.forEach(item => {
            if (item.original && item.current) {
                map[item.original] = item.current;
            }
        });

        // Update ASR editor state if it's open
        if (state.showAsrEditorModal?.value) {
            // Build new list of available speakers
            const newSpeakers = new Set();

            // Apply renames to all segments
            editingSegments.value.forEach(segment => {
                if (map[segment.speaker]) {
                    if (segment.speaker !== map[segment.speaker]) delete segment.speaker_id;
                    segment.speaker = map[segment.speaker];
                }
                newSpeakers.add(segment.speaker);
            });

            // Add any newly added speakers from the modal
            editingSpeakersList.value.forEach(item => {
                if (!item.original && item.current) {
                    // This is a new speaker (no original)
                    newSpeakers.add(item.current);
                }
            });

            // Update available speakers list
            availableSpeakers.value = [...newSpeakers].sort();

            // Update filtered speakers for all segments
            editingSegments.value.forEach(segment => {
                segment.filteredSpeakers = [...availableSpeakers.value];
            });

            closeEditSpeakersModal();
            showToast(t('help.speakersUpdatedSaveToApply'), 'fa-check-circle');
        } else {
            // Regular flow for non-ASR editor context
            speakerMap.value = map;
            closeEditSpeakersModal();
            await saveSpeakerNames();
        }
    };

    Vue.watch?.(() => [selectedRecording.value?.id, state.showAsrEditorModal?.value, editingSegments.value, utils.canEditAsrDraft?.()], () => {
        if (editNamesContext && !namesContextCurrent(editNamesContext)) closeEditSpeakersModal();
    });

    // =========================================
    // Edit Text Modal
    // =========================================

    const openEditTextModal = (segmentIndex) => {
        if (utils.workspace?.active()) return utils.workspace.openEditor(segmentIndex);
        if (!selectedRecording.value?.transcription) return;

        try {
            const transcriptionData = JSON.parse(selectedRecording.value.transcription);
            if (transcriptionData && Array.isArray(transcriptionData) && transcriptionData[segmentIndex]) {
                editingSegmentIndex.value = segmentIndex;
                editedText.value = transcriptionData[segmentIndex].sentence || '';
                showEditTextModal.value = true;
            }
        } catch (e) {
            console.error('Error opening text editor:', e);
            showToast(t('help.errorOpeningTextEditor'), 'fa-exclamation-circle', 3000, 'error');
        }
    };

    const closeEditTextModal = () => {
        showEditTextModal.value = false;
        editingSegmentIndex.value = null;
        editedText.value = '';
    };

    const saveEditedText = () => {
        if (editingSegmentIndex.value === null || !selectedRecording.value?.transcription) return;

        try {
            const transcriptionData = JSON.parse(selectedRecording.value.transcription);
            if (transcriptionData && Array.isArray(transcriptionData) && transcriptionData[editingSegmentIndex.value]) {
                transcriptionData[editingSegmentIndex.value].sentence = editedText.value;

                // Stage in memory only (same as changeSpeaker) — persisted on
                // "Save Names". No surprise live save.
                editedTranscriptData.value = transcriptionData;
                selectedRecording.value.transcription = JSON.stringify(transcriptionData);

                closeEditTextModal();
            }
        } catch (e) {
            console.error('Error saving text:', e);
            showToast(t('help.errorSavingText'), 'fa-exclamation-circle', 3000, 'error');
        }
    };

    // =========================================
    // Change Speaker in Segment
    // =========================================

    const openSpeakerChangeDropdown = (segmentIndex) => {
        editingSpeakerIndex.value = editingSpeakerIndex.value === segmentIndex ? null : segmentIndex;
    };

    const changeSpeaker = (segmentIndex, newSpeakerId) => {
        if (utils.workspace?.active()) {
            if (utils.canEditAsrDraft?.() === false || !editingSegments.value[segmentIndex]) return;
            editingSegments.value[segmentIndex].speaker = newSpeakerId;
            delete editingSegments.value[segmentIndex].speaker_id;
            editingSpeakerIndex.value = null;
            return;
        }
        if (!selectedRecording.value?.transcription) return;

        try {
            const transcriptionData = JSON.parse(selectedRecording.value.transcription);
            if (transcriptionData && Array.isArray(transcriptionData) && transcriptionData[segmentIndex]) {
                transcriptionData[segmentIndex].speaker = newSpeakerId;

                // STAGE the change in memory only. It is persisted when the
                // user clicks "Save Names" (saveSpeakerNames sees
                // editedTranscriptData and calls saveTranscriptEdits).
                //
                // We deliberately do NOT save immediately here. The old
                // behaviour called saveTranscriptImmediately on every single
                // per-line change, which (a) saved without the user asking
                // (the surprise "changes saved" toast) and (b) re-sent the
                // full speaker_map, which the backend applies by renaming
                // EVERY segment matching a mapped speaker — so changing one
                // line silently rewrote all the other lines of that speaker.
                editedTranscriptData.value = transcriptionData;

                // Update the recording's transcription in memory so the
                // modal reflects the change live (still unsaved).
                selectedRecording.value.transcription = JSON.stringify(transcriptionData);

                editingSpeakerIndex.value = null;
            }
        } catch (e) {
            console.error('Error changing speaker:', e);
            showToast(t('help.errorChangingSpeaker'), 'fa-exclamation-circle', 3000, 'error');
        }
    };

    return {
        getSpeakerColor,
        pollForSummaryCompletion,
        // Speaker modal
        openSpeakerModal,
        closeSpeakerModal,
        saveSpeakerNames,

        // Suggestions
        loadVoiceSuggestions,
        applyVoiceSuggestion,
        handleIsMeChange,

        // Auto-identify
        autoIdentifySpeakers,
        showAutoIdDropdown,
        autoIdSplitBtn,
        hasAnySuggestions,
        applySuggestedNames,

        // Add speaker
        openAddSpeakerModal,
        closeAddSpeakerModal,
        addNewSpeaker,
        searchNewSpeaker,
        selectNewSpeakerSuggestion,
        hideNewSpeakerSuggestionsDelayed,

        // Edit speakers modal
        openEditSpeakersModal,
        closeEditSpeakersModal,
        addEditingSpeaker,
        removeEditingSpeaker,
        filterEditingSpeakerSuggestions,
        selectEditingSpeakerSuggestion,
        closeEditingSpeakerSuggestions,
        onEditSpeakerBlur,
        getEditSpeakerDropdownPosition,
        saveEditingSpeakers,

        // Edit text
        openEditTextModal,
        closeEditTextModal,
        saveEditedText,

        // Change speaker
        openSpeakerChangeDropdown,
        changeSpeaker
    };
}
