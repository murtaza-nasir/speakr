/**
 * Audio recording state management
 */

export function createAudioState(ref) {
    // --- Audio Recording State ---
    const isRecording = ref(false);
    const mediaRecorder = ref(null);
    const audioChunks = ref([]);
    const audioBlobURL = ref(null);
    const recordingTime = ref(0);
    const recordingInterval = ref(null);
    const canRecordAudio = ref(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    const canRecordSystemAudio = ref(false);
    const systemAudioSupported = ref(false);
    const systemAudioError = ref('');
    const recordingNotes = ref('');
    const showSystemAudioHelp = ref(false);

    // ASR options for recording view
    const asrLanguage = ref('');
    const asrMinSpeakers = ref('');
    const asrMaxSpeakers = ref('');

    // Audio context and analyzers
    const audioContext = ref(null);
    const analyser = ref(null);
    const micAnalyser = ref(null);
    const systemAnalyser = ref(null);
    const visualizer = ref(null);
    const micVisualizer = ref(null);
    const systemVisualizer = ref(null);
    const animationFrameId = ref(null);
    const recordingMode = ref('microphone');
    const activeStreams = ref([]);

    // --- Wake Lock and Background Recording ---
    const wakeLock = ref(null);
    const recordingNotification = ref(null);
    const isPageVisible = ref(true);

    // --- Recording Size Monitoring ---
    const estimatedFileSize = ref(0);
    const fileSizeWarningShown = ref(false);
    const recordingQuality = ref('optimized');
    const actualBitrate = ref(0);
    const maxRecordingMB = ref(200);
    const sizeCheckInterval = ref(null);

    return {
        // Recording state
        isRecording,
        mediaRecorder,
        audioChunks,
        audioBlobURL,
        recordingTime,
        recordingInterval,
        canRecordAudio,
        canRecordSystemAudio,
        systemAudioSupported,
        systemAudioError,
        recordingNotes,
        showSystemAudioHelp,

        // ASR options
        asrLanguage,
        asrMinSpeakers,
        asrMaxSpeakers,

        // Audio context
        audioContext,
        analyser,
        micAnalyser,
        systemAnalyser,
        visualizer,
        micVisualizer,
        systemVisualizer,
        animationFrameId,
        recordingMode,
        activeStreams,

        // Wake lock
        wakeLock,
        recordingNotification,
        isPageVisible,

        // Size monitoring
        estimatedFileSize,
        fileSizeWarningShown,
        recordingQuality,
        actualBitrate,
        maxRecordingMB,
        sizeCheckInterval
    };
}
