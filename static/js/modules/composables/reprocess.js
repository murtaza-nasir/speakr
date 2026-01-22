/**
 * Reprocessing composable
 * Handles reprocessing transcription and summary
 */

export function useReprocess(state, utils) {
    const { nextTick } = Vue;

    const {
        showReprocessModal, showResetModal, reprocessType,
        reprocessRecording, recordingToReset, selectedRecording,
        recordings, asrReprocessOptions, summaryReprocessPromptSource,
        summaryReprocessSelectedTagId, summaryReprocessCustomPrompt,
        availableTags, processingProgress, processingMessage,
        currentlyProcessingFile, uploadQueue
    } = state;

    const { showToast, setGlobalError, onChatComplete } = utils;

    // Store for active polling intervals
    const reprocessingPolls = new Map();

    // =========================================
    // Reprocess Modal
    // =========================================

    const openReprocessModal = (type, recording = null) => {
        reprocessType.value = type;
        reprocessRecording.value = recording || selectedRecording.value;
        showReprocessModal.value = true;

        // Reset options
        if (type === 'transcription') {
            asrReprocessOptions.language = '';
            asrReprocessOptions.min_speakers = '';
            asrReprocessOptions.max_speakers = '';
        } else {
            summaryReprocessPromptSource.value = 'default';
            summaryReprocessSelectedTagId.value = '';
            summaryReprocessCustomPrompt.value = '';
        }
    };

    const closeReprocessModal = () => {
        showReprocessModal.value = false;
        reprocessRecording.value = null;
        reprocessType.value = null;
    };

    const confirmReprocess = openReprocessModal;
    const cancelReprocess = closeReprocessModal;

    // =========================================
    // Reset Status
    // =========================================

    const confirmReset = (recording) => {
        recordingToReset.value = recording;
        showResetModal.value = true;
    };

    const cancelReset = () => {
        showResetModal.value = false;
        recordingToReset.value = null;
    };

    const executeReset = async () => {
        if (!recordingToReset.value) return;

        const recordingId = recordingToReset.value.id;

        // Close the modal first
        cancelReset();

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${recordingId}/reset_status`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                }
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to reset recording status');

            // Update recording status in list
            const index = recordings.value.findIndex(r => r.id === recordingId);
            if (index !== -1) {
                recordings.value[index].status = 'FAILED';
            }

            if (selectedRecording.value?.id === recordingId) {
                selectedRecording.value.status = 'FAILED';
            }

            showToast('Recording status reset to FAILED', 'fa-undo');
        } catch (error) {
            setGlobalError(`Failed to reset status: ${error.message}`);
        }
    };

    const executeReprocess = async () => {
        if (!reprocessRecording.value || !reprocessType.value) return;

        const recordingId = reprocessRecording.value.id;
        const type = reprocessType.value;

        closeReprocessModal();

        if (type === 'transcription') {
            await reprocessTranscription(
                recordingId,
                asrReprocessOptions.language,
                asrReprocessOptions.min_speakers,
                asrReprocessOptions.max_speakers
            );
        } else {
            await reprocessSummary(
                recordingId,
                summaryReprocessPromptSource.value,
                summaryReprocessSelectedTagId.value,
                summaryReprocessCustomPrompt.value
            );
        }
    };

    // =========================================
    // Transcription Reprocessing
    // =========================================

    const reprocessTranscription = async (recordingId, language, minSpeakers, maxSpeakers) => {
        if (!recordingId) {
            setGlobalError('No recording ID provided for reprocessing.');
            return;
        }

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const requestBody = {
                language: language || ''  // Always send language - empty string means auto-detect
            };
            if (minSpeakers && minSpeakers !== '') requestBody.min_speakers = parseInt(minSpeakers);
            if (maxSpeakers && maxSpeakers !== '') requestBody.max_speakers = parseInt(maxSpeakers);

            const response = await fetch(`/recording/${recordingId}/reprocess_transcription`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify(requestBody)
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to start transcription reprocessing');

            // Update recording status in list
            const index = recordings.value.findIndex(r => r.id === recordingId);
            if (index !== -1) {
                recordings.value[index].status = 'PROCESSING';
            }

            if (selectedRecording.value?.id === recordingId) {
                selectedRecording.value.status = 'PROCESSING';
            }

            showToast('Transcription reprocessing started', 'fa-sync-alt');

            // Start polling for progress
            startReprocessingPoll(recordingId);
        } catch (error) {
            setGlobalError(`Failed to start transcription reprocessing: ${error.message}`);
        }
    };

    // =========================================
    // Summary Reprocessing
    // =========================================

    const reprocessSummary = async (recordingId, promptSource, selectedTagId, customPrompt) => {
        if (!recordingId) {
            setGlobalError('No recording ID provided for reprocessing.');
            return;
        }

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const requestBody = { reprocess_summary: true };

            if (promptSource === 'tag' && selectedTagId) {
                const selectedTag = availableTags.value.find(t => t.id == selectedTagId);
                if (selectedTag && selectedTag.custom_prompt) {
                    requestBody.custom_prompt = selectedTag.custom_prompt;
                }
            } else if (promptSource === 'custom' && customPrompt) {
                requestBody.custom_prompt = customPrompt;
            }

            const response = await fetch(`/recording/${recordingId}/reprocess_summary`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify(requestBody)
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to start summary reprocessing');

            // Update recording status in list
            const index = recordings.value.findIndex(r => r.id === recordingId);
            if (index !== -1) {
                recordings.value[index].status = 'SUMMARIZING';
            }

            if (selectedRecording.value?.id === recordingId) {
                selectedRecording.value.status = 'SUMMARIZING';
            }

            showToast('Summary reprocessing started', 'fa-sync-alt');

            // Start polling for progress
            startReprocessingPoll(recordingId);
        } catch (error) {
            setGlobalError(`Failed to start summary reprocessing: ${error.message}`);
        }
    };

    // =========================================
    // Generate Summary
    // =========================================

    const generateSummary = async () => {
        if (!selectedRecording.value) return;

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${selectedRecording.value.id}/generate_summary`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                }
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to start summary generation');

            selectedRecording.value.status = 'SUMMARIZING';

            const recordingInList = recordings.value.find(r => r.id === selectedRecording.value.id);
            if (recordingInList) {
                recordingInList.status = 'SUMMARIZING';
            }

            showToast('Summary generation started', 'fa-file-alt');

            // Start polling for progress
            startReprocessingPoll(selectedRecording.value.id);
        } catch (error) {
            setGlobalError(`Failed to generate summary: ${error.message}`);
        }
    };

    // =========================================
    // Progress Polling
    // =========================================

    const startReprocessingPoll = (recordingId) => {
        // Stop existing poll if any
        stopReprocessingPoll(recordingId);

        // Track if we've already fetched full data for SUMMARIZING status
        let hasFetchedForSummarizing = false;

        const pollInterval = setInterval(async () => {
            try {
                // Use lightweight status-only endpoint
                const response = await fetch(`/recording/${recordingId}/status`);
                if (!response.ok) throw new Error('Status check failed');

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
                if (selectedRecording.value?.id === recordingId) {
                    selectedRecording.value = {
                        ...selectedRecording.value,
                        status: statusData.status
                    };
                }

                // Check if summarization has started (fetch transcript) or processing is complete
                if (statusData.status === 'SUMMARIZING' || statusData.status === 'COMPLETED') {
                    // Only fetch once when status first becomes SUMMARIZING
                    const shouldFetch = (statusData.status === 'SUMMARIZING' && !hasFetchedForSummarizing) ||
                                      statusData.status === 'COMPLETED';

                    if (shouldFetch) {
                        // Mark that we've fetched for SUMMARIZING
                        if (statusData.status === 'SUMMARIZING') {
                            hasFetchedForSummarizing = true;
                        }

                        // Only stop polling if COMPLETED, keep polling during SUMMARIZING
                        if (statusData.status === 'COMPLETED') {
                            stopReprocessingPoll(recordingId);
                        }

                        // Fetch the full recording with updated data
                        const fullResponse = await fetch(`/api/recordings/${recordingId}`);

                        if (fullResponse.ok) {
                            const fullData = await fullResponse.json();

                            // Update in recordings list first
                            const currentIndex = recordings.value.findIndex(r => r.id === recordingId);
                            if (currentIndex !== -1) {
                                recordings.value[currentIndex] = fullData;
                            }

                            // Always update selectedRecording if it's the current recording
                            if (selectedRecording.value?.id === recordingId) {
                                selectedRecording.value = fullData;
                                await nextTick();
                            }
                        }

                        if (statusData.status === 'COMPLETED') {
                            showToast('Processing completed!', 'fa-check-circle');
                            // Refresh token budget after LLM operations complete
                            if (onChatComplete) onChatComplete();
                        }
                    }
                } else if (statusData.status === 'FAILED') {
                    stopReprocessingPoll(recordingId);

                    // Fetch full recording data to get error details for display
                    try {
                        const failedResponse = await fetch(`/api/recordings/${recordingId}`);
                        if (failedResponse.ok) {
                            const failedData = await failedResponse.json();

                            // Update in recordings list
                            const currentIndex = recordings.value.findIndex(r => r.id === recordingId);
                            if (currentIndex !== -1) {
                                recordings.value[currentIndex] = failedData;
                            }

                            // Update selectedRecording to show error in transcription panel
                            if (selectedRecording.value?.id === recordingId) {
                                selectedRecording.value = failedData;
                                await nextTick();
                            }
                        }
                    } catch (err) {
                        console.error('Failed to fetch error details:', err);
                    }

                    showToast('Processing failed', 'fa-exclamation-circle');
                }
            } catch (error) {
                console.error('Polling error:', error);
                stopReprocessingPoll(recordingId);
            }
        }, 3000);

        reprocessingPolls.set(recordingId, pollInterval);
    };

    const stopReprocessingPoll = (recordingId) => {
        if (reprocessingPolls.has(recordingId)) {
            clearInterval(reprocessingPolls.get(recordingId));
            reprocessingPolls.delete(recordingId);
        }
    };

    return {
        // Reprocess modal
        openReprocessModal,
        closeReprocessModal,
        confirmReprocess,
        cancelReprocess,
        executeReprocess,

        // Reset status
        confirmReset,
        cancelReset,
        executeReset,

        // Transcription
        reprocessTranscription,

        // Summary
        reprocessSummary,
        generateSummary,

        // Polling
        startReprocessingPoll,
        stopReprocessingPoll
    };
}
