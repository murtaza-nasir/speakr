/**
 * Audio Recorder composable
 * Handles audio recording from microphone and/or system audio
 */

import { ref, computed } from 'vue';

export function useAudioRecorder() {
    // State
    const isRecording = ref(false);
    const isPaused = ref(false);
    const audioChunks = ref([]);
    const audioBlobURL = ref(null);
    const recordingMode = ref('microphone');
    const mediaRecorder = ref(null);
    const audioContext = ref(null);
    const activeStreams = ref([]);
    const recordingDuration = ref(0);
    const recordingSize = ref(0);
    const actualBitrate = ref(128000);
    const recordingTimer = ref(null);
    const recordingNotes = ref('');
    const showRecordingDisclaimerModal = ref(false);
    const pendingRecordingMode = ref(null);
    const recordingDisclaimer = ref('');

    // Computed
    const canRecordAudio = computed(() => navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    const canRecordSystemAudio = computed(() => navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);

    const recordingTimeFormatted = computed(() => {
        const hours = Math.floor(recordingDuration.value / 3600);
        const mins = Math.floor((recordingDuration.value % 3600) / 60);
        const secs = recordingDuration.value % 60;
        if (hours > 0) {
            return hours + ':' + String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
        }
        return mins + ':' + String(secs).padStart(2, '0');
    });

    // Methods
    const startRecording = async (mode = 'microphone') => {
        if (recordingDisclaimer.value && recordingDisclaimer.value.trim()) {
            pendingRecordingMode.value = mode;
            showRecordingDisclaimerModal.value = true;
            return;
        }
        await startRecordingActual(mode);
    };

    const acceptDisclaimer = async () => {
        showRecordingDisclaimerModal.value = false;
        if (pendingRecordingMode.value) {
            await startRecordingActual(pendingRecordingMode.value);
            pendingRecordingMode.value = null;
        }
    };

    const cancelDisclaimer = () => {
        showRecordingDisclaimerModal.value = false;
        pendingRecordingMode.value = null;
    };

    const startRecordingActual = async (mode = 'microphone') => {
        recordingMode.value = mode;
        audioChunks.value = [];
        audioBlobURL.value = null;
        recordingNotes.value = '';
        activeStreams.value = [];
        recordingDuration.value = 0;
        recordingSize.value = 0;

        try {
            let combinedStream = null;
            let micStream = null;
            let systemStream = null;

            if (mode === 'microphone' || mode === 'both') {
                if (!canRecordAudio.value) throw new Error('Microphone not supported');
                micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                activeStreams.value.push(micStream);
            }

            if (mode === 'system' || mode === 'both') {
                if (!canRecordSystemAudio.value) throw new Error('System audio not supported');
                try {
                    systemStream = await navigator.mediaDevices.getDisplayMedia({ audio: true, video: true });
                    if (systemStream.getAudioTracks().length === 0) {
                        systemStream.getVideoTracks().forEach(track => track.stop());
                        throw new Error('System audio permission not granted');
                    }
                    activeStreams.value.push(systemStream);
                } catch (err) {
                    if (mode === 'system') throw err;
                    systemStream = null;
                }
            }

            // Combine streams
            if (micStream && systemStream) {
                audioContext.value = new (window.AudioContext || window.webkitAudioContext)();
                const micSource = audioContext.value.createMediaStreamSource(micStream);
                const systemSource = audioContext.value.createMediaStreamSource(systemStream);
                const destination = audioContext.value.createMediaStreamDestination();
                micSource.connect(destination);
                systemSource.connect(destination);
                combinedStream = new MediaStream([destination.stream.getAudioTracks()[0]]);
            } else if (systemStream) {
                combinedStream = new MediaStream(systemStream.getAudioTracks());
            } else if (micStream) {
                combinedStream = micStream;
            }

            if (!combinedStream) throw new Error('No audio streams available');

            // Create MediaRecorder
            const options = { mimeType: 'audio/webm;codecs=opus', audioBitsPerSecond: 32000 };
            if (MediaRecorder.isTypeSupported(options.mimeType)) {
                mediaRecorder.value = new MediaRecorder(combinedStream, options);
                actualBitrate.value = 32000;
            } else {
                mediaRecorder.value = new MediaRecorder(combinedStream);
                actualBitrate.value = 128000;
            }

            mediaRecorder.value.ondataavailable = (event) => {
                if (event.data && event.data.size > 0) {
                    audioChunks.value.push(event.data);
                    recordingSize.value += event.data.size;
                }
            };

            mediaRecorder.value.onstop = () => {
                const audioBlob = new Blob(audioChunks.value, { type: mediaRecorder.value.mimeType });
                audioBlobURL.value = URL.createObjectURL(audioBlob);
            };

            mediaRecorder.value.start(1000);
            isRecording.value = true;

            recordingTimer.value = setInterval(() => {
                recordingDuration.value++;
            }, 1000);

        } catch (error) {
            stopAllStreams();
            throw error;
        }
    };

    const stopRecording = () => {
        if (mediaRecorder.value && isRecording.value) {
            mediaRecorder.value.stop();
            isRecording.value = false;
            isPaused.value = false;

            if (recordingTimer.value) {
                clearInterval(recordingTimer.value);
                recordingTimer.value = null;
            }
            stopAllStreams();
        }
    };

    const pauseRecording = () => {
        if (mediaRecorder.value && isRecording.value && !isPaused.value) {
            mediaRecorder.value.pause();
            isPaused.value = true;
            if (recordingTimer.value) {
                clearInterval(recordingTimer.value);
                recordingTimer.value = null;
            }
        }
    };

    const resumeRecording = () => {
        if (mediaRecorder.value && isRecording.value && isPaused.value) {
            mediaRecorder.value.resume();
            isPaused.value = false;
            recordingTimer.value = setInterval(() => {
                recordingDuration.value++;
            }, 1000);
        }
    };

    const stopAllStreams = () => {
        activeStreams.value.forEach(stream => {
            stream.getTracks().forEach(track => track.stop());
        });
        activeStreams.value = [];

        if (audioContext.value) {
            audioContext.value.close().catch(e => console.error("Error closing AudioContext:", e));
            audioContext.value = null;
        }
    };

    const resetRecording = () => {
        stopRecording();
        audioChunks.value = [];
        audioBlobURL.value = null;
        recordingDuration.value = 0;
        recordingSize.value = 0;
        recordingNotes.value = '';
    };

    const getRecordingBlob = () => {
        if (audioChunks.value.length === 0) return null;
        return new Blob(audioChunks.value, { type: 'audio/webm' });
    };

    return {
        isRecording, isPaused, audioBlobURL, recordingMode, recordingDuration, recordingSize, recordingNotes,
        showRecordingDisclaimerModal, recordingDisclaimer, canRecordAudio, canRecordSystemAudio, recordingTimeFormatted,
        startRecording, stopRecording, pauseRecording, resumeRecording, resetRecording, acceptDisclaimer, cancelDisclaimer, getRecordingBlob
    };
}
