/**
 * Upload management composable
 * Handles file uploads, queue processing, and progress tracking
 */

import * as FailedUploads from '../db/failed-uploads.js';
import * as IncognitoStorage from '../db/incognito-storage.js';

// Parse error message and return friendly error info
function getFriendlyError(errorMessage) {
    if (!errorMessage) return { title: 'Processing Error', message: 'An error occurred' };
    const lowerText = errorMessage.toLowerCase();
    const patterns = [
        { patterns: ['maximum content size limit', 'file too large', '413', 'payload too large', 'exceeded'], title: 'File Too Large', guidance: 'Enable chunking in settings or compress the file' },
        { patterns: ['timed out', 'timeout', 'deadline exceeded'], title: 'Processing Timeout', guidance: 'Try splitting the audio into smaller parts' },
        { patterns: ['401', 'unauthorized', 'invalid api key', 'authentication failed', 'incorrect api key'], title: 'Authentication Error', guidance: 'Check the API key in settings' },
        { patterns: ['rate limit', 'too many requests', '429', 'quota exceeded'], title: 'Rate Limit Exceeded', guidance: 'Wait a few minutes and try again' },
        { patterns: ['connection refused', 'connection reset', 'could not connect', 'network unreachable'], title: 'Connection Error', guidance: 'Check network connection' },
        { patterns: ['503', '502', '500', 'service unavailable', 'server error', 'internal server error'], title: 'Service Unavailable', guidance: 'Try again in a few minutes' },
        { patterns: ['invalid file format', 'unsupported format', 'could not decode', 'corrupt'], title: 'Invalid Audio Format', guidance: 'Convert to MP3 or WAV before uploading' },
        { patterns: ['audio extraction failed', 'ffmpeg failed', 'no audio stream'], title: 'Audio Extraction Failed', guidance: 'Convert to standard audio format' },
    ];
    for (const pattern of patterns) {
        for (const p of pattern.patterns) {
            if (lowerText.includes(p)) return { title: pattern.title, guidance: pattern.guidance };
        }
    }
    return { title: 'Processing Error', guidance: 'Try reprocessing the recording' };
}

export function useUpload(state, utils) {
    const {
        uploadQueue, currentlyProcessingFile, processingProgress, processingMessage,
        isProcessingActive, pollInterval, progressPopupMinimized, progressPopupClosed,
        maxFileSizeMB, chunkingEnabled, chunkingMode, chunkingLimit, maxConcurrentUploads,
        recordings, selectedRecording, totalRecordings, globalError,
        selectedTagIds, uploadLanguage, uploadMinSpeakers, uploadMaxSpeakers,
        useAsrEndpoint, connectorSupportsDiarization, asrLanguage, asrMinSpeakers, asrMaxSpeakers,
        dragover, availableTags, uploadTagSearchFilter,
        // Folder state
        availableFolders, selectedFolderId,
        // Incognito mode state
        incognitoMode, incognitoRecording, incognitoProcessing,
        // View state
        currentView
    } = state;

    const { computed, nextTick, ref } = Vue;

    const { setGlobalError, showToast, formatFileSize, onChatComplete } = utils;

    // Compute selected tags from IDs
    const selectedTags = computed(() => {
        return selectedTagIds.value.map(id =>
            availableTags.value.find(t => t.id === id)
        ).filter(Boolean);
    });

    // --- Tag Drag-and-Drop State ---
    const draggedTagIndex = ref(null);
    const dragOverTagIndex = ref(null);

    // Reorder selectedTagIds array
    const reorderSelectedTags = (fromIndex, toIndex) => {
        const tagIds = [...selectedTagIds.value];
        const [removed] = tagIds.splice(fromIndex, 1);
        tagIds.splice(toIndex, 0, removed);
        selectedTagIds.value = tagIds;
        applyTagDefaults(); // Re-apply defaults since first tag may have changed
    };

    // === MOUSE DRAG HANDLERS ===
    const handleTagDragStart = (index, event) => {
        draggedTagIndex.value = index;
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', index.toString());
    };

    const handleTagDragOver = (index, event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
        dragOverTagIndex.value = index;
    };

    const handleTagDrop = (targetIndex, event) => {
        event.preventDefault();
        if (draggedTagIndex.value !== null && draggedTagIndex.value !== targetIndex) {
            reorderSelectedTags(draggedTagIndex.value, targetIndex);
        }
        draggedTagIndex.value = null;
        dragOverTagIndex.value = null;
    };

    const handleTagDragEnd = () => {
        draggedTagIndex.value = null;
        dragOverTagIndex.value = null;
    };

    // === TOUCH HANDLERS (Mobile) ===
    let touchStartIndex = null;

    const handleTagTouchStart = (index, event) => {
        touchStartIndex = index;
        draggedTagIndex.value = index;
    };

    const handleTagTouchMove = (event) => {
        if (touchStartIndex === null) return;
        event.preventDefault();

        const touch = event.touches[0];
        const elementBelow = document.elementFromPoint(touch.clientX, touch.clientY);
        const tagElement = elementBelow?.closest('[data-tag-index]');

        if (tagElement) {
            const targetIndex = parseInt(tagElement.dataset.tagIndex);
            dragOverTagIndex.value = targetIndex;
        }
    };

    const handleTagTouchEnd = () => {
        if (touchStartIndex !== null && dragOverTagIndex.value !== null &&
            touchStartIndex !== dragOverTagIndex.value) {
            reorderSelectedTags(touchStartIndex, dragOverTagIndex.value);
        }
        touchStartIndex = null;
        draggedTagIndex.value = null;
        dragOverTagIndex.value = null;
    };

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
                if (!item.preserveOptions) {
                    // For file uploads: use current UI selection (user may have changed tags after dropping)
                    item.tags = [...selectedTags.value];
                    item.asrOptions = {
                        language: asrLanguage.value,
                        min_speakers: asrMinSpeakers.value,
                        max_speakers: asrMaxSpeakers.value
                    };
                    item.folder_id = selectedFolderId.value;
                }
                // Change status to 'ready' to remove from upload view but keep in queue
                item.status = 'ready';
            }
        }
        progressPopupMinimized.value = false;
        progressPopupClosed.value = false;
        startProcessingQueue();
    };

    // --- Parallel Upload System ---
    // Concurrency limiter: configurable via MAX_CONCURRENT_UPLOADS env var (default 3)
    let activeUploadCount = 0;
    const pendingUploadQueue = []; // Functions waiting for a slot

    const acquireUploadSlot = () => {
        return new Promise(resolve => {
            if (activeUploadCount < (maxConcurrentUploads?.value || 3)) {
                activeUploadCount++;
                resolve();
            } else {
                pendingUploadQueue.push(resolve);
            }
        });
    };

    const releaseUploadSlot = () => {
        activeUploadCount--;
        if (pendingUploadQueue.length > 0) {
            activeUploadCount++;
            const next = pendingUploadQueue.shift();
            next();
        }
        // When all uploads are done, clear processing active flag
        const stillUploading = uploadQueue.value.some(item =>
            ['uploading', 'ready'].includes(item.status)
        );
        if (!stillUploading) {
            isProcessingActive.value = false;
        }
    };

    const resetCurrentFileProcessingState = () => {
        if (pollInterval.value) clearInterval(pollInterval.value);
        pollInterval.value = null;
        currentlyProcessingFile.value = null;
        processingProgress.value = 0;
        processingMessage.value = '';
    };

    /**
     * Upload a single file to the server.
     * Acquires a concurrency slot, uploads, then releases.
     * Status updates are per-item (no global processingProgress).
     */
    const uploadSingleFile = async (fileItem) => {
        await acquireUploadSlot();

        fileItem.status = 'uploading';
        fileItem.progress = 5;

        try {
            const formData = new FormData();
            formData.append('file', fileItem.file);

            // Send file's lastModified timestamp for meeting_date
            if (fileItem.file.lastModified) {
                const lastModified = fileItem.file.lastModified;
                formData.append('file_last_modified', lastModified.toString());
            }

            if (fileItem.notes) {
                formData.append('notes', fileItem.notes);
            }

            // Add tags if selected
            const tagsToUse = fileItem.tags || selectedTags.value || [];
            tagsToUse.forEach((tag, index) => {
                const tagId = tag.id || tag;
                formData.append(`tag_ids[${index}]`, tagId);
            });

            // Add folder if selected
            const folderToUse = fileItem.folder_id || selectedFolderId.value;
            if (folderToUse) {
                formData.append('folder_id', folderToUse);
            }

            // Add ASR options
            const asrOpts = fileItem.asrOptions || {};
            const language = asrOpts.language || uploadLanguage.value;
            if (language) {
                formData.append('language', language);
            }

            if (connectorSupportsDiarization.value) {
                const minSpeakers = asrOpts.min_speakers || uploadMinSpeakers.value;
                const maxSpeakers = asrOpts.max_speakers || uploadMaxSpeakers.value;

                if (minSpeakers && minSpeakers !== '') {
                    formData.append('min_speakers', minSpeakers.toString());
                }
                if (maxSpeakers && maxSpeakers !== '') {
                    formData.append('max_speakers', maxSpeakers.toString());
                }
            }

            // Use XMLHttpRequest for per-file upload progress
            const data = await new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();

                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable) {
                        // Map upload progress to 5-90% range
                        fileItem.progress = Math.round(5 + (e.loaded / e.total) * 85);
                    }
                };

                xhr.onload = () => {
                    const contentType = xhr.getResponseHeader('content-type') || '';
                    if (!contentType.includes('application/json')) {
                        const titleMatch = xhr.responseText.match(/<title>([^<]+)<\/title>/i);
                        const h1Match = xhr.responseText.match(/<h1>([^<]+)<\/h1>/i);
                        reject(new Error(titleMatch?.[1] || h1Match?.[1] ||
                            `Server error (${xhr.status}): Response was not JSON`));
                        return;
                    }

                    let parsed;
                    try {
                        parsed = JSON.parse(xhr.responseText);
                    } catch {
                        reject(new Error(`Invalid JSON response (${xhr.status})`));
                        return;
                    }

                    if (xhr.status === 202 && parsed.id) {
                        resolve(parsed);
                    } else if (!String(xhr.status).startsWith('2')) {
                        let errorMsg = parsed.error || `Upload failed with status ${xhr.status}`;
                        if (xhr.status === 413) errorMsg = parsed.error || `File too large. Max: ${parsed.max_size_mb?.toFixed(0) || maxFileSizeMB.value} MB.`;
                        reject(new Error(errorMsg));
                    } else {
                        reject(new Error('Unexpected success response from server after upload.'));
                    }
                };

                xhr.onerror = () => reject(new Error('Network error during upload'));
                xhr.ontimeout = () => reject(new Error('Upload timed out'));

                // Store abort controller on item for cancellation
                fileItem._xhr = xhr;

                xhr.open('POST', '/upload');
                // Include CSRF token (required for POST requests)
                const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
                if (csrfToken) {
                    xhr.setRequestHeader('X-CSRFToken', csrfToken);
                }
                xhr.send(formData);
            });

            // Upload succeeded - recording is now on the server
            console.log(`File ${fileItem.file.name} uploaded. Recording ID: ${data.id}. Server will process via job queue.`);
            fileItem.status = 'pending';
            fileItem.recordingId = data.id;
            fileItem.progress = 100;

            // Add to recordings list
            recordings.value.unshift(data);
            totalRecordings.value++;

            // Handle duplicate warning
            if (data.duplicate_warning) {
                const warning = data.duplicate_warning;
                const existingDate = warning.existing_created_at
                    ? new Date(warning.existing_created_at).toLocaleDateString()
                    : '';
                const existingName = warning.existing_title || 'Unknown';
                showToast(
                    `⚠️ ${existingName} (${existingDate})`,
                    'fa-copy'
                );
                fileItem.duplicateWarning = warning;
            }

        } catch (error) {
            console.error(`Upload Error for ${fileItem.file.name} (Client ID: ${fileItem.clientId}):`, error);
            fileItem.status = 'failed';
            fileItem.error = error.message;
            fileItem.progress = 0;

            // Show friendly error message
            const friendlyErr = getFriendlyError(error.message);
            setGlobalError(`${friendlyErr.title}: ${friendlyErr.guidance}`);

            // Store failed upload in IndexedDB for background sync retry
            try {
                await FailedUploads.storeFailedUpload({
                    file: fileItem.file,
                    fileName: fileItem.file.name,
                    fileSize: fileItem.file.size,
                    clientId: fileItem.clientId,
                    notes: fileItem.notes,
                    tags: fileItem.tags,
                    asrOptions: fileItem.asrOptions,
                    error: error.message
                });

                if ('serviceWorker' in navigator && 'sync' in ServiceWorkerRegistration.prototype) {
                    const registration = await navigator.serviceWorker.ready;
                    await registration.sync.register('sync-uploads');
                    console.log('[Upload] Registered background sync for failed upload');
                }
            } catch (syncError) {
                console.warn('[Upload] Failed to register background sync:', syncError);
            }
        } finally {
            fileItem._xhr = null;
            releaseUploadSlot();
        }
    };

    /**
     * Start uploading all ready files in parallel (with concurrency limit).
     * Processing status is tracked via allJobs polling in app.modular.js.
     */
    const startProcessingQueue = async () => {
        const readyItems = uploadQueue.value.filter(item => item.status === 'ready');
        if (readyItems.length === 0) {
            console.log("No files ready to upload.");
            return;
        }

        isProcessingActive.value = true;
        console.log(`Starting parallel upload of ${readyItems.length} file(s) (max ${maxConcurrentUploads?.value || 3} concurrent)...`);

        // Fire off all uploads concurrently (semaphore handles limiting)
        const uploadPromises = readyItems.map(item => uploadSingleFile(item));
        // Don't await - let them run in background. isProcessingActive is cleared by releaseUploadSlot.
        Promise.allSettled(uploadPromises).then(() => {
            console.log('All uploads settled.');
        });
    };

    // Keep backward-compat aliases
    const startStatusPolling = (fileItem, recordingId) => {
        // No longer needed - allJobs polling handles status tracking
        fileItem.recordingId = recordingId;
    };

    const pollProcessingStatus = () => {
        // No-op: status tracking is now handled by allJobs polling in app.modular.js
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
        if (firstTag && connectorSupportsDiarization.value) {
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

    // === INCOGNITO MODE FUNCTIONS ===

    /**
     * Upload and process a file in incognito mode.
     * The file is processed synchronously and no data is saved to the database.
     * Results are stored only in sessionStorage.
     */
    const startIncognitoUpload = async () => {
        const pendingFiles = uploadQueue.value.filter(item => item.status === 'queued');
        if (pendingFiles.length === 0) {
            return;
        }

        // Only process the first file for incognito mode
        const fileItem = pendingFiles[0];

        // Check if incognito mode state is available
        if (!incognitoMode || !incognitoProcessing || !incognitoRecording) {
            console.warn('[Incognito] Incognito state not available, falling back to normal upload');
            startUpload();
            return;
        }

        incognitoProcessing.value = true;
        processingMessage.value = 'Processing in incognito mode...';
        processingProgress.value = 10;
        progressPopupMinimized.value = false;
        progressPopupClosed.value = false;

        try {
            const formData = new FormData();
            formData.append('file', fileItem.file);

            // Add ASR options
            const asrOpts = fileItem.asrOptions || {};
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

            // Request auto-summarization
            formData.append('auto_summarize', 'true');

            processingMessage.value = 'Uploading file for incognito processing...';
            processingProgress.value = 20;

            console.log('[Incognito] Uploading file:', fileItem.file.name);

            const response = await fetch('/api/recordings/incognito', {
                method: 'POST',
                body: formData
            });

            processingProgress.value = 50;

            // Parse response
            const contentType = response.headers.get('content-type') || '';
            if (!contentType.includes('application/json')) {
                const text = await response.text();
                const titleMatch = text.match(/<title>([^<]+)<\/title>/i);
                throw new Error(titleMatch?.[1] || `Server error (${response.status})`);
            }

            const data = await response.json();

            if (!response.ok || data.error) {
                throw new Error(data.error || `Processing failed with status ${response.status}`);
            }

            processingProgress.value = 80;
            processingMessage.value = 'Processing complete!';

            // Store result in sessionStorage
            const incognitoData = {
                id: 'incognito',
                incognito: true,
                title: data.title || 'Incognito Recording',
                transcription: data.transcription,
                summary: data.summary,
                summary_html: data.summary_html,
                created_at: data.created_at,
                original_filename: data.original_filename,
                file_size: data.file_size,
                audio_duration_seconds: data.audio_duration_seconds,
                processing_time_seconds: data.processing_time_seconds,
                status: 'COMPLETED'
            };

            IncognitoStorage.saveIncognitoRecording(incognitoData);
            incognitoRecording.value = incognitoData;

            // Remove the processed file from queue
            const index = uploadQueue.value.findIndex(item => item.clientId === fileItem.clientId);
            if (index !== -1) {
                uploadQueue.value.splice(index, 1);
            }

            processingProgress.value = 100;
            processingMessage.value = 'Incognito recording ready!';

            // Auto-select the incognito recording and switch to detail view
            selectedRecording.value = incognitoData;
            currentView.value = 'detail';

            // Show toast
            showToast('Incognito recording processed - data will be lost when tab closes', 'fa-user-secret');

            console.log('[Incognito] Processing complete');

        } catch (error) {
            console.error('[Incognito] Processing failed:', error);
            const friendlyErr = getFriendlyError(error.message);
            setGlobalError(`${friendlyErr.title}: ${friendlyErr.guidance}`);
            fileItem.status = 'failed';
            fileItem.error = error.message;
        } finally {
            incognitoProcessing.value = false;
            processingProgress.value = 0;
            processingMessage.value = '';
        }
    };

    /**
     * Clear the incognito recording with confirmation
     */
    const clearIncognitoRecordingWithConfirm = () => {
        if (incognitoRecording && incognitoRecording.value) {
            if (confirm('This will permanently discard your incognito recording. Continue?')) {
                IncognitoStorage.clearIncognitoRecording();
                incognitoRecording.value = null;
                // If the incognito recording was selected, clear selection
                if (selectedRecording.value?.id === 'incognito') {
                    selectedRecording.value = null;
                }
                showToast('Incognito recording discarded', 'fa-trash');
            }
        }
    };

    /**
     * Select the incognito recording for viewing
     */
    const selectIncognitoRecording = () => {
        if (incognitoRecording && incognitoRecording.value) {
            selectedRecording.value = incognitoRecording.value;
            currentView.value = 'detail';
        }
    };

    /**
     * Load incognito recording from sessionStorage on app init
     */
    const loadIncognitoRecording = () => {
        const stored = IncognitoStorage.getIncognitoRecording();
        if (stored && incognitoRecording) {
            incognitoRecording.value = stored;
            console.log('[Incognito] Loaded recording from sessionStorage');
        }
    };

    /**
     * Check if there's an incognito recording (for navigation guards)
     */
    const hasIncognitoRecording = () => {
        return IncognitoStorage.hasIncognitoRecording();
    };

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
        filteredAvailableTagsForUpload,
        // Tag drag-and-drop
        draggedTagIndex,
        dragOverTagIndex,
        handleTagDragStart,
        handleTagDragOver,
        handleTagDrop,
        handleTagDragEnd,
        handleTagTouchStart,
        handleTagTouchMove,
        handleTagTouchEnd,
        // Incognito mode
        startIncognitoUpload,
        clearIncognitoRecordingWithConfirm,
        selectIncognitoRecording,
        loadIncognitoRecording,
        hasIncognitoRecording
    };
}
