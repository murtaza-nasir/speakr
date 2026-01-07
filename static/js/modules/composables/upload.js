/**
 * Upload management composable
 * Handles file uploads, queue processing, and progress tracking
 */

import * as FailedUploads from '../db/failed-uploads.js';

export function useUpload(state, utils) {
    const {
        uploadQueue, currentlyProcessingFile, processingProgress, processingMessage,
        isProcessingActive, pollInterval, progressPopupMinimized, progressPopupClosed,
        maxFileSizeMB, chunkingEnabled, chunkingMode, chunkingLimit,
        recordings, selectedRecording, totalRecordings, globalError,
        selectedTagIds, uploadLanguage, uploadMinSpeakers, uploadMaxSpeakers,
        useAsrEndpoint, asrLanguage, asrMinSpeakers, asrMaxSpeakers,
        dragover, availableTags, uploadTagSearchFilter
    } = state;

    const { computed, nextTick } = Vue;

    const { setGlobalError, showToast, formatFileSize } = utils;

    // Compute selected tags from IDs
    const selectedTags = computed(() => {
        return selectedTagIds.value.map(id =>
            availableTags.value.find(t => t.id === id)
        ).filter(Boolean);
    });

    // Handle drag events
    const handleDragOver = (e) => {
        e.preventDefault();
        dragover.value = true;
    };

    const handleDragLeave = (e) => {
        if (e.relatedTarget && e.currentTarget.contains(e.relatedTarget)) {
            return;
        }
        dragover.value = false;
    };

    const handleDrop = (e) => {
        e.preventDefault();
        dragover.value = false;
        addFilesToQueue(e.dataTransfer.files);
    };

    const handleFileSelect = (e) => {
        addFilesToQueue(e.target.files);
        e.target.value = null;
    };

    // Add files to the upload queue
    const addFilesToQueue = (files) => {
        let filesAdded = 0;
        for (const file of files) {
            const fileObject = file.file ? file.file : file;
            const notes = file.notes || null;
            const tags = file.tags || selectedTags.value || [];
            const asrOptions = file.asrOptions || {
                language: asrLanguage.value,
                min_speakers: asrMinSpeakers.value,
                max_speakers: asrMaxSpeakers.value
            };

            // Check if it's an audio file or video container with audio
            const isAudioFile = fileObject && (
                fileObject.type.startsWith('audio/') ||
                fileObject.type === 'video/mp4' ||
                fileObject.type === 'video/quicktime' ||
                fileObject.type === 'video/x-msvideo' ||
                fileObject.type === 'video/webm' ||
                fileObject.name.toLowerCase().endsWith('.amr') ||
                fileObject.name.toLowerCase().endsWith('.3gp') ||
                fileObject.name.toLowerCase().endsWith('.3gpp') ||
                fileObject.name.toLowerCase().endsWith('.mp4') ||
                fileObject.name.toLowerCase().endsWith('.mov') ||
                fileObject.name.toLowerCase().endsWith('.avi') ||
                fileObject.name.toLowerCase().endsWith('.mkv') ||
                fileObject.name.toLowerCase().endsWith('.webm') ||
                fileObject.name.toLowerCase().endsWith('.weba')
            );

            if (isAudioFile) {
                // Only check general file size limit
                if (fileObject.size > maxFileSizeMB.value * 1024 * 1024) {
                    setGlobalError(`File "${fileObject.name}" exceeds the maximum size of ${maxFileSizeMB.value} MB and was skipped.`);
                    continue;
                }

                const clientId = `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;

                uploadQueue.value.push({
                    file: fileObject,
                    notes: notes,
                    tags: tags,
                    asrOptions: asrOptions,
                    status: 'queued',
                    recordingId: null,
                    clientId: clientId,
                    error: null,
                    willAutoSummarize: false // Server will tell us via SUMMARIZING status
                });
                filesAdded++;
            } else if (fileObject) {
                setGlobalError(`Invalid file type "${fileObject.name}". Only audio files and video containers with audio (MP3, WAV, MP4, MOV, AVI, etc.) are accepted. File skipped.`);
            }
        }
        if (filesAdded > 0) {
            console.log(`Added ${filesAdded} file(s) to the queue.`);
        }
    };

    // Remove a file from the queue before processing starts
    const removeFromQueue = (clientId) => {
        const index = uploadQueue.value.findIndex(item => item.clientId === clientId);
        if (index !== -1 && (uploadQueue.value[index].status === 'queued' || uploadQueue.value[index].status === 'ready')) {
            uploadQueue.value.splice(index, 1);
            console.log(`Removed file from queue: ${clientId}`);
        }
    };

    // Cancel a waiting file from the upload progress queue
    const cancelWaitingFile = (clientId) => {
        const index = uploadQueue.value.findIndex(item => item.clientId === clientId);
        if (index !== -1 && uploadQueue.value[index].status === 'ready') {
            uploadQueue.value.splice(index, 1);
            console.log(`Cancelled waiting file: ${clientId}`);
            showToast('File removed from queue', 'fa-trash');
        }
    };

    // Clear completed uploads from queue
    const clearCompletedUploads = () => {
        uploadQueue.value = uploadQueue.value.filter(item => !['completed', 'failed'].includes(item.status));
    };

    // Start processing all queued files
    const startUpload = () => {
        const pendingFiles = uploadQueue.value.filter(item => item.status === 'queued');
        if (pendingFiles.length === 0) {
            return;
        }
        // Update all queued files with current tags and ASR options
        // AND change their status to 'ready' so they move to upload progress immediately
        for (const item of uploadQueue.value) {
            if (item.status === 'queued') {
                // Always use the CURRENT tag selection - override whatever was captured at add time
                item.tags = [...selectedTags.value];
                // Always use the CURRENT ASR options
                item.asrOptions = {
                    language: asrLanguage.value,
                    min_speakers: asrMinSpeakers.value,
                    max_speakers: asrMaxSpeakers.value
                };
                // Change status to 'ready' to remove from upload view but keep in queue
                item.status = 'ready';
            }
        }
        progressPopupMinimized.value = false;
        progressPopupClosed.value = false;
        if (!isProcessingActive.value) {
            startProcessingQueue();
        }
    };

    const resetCurrentFileProcessingState = () => {
        if (pollInterval.value) clearInterval(pollInterval.value);
        pollInterval.value = null;
        currentlyProcessingFile.value = null;
        processingProgress.value = 0;
        processingMessage.value = '';
    };

    const startProcessingQueue = async () => {
        console.log("Attempting to start processing queue...");
        if (isProcessingActive.value) {
            console.log("Queue processor already active.");
            return;
        }

        isProcessingActive.value = true;
        resetCurrentFileProcessingState();

        const nextFileItem = uploadQueue.value.find(item => item.status === 'ready' || item.status === 'queued');

        if (nextFileItem) {
            console.log(`Processing next file: ${nextFileItem.file.name} (Client ID: ${nextFileItem.clientId})`);
            currentlyProcessingFile.value = nextFileItem;

            // Check if this is a "reload" item (existing recording being tracked)
            if (nextFileItem.clientId.startsWith('reload-')) {
                console.log(`Skipping upload for existing recording: ${nextFileItem.recordingId}`);
                nextFileItem.status = 'processing';
                startStatusPolling(nextFileItem, nextFileItem.recordingId);
                return;
            }

            nextFileItem.status = 'uploading';
            processingMessage.value = 'Preparing upload...';
            processingProgress.value = 5;

            try {
                const formData = new FormData();
                formData.append('file', nextFileItem.file);

                // Send file's lastModified timestamp for meeting_date
                if (nextFileItem.file.lastModified) {
                    const lastModified = nextFileItem.file.lastModified;
                    formData.append('file_last_modified', lastModified.toString());
                    console.log(`[Upload] File lastModified: ${lastModified} (${new Date(lastModified).toISOString()})`);
                }

                if (nextFileItem.notes) {
                    formData.append('notes', nextFileItem.notes);
                }

                // Add tags if selected
                const tagsToUse = nextFileItem.tags || selectedTags.value || [];
                tagsToUse.forEach((tag, index) => {
                    const tagId = tag.id || tag;
                    formData.append(`tag_ids[${index}]`, tagId);
                });

                // Add ASR advanced options if ASR endpoint is enabled
                if (useAsrEndpoint.value) {
                    const asrOpts = nextFileItem.asrOptions || {};
                    const language = asrOpts.language || uploadLanguage.value;
                    const minSpeakers = asrOpts.min_speakers || uploadMinSpeakers.value;
                    const maxSpeakers = asrOpts.max_speakers || uploadMaxSpeakers.value;

                    if (language) {
                        formData.append('language', language);
                    }
                    if (minSpeakers && minSpeakers !== '') {
                        formData.append('min_speakers', minSpeakers.toString());
                    }
                    if (maxSpeakers && maxSpeakers !== '') {
                        formData.append('max_speakers', maxSpeakers.toString());
                    }
                }

                processingMessage.value = 'Uploading file...';
                processingProgress.value = 10;

                const response = await fetch('/upload', { method: 'POST', body: formData });

                // Safely parse JSON response, handling HTML error pages
                let data;
                const contentType = response.headers.get('content-type') || '';
                if (!contentType.includes('application/json')) {
                    const text = await response.text();
                    const titleMatch = text.match(/<title>([^<]+)<\/title>/i);
                    const h1Match = text.match(/<h1>([^<]+)<\/h1>/i);
                    throw new Error(titleMatch?.[1] || h1Match?.[1] ||
                        `Server error (${response.status}): Response was not JSON`);
                }
                data = await response.json();

                if (!response.ok) {
                    let errorMsg = data.error || `Upload failed with status ${response.status}`;
                    if (response.status === 413) errorMsg = data.error || `File too large. Max: ${data.max_size_mb?.toFixed(0) || maxFileSizeMB.value} MB.`;
                    throw new Error(errorMsg);
                }

                if (response.status === 202 && data.id) {
                    console.log(`File ${nextFileItem.file.name} uploaded. Recording ID: ${data.id}. Starting status poll.`);
                    nextFileItem.status = 'pending';
                    nextFileItem.recordingId = data.id;
                    processingMessage.value = 'Upload complete. Waiting for processing...';
                    processingProgress.value = 30;

                    recordings.value.unshift(data);
                    totalRecordings.value++;
                    pollProcessingStatus(nextFileItem);

                } else {
                    throw new Error('Unexpected success response from server after upload.');
                }

            } catch (error) {
                console.error(`Upload/Processing Error for ${nextFileItem.file.name} (Client ID: ${nextFileItem.clientId}):`, error);
                nextFileItem.status = 'failed';
                nextFileItem.error = error.message;
                const failedRecordIndex = recordings.value.findIndex(r => r.id === nextFileItem.recordingId);
                if (failedRecordIndex !== -1) {
                    recordings.value[failedRecordIndex].status = 'FAILED';
                    recordings.value[failedRecordIndex].transcription = `Upload/Processing failed: ${error.message}`;
                } else {
                    setGlobalError(`Failed to process "${nextFileItem.file.name}": ${error.message}`);
                }

                // Store failed upload in IndexedDB for background sync retry
                try {
                    await FailedUploads.storeFailedUpload({
                        file: nextFileItem.file,
                        fileName: nextFileItem.file.name,
                        fileSize: nextFileItem.file.size,
                        clientId: nextFileItem.clientId,
                        notes: nextFileItem.notes,
                        tags: nextFileItem.tags,
                        asrOptions: nextFileItem.asrOptions,
                        error: error.message
                    });

                    // Register for background sync
                    if ('serviceWorker' in navigator && 'sync' in ServiceWorkerRegistration.prototype) {
                        const registration = await navigator.serviceWorker.ready;
                        await registration.sync.register('sync-uploads');
                        console.log('[Upload] Registered background sync for failed upload');
                        showToast('Upload will retry automatically when connection is restored', 'info');
                    }
                } catch (syncError) {
                    console.warn('[Upload] Failed to register background sync:', syncError);
                }

                resetCurrentFileProcessingState();
                isProcessingActive.value = false;
                await Vue.nextTick();
                startProcessingQueue();
            }
        } else {
            console.log("Upload queue is empty or no files are queued.");
            isProcessingActive.value = false;
        }
    };

    const startStatusPolling = (fileItem, recordingId) => {
        fileItem.recordingId = recordingId;
        pollProcessingStatus(fileItem);
    };

    const pollProcessingStatus = (fileItem) => {
        if (pollInterval.value) clearInterval(pollInterval.value);

        const recordingId = fileItem.recordingId;
        if (!recordingId) {
            console.error("Cannot poll status without recording ID for", fileItem.file.name);
            fileItem.status = 'failed';
            fileItem.error = 'Internal error: Missing recording ID for polling.';
            resetCurrentFileProcessingState();
            isProcessingActive.value = false;
            Vue.nextTick(startProcessingQueue);
            return;
        }

        processingMessage.value = 'Waiting for transcription...';
        processingProgress.value = 40;

        pollInterval.value = setInterval(async () => {
            const shouldStopPolling = !currentlyProcessingFile.value ||
                                     currentlyProcessingFile.value.clientId !== fileItem.clientId ||
                                     fileItem.status === 'failed' ||
                                     (fileItem.status === 'completed' && (!fileItem.willAutoSummarize || fileItem.summaryCompleted));

            if (shouldStopPolling) {
                console.log(`Polling stopped for ${fileItem.clientId} as it's no longer active or finished.`);
                clearInterval(pollInterval.value);
                pollInterval.value = null;
                if (currentlyProcessingFile.value && currentlyProcessingFile.value.clientId === fileItem.clientId) {
                    resetCurrentFileProcessingState();
                    isProcessingActive.value = false;
                    await Vue.nextTick();
                    startProcessingQueue();
                }
                return;
            }

            try {
                console.log(`Polling status for recording ID: ${recordingId} (${fileItem.file.name})`);
                // Use lightweight status-only endpoint
                const response = await fetch(`/recording/${recordingId}/status`);
                if (!response.ok) throw new Error(`Status check failed with status ${response.status}`);

                // Safely parse JSON response, handling HTML error pages
                const statusContentType = response.headers.get('content-type') || '';
                if (!statusContentType.includes('application/json')) {
                    const text = await response.text();
                    const titleMatch = text.match(/<title>([^<]+)<\/title>/i);
                    const h1Match = text.match(/<h1>([^<]+)<\/h1>/i);
                    throw new Error(titleMatch?.[1] || h1Match?.[1] ||
                        `Server error (${response.status}): Status response was not JSON`);
                }
                const statusData = await response.json();
                const galleryIndex = recordings.value.findIndex(r => r.id === recordingId);

                // Update status in recordings list
                if (galleryIndex !== -1) {
                    // Create new object to ensure Vue reactivity
                    recordings.value[galleryIndex] = {
                        ...recordings.value[galleryIndex],
                        status: statusData.status
                    };

                    // Update selectedRecording with new object reference for reactivity
                    if (selectedRecording.value?.id === recordingId) {
                        selectedRecording.value = {
                            ...selectedRecording.value,
                            status: statusData.status
                        };
                    }
                }

                const previousStatus = fileItem.status;
                fileItem.status = statusData.status;

                if (statusData.status === 'COMPLETED') {
                    // Fetch full recording data when complete
                    const fullResponse = await fetch(`/api/recordings/${recordingId}`);
                    if (fullResponse.ok) {
                        const data = await fullResponse.json();

                        // Update recordings list first
                        if (galleryIndex !== -1) {
                            recordings.value[galleryIndex] = data;
                        }

                        // Always update selectedRecording if it's the current recording,
                        // even if it's not in the current recordings list (e.g., filtered out)
                        if (selectedRecording.value?.id === recordingId) {
                            selectedRecording.value = data;
                            // Force Vue to detect the change
                            await nextTick();
                        }

                        // Store display name separately since File.name is read-only
                        fileItem.displayName = data.title || data.original_filename || fileItem.file.name;
                    }

                    console.log(`Processing COMPLETED for ${fileItem.displayName} (ID: ${recordingId})`);

                    if (previousStatus === 'summarizing') {
                        console.log(`Auto-summary completed for ${fileItem.displayName}`);
                        processingMessage.value = 'Processing complete!';
                        processingProgress.value = 100;
                        fileItem.status = 'completed';
                        fileItem.summaryCompleted = true;

                        clearInterval(pollInterval.value);
                        pollInterval.value = null;
                        resetCurrentFileProcessingState();
                        isProcessingActive.value = false;

                        startProcessingQueue();
                        return;
                    } else if (fileItem.willAutoSummarize && !fileItem.hasCheckedForAutoSummary) {
                        processingMessage.value = 'Transcription complete!';
                        processingProgress.value = 85;
                        fileItem.status = 'awaiting_summary';
                        fileItem.hasCheckedForAutoSummary = true;
                        fileItem.autoSummaryStartTime = Date.now();
                        return;
                    } else if (fileItem.willAutoSummarize && fileItem.hasCheckedForAutoSummary) {
                        const waitTime = Date.now() - fileItem.autoSummaryStartTime;
                        const maxWaitTime = 5000; // 5 seconds - if no summarization starts, complete

                        if (waitTime > maxWaitTime) {
                            console.log(`Auto-summary did not start within ${maxWaitTime}ms, completing`);
                            processingMessage.value = 'Processing complete!';
                            processingProgress.value = 100;
                            fileItem.status = 'completed';
                            fileItem.summaryCompleted = false; // Summary didn't happen
                            clearInterval(pollInterval.value);
                            pollInterval.value = null;
                            resetCurrentFileProcessingState();
                            isProcessingActive.value = false;
                            startProcessingQueue();
                            return;
                        }
                        return;
                    } else {
                        processingMessage.value = 'Processing complete!';
                        processingProgress.value = 100;
                        fileItem.status = 'completed';
                        fileItem.summaryCompleted = true;

                        clearInterval(pollInterval.value);
                        pollInterval.value = null;
                        resetCurrentFileProcessingState();
                        isProcessingActive.value = false;
                        startProcessingQueue();
                        return;
                    }

                } else if (statusData.status === 'FAILED') {
                    console.log(`Processing FAILED for ${fileItem.displayName || fileItem.file.name} (ID: ${recordingId})`);
                    processingMessage.value = 'Processing failed.';
                    processingProgress.value = 100;
                    fileItem.status = 'failed';

                    // Fetch full data to get error details
                    try {
                        const failedResponse = await fetch(`/api/recordings/${recordingId}`);
                        if (failedResponse.ok) {
                            const failedData = await failedResponse.json();
                            fileItem.error = failedData.error_message || 'Processing failed on server.';
                        } else {
                            fileItem.error = 'Processing failed on server.';
                        }
                    } catch (err) {
                        fileItem.error = 'Processing failed on server.';
                    }

                    setGlobalError(`Processing failed for "${fileItem.displayName || fileItem.file.name}".`);
                    clearInterval(pollInterval.value);
                    pollInterval.value = null;
                    resetCurrentFileProcessingState();
                    isProcessingActive.value = false;
                    await Vue.nextTick();
                    startProcessingQueue();

                } else if (statusData.status === 'PROCESSING') {
                    const couldUseChunking = chunkingEnabled.value && !useAsrEndpoint.value;

                    if (couldUseChunking) {
                        if (chunkingMode.value === 'size') {
                            const chunkThresholdBytes = chunkingLimit.value * 1024 * 1024;
                            const willUseChunking = fileItem.file.size > chunkThresholdBytes;

                            if (willUseChunking) {
                                processingMessage.value = 'Processing large file (chunking in progress)...';
                                const maxProgress = fileItem.willAutoSummarize ? 70 : 80;
                                processingProgress.value = Math.round(Math.min(maxProgress, processingProgress.value + Math.random() * 3));
                            } else {
                                processingMessage.value = 'Transcription in progress...';
                                const maxProgress = fileItem.willAutoSummarize ? 65 : 75;
                                processingProgress.value = Math.round(Math.min(maxProgress, processingProgress.value + Math.random() * 5));
                            }
                        } else {
                            processingMessage.value = 'Processing file (chunking determined server-side)...';
                            const maxProgress = fileItem.willAutoSummarize ? 70 : 80;
                            processingProgress.value = Math.round(Math.min(maxProgress, processingProgress.value + Math.random() * 3));
                        }
                    } else {
                        processingMessage.value = 'Transcription in progress...';
                        const maxProgress = fileItem.willAutoSummarize ? 65 : 75;
                        processingProgress.value = Math.round(Math.min(maxProgress, processingProgress.value + Math.random() * 5));
                    }
                } else if (statusData.status === 'SUMMARIZING') {
                    console.log(`Auto-summary started for ${fileItem.displayName || fileItem.file.name}`);
                    processingMessage.value = 'Generating summary...';
                    processingProgress.value = 90;
                    fileItem.status = 'summarizing';
                } else {
                    processingMessage.value = 'Waiting in queue...';
                    processingProgress.value = 45;
                }
            } catch (error) {
                console.error(`Polling Error for ${fileItem.displayName || fileItem.file.name} (ID: ${recordingId}):`, error);
                fileItem.status = 'failed';
                fileItem.error = `Error checking status: ${error.message}`;
                setGlobalError(`Error checking status for "${fileItem.displayName || fileItem.file.name}": ${error.message}.`);
                const galleryIndex = recordings.value.findIndex(r => r.id === recordingId);
                if (galleryIndex !== -1) recordings.value[galleryIndex].status = 'FAILED';

                clearInterval(pollInterval.value);
                pollInterval.value = null;
                resetCurrentFileProcessingState();
                isProcessingActive.value = false;
                await Vue.nextTick();
                startProcessingQueue();
            }
        }, 5000);
    };

    // Tag selection helpers
    const addTagToSelection = (tagId) => {
        if (!selectedTagIds.value.includes(tagId)) {
            selectedTagIds.value.push(tagId);
            applyTagDefaults();
        }
    };

    const removeTagFromSelection = (tagId) => {
        const index = selectedTagIds.value.indexOf(tagId);
        if (index > -1) {
            selectedTagIds.value.splice(index, 1);
            applyTagDefaults();
        }
    };

    const applyTagDefaults = () => {
        const selectedTagsObjects = selectedTagIds.value.map(tagId =>
            availableTags.value.find(tag => tag.id == tagId)
        ).filter(Boolean);

        const firstTag = selectedTagsObjects[0];
        if (firstTag && useAsrEndpoint.value) {
            if (firstTag.default_language) {
                uploadLanguage.value = firstTag.default_language;
            }
            if (firstTag.default_min_speakers) {
                uploadMinSpeakers.value = firstTag.default_min_speakers;
            }
            if (firstTag.default_max_speakers) {
                uploadMaxSpeakers.value = firstTag.default_max_speakers;
            }
        }
    };

    // Computed property for filtered available tags in upload view
    const filteredAvailableTagsForUpload = computed(() => {
        const availableForSelection = availableTags.value.filter(tag => !selectedTagIds.value.includes(tag.id));
        if (!uploadTagSearchFilter.value) return availableForSelection;

        const filter = uploadTagSearchFilter.value.toLowerCase();
        return availableForSelection.filter(tag =>
            tag.name.toLowerCase().includes(filter)
        );
    });

    return {
        handleDragOver,
        handleDragLeave,
        handleDrop,
        handleFileSelect,
        addFilesToQueue,
        removeFromQueue,
        cancelWaitingFile,
        clearCompletedUploads,
        startUpload,
        startProcessingQueue,
        resetCurrentFileProcessingState,
        startStatusPolling,
        pollProcessingStatus,
        addTagToSelection,
        removeTagFromSelection,
        applyTagDefaults,
        filteredAvailableTagsForUpload
    };
}
