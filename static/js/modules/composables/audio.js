/**
 * Audio recording composable
 * Handles microphone/system audio recording with visualizers and wake lock
 */

export function useAudio(state, utils) {
    const {
        isRecording, mediaRecorder, audioContext, analyser, micAnalyser, systemAnalyser,
        audioChunks, recordingTime, recordingInterval, recordingMode, audioBlobURL,
        estimatedFileSize, actualBitrate, recordingNotes, recordingQuality,
        maxRecordingMB, fileSizeWarningShown, sizeCheckInterval, recordingDisclaimer,
        showRecordingDisclaimerModal, currentView, isDarkMode, wakeLock, animationFrameId,
        activeStreams, visualizer, micVisualizer, systemVisualizer, canRecordAudio,
        canRecordSystemAudio, systemAudioSupported, systemAudioError, globalError,
        selectedTagIds, asrLanguage, asrMinSpeakers, asrMaxSpeakers, uploadQueue,
        progressPopupMinimized, progressPopupClosed
    } = state;

    const { showToast, setGlobalError, formatFileSize, startUploadQueue } = utils;

    // Acquire wake lock to prevent screen from sleeping during recording
    const acquireWakeLock = async () => {
        try {
            if ('wakeLock' in navigator) {
                wakeLock.value = await navigator.wakeLock.request('screen');
                console.log('[WakeLock] Acquired - screen will stay awake during recording');

                // Listen for wake lock release
                wakeLock.value.addEventListener('release', () => {
                    console.log('[WakeLock] Released');
                });

                return true;
            } else {
                console.warn('[WakeLock] Wake Lock API not supported');
                showToast('Screen may sleep during recording', 'info');
                return false;
            }
        } catch (err) {
            console.warn('[WakeLock] Could not acquire:', err.message);
            if (err.name === 'NotAllowedError') {
                showToast('Screen lock permission denied', 'warning');
            } else if (err.name === 'NotSupportedError') {
                showToast('Wake lock not supported on this device', 'info');
            }
            return false;
        }
    };

    // Release wake lock
    const releaseWakeLock = async () => {
        if (wakeLock.value) {
            try {
                await wakeLock.value.release();
                wakeLock.value = null;
                console.log('[WakeLock] Released');
            } catch (err) {
                console.warn('[WakeLock] Could not release:', err.message);
            }
        }
    };

    // Show recording notification
    const showRecordingNotification = async () => {
        if ('Notification' in window && Notification.permission === 'granted') {
            // Notifications handled by service worker
        }
    };

    // Detect system audio capabilities
    const detectSystemAudioCapabilities = async () => {
        systemAudioSupported.value = false;
        canRecordSystemAudio.value = false;
        systemAudioError.value = '';

        // Check if getDisplayMedia is available
        if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
            systemAudioError.value = 'getDisplayMedia API not supported';
            return;
        }

        try {
            // Test if we can request system audio (this will prompt user)
            // We'll do this only when user actually tries to record
            systemAudioSupported.value = true;
            canRecordSystemAudio.value = true;
            systemAudioError.value = '';
        } catch (error) {
            systemAudioError.value = error.message;
            console.warn('System audio detection failed:', error);
        }
    };

    // Hide recording notification
    const hideRecordingNotification = async () => {
        // Notifications cleared when recording stops
    };

    // Handle visibility change (for wake lock re-acquisition)
    const handleVisibilityChange = async () => {
        if (document.visibilityState === 'visible' && isRecording.value) {
            console.log('[Visibility] Page visible, re-acquiring wake lock');
            const acquired = await acquireWakeLock();
            if (acquired) {
                showToast('Recording resumed - screen will stay awake', 'success');
            }
        } else if (document.visibilityState === 'hidden' && isRecording.value) {
            console.log('[Visibility] Page hidden, wake lock may be released by browser');
        }
    };

    // Start recording
    const startRecording = async (mode = 'microphone') => {
        // Check if there's a disclaimer to show
        if (recordingDisclaimer.value && recordingDisclaimer.value.trim() !== '') {
            showRecordingDisclaimerModal.value = true;
            state.pendingRecordingMode = mode;
            return;
        }

        await startRecordingInternal(mode);
    };

    // Accept recording disclaimer and start recording
    const acceptRecordingDisclaimer = async () => {
        showRecordingDisclaimerModal.value = false;
        await startRecordingInternal(state.pendingRecordingMode || 'microphone');
    };

    // Cancel recording disclaimer
    const cancelRecordingDisclaimer = () => {
        showRecordingDisclaimerModal.value = false;
        state.pendingRecordingMode = null;
    };

    // Internal start recording function
    const startRecordingInternal = async (mode) => {
        try {
            recordingMode.value = mode;
            audioChunks.value = [];
            recordingTime.value = 0;
            estimatedFileSize.value = 0;
            fileSizeWarningShown.value = false;

            let stream;
            let combinedStream;

            if (mode === 'microphone') {
                stream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: true,
                        noiseSuppression: true,
                        sampleRate: 48000
                    }
                });
                activeStreams.value = [stream];

                audioContext.value = new (window.AudioContext || window.webkitAudioContext)();
                const source = audioContext.value.createMediaStreamSource(stream);
                analyser.value = audioContext.value.createAnalyser();
                analyser.value.fftSize = 256;
                source.connect(analyser.value);

            } else if (mode === 'system') {
                stream = await navigator.mediaDevices.getDisplayMedia({
                    video: true,
                    audio: {
                        echoCancellation: false,
                        noiseSuppression: false,
                        autoGainControl: false
                    }
                });

                const audioTrack = stream.getAudioTracks()[0];
                if (!audioTrack) {
                    stream.getTracks().forEach(track => track.stop());
                    throw new Error('No system audio track available. Please share a tab or window with audio.');
                }

                // Stop video track
                stream.getVideoTracks().forEach(track => track.stop());
                stream = new MediaStream([audioTrack]);
                activeStreams.value = [stream];

                audioContext.value = new (window.AudioContext || window.webkitAudioContext)();
                const source = audioContext.value.createMediaStreamSource(stream);
                analyser.value = audioContext.value.createAnalyser();
                analyser.value.fftSize = 256;
                source.connect(analyser.value);

            } else if (mode === 'both') {
                const micStream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: true,
                        noiseSuppression: true,
                        sampleRate: 48000
                    }
                });

                const displayStream = await navigator.mediaDevices.getDisplayMedia({
                    video: true,
                    audio: {
                        echoCancellation: false,
                        noiseSuppression: false,
                        autoGainControl: false
                    }
                });

                const systemAudioTrack = displayStream.getAudioTracks()[0];
                if (!systemAudioTrack) {
                    micStream.getTracks().forEach(track => track.stop());
                    displayStream.getTracks().forEach(track => track.stop());
                    throw new Error('No system audio track available.');
                }

                // Stop video tracks
                displayStream.getVideoTracks().forEach(track => track.stop());

                // Create audio context and combine streams
                audioContext.value = new (window.AudioContext || window.webkitAudioContext)();
                const destination = audioContext.value.createMediaStreamDestination();

                const micSource = audioContext.value.createMediaStreamSource(micStream);
                const systemSource = audioContext.value.createMediaStreamSource(new MediaStream([systemAudioTrack]));

                // Create analysers for each source
                micAnalyser.value = audioContext.value.createAnalyser();
                micAnalyser.value.fftSize = 256;
                systemAnalyser.value = audioContext.value.createAnalyser();
                systemAnalyser.value.fftSize = 256;

                micSource.connect(micAnalyser.value);
                micSource.connect(destination);
                systemSource.connect(systemAnalyser.value);
                systemSource.connect(destination);

                combinedStream = destination.stream;
                activeStreams.value = [micStream, displayStream];
                stream = combinedStream;
            }

            // Determine best mime type
            const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus'
                : 'audio/webm';

            const recorder = new MediaRecorder(stream, { mimeType });

            recorder.ondataavailable = (event) => {
                if (event.data.size > 0) {
                    audioChunks.value.push(event.data);
                }
            };

            recorder.onstop = () => {
                const blob = new Blob(audioChunks.value, { type: mimeType });
                audioBlobURL.value = URL.createObjectURL(blob);
                stopSizeMonitoring();
            };

            mediaRecorder.value = recorder;
            recorder.start(1000);
            isRecording.value = true;

            // Start timer
            recordingInterval.value = setInterval(() => {
                recordingTime.value++;
            }, 1000);

            // Start size monitoring
            startSizeMonitoring();

            // Acquire wake lock
            await acquireWakeLock();

            // Show notification
            await showRecordingNotification();

            // Start visualizers
            drawVisualizers();

            // Notify service worker
            if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
                navigator.serviceWorker.controller.postMessage({
                    type: 'RECORDING_STATE',
                    isRecording: true
                });
            }

            // Switch to recording view
            currentView.value = 'recording';

        } catch (error) {
            console.error('Recording error:', error);
            setGlobalError(`Failed to start recording: ${error.message}`);

            // Clean up any started streams
            if (activeStreams.value.length > 0) {
                activeStreams.value.forEach(stream => {
                    stream.getTracks().forEach(track => track.stop());
                });
                activeStreams.value = [];
            }
        }
    };

    // Stop recording
    const stopRecording = async () => {
        if (mediaRecorder.value && isRecording.value) {
            mediaRecorder.value.stop();
            isRecording.value = false;

            // Clear the recording timer
            if (recordingInterval.value) {
                clearInterval(recordingInterval.value);
                recordingInterval.value = null;
            }

            stopSizeMonitoring();
            cancelAnimationFrame(animationFrameId.value);
            animationFrameId.value = null;

            // Stop all active media streams (mic, screen share, etc.)
            if (activeStreams.value.length > 0) {
                activeStreams.value.forEach(stream => {
                    stream.getTracks().forEach(track => track.stop());
                });
                activeStreams.value = [];
            }

            // Release wake lock
            await releaseWakeLock();

            // Hide recording notification
            await hideRecordingNotification();

            // Notify service worker
            if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
                navigator.serviceWorker.controller.postMessage({
                    type: 'RECORDING_STATE',
                    isRecording: false,
                    duration: recordingTime.value
                });
            }
        }
    };

    // Upload recorded audio
    const uploadRecordedAudio = () => {
        if (!audioBlobURL.value) {
            setGlobalError("No recorded audio to upload.");
            return;
        }
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        const recordedFile = new File(audioChunks.value, `recording-${timestamp}.webm`, { type: 'audio/webm' });

        // Get selected tags as objects and create a DEEP copy to prevent reactivity issues
        const selectedTagsTemp = selectedTagIds.value.map(tagId => {
            const tag = state.availableTags.value.find(t => t.id == tagId);
            return tag || null;
        }).filter(Boolean);

        // Deep clone to completely break reactivity chain - JSON parse/stringify removes all proxies
        const selectedTags = JSON.parse(JSON.stringify(selectedTagsTemp));

        // Add to upload queue
        uploadQueue.value.push({
            file: recordedFile,
            notes: recordingNotes.value,
            tags: selectedTags, // Completely non-reactive deep copy
            asrOptions: {
                language: asrLanguage.value,
                min_speakers: asrMinSpeakers.value,
                max_speakers: asrMaxSpeakers.value
            },
            status: 'queued',
            recordingId: null,
            clientId: `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`,
            error: null,
            willAutoSummarize: false // Server will tell us via SUMMARIZING status
        });

        discardRecording();

        // Return to upload view (main UI)
        currentView.value = 'upload';

        // Start upload immediately
        progressPopupMinimized.value = false;
        progressPopupClosed.value = false;

        if (startUploadQueue) {
            startUploadQueue();
        }
    };

    // Discard recording
    const discardRecording = async () => {
        if (audioBlobURL.value) {
            URL.revokeObjectURL(audioBlobURL.value);
        }
        audioBlobURL.value = null;
        audioChunks.value = [];
        isRecording.value = false;
        recordingTime.value = 0;
        if (recordingInterval.value) clearInterval(recordingInterval.value);
        recordingNotes.value = '';
        selectedTagIds.value = [];
        asrLanguage.value = '';
        asrMinSpeakers.value = '';
        asrMaxSpeakers.value = '';

        await releaseWakeLock();
        await hideRecordingNotification();
    };

    // Draw single visualizer
    const drawSingleVisualizer = (analyserNode, canvasElement) => {
        if (!analyserNode || !canvasElement) return;

        const bufferLength = analyserNode.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);
        analyserNode.getByteFrequencyData(dataArray);

        const canvasCtx = canvasElement.getContext('2d');
        const WIDTH = canvasElement.width;
        const HEIGHT = canvasElement.height;

        canvasCtx.clearRect(0, 0, WIDTH, HEIGHT);

        const barWidth = (WIDTH / bufferLength) * 1.5;
        let barHeight;
        let x = 0;

        const buttonColor = getComputedStyle(document.documentElement).getPropertyValue('--bg-button').trim();
        const buttonHoverColor = getComputedStyle(document.documentElement).getPropertyValue('--bg-button-hover').trim();

        const gradient = canvasCtx.createLinearGradient(0, 0, 0, HEIGHT);
        if (isDarkMode.value) {
            gradient.addColorStop(0, buttonColor);
            gradient.addColorStop(0.6, buttonHoverColor);
            gradient.addColorStop(1, 'rgba(0, 0, 0, 0.2)');
        } else {
            gradient.addColorStop(0, buttonColor);
            gradient.addColorStop(0.5, buttonHoverColor);
            gradient.addColorStop(1, 'rgba(0, 0, 0, 0.1)');
        }

        for (let i = 0; i < bufferLength; i++) {
            barHeight = dataArray[i] / 2.5;
            canvasCtx.fillStyle = gradient;
            canvasCtx.fillRect(x, HEIGHT - barHeight, barWidth, barHeight);
            x += barWidth + 2;
        }
    };

    // Draw visualizers
    const drawVisualizers = () => {
        if (!isRecording.value) {
            if (animationFrameId.value) {
                cancelAnimationFrame(animationFrameId.value);
                animationFrameId.value = null;
            }
            return;
        }

        animationFrameId.value = requestAnimationFrame(drawVisualizers);

        if (recordingMode.value === 'both') {
            drawSingleVisualizer(micAnalyser.value, micVisualizer.value);
            drawSingleVisualizer(systemAnalyser.value, systemVisualizer.value);
        } else {
            drawSingleVisualizer(analyser.value, visualizer.value);
        }
    };

    // Update file size estimate
    const updateFileSizeEstimate = () => {
        if (!isRecording.value || !audioChunks.value.length) return;

        const totalSize = audioChunks.value.reduce((sum, chunk) => sum + chunk.size, 0);
        estimatedFileSize.value = totalSize;

        if (recordingTime.value > 0) {
            actualBitrate.value = (totalSize * 8) / recordingTime.value;
        }

        // Check for size warning
        const sizeMB = totalSize / (1024 * 1024);
        const warningThresholdMB = maxRecordingMB.value * 0.8;

        if (sizeMB > warningThresholdMB && !fileSizeWarningShown.value) {
            fileSizeWarningShown.value = true;
            showToast(
                `Recording size is ${formatFileSize(totalSize)}. Consider stopping soon.`,
                'fa-exclamation-triangle',
                5000
            );
        }

        // Auto-stop if max size reached
        if (sizeMB > maxRecordingMB.value) {
            stopRecording();
            showToast(
                `Recording automatically stopped at ${formatFileSize(totalSize)}`,
                'fa-stop-circle',
                7000
            );
        }
    };

    // Start size monitoring
    const startSizeMonitoring = () => {
        if (sizeCheckInterval.value) {
            clearInterval(sizeCheckInterval.value);
        }
        sizeCheckInterval.value = setInterval(updateFileSizeEstimate, 2000);
    };

    // Stop size monitoring
    const stopSizeMonitoring = () => {
        if (sizeCheckInterval.value) {
            clearInterval(sizeCheckInterval.value);
            sizeCheckInterval.value = null;
        }
    };

    // Check if there's an unsaved recording
    const hasUnsavedRecording = () => {
        return isRecording.value || audioBlobURL.value;
    };

    // Initialize audio capabilities
    const initializeAudio = async () => {
        await detectSystemAudioCapabilities();
    };

    return {
        startRecording,
        stopRecording,
        discardRecording,
        uploadRecordedAudio,
        acceptRecordingDisclaimer,
        cancelRecordingDisclaimer,
        updateFileSizeEstimate,
        startSizeMonitoring,
        stopSizeMonitoring,
        drawVisualizers,
        drawSingleVisualizer,
        handleVisibilityChange,
        hasUnsavedRecording,
        acquireWakeLock,
        releaseWakeLock,
        detectSystemAudioCapabilities,
        initializeAudio
    };
}
