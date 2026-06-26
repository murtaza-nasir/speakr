/**
 * Upload management composable
 * Handles file uploads, queue processing, and progress tracking
 */

import * as FailedUploads from '../db/failed-uploads.js';
import * as IncognitoStorage from '../db/incognito-storage.js';
import * as RecordingDB from '../db/recording-persistence.js';
import { getUploadCsrfToken, isCsrfRejection } from '../csrf.js';
import { computeUploadTimeout } from '../utils/upload-timeout.js';

// Parse error message and return friendly error info
function getFriendlyError(errorMessage, t) {
    const _t = t || ((key) => key);
    if (!errorMessage) return { title: _t('errors.processingError'), message: _t('errors.processingErrorMessage') };
    const lowerText = errorMessage.toLowerCase();
    const patterns = [
        // Reverse-proxy 413 (nginx / NPM default). Must match BEFORE the
        // generic 413 pattern below; the proxy rejects the request body before
        // Speakr sees it, so enabling chunking on the Speakr side will not help.
        { patterns: ['request entity too large', 'request body is too large', '413 request entity too large'], title: _t('errors.uploadBlockedByProxyTitle'), guidance: _t('errors.uploadBlockedByProxyGuidance') },
        { patterns: ['maximum content size limit', 'file too large', 'payload too large', 'exceeded', 'content too large'], title: _t('errors.fileTooLargeTitle'), guidance: _t('errors.enableChunkingGuidance') },
        { patterns: ['timed out', 'timeout', 'deadline exceeded'], title: _t('errors.processingTimeout'), guidance: _t('errors.splitAudioGuidance') },
        { patterns: ['401', 'unauthorized', 'invalid api key', 'authentication failed', 'incorrect api key'], title: _t('errors.authenticationError'), guidance: _t('errors.checkApiKeyGuidance') },
        { patterns: ['rate limit', 'too many requests', '429', 'quota exceeded'], title: _t('errors.rateLimitExceeded'), guidance: _t('errors.waitAndRetryGuidance') },
        { patterns: ['connection refused', 'connection reset', 'could not connect', 'network unreachable'], title: _t('errors.connectionError'), guidance: _t('errors.checkNetworkGuidance') },
        { patterns: ['503', '502', '500', 'service unavailable', 'server error', 'internal server error'], title: _t('errors.serviceUnavailable'), guidance: _t('errors.tryAgainLaterGuidance') },
        { patterns: ['invalid file format', 'unsupported format', 'could not decode', 'corrupt'], title: _t('errors.invalidAudioFormat'), guidance: _t('errors.convertFormatGuidance') },
        { patterns: ['audio extraction failed', 'ffmpeg failed', 'no audio stream'], title: _t('errors.audioExtractionFailed'), guidance: _t('errors.convertStandardGuidance') },
    ];
    for (const pattern of patterns) {
        for (const p of pattern.patterns) {
            if (lowerText.includes(p)) return { title: pattern.title, guidance: pattern.guidance };
        }
    }
    return { title: _t('errors.processingError'), guidance: _t('errors.processingErrorFallbackGuidance') };
}

export function useUpload(state, utils) {
    const {
        uploadQueue, currentlyProcessingFile, processingProgress, processingMessage,
        isProcessingActive, pollInterval, progressPopupMinimized, progressPopupClosed,
        maxFileSizeMB, chunkingEnabled, chunkingMode, chunkingLimit, maxConcurrentUploads,
        recordings, selectedRecording, totalRecordings, globalError,
        selectedTagIds, uploadLanguage, uploadMinSpeakers, uploadMaxSpeakers, uploadHotwords, uploadInitialPrompt, uploadTranscriptionModel, uploadPromptVariables, transcriptionModelOptions,
        useAsrEndpoint, connectorSupportsDiarization, asrLanguage, asrMinSpeakers, asrMaxSpeakers,
        dragover, availableTags, uploadTagSearchFilter,
        // Folder state
        availableFolders, selectedFolderId,
        // Incognito mode state
        incognitoMode, incognitoRecording, incognitoProcessing,
        // Video / audio-only upload state
        videoRetentionEnabled, keepAudioOnly, maxAudioOnlyVideoSizeMB,
        // View state
        currentView, showUploadModal,
        // Upload disclaimer state
        uploadDisclaimer, showUploadDisclaimerModal
    } = state;

    const { computed, nextTick, ref, markRaw } = Vue;

    const { setGlobalError, showToast, formatFileSize, onChatComplete, t } = utils;

    // Probe a File for its audio/video duration without uploading it.
    // Uses a hidden <audio> or <video> element with preload="metadata"
    // so the browser only reads the container headers, not the whole
    // payload. Resolves to seconds (number) or null if the file can't
    // be parsed (corrupt, unsupported codec, etc.). Used by the upload
    // queue to populate item.duration on each queued file so the user
    // sees length + size next to the filename before uploading.
    const computeFileDuration = (file) => new Promise((resolve) => {
        try {
            const isVideo = /^video\//i.test(file.type)
                || /\.(mp4|mov|mkv|avi|webm|m4v|wmv|flv|ts|mts|mpeg|mpg|ogv|vob|asf)$/i.test(file.name);
            const url = URL.createObjectURL(file);
            const el = document.createElement(isVideo ? 'video' : 'audio');
            el.preload = 'metadata';
            const done = (val) => {
                try { URL.revokeObjectURL(url); } catch (_) {}
                resolve(val);
            };
            el.onloadedmetadata = () => done(isFinite(el.duration) ? el.duration : null);
            el.onerror = () => done(null);
            // Safety timeout — some browsers stall on unsupported codecs
            setTimeout(() => done(null), 8000);
            el.src = url;
        } catch (_) {
            resolve(null);
        }
    });

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

    // Restore the previous upload's form choices (tags / folder /
    // language / min-max speakers) when (a) we have a memo from a
    // prior successful upload and (b) the user hasn't set anything
    // for this upload yet. Idempotent — only fills empty slots so a
    // user who already picked something doesn't get clobbered. Called
    // automatically when the first file lands in the queue.
    let _hydrateAttempted = false;
    const hydrateUploadDefaults = () => {
        if (_hydrateAttempted) return;
        _hydrateAttempted = true;
        let memo;
        try {
            const raw = localStorage.getItem('lastUploadDefaults');
            if (!raw) return;
            memo = JSON.parse(raw);
        } catch (_) { return; }
        if (!memo) return;
        // Tags: only fill if nothing currently selected. Also drop
        // any tag IDs that no longer exist on the user's account.
        if (selectedTagIds && (!selectedTagIds.value || selectedTagIds.value.length === 0)
            && Array.isArray(memo.tagIds) && memo.tagIds.length > 0
            && Array.isArray(availableTags?.value)) {
            const liveIds = new Set(availableTags.value.map(t => t.id));
            const restored = memo.tagIds.filter(id => liveIds.has(id));
            if (restored.length > 0) selectedTagIds.value = restored;
        }
        if (selectedFolderId && (selectedFolderId.value == null) && memo.folderId != null) {
            // Only restore if the folder still exists.
            if (Array.isArray(availableFolders?.value)
                && availableFolders.value.some(f => f.id === memo.folderId)) {
                selectedFolderId.value = memo.folderId;
            }
        }
        if (uploadLanguage && !uploadLanguage.value && memo.language) {
            uploadLanguage.value = memo.language;
        }
        if (uploadMinSpeakers && !uploadMinSpeakers.value && memo.minSpeakers) {
            uploadMinSpeakers.value = memo.minSpeakers;
        }
        if (uploadMaxSpeakers && !uploadMaxSpeakers.value && memo.maxSpeakers) {
            uploadMaxSpeakers.value = memo.maxSpeakers;
        }
    };

    // Add files to the upload queue
    const addFilesToQueue = (files) => {
        // Restore last-upload defaults the first time a file lands in
        // the queue (deferred until then so a user who opens then
        // immediately closes the modal doesn't get residual chips).
        hydrateUploadDefaults();
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
                // Per-file effective size limit. Audio files always use
                // the regular limit. Video files use the audio-only-mode
                // limit unconditionally at the precheck so the file can
                // land in the queue and the "Keep audio only" toggle
                // becomes visible. Three reasons:
                //   1. With VIDEO_RETENTION=off, the server always
                //      extracts audio, so the larger limit is the right
                //      ceiling anyway.
                //   2. With VIDEO_RETENTION=on and the toggle off, the
                //      backend would still 413 a file over the regular
                //      limit; we surface that with an inline warning
                //      below rather than blocking the file at the door.
                //   3. The toggle component only renders once a video
                //      is already in the queue, so blocking video files
                //      at precheck creates a chicken-and-egg loop.
                const isVideoExt = /\.(mp4|mov|mkv|avi|webm|m4v|wmv|flv|ts|mts)$/i.test(fileObject.name);
                const effectiveLimitMB = isVideoExt
                    ? (maxAudioOnlyVideoSizeMB && maxAudioOnlyVideoSizeMB.value) || maxFileSizeMB.value
                    : maxFileSizeMB.value;
                if (fileObject.size > effectiveLimitMB * 1024 * 1024) {
                    const errKey = isVideoExt
                        ? 'upload.videoExceedsAudioOnlyMaxSize'
                        : 'upload.fileExceedsMaxSize';
                    setGlobalError(t(errKey, { name: fileObject.name, size: effectiveLimitMB }));
                    continue;
                }

                const clientId = `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;

                // markRaw prevents Vue from wrapping the File in a reactive
                // proxy. Vue's proxy walks object properties when accessed,
                // which is extremely expensive for binary File/Blob payloads
                // and was a likely cause of the UI freeze seen when 10+ files
                // were queued at once (issue #280).
                const queueItem = {
                    file: markRaw ? markRaw(fileObject) : fileObject,
                    notes: notes,
                    tags: tags,
                    asrOptions: asrOptions,
                    status: 'queued',
                    recordingId: null,
                    clientId: clientId,
                    error: null,
                    // Populated asynchronously by computeFileDuration
                    // below — the queue display reads item.duration to
                    // show e.g. "12:34" next to the filename. Leaving
                    // it null shows just size until probing finishes
                    // (usually well under a second).
                    duration: null,
                    willAutoSummarize: false // Server will tell us via SUMMARIZING status
                };
                uploadQueue.value.push(queueItem);
                // Fire-and-forget duration probe. Mutating the queue
                // item's `duration` field after the push is reactive
                // because the item itself is a plain object Vue
                // tracks; only the inner File is markRaw.
                computeFileDuration(fileObject).then(d => {
                    queueItem.duration = d;
                }).catch(() => {});
                filesAdded++;
            } else if (fileObject) {
                setGlobalError(t('upload.invalidFileType', { name: fileObject.name }));
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
            showToast(t('upload.fileRemovedFromQueue'), 'fa-trash');
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
        // Show upload disclaimer if configured
        if (uploadDisclaimer.value && uploadDisclaimer.value.trim() !== '') {
            showUploadDisclaimerModal.value = true;
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
                        max_speakers: asrMaxSpeakers.value,
                        hotwords: uploadHotwords.value,
                        initial_prompt: uploadInitialPrompt.value,
                        transcription_model: uploadTranscriptionModel.value,
                        prompt_variables: { ...uploadPromptVariables },
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

            // Add hotwords and initial prompt
            const hotwords = asrOpts.hotwords || uploadHotwords.value;
            const initialPrompt = asrOpts.initial_prompt || uploadInitialPrompt.value;
            if (hotwords && hotwords.trim()) {
                formData.append('hotwords', hotwords.trim());
            }
            if (initialPrompt && initialPrompt.trim()) {
                formData.append('initial_prompt', initialPrompt.trim());
            }

            // Per-upload model override (issue #266)
            const transcriptionModel = asrOpts.transcription_model || uploadTranscriptionModel.value;
            if (transcriptionModel && transcriptionModel.trim()) {
                formData.append('transcription_model', transcriptionModel.trim());
            }

            // Per-recording prompt template variables (issue #253). Carry the
            // values either from a queued item's saved options or from the
            // current upload-form state. The server sanitises before storing.
            const promptVariables = asrOpts.prompt_variables || uploadPromptVariables;
            if (promptVariables && typeof promptVariables === 'object') {
                const cleaned = {};
                for (const [key, value] of Object.entries(promptVariables)) {
                    if (value !== undefined && value !== null && String(value).trim() !== '') {
                        cleaned[key] = String(value);
                    }
                }
                if (Object.keys(cleaned).length > 0) {
                    formData.append('prompt_variables', JSON.stringify(cleaned));
                }
            }

            // Per-upload "keep audio only" override. True when the user
            // toggled it explicitly (only visible when VIDEO_RETENTION is
            // on at the server) OR implicitly when VIDEO_RETENTION is
            // off AND the file is a video — in both cases the upload is
            // allowed up to the larger audio-only video limit and the
            // server discards the video stream.
            const isVideoExt = /\.(mp4|mov|mkv|avi|webm|m4v|wmv|flv|ts|mts)$/i.test(fileItem.file.name);
            const keepAudioOnlyForThisFile = (
                (videoRetentionEnabled && videoRetentionEnabled.value)
                    ? !!(keepAudioOnly && keepAudioOnly.value)
                    : isVideoExt
            );
            if (keepAudioOnlyForThisFile) {
                formData.append('keep_audio_only', 'true');
            }

            // Use XMLHttpRequest for per-file upload progress. XHR bypasses
            // the fetch interceptor in csrf-refresh.js, so this path must
            // handle CSRF token expiry itself: Flask-WTF's default
            // WTF_CSRF_TIME_LIMIT is one hour, and the meta-tag token goes
            // stale after a long recording or laptop sleep (the 45-minute
            // interval refresh in csrf-refresh.js doesn't fire in throttled /
            // suspended tabs). Without this, the upload 400s and the
            // recording never reaches the server (the IndexedDB retry path
            // would then re-fail with the same stale token).
            const sendUpload = (csrfToken) => new Promise((resolve, reject) => {
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
                        if (isCsrfRejection(xhr.status, xhr.responseText)) {
                            const err = new Error(`CSRF token rejected (${xhr.status})`);
                            err.isCsrfRejection = true;
                            reject(err);
                            return;
                        }
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
                        const err = new Error(errorMsg);
                        if (isCsrfRejection(xhr.status, parsed.error || '')) {
                            err.isCsrfRejection = true;
                        }
                        reject(err);
                    } else {
                        reject(new Error('Unexpected success response from server after upload.'));
                    }
                };

                xhr.onerror = () => reject(new Error('Network error during upload'));
                xhr.ontimeout = () => reject(new Error('Upload timed out'));

                // Without an explicit timeout, a stalled connection (no FIN/RST,
                // just no progress) hangs forever and ontimeout never fires — so
                // the upload never reaches the catch block that persists the file
                // for retry / triggers the local-download fallback. Set a
                // size-scaled ceiling so a stalled upload eventually fails over.
                xhr.timeout = computeUploadTimeout(fileItem.file?.size || 0);

                // Store abort controller on item for cancellation
                fileItem._xhr = xhr;

                xhr.open('POST', '/upload');
                // Include CSRF token (required for POST requests)
                if (csrfToken) {
                    xhr.setRequestHeader('X-CSRFToken', csrfToken);
                }
                xhr.send(formData);
            });

            // Refresh the token right before sending, and retry exactly once
            // with another fresh token if the server still rejects it —
            // mirroring what the fetch interceptor in csrf-refresh.js does
            // for non-XHR requests.
            let data;
            try {
                data = await sendUpload(await getUploadCsrfToken());
            } catch (uploadError) {
                if (!uploadError.isCsrfRejection) throw uploadError;
                console.warn(`[Upload] CSRF rejection for ${fileItem.file.name}; refreshing token and retrying once.`);
                fileItem.progress = 5;
                data = await sendUpload(await getUploadCsrfToken());
            }

            // Upload succeeded - recording is now on the server
            console.log(`File ${fileItem.file.name} uploaded. Recording ID: ${data.id}. Server will process via job queue.`);
            fileItem.status = 'pending';
            fileItem.recordingId = data.id;
            fileItem.progress = 100;

            // If this was a resurfaced retry (#313), drop its IndexedDB record
            // now that it's safely on the server so it isn't retried again.
            if (fileItem._failedUploadId) {
                try { await FailedUploads.deleteFailedUpload(fileItem._failedUploadId); }
                catch (e) { console.warn('[Upload] Could not clear retried upload from IndexedDB:', e); }
            }

            // The in-progress recording session is now safe to clear (issue
            // #287(b)): the audio is on the server. Only do this for queue
            // items that came from an in-app recording, to avoid touching the
            // session for file-drag-drop uploads that never wrote one.
            if (fileItem.fromInProgressRecording) {
                try {
                    await RecordingDB.clearRecordingSession();
                } catch (dbError) {
                    console.warn('[Upload] Failed to clear recording session after successful upload:', dbError);
                }
            }

            // Add to recordings list
            recordings.value.unshift(data);
            totalRecordings.value++;

            // Remember the upload-form choices so the NEXT modal open
            // can pre-fill them. Most users follow a routine (same
            // tags, same folder, same language) and re-typing them on
            // every upload is friction. Persisted in localStorage;
            // restored by hydrateUploadDefaults() on the first file
            // added to the queue.
            try {
                const memo = {
                    tagIds: Array.isArray(selectedTagIds?.value) ? [...selectedTagIds.value] : [],
                    folderId: selectedFolderId?.value ?? null,
                    language: uploadLanguage?.value ?? '',
                    minSpeakers: uploadMinSpeakers?.value ?? '',
                    maxSpeakers: uploadMaxSpeakers?.value ?? ''
                };
                localStorage.setItem('lastUploadDefaults', JSON.stringify(memo));
            } catch (_) { /* localStorage disabled / quota — ignore */ }

            // For in-app recordings (where the upload modal opened
            // automatically after the user stopped recording), auto-
            // navigate to the new recording's detail view — otherwise
            // the user closes the modal and lands on an empty surface
            // because nothing is selected. This only fires for queue
            // items that came from an in-app recording (set in
            // audio.js's recording-stop path) so bulk drag-drop
            // uploads of many files still leave the user wherever
            // they were.
            if (fileItem.fromInProgressRecording) {
                selectedRecording.value = data;
                currentView.value = 'detail';
                if (showUploadModal) showUploadModal.value = false;
            }

            // Handle duplicate warning
            if (data.duplicate_warning) {
                const warning = data.duplicate_warning;
                const existingDate = warning.existing_created_at
                    ? utils.parseServerInstant(warning.existing_created_at).toLocaleDateString()
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
            const friendlyErr = getFriendlyError(error.message, t);
            setGlobalError(`${friendlyErr.title}: ${friendlyErr.guidance}`);

            // If this failure is itself a resurfaced retry, just bump the retry
            // count on the EXISTING IndexedDB record — don't store a duplicate or
            // re-download. resurfaceFailedUploads() will pick it up again on the
            // next online/load (until it hits the retry cap).
            if (fileItem._failedUploadId) {
                try {
                    await FailedUploads.updateRetryCount(
                        fileItem._failedUploadId, (fileItem._retryCount || 0) + 1, error.message);
                } catch (e) {
                    console.warn('[Upload] Could not bump retry count:', e);
                }
            } else {
                // Defense-in-depth recovery (issue #297, #287, #313):
                //   1. Persist the file to IndexedDB so it can be auto-retried
                //      in-app on the next online/page-load (resurfaceFailedUploads).
                //   2. If IndexedDB persistence fails (quota exceeded, private
                //      mode, missing IndexedDB), fall back to a browser download
                //      so the user keeps a local copy. Permanent audio loss is the
                //      worst outcome, so both layers are best-effort.
                let persistedToIndexedDB = false;
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
                    persistedToIndexedDB = true;
                } catch (dbError) {
                    console.warn('[Upload] IndexedDB persistence failed for failed upload:', dbError);
                }

                if (persistedToIndexedDB) {
                    // Saved for retry. We deliberately do NOT retry right here
                    // (a server-side failure while still "online" would just storm
                    // the endpoint); resurfaceFailedUploads() resubmits it through
                    // the normal CSRF-correct upload path on the next `online`
                    // event or page load — in every browser, no Background Sync.
                    showToast?.(
                        (t && t('toasts.uploadFailedWillRetry'))
                            || 'Upload failed — saved and will retry automatically when you reconnect.',
                        'fa-rotate'
                    );
                } else {
                    // IndexedDB rejected the failed-upload write. Last-resort:
                    // hand the audio back to the user as a browser download so it
                    // survives a tab close.
                    const downloaded = FailedUploads.triggerLocalDownload(
                        fileItem.file,
                        fileItem.file?.name || `speakr-recording-${Date.now()}.webm`
                    );
                    if (downloaded) {
                        showToast?.(
                            (t && t('toasts.uploadFailedDownloadedLocally'))
                                || 'Upload failed. Audio saved to your Downloads folder for retry.',
                            'fa-file-download'
                        );
                    } else {
                        showToast?.(
                            (t && t('toasts.uploadFailedNoRecovery'))
                                || 'Upload failed and audio could not be saved locally. Please record again.',
                            'fa-triangle-exclamation'
                        );
                    }
                }
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
        // Apply hotwords/initial_prompt from first tag (works for all connectors)
        if (firstTag) {
            if (firstTag.default_hotwords) {
                uploadHotwords.value = firstTag.default_hotwords;
            }
            if (firstTag.default_initial_prompt) {
                uploadInitialPrompt.value = firstTag.default_initial_prompt;
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
        processingMessage.value = t('incognito.processingInProgress');
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

            const hotwords = asrOpts.hotwords || uploadHotwords.value;
            const initialPrompt = asrOpts.initial_prompt || uploadInitialPrompt.value;
            if (hotwords && hotwords.trim()) {
                formData.append('hotwords', hotwords.trim());
            }
            if (initialPrompt && initialPrompt.trim()) {
                formData.append('initial_prompt', initialPrompt.trim());
            }
            const incogModel = asrOpts.transcription_model || uploadTranscriptionModel.value;
            if (incogModel && incogModel.trim()) {
                formData.append('transcription_model', incogModel.trim());
            }

            // Request auto-summarization
            formData.append('auto_summarize', 'true');

            processingMessage.value = t('incognito.uploadingFile');
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
            processingMessage.value = t('incognito.processingComplete');

            // Store result in sessionStorage
            const incognitoData = {
                id: 'incognito',
                incognito: true,
                title: data.title || t('incognito.recordingTitle'),
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
            processingMessage.value = t('incognito.recordingReady');

            // Auto-select the incognito recording and switch to detail view
            selectedRecording.value = incognitoData;
            currentView.value = 'detail';
            // Dismiss the upload modal if it was still open behind the
            // success transition.
            if (showUploadModal) showUploadModal.value = false;

            // Show toast
            showToast(t('incognito.recordingProcessed'), 'fa-user-secret');

            console.log('[Incognito] Processing complete');

        } catch (error) {
            console.error('[Incognito] Processing failed:', error);
            const friendlyErr = getFriendlyError(error.message, t);
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
            if (confirm(t('incognito.discardConfirm'))) {
                IncognitoStorage.clearIncognitoRecording();
                incognitoRecording.value = null;
                // If the incognito recording was selected, clear selection
                if (selectedRecording.value?.id === 'incognito') {
                    selectedRecording.value = null;
                }
                showToast(t('incognito.recordingDiscarded'), 'fa-trash');
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
            // Dismiss the upload modal if it was still open behind the
            // success transition.
            if (showUploadModal) showUploadModal.value = false;
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

    // Cap so a permanently-broken upload doesn't retry forever.
    const MAX_AUTO_RETRIES = 3;
    let _isResurfacing = false;

    // Auto-retry path for #313: read the failed uploads persisted in IndexedDB,
    // rebuild a File for each, and resubmit them through the normal upload queue
    // (correct CSRF, visible in the Processing Queue, works in every browser —
    // no Background Sync). Triggered on page load and on the browser `online`
    // event. Items are removed from IndexedDB on success and retry-counted on
    // failure (see uploadSingleFile).
    const resurfaceFailedUploads = async () => {
        if (_isResurfacing) return;
        if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
        _isResurfacing = true;
        try {
            const failed = await FailedUploads.getFailedUploads();
            let queued = 0;
            for (const rec of failed) {
                if (!rec || !rec.fileData) continue;
                if ((rec.retryCount || 0) >= MAX_AUTO_RETRIES) continue;

                // Skip if this upload is already represented in the queue and
                // actively in flight; otherwise drop a stale failed item so we
                // don't show duplicates.
                const existingIdx = uploadQueue.value.findIndex(i => i.clientId === rec.clientId);
                if (existingIdx !== -1) {
                    const st = uploadQueue.value[existingIdx].status;
                    if (['uploading', 'pending', 'ready', 'queued'].includes(st)) continue;
                    uploadQueue.value.splice(existingIdx, 1);
                }

                const file = new File([rec.fileData], rec.fileName || `speakr-recording-${Date.now()}.webm`,
                    { type: rec.mimeType || 'audio/webm' });
                uploadQueue.value.push({
                    file: markRaw ? markRaw(file) : file,
                    notes: rec.notes || '',
                    tags: rec.tags || [],
                    asrOptions: rec.asrOptions || {},
                    status: 'ready',          // picked up by startProcessingQueue (skips startUpload's UI-state overwrite)
                    recordingId: null,
                    clientId: rec.clientId,
                    error: null,
                    duration: null,
                    willAutoSummarize: false,
                    preserveOptions: true,    // keep the stored tags/ASR, don't clobber with current UI state
                    _failedUploadId: rec.id,
                    _retryCount: rec.retryCount || 0,
                });
                queued++;
            }
            if (queued > 0) {
                showToast?.(
                    (t && t('toasts.retryingPendingUploads')) || 'Retrying pending upload(s)…',
                    'fa-rotate'
                );
                startProcessingQueue();
            }
        } catch (e) {
            console.warn('[Upload] resurfaceFailedUploads failed:', e);
        } finally {
            _isResurfacing = false;
        }
    };

    return {
        resurfaceFailedUploads,
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
