/**
 * Upload composable
 * Handles file upload queue and processing
 */

import { ref, computed, nextTick } from 'vue';
import { uploadFile } from '../utils/apiClient.js';

export function useUpload() {
    // State
    const uploadQueue = ref([]);
    const currentlyProcessingFile = ref(null);
    const processingProgress = ref(0);
    const processingMessage = ref('');
    const isProcessingActive = ref(false);
    const pollInterval = ref(null);
    const progressPopupMinimized = ref(false);
    const progressPopupClosed = ref(false);
    const maxFileSizeMB = ref(250);
    const chunkingEnabled = ref(true);
    const dragover = ref(false);

    // Computed
    const hasQueuedFiles = computed(() => {
        return uploadQueue.value.some(item => item.status === 'queued');
    });

    const processingCount = computed(() => {
        return uploadQueue.value.filter(item => item.status === 'processing' || item.status === 'queued').length;
    });

    const completedCount = computed(() => {
        return uploadQueue.value.filter(item => item.status === 'completed').length;
    });

    const errorCount = computed(() => {
        return uploadQueue.value.filter(item => item.status === 'error').length;
    });

    // Methods
    const addFilesToQueue = (files) => {
        const maxFileSize = maxFileSizeMB.value * 1024 * 1024;

        for (const file of files) {
            if (file.size > maxFileSize) {
                uploadQueue.value.push({
                    file,
                    status: 'error',
                    error: `File exceeds maximum size of ${maxFileSizeMB.value}MB`,
                    clientId: Date.now() + Math.random()
                });
                continue;
            }

            const isAudio = file.type.startsWith('audio/') ||
                           file.type.startsWith('video/') ||
                           /\.(mp3|wav|ogg|m4a|flac|webm|mp4|mov|avi|mkv)$/i.test(file.name);

            if (!isAudio) {
                uploadQueue.value.push({
                    file,
                    status: 'error',
                    error: 'File type not supported',
                    clientId: Date.now() + Math.random()
                });
                continue;
            }

            uploadQueue.value.push({
                file,
                status: 'queued',
                recordingId: null,
                clientId: Date.now() + Math.random(),
                error: null
            });
        }

        if (!isProcessingActive.value && hasQueuedFiles.value) {
            startProcessingQueue();
        }
    };

    const startProcessingQueue = async () => {
        if (isProcessingActive.value) return;

        const nextItem = uploadQueue.value.find(item => item.status === 'queued');
        if (!nextItem) {
            isProcessingActive.value = false;
            return;
        }

        isProcessingActive.value = true;
        currentlyProcessingFile.value = nextItem;
        nextItem.status = 'uploading';
        processingProgress.value = 0;
        processingMessage.value = 'Uploading...';

        try {
            const data = await uploadFile('/api/recordings/upload', nextItem.file, (progress) => {
                processingProgress.value = progress;
                processingMessage.value = `Uploading... ${Math.round(progress)}%`;
            });

            nextItem.recordingId = data.recording_id;
            nextItem.status = 'processing';
            processingMessage.value = 'Processing...';

            // Start polling for status
            pollProcessingStatus(nextItem);

        } catch (error) {
            nextItem.status = 'error';
            nextItem.error = error.message;
            currentlyProcessingFile.value = null;
            isProcessingActive.value = false;

            // Continue with next file
            if (hasQueuedFiles.value) {
                await nextTick();
                startProcessingQueue();
            }
        }
    };

    const pollProcessingStatus = (queueItem) => {
        if (pollInterval.value) {
            clearInterval(pollInterval.value);
        }

        pollInterval.value = setInterval(async () => {
            try {
                const response = await fetch(`/api/recordings/${queueItem.recordingId}/status`);
                const data = await response.json();

                if (data.status === 'COMPLETED') {
                    clearInterval(pollInterval.value);
                    pollInterval.value = null;

                    queueItem.status = 'completed';
                    currentlyProcessingFile.value = null;
                    isProcessingActive.value = false;
                    processingProgress.value = 100;
                    processingMessage.value = 'Complete!';

                    // Continue with next file
                    if (hasQueuedFiles.value) {
                        await nextTick();
                        startProcessingQueue();
                    }

                } else if (data.status === 'ERROR') {
                    clearInterval(pollInterval.value);
                    pollInterval.value = null;

                    queueItem.status = 'error';
                    queueItem.error = data.error_message || 'Processing failed';
                    currentlyProcessingFile.value = null;
                    isProcessingActive.value = false;

                    // Continue with next file
                    if (hasQueuedFiles.value) {
                        await nextTick();
                        startProcessingQueue();
                    }

                } else {
                    // Still processing
                    if (data.status === 'SUMMARIZING') {
                        processingMessage.value = 'Generating summary...';
                        processingProgress.value = 80;
                    } else {
                        processingMessage.value = 'Transcribing...';
                        processingProgress.value = 50;
                    }
                }

            } catch (error) {
                console.error('Error polling status:', error);
            }
        }, 3000);
    };

    const removeFromQueue = (clientId) => {
        const index = uploadQueue.value.findIndex(item => item.clientId === clientId);
        if (index > -1) {
            uploadQueue.value.splice(index, 1);
        }
    };

    const clearCompletedFromQueue = () => {
        uploadQueue.value = uploadQueue.value.filter(item =>
            item.status !== 'completed' && item.status !== 'error'
        );
    };

    const handleDragEnter = (event) => {
        event.preventDefault();
        dragover.value = true;
    };

    const handleDragLeave = (event) => {
        event.preventDefault();
        dragover.value = false;
    };

    const handleDrop = (event) => {
        event.preventDefault();
        dragover.value = false;

        const files = Array.from(event.dataTransfer.files);
        if (files.length > 0) {
            addFilesToQueue(files);
        }
    };

    const handleFileSelect = (event) => {
        const files = Array.from(event.target.files);
        if (files.length > 0) {
            addFilesToQueue(files);
        }
        event.target.value = '';
    };

    const minimizeProgressPopup = () => {
        progressPopupMinimized.value = true;
    };

    const maximizeProgressPopup = () => {
        progressPopupMinimized.value = false;
    };

    const closeProgressPopup = () => {
        progressPopupClosed.value = true;
    };

    const loadConfig = async () => {
        try {
            const response = await fetch('/api/config');
            const data = await response.json();
            maxFileSizeMB.value = data.max_file_size_mb || 250;
            chunkingEnabled.value = data.chunking_enabled !== false;
        } catch (error) {
            console.error('Error loading config:', error);
        }
    };

    return {
        // State
        uploadQueue,
        currentlyProcessingFile,
        processingProgress,
        processingMessage,
        isProcessingActive,
        progressPopupMinimized,
        progressPopupClosed,
        maxFileSizeMB,
        chunkingEnabled,
        dragover,

        // Computed
        hasQueuedFiles,
        processingCount,
        completedCount,
        errorCount,

        // Methods
        addFilesToQueue,
        startProcessingQueue,
        removeFromQueue,
        clearCompletedFromQueue,
        handleDragEnter,
        handleDragLeave,
        handleDrop,
        handleFileSelect,
        minimizeProgressPopup,
        maximizeProgressPopup,
        closeProgressPopup,
        loadConfig
    };
}
