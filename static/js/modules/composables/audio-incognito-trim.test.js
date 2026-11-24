/**
 * Incognito from New recording sends the same range fields as a queued file.
 * The range control is the one already on the finished recording.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../db/recording-persistence.js', () => ({
    clearRecordingSession: vi.fn(async () => {}),
    makeSessionInMemoryOnly: vi.fn(),
    checkForRecoverableRecording: vi.fn(async () => null),
    recoverRecording: vi.fn(async () => null),
}));
vi.mock('../db/incognito-storage.js', () => ({
    saveIncognitoRecording: vi.fn(),
}));
vi.mock('../db/server-recording-sessions.js', () => ({
    abortSession: vi.fn(async () => {}),
}));
vi.mock('../utils/audio-capture.js', () => ({
    DISPLAY_MEDIA_CONSTRAINTS: {},
    closeAudioContext: vi.fn(),
    stopStreams: vi.fn(),
}));
vi.mock('../utils/platform.js', () => ({
    findExternalAudioInputCandidates: () => [],
    getAudioInputCandidateSignature: () => '',
    shouldAutoOfferExternalAudioInput: () => false,
}));

const ref = (value) => ({ value });
globalThis.Vue = {
    ref,
    watch: () => {},
    computed: (fn) => ({ get value() { return fn(); } }),
    markRaw: (x) => x,
    nextTick: async () => {},
};
Object.defineProperty(globalThis, 'navigator', {
    value: { userAgent: 'node' },
    configurable: true,
});
globalThis.document = { getElementById: () => null };
globalThis.window = {};
globalThis.URL.revokeObjectURL = () => {};

const { useAudio } = await import('./audio.js');

function makeAudio() {
    const names = [
        'isRecording', 'isPaused', 'mediaRecorder', 'audioContext', 'analyser', 'micAnalyser', 'systemAnalyser',
        'audioChunks', 'recordingTime', 'recordingInterval', 'recordingMode', 'audioBlobURL',
        'estimatedFileSize', 'actualBitrate', 'recordingNotes', 'recordingQuality',
        'maxRecordingMB', 'fileSizeWarningShown', 'sizeCheckInterval', 'isServerStreamedRecording',
        'isFinalizingRecording', 'recordingDisclaimer', 'showRecordingDisclaimerModal', 'pendingRecordingMode',
        'currentView', 'showUploadModal', 'showSystemAudioHelp', 'disableAudioProcessing',
        'recordSystemVideo', 'recordingVideoActive', 'videoRetentionEnabled',
        'inputAudioDevices', 'selectedMicDeviceId', 'selectedSecondaryDeviceId',
        'isDarkMode', 'wakeLock', 'animationFrameId', 'activeStreams', 'visualizer', 'micVisualizer',
        'systemVisualizer', 'canRecordAudio', 'canRecordSystemAudio', 'systemAudioSupported', 'systemAudioError',
        'globalError', 'selectedTagIds', 'selectedFolderId', 'asrLanguage', 'asrMinSpeakers', 'asrMaxSpeakers',
        'asrInitialPrompt', 'asrHotwords', 'asrTranscriptionModel', 'uploadQueue', 'progressPopupMinimized',
        'enableIncognitoMode', 'incognitoMode', 'incognitoRecording', 'incognitoProcessing',
        'processingMessage', 'processingProgress', 'selectedRecording',
    ];
    const state = {};
    for (const name of names) state[name] = ref(null);
    state.audioChunks.value = [];
    state.activeStreams.value = [];
    state.selectedTagIds.value = [];
    state.asrLanguage.value = '';
    state.asrMinSpeakers.value = '';
    state.asrMaxSpeakers.value = '';
    state.asrInitialPrompt.value = '';
    state.asrHotwords.value = '';
    state.asrTranscriptionModel.value = '';
    state.isRecording.value = false;
    state.recordingTime.value = 0;
    state.incognitoProcessing.value = false;
    state.incognitoRecording.value = null;
    state.incognitoMode.value = true;
    state.enableIncognitoMode.value = true;
    state.processingProgress.value = 0;
    state.processingMessage.value = '';
    state.progressPopupMinimized.value = false;
    state.audioBlobURL.value = 'blob:captured';
    const utils = { showToast: vi.fn(), setGlobalError: vi.fn(), formatFileSize: (n) => `${n}`, startUploadQueue: vi.fn(), t: (key) => key };
    return { state, utils, audio: useAudio(state, utils) };
}

describe('incognito from New recording', () => {
    let fetchMock;

    beforeEach(() => {
        fetchMock = vi.fn(async () => ({
            ok: true,
            headers: { get: () => 'application/json' },
            json: async () => ({ transcription: 'kept', title: 'Incognito' }),
        }));
        globalThis.fetch = fetchMock;
    });

    afterEach(() => {
        delete globalThis.fetch;
    });

    it('sends trim_start and trim_end when the range control drops part of the file', async () => {
        const { audio } = makeAudio();
        audio.captureDuration.value = 6.2;
        audio.enableCaptureTrim();
        audio.captureTrimStart.value = 2;
        audio.captureTrimEnd.value = 5;

        await audio.uploadRecordedAudioIncognito();

        expect(fetchMock).toHaveBeenCalledTimes(1);
        const [url, opts] = fetchMock.mock.calls[0];
        expect(url).toBe('/api/recordings/incognito');
        expect(opts.body.get('trim_start')).toBe('2');
        expect(opts.body.get('trim_end')).toBe('5');
    });

    it('sends no range when the control still covers the whole file', async () => {
        const { audio } = makeAudio();
        audio.captureDuration.value = 6.2;
        audio.enableCaptureTrim();

        await audio.uploadRecordedAudioIncognito();

        const form = fetchMock.mock.calls[0][1].body;
        expect(form.get('trim_start')).toBeNull();
        expect(form.get('trim_end')).toBeNull();
    });
});
