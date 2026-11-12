/**
 * Reprocessing composable
 * Handles reprocessing transcription and summary
 */

import * as IncognitoStorage from '../db/incognito-storage.js';
import { readJsonResponse } from '../utils/response.js';
import { buildRegenerateBody, normalizePrompt } from '../utils/regenerate-summary.js';

export function useReprocess(state, utils) {
    const { nextTick } = Vue;

    const {
        showReprocessModal, showResetModal, reprocessType,
        reprocessRecording, recordingToReset, selectedRecording,
        recordings, asrReprocessOptions, reprocessPromptVariables,
        processingProgress, processingMessage,
        currentlyProcessingFile, uploadQueue, userTranscriptionLanguage,
        showRegenerateSummaryModal, regenSummary
    } = state;

    const { showToast, setGlobalError, onChatComplete } = utils;
    const t = (key, params) => (utils.t ? utils.t(key, params) : key);

    // Store for active polling intervals
    const reprocessingPolls = new Map();

    // =========================================
    // Reprocess Modal
    // =========================================

    const openReprocessModal = (type, recording = null) => {
        // Summaries have their own dialog (#584); this one is transcription only.
        if (type === 'summary') {
            openRegenerateSummaryModal(recording);
            return;
        }
        reprocessType.value = type;
        reprocessRecording.value = recording || selectedRecording.value;
        showReprocessModal.value = true;

        // Reset options
        if (type === 'transcription') {
            asrReprocessOptions.language = userTranscriptionLanguage?.value || '';
            asrReprocessOptions.min_speakers = '';
            asrReprocessOptions.max_speakers = '';
            // Pre-fill the hints with what this recording actually used so the
            // user can see and iterate on them rather than starting blank (#309).
            asrReprocessOptions.hotwords = reprocessRecording.value?.resolved_hotwords || '';
            asrReprocessOptions.initial_prompt = reprocessRecording.value?.resolved_initial_prompt || '';
            asrReprocessOptions.transcription_model = '';
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

            const data = await readJsonResponse(response, t, 'errors.resetStatusFailed');

            // Update recording status in list
            const index = recordings.value.findIndex(r => r.id === recordingId);
            if (index !== -1) {
                recordings.value[index].status = 'FAILED';
            }

            if (selectedRecording.value?.id === recordingId) {
                selectedRecording.value.status = 'FAILED';
            }

            showToast(t('messages.statusResetToFailed'), 'fa-undo');
        } catch (error) {
            setGlobalError(t('errors.resetStatusFailedDetail', { error: error.message }));
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
                asrReprocessOptions.max_speakers,
                asrReprocessOptions.hotwords,
                asrReprocessOptions.initial_prompt,
                asrReprocessOptions.transcription_model
            );
        }
    };

    // =========================================
    // Transcription Reprocessing
    // =========================================

    const reprocessTranscription = async (recordingId, language, minSpeakers, maxSpeakers, hotwords, initialPrompt, transcriptionModel) => {
        if (!recordingId) {
            setGlobalError(t('errors.noRecordingToReprocess'));
            return;
        }

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const requestBody = {
                language: language || ''  // Always send language - empty string means auto-detect
            };
            if (minSpeakers && minSpeakers !== '') requestBody.min_speakers = parseInt(minSpeakers);
            if (maxSpeakers && maxSpeakers !== '') requestBody.max_speakers = parseInt(maxSpeakers);
            if (hotwords && hotwords.trim()) requestBody.hotwords = hotwords.trim();
            if (initialPrompt && initialPrompt.trim()) requestBody.initial_prompt = initialPrompt.trim();
            if (transcriptionModel && transcriptionModel.trim()) requestBody.transcription_model = transcriptionModel.trim();

            const response = await fetch(`/recording/${recordingId}/reprocess_transcription`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify(requestBody)
            });

            const data = await readJsonResponse(response, t, 'errors.reprocessTranscriptionFailed');

            // Update recording status in list
            const index = recordings.value.findIndex(r => r.id === recordingId);
            if (index !== -1) {
                recordings.value[index].status = 'PROCESSING';
            }

            if (selectedRecording.value?.id === recordingId) {
                selectedRecording.value.status = 'PROCESSING';
            }

            showToast(t('messages.transcriptionReprocessingStarted'), 'fa-sync-alt');

            // Start polling for progress
            startReprocessingPoll(recordingId);
        } catch (error) {
            setGlobalError(t('errors.startTranscriptionReprocessingFailedDetail', { error: error.message }));
        }
    };

    // =========================================
    // Summary Reprocessing
    // =========================================

    // body: the prompt fields of the request (buildRegenerateBody); an empty
    // body regenerates with the resolved prompt.
    const reprocessSummary = async (recordingId, body = {}) => {
        if (!recordingId) {
            setGlobalError(t('errors.noRecordingToReprocess'));
            return;
        }

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const requestBody = { reprocess_summary: true, ...body };

            const response = await fetch(`/recording/${recordingId}/reprocess_summary`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify(requestBody)
            });

            const data = await readJsonResponse(response, t, 'errors.reprocessSummaryFailed');

            // Update recording status in list
            const index = recordings.value.findIndex(r => r.id === recordingId);
            if (index !== -1) {
                recordings.value[index].status = 'SUMMARIZING';
            }

            if (selectedRecording.value?.id === recordingId) {
                selectedRecording.value.status = 'SUMMARIZING';
            }

            showToast(t('messages.summaryReprocessingStarted'), 'fa-sync-alt');

            // Start polling for progress
            startReprocessingPoll(recordingId);
        } catch (error) {
            setGlobalError(t('errors.startSummaryReprocessingFailedDetail', { error: error.message }));
        }
    };

    // =========================================
    // Generate Summary
    // =========================================

    // =========================================
    // Regenerate summary dialog (#584)
    // =========================================

    // Show a prompt description from GET /recording/<id>/summary_prompt.
    // fill: put its resolved prompt in the textarea as the starting point.
    const applySummaryPromptInfo = (data, fill) => {
        regenSummary.resolvedPrompt = data.prompt || '';
        regenSummary.isOwner = !!data.is_owner;
        regenSummary.startFrom = Array.isArray(data.start_from) ? data.start_from : [];
        if (fill) {
            regenSummary.prompt = data.prompt || '';
            regenSummary.base = {
                key: data.source_key,
                kind: data.source_key === 'tags' ? 'tags' : data.source,
                name: data.source_name || '',
                prompt: data.prompt || '',
                save_target: data.save_target || null,
            };
        }
    };

    const openRegenerateSummaryModal = async (recording = null) => {
        const rec = recording || selectedRecording.value;
        if (!rec || rec.incognito === true) return;
        Object.assign(regenSummary, {
            recording: rec,
            // Without a summary the dialog generates one, as the empty-state
            // button does; otherwise it replaces the summary.
            generate: !rec.summary,
            loading: true,
            loadError: '',
            saving: false,
            prompt: '',
            resolvedPrompt: '',
            instructions: '',
            isOwner: false,
            base: null,
            startFrom: [],
            startFromOpen: false,
            variablesOpen: false,
        });
        // Hydrate the prompt-variables form from the recording's saved values
        // so the user starts with what they had at upload.
        for (const k of Object.keys(reprocessPromptVariables)) {
            delete reprocessPromptVariables[k];
        }
        for (const [k, v] of Object.entries(rec.prompt_variables || {})) {
            reprocessPromptVariables[k] = v == null ? '' : String(v);
        }
        showRegenerateSummaryModal.value = true;
        try {
            const response = await fetch(`/recording/${rec.id}/summary_prompt`);
            const data = await readJsonResponse(response, t, 'regenerateSummary.loadFailed');
            if (regenSummary.recording !== rec) return;
            applySummaryPromptInfo(data, true);
        } catch (error) {
            regenSummary.loadError = t('regenerateSummary.loadFailedDetail', { error: error.message });
        } finally {
            if (regenSummary.recording === rec) regenSummary.loading = false;
        }
    };

    const closeRegenerateSummaryModal = () => {
        showRegenerateSummaryModal.value = false;
        regenSummary.recording = null;
        regenSummary.startFromOpen = false;
    };

    // Fill the textarea from a Start from entry; null is Blank.
    const startRegenerateFrom = (option) => {
        if (option) {
            regenSummary.prompt = option.prompt || '';
            regenSummary.base = { ...option };
        } else {
            regenSummary.prompt = '';
            regenSummary.base = { key: 'blank', kind: 'blank', name: '', prompt: '', save_target: null };
        }
        regenSummary.startFromOpen = false;
    };

    const resetRegeneratePrompt = () => {
        regenSummary.prompt = regenSummary.base ? (regenSummary.base.prompt || '') : regenSummary.resolvedPrompt;
    };

    // Write the edited prompt to the base's save target. Only on request.
    const saveRegeneratePrompt = async () => {
        const rec = regenSummary.recording;
        const target = regenSummary.base && regenSummary.base.save_target;
        if (!rec || !target || regenSummary.saving) return;
        const prompt = normalizePrompt(regenSummary.prompt);
        regenSummary.saving = true;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${rec.id}/summary_prompt`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                body: JSON.stringify({ target: target.kind, id: target.id, prompt }),
            });
            const data = await readJsonResponse(response, t, 'regenerateSummary.saveFailed');
            if (regenSummary.recording !== rec) return;
            applySummaryPromptInfo(data, false);
            // The saved place is now the base, so the prompt reads as unedited.
            const key = target.kind === 'user' ? 'user' : `${target.kind}:${target.id}`;
            const saved = regenSummary.startFrom.find(o => o.key === key);
            regenSummary.base = saved ? { ...saved } : { ...regenSummary.base, prompt };
            if (target.kind === 'user' && state.userSummaryPrompt) state.userSummaryPrompt.value = prompt;
            if (target.kind === 'tag' && state.availableTags && Array.isArray(state.availableTags.value)) {
                const tag = state.availableTags.value.find(tg => tg.id === target.id);
                if (tag) tag.custom_prompt = prompt;
            }
            showToast(t('regenerateSummary.saved'), 'fa-check-circle');
        } catch (error) {
            setGlobalError(t('regenerateSummary.saveFailedDetail', { error: error.message }));
        } finally {
            regenSummary.saving = false;
        }
    };

    const executeRegenerateSummary = async () => {
        const rec = regenSummary.recording;
        if (!rec || regenSummary.loading || regenSummary.loadError) return;
        if (!normalizePrompt(regenSummary.prompt)) return;
        const body = buildRegenerateBody({
            prompt: regenSummary.prompt,
            resolvedPrompt: regenSummary.resolvedPrompt,
            isOwner: regenSummary.isOwner,
            instructions: regenSummary.instructions,
            variables: { ...reprocessPromptVariables },
        });
        const generate = regenSummary.generate;
        closeRegenerateSummaryModal();
        if (generate) {
            await generateSummary({ body });
        } else {
            await reprocessSummary(rec.id, body);
        }
    };

    // options.body: prompt fields from the Regenerate summary dialog; without
    // it the summary uses the resolved prompt.
    const generateSummary = async (options = {}) => {
        if (!selectedRecording.value) return;

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

            // Check if this is an incognito recording
            if (selectedRecording.value.incognito === true) {
                // Use incognito summary endpoint - generate synchronously
                const response = await fetch('/api/recordings/incognito/summary', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfToken
                    },
                    body: JSON.stringify({
                        transcription: selectedRecording.value.transcription
                    })
                });

                const data = await readJsonResponse(response, t, 'errors.generateSummaryFailed');

                // Update the incognito recording with the new summary
                selectedRecording.value.summary = data.summary;
                selectedRecording.value.summary_html = data.summary_html;

                // Update sessionStorage
                IncognitoStorage.updateIncognitoRecording({
                    summary: data.summary,
                    summary_html: data.summary_html
                });

                showToast(t('messages.summaryGenerated'), 'fa-file-alt');
                return;
            }

            // Regular recording - use existing flow, with the dialog's prompt
            // fields when it opened this (#253, #584).
            const body = options.body || {};
            const response = await fetch(`/recording/${selectedRecording.value.id}/generate_summary`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify(body),
            });

            const data = await readJsonResponse(response, t, 'errors.generateSummaryFailed');

            selectedRecording.value.status = 'SUMMARIZING';

            const recordingInList = recordings.value.find(r => r.id === selectedRecording.value.id);
            if (recordingInList) {
                recordingInList.status = 'SUMMARIZING';
            }

            showToast(t('messages.summaryGenerationStarted'), 'fa-file-alt');

            // Start polling for progress
            startReprocessingPoll(selectedRecording.value.id);
        } catch (error) {
            setGlobalError(t('errors.generateSummaryFailedDetail', { error: error.message }));
        }
    };

    // =========================================
    // Progress Polling
    // =========================================

    const startReprocessingPoll = (recordingId, options = {}) => {
        // Stop existing poll if any
        stopReprocessingPoll(recordingId);

        // Track if we've already fetched full data for SUMMARIZING status
        let hasFetchedForSummarizing = false;
        // Fire options.onAudioReady exactly once, when the recording's audio
        // first becomes playable (used by merge to remove deleted originals and
        // enable the play button — see bulk-operations.js).
        let audioReadyFired = false;

        const pollInterval = setInterval(async () => {
            try {
                // Use lightweight status-only endpoint
                const response = await fetch(`/recording/${recordingId}/status`);
                // Polls every few seconds, so a raw parse error here would
                // repeat until the poll gives up. Same reader as the rest.
                const statusData = await readJsonResponse(response, t, 'errors.statusCheckFailed');

                // Update status (and audio_ready, when the endpoint reports it)
                // in the recordings list.
                const index = recordings.value.findIndex(r => r.id === recordingId);

                if (index !== -1) {
                    // Create new object to ensure Vue reactivity
                    recordings.value[index] = {
                        ...recordings.value[index],
                        status: statusData.status,
                        ...(statusData.audio_ready !== undefined ? { audio_ready: statusData.audio_ready } : {})
                    };
                }

                // Update selectedRecording with new object reference for reactivity
                if (selectedRecording.value?.id === recordingId) {
                    selectedRecording.value = {
                        ...selectedRecording.value,
                        status: statusData.status,
                        ...(statusData.audio_ready !== undefined ? { audio_ready: statusData.audio_ready } : {})
                    };
                }

                // One-shot callback when audio first becomes playable.
                if (!audioReadyFired && statusData.audio_ready === true) {
                    audioReadyFired = true;
                    if (typeof options.onAudioReady === 'function') {
                        try { options.onAudioReady(); } catch (e) { console.error('onAudioReady error:', e); }
                    }
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
                            showToast((utils.t && utils.t('toasts.processingCompleted')) || 'Processing finished', 'fa-check-circle');
                            // Refresh token budget after LLM operations complete
                            if (onChatComplete) onChatComplete();
                        }
                    }
                } else if (statusData.status === 'FAILED') {
                    stopReprocessingPoll(recordingId);

                    // Fetch full recording data to get error details for display
                    let failureReason = '';
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

                            // The backend stores the failure reason as a marker in
                            // the summary (e.g. "[Summary generation failed: Monthly
                            // token budget exceeded ...]"). Pull the reason out so the
                            // toast explains WHY, not just "failed".
                            const m = (failedData.summary || '').match(/^\[Summary[^:\]]*:\s*([\s\S]+?)\]\s*$/);
                            if (m) failureReason = m[1].trim();
                        }
                    } catch (err) {
                        console.error('Failed to fetch error details:', err);
                    }

                    const base = (utils.t && utils.t('toasts.processingFailed')) || 'Processing failed';
                    showToast(failureReason ? `${base}: ${failureReason}` : base, 'fa-exclamation-circle', 6000, 'error');
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
        openRegenerateSummaryModal,
        closeRegenerateSummaryModal,
        startRegenerateFrom,
        resetRegeneratePrompt,
        saveRegeneratePrompt,
        executeRegenerateSummary,

        // Polling
        startReprocessingPoll,
        stopReprocessingPoll
    };
}
