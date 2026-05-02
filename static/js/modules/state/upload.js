/**
 * Upload state management
 */

export function createUploadState(ref, computed) {
    // --- Upload State ---
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
    const chunkingMode = ref('size');
    const chunkingLimit = ref(20);
    const chunkingLimitDisplay = ref('20MB');
    const maxConcurrentUploads = ref(3);
    const recordingDisclaimer = ref('');
    const showRecordingDisclaimerModal = ref(false);
    const pendingRecordingMode = ref(null);

    // Advanced Options for ASR
    const showAdvancedOptions = ref(false);
    const uploadLanguage = ref('');
    const uploadMinSpeakers = ref('');
    const uploadMaxSpeakers = ref('');
    const uploadHotwords = ref('');
    const uploadInitialPrompt = ref('');
    // Per-upload transcription model selection (issue #266). Empty string = no
    // override; backend falls back to env-configured default.
    const uploadTranscriptionModel = ref('');
    // Populated from /api/config when admin set TRANSCRIPTION_MODELS_AVAILABLE.
    const transcriptionModelOptions = ref([]);

    // --- Computed Properties ---
    const totalInQueue = computed(() => uploadQueue.value.length);
    const completedInQueue = computed(() => uploadQueue.value.filter(item => item.status === 'completed' || item.status === 'failed').length);
    const finishedFilesInQueue = computed(() => uploadQueue.value.filter(item => ['completed', 'failed'].includes(item.status)));

    const clearCompletedUploads = () => {
        uploadQueue.value = uploadQueue.value.filter(item => !['completed', 'failed'].includes(item.status));
    };

    return {
        uploadQueue,
        currentlyProcessingFile,
        processingProgress,
        processingMessage,
        isProcessingActive,
        pollInterval,
        progressPopupMinimized,
        progressPopupClosed,
        maxFileSizeMB,
        chunkingEnabled,
        chunkingMode,
        chunkingLimit,
        chunkingLimitDisplay,
        maxConcurrentUploads,
        recordingDisclaimer,
        showRecordingDisclaimerModal,
        pendingRecordingMode,

        // Advanced Options
        showAdvancedOptions,
        uploadLanguage,
        uploadMinSpeakers,
        uploadMaxSpeakers,
        uploadHotwords,
        uploadInitialPrompt,
        uploadTranscriptionModel,
        transcriptionModelOptions,

        // Computed
        totalInQueue,
        completedInQueue,
        finishedFilesInQueue,

        // Methods
        clearCompletedUploads
    };
}
