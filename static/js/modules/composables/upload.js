/**
 * Upload management composable
 * Handles file uploads, queue processing, and progress tracking
 */

import * as FailedUploads from '../db/failed-uploads.js';
import * as IncognitoStorage from '../db/incognito-storage.js';
import * as RecordingDB from '../db/recording-persistence.js';
import { getUploadCsrfToken, isCsrfRejection } from '../csrf.js';
import { shouldSliceUpload, uploadFileInSlices } from '../db/sliced-file-upload.js';
import { computeUploadTimeout } from '../utils/upload-timeout.js';
import { recorderInUse } from '../utils/recorder-state.js';
import { confirmDialog } from '../utils/dialogs.js';
import { getFriendlyError } from '../utils/upload-errors.js';
import { uploadCancelPlan } from '../utils/upload-cancel.js';
import {
    sortQueuedForJoin, moveQueuedItem, placeQueuedItem, totalJoinDuration,
    newJoinGroupId, MAX_JOIN_FILES,
} from '../utils/upload-join.js';
import { UPLOAD_ACCEPT, isAcceptedUploadFile, isVideoFileName } from '../utils/upload-file-type.js';
import * as ServerSessions from '../db/server-recording-sessions.js';
import {
    failedUploadRecord, partitionByOwner, belongsTo, resurfaceAction, queueItemFromRecord, coveredByPendingUpload,
} from '../utils/pending-uploads.js';

export function useUpload(state, utils) {
    const {
        uploadQueue, currentlyProcessingFile, processingProgress, processingMessage,
        isProcessingActive, pollInterval, progressPopupMinimized,
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
        uploadDisclaimer, showUploadDisclaimerModal,
        isRecording
    } = state;

    const { computed, nextTick, ref, markRaw } = Vue;

    const { setGlobalError, showToast, formatFileSize, onChatComplete, t } = utils;

    // How often, and how far apart, an upload answered with 409
    // upload_in_progress is sent again before it counts as failed.
    const IN_PROGRESS_ATTEMPTS = 12;
    const IN_PROGRESS_RETRY_MS = 10000;

    // An upload error shows its message in the interface language; matchText keeps
    // the English wording that getFriendlyError's text patterns recognise.
    const uploadError = (message, matchText) => {
        const err = new Error(message);
        err.matchText = matchText;
        return err;
    };

    // --- Join several files into one recording ------------------------------
    // Offered once two or more files are queued. The queue order is the join
    // order; turning the switch on sorts the files by modified time and name.
    const joinMode = ref(false);
    const joinTitle = ref('');
    const joinDragClientId = ref(null);
    const joinQueuedItems = computed(() => uploadQueue.value.filter(item => item.status === 'queued'));
    const joinAvailable = computed(() =>
        joinQueuedItems.value.length >= 2
        && joinQueuedItems.value.length <= MAX_JOIN_FILES
        && !(incognitoMode && incognitoMode.value));
    const joinActive = computed(() => joinMode.value && joinAvailable.value);
    const joinTotalDuration = computed(() => totalJoinDuration(joinQueuedItems.value));
    const joinHasVideo = computed(() => joinQueuedItems.value.some(item => isVideoFileName(item.file?.name)));

    const setJoinMode = (on) => {
        joinMode.value = !!on;
        if (on) uploadQueue.value = sortQueuedForJoin(uploadQueue.value);
    };
    const moveJoinItem = (clientId, delta) => {
        uploadQueue.value = moveQueuedItem(uploadQueue.value, clientId, delta);
    };
    const handleJoinDragStart = (clientId, event) => {
        joinDragClientId.value = clientId;
        if (event?.dataTransfer) {
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', clientId);
        }
    };
    const handleJoinDrop = (targetIndex) => {
        if (joinDragClientId.value) {
            uploadQueue.value = placeQueuedItem(uploadQueue.value, joinDragClientId.value, targetIndex);
        }
        joinDragClientId.value = null;
    };
    const handleJoinDragEnd = () => { joinDragClientId.value = null; };

    // One part failed: stop the others and discard what the server holds, so
    // the user retries the whole join with the files they still have.
    const failJoinGroup = async (failedItem, message) => {
        const group = failedItem.join.group;
        for (const item of uploadQueue.value) {
            if (item === failedItem || !item.join || item.join.group !== group) continue;
            if (['ready', 'uploading', 'pending', 'completed'].includes(item.status)) {
                item.joinCancelled = true;
                item.status = 'failed';
                item.error = message;
                item.progress = 0;
                if (item._xhr) {
                    try { item._xhr.abort(); } catch (_) { /* already finished */ }
                }
            }
        }
        try {
            const token = await getUploadCsrfToken();
            await fetch(`/upload/join/${encodeURIComponent(group)}`, {
                method: 'DELETE',
                headers: token ? { 'X-CSRFToken': token } : {},
            });
        } catch (e) {
            console.warn('[Upload] Could not discard the uploaded parts of a failed join:', e);
        }
    };

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

            // Audio, or a video container with audio; the picker offers the same (#510)
            const isAudioFile = isAcceptedUploadFile(fileObject);

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
                const isVideoExt = isVideoFileName(fileObject.name);
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

    // Cancel an upload: a file that still waits is removed from the queue;
    // one in flight has its request aborted and the server is told to
    // discard what the aborted request may still produce (the converted
    // file, the recording). Resolves to { ok, action, reason }, see
    // uploadCancelPlan. A file whose upload has finished is a job on the
    // server by then and is canceled there (cancelJob in app.modular.js).
    const cancelUpload = async (clientId) => {
        const item = uploadQueue.value.find(entry => entry.clientId === clientId);
        const plan = uploadCancelPlan(item);
        if (plan.action === 'remove') {
            uploadQueue.value = uploadQueue.value.filter(entry => entry.clientId !== clientId);
            return { ok: true, ...plan };
        }
        if (plan.action !== 'abort') return { ok: false, ...plan };

        item.uploadCanceled = true;
        // The other files of a join are stopped with it, and the parts the
        // server holds are discarded.
        const group = item.join ? item.join.group : null;
        const stopped = group
            ? uploadQueue.value.filter(entry => entry.join && entry.join.group === group)
            : [item];
        for (const entry of stopped) {
            entry.uploadCanceled = true;
            if (entry._xhr) {
                try { entry._xhr.abort(); } catch (_) { /* already finished */ }
            }
        }
        try {
            const token = await getUploadCsrfToken();
            const headers = token ? { 'X-CSRFToken': token } : {};
            if (group) {
                // The parts of a join are stored apart from recordings.
                await fetch(`/upload/join/${encodeURIComponent(group)}`, { method: 'DELETE', headers });
            } else {
                await fetch('/upload/cancel', {
                    method: 'POST',
                    headers: { ...headers, 'Content-Type': 'application/json' },
                    body: JSON.stringify({ client_upload_id: item.clientId }),
                });
            }
        } catch (e) {
            console.warn('[Upload] Could not tell the server about a canceled upload:', e);
        }
        const gone = new Set(stopped.map(entry => entry.clientId));
        uploadQueue.value = uploadQueue.value.filter(entry => !gone.has(entry.clientId));
        return { ok: true, ...plan };
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
    // "Upload" closes the modal after queuing; "Upload & Add More" keeps it
    // open. The flag survives the disclaimer round-trip (startUpload returns
    // early, acceptUploadDisclaimer re-invokes it) and is reset when the
    // disclaimer is cancelled (cancelPendingUploadClose).
    let closeModalAfterUpload = false;

    const startUploadAndClose = () => {
        closeModalAfterUpload = true;
        startUpload();
    };

    const cancelPendingUploadClose = () => {
        closeModalAfterUpload = false;
    };

    const startUpload = () => {
        const pendingFiles = uploadQueue.value.filter(item => item.status === 'queued');
        if (pendingFiles.length === 0) {
            closeModalAfterUpload = false;
            return;
        }
        // Show upload disclaimer if configured
        if (uploadDisclaimer.value && uploadDisclaimer.value.trim() !== '') {
            showUploadDisclaimerModal.value = true;
            return;
        }
        // Joining: every queued file becomes one part of the same group, in
        // queue order.
        if (joinActive.value) {
            const items = joinQueuedItems.value;
            const group = newJoinGroupId();
            const title = joinTitle.value.trim();
            items.forEach((item, index) => {
                item.join = { group, index, count: items.length, title };
                item.displayName = `${item.file.name} · ${index + 1}/${items.length}`;
            });
        }
        joinMode.value = false;
        joinTitle.value = '';

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
        startProcessingQueue();
        if (closeModalAfterUpload) {
            closeModalAfterUpload = false;
            if (showUploadModal) showUploadModal.value = false;
        }
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
     *
     * Files past the slice threshold go up through the sliced-upload
     * transport instead of one multipart POST, so a reverse proxy whose
     * body limit is below the file size cannot reject them. Both
     * transports send the same form and return the same response, so
     * everything below the transport call is shared.
     */
    const uploadSingleFile = async (fileItem) => {
        await acquireUploadSlot();
        if (fileItem.joinCancelled) {
            releaseUploadSlot();
            return;
        }

        fileItem.status = 'uploading';
        fileItem.progress = 5;
        const sliced = shouldSliceUpload(fileItem.file);

        try {
            const formData = new FormData();
            if (!sliced) formData.append('file', fileItem.file);
            // A recording made here was checked against the storage quota
            // when it started, so the server keeps it whatever its size (#413).
            if (fileItem.fromInProgressRecording) formData.append('from_recorder', 'true');

            // Send file's lastModified timestamp for meeting_date
            if (fileItem.file.lastModified) {
                const lastModified = fileItem.file.lastModified;
                formData.append('file_last_modified', lastModified.toString());
                // Client timezone offset so filename-parsed wall-clock dates
                // can be converted to UTC server-side (#342)
                formData.append('client_tz_offset', new Date().getTimezoneOffset().toString());
            }

            // The browser's IANA zone, so the title of this recording already
            // uses the uploader's local time (#412).
            try {
                const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
                if (zone) formData.append('client_timezone', zone);
            } catch (e) { /* older browser: the account or admin default applies */ }

            if (fileItem.notes) {
                formData.append('notes', fileItem.notes);
            }

            if (fileItem.join) {
                formData.append('join_group', fileItem.join.group);
                formData.append('join_index', String(fileItem.join.index));
                formData.append('join_count', String(fileItem.join.count));
                if (fileItem.join.title) formData.append('title', fileItem.join.title);
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

            // Names this upload for POST /upload/cancel (cancelUpload below).
            formData.append('client_upload_id', fileItem.clientId);

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

                fileItem.serverProcessing = false;
                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable) {
                        // Map upload progress to 5-90% range
                        fileItem.progress = Math.round(5 + (e.loaded / e.total) * 85);
                    }
                };
                // Every byte is sent; the server now checks and converts the
                // file before answering, which can take minutes.
                xhr.upload.onload = () => { fileItem.serverProcessing = true; fileItem.serverProcessingAt = Date.now(); };

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
                        reject(uploadError(titleMatch?.[1] || h1Match?.[1] ||
                            t('errors.serverError', { status: xhr.status }), `Server error (${xhr.status}): Response was not JSON`));
                        return;
                    }

                    let parsed;
                    try {
                        parsed = JSON.parse(xhr.responseText);
                    } catch {
                        reject(uploadError(t('errors.responseUnreadable'), `Invalid JSON response (${xhr.status})`));
                        return;
                    }

                    if (xhr.status === 202 && (parsed.id || parsed.join_pending)) {
                        resolve(parsed);
                    } else if (!String(xhr.status).startsWith('2')) {
                        let errorMsg = parsed.error || t('errors.uploadFailedWithStatus', { status: xhr.status });
                        // English twin for getFriendlyError's text patterns.
                        let matchText = parsed.error || `Upload failed with status ${xhr.status}`;
                        if (xhr.status === 413) {
                            const maxMb = parsed.max_size_mb?.toFixed(0) || maxFileSizeMB.value;
                            errorMsg = parsed.error || t('errors.fileTooLargeMaxSize', { size: maxMb });
                            matchText = parsed.error || `File too large. Max: ${maxMb} MB.`;
                        }
                        if (parsed.code === 'storage_quota_exceeded' && t) {
                            const quotaMsg = t('storageQuota.uploadRefused', {
                                used: formatFileSize(parsed.used_bytes), quota: formatFileSize(parsed.quota_bytes),
                                file: formatFileSize(parsed.file_bytes),
                            });
                            if (quotaMsg && quotaMsg !== 'storageQuota.uploadRefused') errorMsg = quotaMsg;
                        }
                        // A file the server could not read (#482): the item says so in the UI language.
                        if (parsed.code === 'audio_decode_failed' && t) errorMsg = t('errors.audioDecodeFailedGuidance');
                        if (parsed.code === 'empty_file' && t) errorMsg = t('errors.emptyFileGuidance');
                        const err = uploadError(errorMsg, matchText);
                        err.code = parsed.code;
                        if (isCsrfRejection(xhr.status, parsed.error || '')) {
                            err.isCsrfRejection = true;
                        }
                        reject(err);
                    } else {
                        reject(uploadError(t('errors.unexpectedResponse'), 'The upload finished with an unexpected response.'));
                    }
                };

                xhr.onerror = () => reject(uploadError(t('errors.networkErrorDuringUpload'), 'Network error during upload'));
                xhr.ontimeout = () => reject(uploadError(t('errors.uploadTimedOut'), 'Upload timed out'));
                // abort() fires neither of the two above; without this the
                // upload of a canceled file would wait forever.
                xhr.onabort = () => reject(uploadError('Upload canceled', 'Upload canceled'));

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

            let data;
            if (sliced) {
                data = await uploadFileInSlices(fileItem.file, formData, {
                    onProgress: (fraction) => {
                        fileItem.progress = Math.round(5 + fraction * 85);
                        fileItem.serverProcessing = fraction >= 1;
                        if (fraction >= 1 && !fileItem.serverProcessingAt) fileItem.serverProcessingAt = Date.now();
                    },
                    onXhr: (xhr) => { fileItem._xhr = xhr; },
                    isCanceled: () => !!fileItem.uploadCanceled,
                });
            } else {
                // Refresh the token right before sending, and retry exactly once
                // with another fresh token if the server still rejects it —
                // mirroring what the fetch interceptor in csrf-refresh.js does
                // for non-XHR requests.
                const sendOnce = async () => {
                    try {
                        return await sendUpload(await getUploadCsrfToken());
                    } catch (uploadError) {
                        if (!uploadError.isCsrfRejection) throw uploadError;
                        console.warn(`[Upload] CSRF rejection for ${fileItem.file.name}; refreshing token and retrying once.`);
                        fileItem.progress = 5;
                        return await sendUpload(await getUploadCsrfToken());
                    }
                };
                // An earlier request of this upload is still storing it
                // (#626): send again a little later, and the server answers
                // with the recording that request stored.
                for (let attempt = 1; ; attempt++) {
                    try {
                        data = await sendOnce();
                        break;
                    } catch (uploadError) {
                        if (uploadError.code !== 'upload_in_progress' || attempt >= IN_PROGRESS_ATTEMPTS
                            || fileItem.uploadCanceled) throw uploadError;
                        console.warn(`[Upload] ${fileItem.file.name} is still being stored by an earlier request; sending again shortly.`);
                        fileItem.progress = 5;
                        await new Promise(resolve => setTimeout(resolve, IN_PROGRESS_RETRY_MS));
                        if (fileItem.uploadCanceled) throw uploadError;
                    }
                }
            }

            // The answer arrived at the moment the user canceled: the server
            // deletes this recording (POST /upload/cancel), so it is not shown.
            if (fileItem.uploadCanceled) return;

            // One part of a join is stored; the part that completes the group
            // gets the recording back.
            if (data.join_pending) {
                // Stays active until the rest of its join arrives.
                fileItem.status = 'pending';
                fileItem.progress = 100;
                fileItem.serverProcessing = false;
                fileItem.joinWaiting = { received: data.received, total: data.total };
                return;
            }
            if (fileItem.join) {
                // From here one entry stands for the joined recording.
                const group = fileItem.join.group;
                uploadQueue.value = uploadQueue.value.filter(item =>
                    item === fileItem || !(item.join && item.join.group === group));
                fileItem.displayName = data.title || fileItem.displayName;
                if (recordings.value.some(r => r.id === data.id)) {
                    fileItem.status = 'pending';
                    fileItem.recordingId = data.id;
                    fileItem.progress = 100;
                    return;
                }
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
            //
            // Only the session this recording was saved under is cleared: the
            // upload can finish after the user has started the next recording,
            // whose crash-recovery copy must survive (#407).
            if (fileItem.fromInProgressRecording) {
                try {
                    await RecordingDB.clearRecordingSessionIfStartedAt(fileItem.recordingSessionStartTime);
                } catch (dbError) {
                    console.warn('[Upload] Failed to clear recording session after successful upload:', dbError);
                }
            }

            // The recording went up in one piece after its session on the
            // server could not be finished (offline at Stop): that session
            // holds a part of the same audio and is removed, so that it is not
            // offered for recovery at the next start (#650).
            if (fileItem.serverSessionId) {
                try {
                    await ServerSessions.discardSession(fileItem.serverSessionId);
                } catch (sessionError) {
                    console.warn('[Upload] Could not remove the server session of an uploaded recording:', sessionError);
                }
            }

            // Add to recordings list (a replayed retry may be listed already)
            if (!recordings.value.some(r => r.id === data.id)) {
                recordings.value.unshift(data);
                totalRecordings.value++;
            }

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
            //
            // Not while the next recording is under way (#407): the upload can
            // finish minutes after the user started recording again, and
            // switching views then took them off the recorder.
            // An upload sent again after a reload (_resurfaced) leaves the
            // person where they are.
            if (fileItem.fromInProgressRecording && !fileItem._resurfaced && !recorderInUse(isRecording, currentView)) {
                selectedRecording.value = data;
                currentView.value = 'detail';
                if (showUploadModal) showUploadModal.value = false;
            }

            // Handle duplicate warning
            if (data.duplicate_warning) {
                const warning = data.duplicate_warning;
                const existingDate = warning.existing_created_at
                    ? utils.parseServerInstant(warning.existing_created_at).toLocaleDateString(window.i18n?.getIntlLocale?.())
                    : '';
                const existingName = warning.existing_title || t('sharing.unknown');
                showToast(
                    `⚠️ ${existingName} (${existingDate})`,
                    'fa-copy'
                );
                fileItem.duplicateWarning = warning;
            }

        } catch (error) {
            if (fileItem.uploadCanceled) {
                // Stopped by cancelUpload: not a failure, nothing to retry.
                return;
            }
            if (fileItem.joinCancelled) {
                // Stopped because another part of its join failed.
                fileItem.status = 'failed';
                return;
            }
            console.error(`Upload Error for ${fileItem.file.name} (Client ID: ${fileItem.clientId}):`, error);
            fileItem.status = 'failed';
            fileItem.error = error.message;
            fileItem.progress = 0;
            if (fileItem.join) {
                const message = t('upload.joinFailed', { name: fileItem.file.name });
                await failJoinGroup(fileItem, message);
                setGlobalError(`${message} ${error.message}`);
                return;
            }

            // Show friendly error message
            const friendlyErr = getFriendlyError(error.matchText || error.message, t, error.code);
            setGlobalError(`${friendlyErr.title}: ${friendlyErr.guidance}`);

            // If this failure is itself a resurfaced retry, just bump the retry
            // count on the EXISTING IndexedDB record — don't store a duplicate or
            // re-download. resurfaceFailedUploads() will pick it up again on the
            // next online/load. Once the retry cap is reached, hand the audio
            // back as a browser download instead of leaving the record trapped
            // in IndexedDB (capped records are never resurfaced again).
            if (fileItem._failedUploadId) {
                const newRetryCount = (fileItem._retryCount || 0) + 1;
                try {
                    await FailedUploads.updateRetryCount(
                        fileItem._failedUploadId, newRetryCount, error.message);
                } catch (e) {
                    console.warn('[Upload] Could not bump retry count:', e);
                }
                if (newRetryCount >= MAX_AUTO_RETRIES) {
                    await handOffExhaustedUpload(fileItem._failedUploadId, fileItem.file);
                }
            } else if (fileItem.fromInProgressRecording) {
                // Defense-in-depth recovery (issue #297, #287, #313) for in-app
                // recordings, whose ONLY copy lives in browser memory:
                //   1. Persist the file to IndexedDB so it can be auto-retried
                //      in-app on the next online/page-load (resurfaceFailedUploads).
                //   2. If IndexedDB persistence fails (quota exceeded, private
                //      mode, missing IndexedDB), fall back to a browser download
                //      so the user keeps a local copy. Permanent audio loss is the
                //      worst outcome, so both layers are best-effort.
                //
                // The record keeps the recording's crash-recovery start time,
                // its server session and the account (#650); the queue item
                // gets the record's id, so that a later success or failure of
                // this item updates that record and no second one is written.
                let persistedToIndexedDB = false;
                try {
                    const failedUploadId = await FailedUploads.storeFailedUpload(
                        failedUploadRecord(fileItem, error, currentUserId()));
                    persistedToIndexedDB = true;
                    fileItem._failedUploadId = failedUploadId;
                    fileItem._retryCount = 0;
                } catch (dbError) {
                    console.warn('[Upload] IndexedDB persistence failed for failed upload:', dbError);
                }

                // The record now holds the whole recording. The crash-recovery
                // copy of the same recording (the chunks saved while it was
                // recorded) would be offered as a second recording at the next
                // start, beside the upload of the record: it goes.
                if (persistedToIndexedDB && fileItem.recordingSessionStartTime) {
                    try {
                        await RecordingDB.clearRecordingSessionIfStartedAt(fileItem.recordingSessionStartTime);
                    } catch (dbError) {
                        console.warn('[Upload] Could not clear the crash-recovery copy of a saved upload:', dbError);
                    }
                }

                if (persistedToIndexedDB) {
                    // Saved for retry. We deliberately do NOT retry right here
                    // (a server-side failure while still "online" would just storm
                    // the endpoint); resurfaceFailedUploads() resubmits it through
                    // the normal CSRF-correct upload path on the next `online`
                    // event or page load — in every browser, no Background Sync.
                    showToast?.(
                        (t && t('toasts.uploadFailedWillRetry'))
                            || 'Couldn\'t upload. The recording is saved and is uploaded again when you reconnect.',
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
                                || 'Couldn\'t upload, and the audio couldn\'t be saved. Record again.',
                            'fa-triangle-exclamation'
                        );
                    }
                }
            }
            // else: the file came from the user's filesystem (picker or
            // drag-drop), so the original still exists on disk and nothing is
            // lost by the failure. Persisting a copy to IndexedDB — or worse,
            // re-downloading a multi-GB file into Downloads — would only
            // duplicate it. The queue item stays 'failed' with the error
            // surfaced above; the user can re-add the file once the cause
            // (e.g. a proxy size limit) is addressed.
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
        // Apply language from first tag (works for all connectors; speaker counts need diarization)
        if (firstTag && firstTag.default_language) {
            uploadLanguage.value = firstTag.default_language;
        }
        if (firstTag && connectorSupportsDiarization.value) {
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
                throw uploadError(titleMatch?.[1] || t('errors.serverErrorStatus', { status: response.status }), titleMatch?.[1] || `Server error (${response.status})`);
            }

            const data = await response.json();

            if (!response.ok || data.error) {
                const err = uploadError(data.error || t('errors.processingFailedWithStatus', { status: response.status }), data.error || `Processing failed with status ${response.status}`);
                err.code = data.code;
                err.errorType = data.error_type;
                throw err;
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

            // Auto-select the incognito recording and switch to detail view,
            // unless the next recording is already under way (#407); it stays
            // available from the incognito entry in the sidebar.
            if (!recorderInUse(isRecording, currentView)) {
                selectedRecording.value = incognitoData;
                currentView.value = 'detail';
            }
            // Dismiss the upload modal if it was still open behind the
            // success transition.
            if (showUploadModal) showUploadModal.value = false;

            // Show toast
            showToast(t('incognito.recordingProcessed'), 'fa-user-secret');

            console.log('[Incognito] Processing complete');

        } catch (error) {
            console.error('[Incognito] Processing failed:', error);
            const friendlyErr = getFriendlyError(error.matchText || error.message, t, error.code, error.errorType);
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
    const clearIncognitoRecordingWithConfirm = async () => {
        if (incognitoRecording && incognitoRecording.value) {
            if (await confirmDialog({ message: t('incognito.discardConfirm'), confirmLabel: t('common.discard'), danger: true })) {
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

    // When auto-retry gives up on a persisted recording, hand the audio back
    // to the user as a browser download and drop the IndexedDB record —
    // otherwise capped records sit invisibly in IndexedDB forever (the
    // resurfacing loop skips them) and the recording is effectively lost.
    // Keeps the record when the download could not be triggered so a later
    // pass can try again.
    const handOffExhaustedUpload = async (failedUploadId, file) => {
        const downloaded = FailedUploads.triggerLocalDownload(
            file, file?.name || `speakr-recording-${Date.now()}.webm`);
        if (!downloaded) return false;
        try {
            await FailedUploads.deleteFailedUpload(failedUploadId);
        } catch (e) {
            console.warn('[Upload] Could not delete exhausted upload record:', e);
        }
        showToast?.(
            (t && t('toasts.uploadRetriesExhaustedDownloaded'))
                || 'Upload still failing. The audio was saved to your Downloads folder.',
            'fa-file-download'
        );
        return true;
    };

    // The account signed in on this page (#app data-current-user-id).
    const currentUserId = () => {
        if (utils.currentUserId) return String(utils.currentUserId() || '');
        try {
            const el = typeof document !== 'undefined' ? document.getElementById('app') : null;
            return (el && el.dataset && el.dataset.currentUserId) || '';
        } catch (e) {
            return '';
        }
    };

    // A browser shared by several accounts, or a session that ended without
    // a sign-out (the sign-out deletes every copy): what waits in this
    // browser for another account is deleted, never uploaded to this one,
    // and the person is told how many (#650). That is the failed-upload
    // records, the crash-recovery copy and the remembered server session of
    // that account. Copies saved by a release before this rule have no
    // account and are left as they were.
    // Resolved when the app's loading overlay (loading.js) is gone, or after 12 s.
    const pageShown = () => new Promise((resolve) => {
        if (typeof document === 'undefined' || !document.documentElement) { resolve(); return; }
        const started = Date.now();
        const look = () => {
            if (!document.documentElement.classList.contains('app-loading') || Date.now() - started > 12000) resolve();
            else setTimeout(look, 200);
        };
        look();
    });

    let _removing = null;
    const removeOtherAccountsCopies = () => {
        if (!_removing) _removing = (async () => {
            const me = currentUserId();
            if (!me) return 0;
            let removed = 0;
            try {
                const failed = await FailedUploads.getFailedUploads();
                for (const record of partitionByOwner(failed, me).others) {
                    try {
                        await FailedUploads.deleteFailedUpload(record.id);
                        removed++;
                    } catch (e) {
                        console.warn('[Upload] Could not delete a record of another account:', e);
                    }
                }
            } catch (e) {
                console.warn('[Upload] Could not read the records of other accounts:', e);
            }
            try {
                const session = await RecordingDB.checkForRecoverableRecording();
                if (session && session.ownerId && !belongsTo(session, me)) {
                    await RecordingDB.clearRecordingSessionIfStartedAt(session.startTime);
                    removed++;
                }
            } catch (e) {
                console.warn('[Upload] Could not check the crash-recovery copy:', e);
            }
            try {
                const remembered = ServerSessions.getRememberedSession();
                if (remembered && remembered.owner_id && !belongsTo({ ownerId: remembered.owner_id }, me)) {
                    // A session of another account cannot be deleted from this
                    // one (the server answers 404); its pointer is forgotten and
                    // the server removes the session when it expires.
                    ServerSessions.forgetActiveSession();
                    removed++;
                }
            } catch (e) {
                console.warn('[Upload] Could not check the remembered server session:', e);
            }
            if (removed > 0) {
                // Shown once the loading overlay is gone, so that it is seen.
                pageShown().then(() => showToast?.(removed === 1
                    ? t('upload.otherAccountCopiesDeletedOne')
                    : t('upload.otherAccountCopiesDeleted', { count: removed }), 'fa-trash', 8000));
            }
            return removed;
        })().finally(() => { _removing = null; });
        return _removing;
    };

    // Auto-retry path for #313: read the failed uploads persisted in IndexedDB,
    // rebuild a File for each, and resubmit them through the normal upload queue
    // (correct CSRF, visible in the Processing Queue, works in every browser —
    // no Background Sync). Triggered on page load and on the browser `online`
    // event. Items are removed from IndexedDB on success and retry-counted on
    // failure (see uploadSingleFile). The recordings made on the offline
    // recorder page (offline-recorder.html) are records of the same store.
    const resurfaceFailedUploads = async () => {
        if (_isResurfacing) return;
        if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
        _isResurfacing = true;
        try {
            await removeOtherAccountsCopies();
            const failed = await FailedUploads.getFailedUploads();
            const split = partitionByOwner(failed, currentUserId());
            let queued = 0;
            for (const rec of split.mine) {
                if (!rec || !rec.fileData) continue;
                if ((rec.retryCount || 0) >= MAX_AUTO_RETRIES) {
                    // Records that hit the cap before this hand-off existed (or
                    // whose earlier hand-off download failed): give the audio
                    // back now instead of skipping it forever.
                    const file = new File([rec.fileData],
                        rec.fileName || `speakr-recording-${Date.now()}.webm`,
                        { type: rec.mimeType || 'audio/webm' });
                    await handOffExhaustedUpload(rec.id, file);
                    continue;
                }

                // Skip if this upload is already represented in the queue and
                // waits or is in flight; replace a failed item of the same
                // upload so it is neither shown nor sent twice.
                const action = resurfaceAction(rec, uploadQueue.value);
                if (action === 'skip') continue;
                if (action === 'replace') {
                    uploadQueue.value = uploadQueue.value.filter(i => i.clientId !== rec.clientId);
                }

                const file = new File([rec.fileData], rec.fileName || `speakr-recording-${Date.now()}.webm`,
                    { type: rec.mimeType || 'audio/webm' });
                // status 'ready': picked up by startProcessingQueue (skips
                // startUpload's UI-state overwrite).
                uploadQueue.value.push(queueItemFromRecord(rec, markRaw ? markRaw(file) : file));
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

    /**
     * Whether a copy that the recovery check would offer (the local
     * crash-recovery session with this start time, or the server session
     * with this id) is held by a record that waits for its upload (#650).
     * Such a copy is not offered: the record is uploaded instead.
     */
    const pendingUploadCovers = async ({ startTime = null, sessionId = null } = {}) => {
        try {
            const failed = await FailedUploads.getFailedUploads();
            return coveredByPendingUpload(partitionByOwner(failed, currentUserId()).mine, { startTime, sessionId });
        } catch (e) {
            return false;
        }
    };

    return {
        uploadAccept: UPLOAD_ACCEPT,
        // Join several files into one recording
        joinMode,
        joinTitle,
        joinAvailable,
        joinActive,
        joinTotalDuration,
        joinHasVideo,
        joinDragClientId,
        setJoinMode,
        moveJoinItem,
        handleJoinDragStart,
        handleJoinDrop,
        handleJoinDragEnd,
        resurfaceFailedUploads,
        pendingUploadCovers,
        removeOtherAccountsCopies,
        handleDragOver,
        handleDragLeave,
        handleDrop,
        handleFileSelect,
        addFilesToQueue,
        removeFromQueue,
        cancelWaitingFile,
        cancelUpload,
        clearCompletedUploads,
        startUpload,
        startUploadAndClose,
        cancelPendingUploadClose,
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
