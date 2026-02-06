/**
 * Audio recording composable
 * Handles microphone/system audio recording with visualizers and wake lock
 */

import * as RecordingDB from '../db/recording-persistence.js';
import * as IncognitoStorage from '../db/incognito-storage.js';

export function useAudio(state, utils) {
    const {
        isRecording, mediaRecorder, audioContext, analyser, micAnalyser, systemAnalyser,
        audioChunks, recordingTime, recordingInterval, recordingMode, audioBlobURL,
        estimatedFileSize, actualBitrate, recordingNotes, recordingQuality,
        maxRecordingMB, fileSizeWarningShown, sizeCheckInterval, recordingDisclaimer,
        showRecordingDisclaimerModal, pendingRecordingMode, currentView, isDarkMode, wakeLock, animationFrameId,
        activeStreams, visualizer, micVisualizer, systemVisualizer, canRecordAudio,
        canRecordSystemAudio, systemAudioSupported, systemAudioError, globalError,
        selectedTagIds, selectedFolderId, asrLanguage, asrMinSpeakers, asrMaxSpeakers, uploadQueue,
        progressPopupMinimized, progressPopupClosed,
        // Incognito mode
        enableIncognitoMode, incognitoMode, incognitoRecording, incognitoProcessing,
        processingMessage, processingProgress, selectedRecording
    } = state;

    const { showToast, setGlobalError, formatFileSize, startUploadQueue } = utils;

    // Local state for pending streams and chunk tracking
    let pendingDisplayStream = null;
    let currentChunkIndex = 0;

    // iOS detection
    const isiOS = () => {
        return /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
    };

    // Silent audio for iOS wake lock alternative
    let silentAudio = null;

    // Create silent audio using data URL (1 second of silence)
    const createSilentAudio = () => {
        if (!silentAudio) {
            // Base64 encoded 1-second silent MP3
            const silentMp3 = 'data:audio/mp3;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAADhAC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7v////////////////////////////////////////////////////////////AAAAAExhdmM1OC4xMwAAAAAAAAAAAAAAACQCgAAAAAAAAAOEfxVqYQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA//sQZAAP8AAAaQAAAAgAAA0gAAABAAABpAAAACAAADSAAAAETEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//sQZDwP8AAAaQAAAAgAAA0gAAABAAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU=';
            silentAudio = new Audio(silentMp3);
            silentAudio.loop = true;
            silentAudio.volume = 0.01; // Very low volume, almost silent
        }
        return silentAudio;
    };

    // Start iOS wake lock (play silent audio)
    const startiOSWakeLock = async () => {
        try {
            const audio = createSilentAudio();
            await audio.play();
            console.log('[iOS Wake Lock] Silent audio playing to prevent sleep');
            return true;
        } catch (error) {
            console.warn('[iOS Wake Lock] Failed to start silent audio:', error);
            showToast('iOS wake lock may not work - keep screen active', 'warning');
            return false;
        }
    };

    // Stop iOS wake lock (stop silent audio)
    const stopiOSWakeLock = () => {
        if (silentAudio) {
            silentAudio.pause();
            silentAudio.currentTime = 0;
            console.log('[iOS Wake Lock] Silent audio stopped');
        }
    };

    // Acquire wake lock to prevent screen from sleeping during recording
    const acquireWakeLock = async () => {
        // iOS doesn't support Wake Lock API - use silent audio instead
        if (isiOS()) {
            return await startiOSWakeLock();
        }

        // Android/Desktop: use native Wake Lock API
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
        // iOS: stop silent audio
        if (isiOS()) {
            stopiOSWakeLock();
            return;
        }

        // Android/Desktop: release native wake lock
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

    // Note: System audio capability detection is now handled by computed property
    // canRecordSystemAudio = computed(() => navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia)

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
    // IMPORTANT: For Firefox, getDisplayMedia MUST be the first async call from user gesture
    const startRecording = async (mode = 'microphone') => {
        const needsDisplayMedia = mode === 'system' || mode === 'both';

        // For system audio modes, get display media FIRST before any other operations
        // This is required for Firefox's "transient activation" security model
        if (needsDisplayMedia) {
            try {
                const displayStream = await navigator.mediaDevices.getDisplayMedia({
                    video: true,
                    audio: true
                });

                // Check if we got an audio track
                const audioTrack = displayStream.getAudioTracks()[0];
                if (!audioTrack) {
                    displayStream.getTracks().forEach(track => track.stop());
                    showToast('No audio track - check "Share audio" option', 'error');
                    return;
                }

                // Store stream for use after disclaimer (if any)
                pendingDisplayStream = displayStream;
            } catch (error) {
                console.error('[Recording] Failed to get display media:', error);
                if (error.name === 'NotAllowedError') {
                    showToast('Screen sharing was cancelled', 'error');
                } else {
                    showToast(`Failed to capture: ${error.message}`, 'error');
                }
                return;
            }
        }

        // Now check for disclaimer (after we've secured the display stream)
        if (recordingDisclaimer.value && recordingDisclaimer.value.trim() !== '') {
            showRecordingDisclaimerModal.value = true;
            pendingRecordingMode.value = mode;
            return;
        }

        await startRecordingInternal(mode);
    };

    // Accept recording disclaimer and start recording
    const acceptRecordingDisclaimer = async () => {
        showRecordingDisclaimerModal.value = false;
        await startRecordingInternal(pendingRecordingMode.value || 'microphone');
    };

    // Cancel recording disclaimer
    const cancelRecordingDisclaimer = () => {
        showRecordingDisclaimerModal.value = false;
        // Clean up pending display stream if user cancels
        if (pendingDisplayStream) {
            pendingDisplayStream.getTracks().forEach(track => track.stop());
            pendingDisplayStream = null;
        }
        pendingRecordingMode.value = null;
    };

    // Internal start recording function
    const startRecordingInternal = async (mode) => {
        try {
            recordingMode.value = mode;
            audioChunks.value = [];
            recordingTime.value = 0;
            estimatedFileSize.value = 0;
            fileSizeWarningShown.value = false;

            // Initialize IndexedDB session
            currentChunkIndex = 0;

            let stream;
            let combinedStream;

            if (mode === 'microphone') {
                if (!canRecordAudio.value) {
                    throw new Error('Microphone recording is not available. Make sure you are using HTTPS.');
                }
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
                if (!canRecordSystemAudio.value) {
                    throw new Error('System audio recording is not available. Make sure you are using HTTPS.');
                }
                // Use pre-obtained display stream (required for Firefox user gesture)
                // or get it now for browsers that don't require immediate call
                const isFirefox = navigator.userAgent.toLowerCase().indexOf('firefox') > -1;

                if (pendingDisplayStream) {
                    stream = pendingDisplayStream;
                    pendingDisplayStream = null;
                } else {
                    const displayMediaConstraints = {
                        video: true,
                        audio: isFirefox ? true : {
                            echoCancellation: false,
                            noiseSuppression: false,
                            autoGainControl: false
                        }
                    };
                    stream = await navigator.mediaDevices.getDisplayMedia(displayMediaConstraints);
                }

                const audioTrack = stream.getAudioTracks()[0];
                if (!audioTrack) {
                    stream.getTracks().forEach(track => track.stop());
                    const browserName = isFirefox ? 'Firefox' : 'your browser';
                    throw new Error(
                        `No system audio track available. In ${browserName}, please:\n` +
                        `1. Share a BROWSER TAB that is actively playing audio\n` +
                        `2. Make sure "Share tab audio" checkbox is checked\n` +
                        `3. The audio must be playing when you start sharing`
                    );
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
                if (!canRecordAudio.value || !canRecordSystemAudio.value) {
                    throw new Error('Recording is not available. Make sure you are using HTTPS.');
                }
                const micStream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: true,
                        noiseSuppression: true,
                        sampleRate: 48000
                    }
                });

                // Use pre-obtained display stream or get it now
                const isFirefox = navigator.userAgent.toLowerCase().indexOf('firefox') > -1;
                let displayStream;

                if (pendingDisplayStream) {
                    displayStream = pendingDisplayStream;
                    pendingDisplayStream = null;
                } else {
                    displayStream = await navigator.mediaDevices.getDisplayMedia({
                        video: true,
                        audio: isFirefox ? true : {
                            echoCancellation: false,
                            noiseSuppression: false,
                            autoGainControl: false
                        }
                    });
                }

                const systemAudioTrack = displayStream.getAudioTracks()[0];
                if (!systemAudioTrack) {
                    micStream.getTracks().forEach(track => track.stop());
                    displayStream.getTracks().forEach(track => track.stop());
                    const browserName = isFirefox ? 'Firefox' : 'your browser';
                    throw new Error(
                        `No system audio track available. In ${browserName}, please:\n` +
                        `1. Share a BROWSER TAB that is actively playing audio\n` +
                        `2. Make sure "Share tab audio" checkbox is checked\n` +
                        `3. The audio must be playing when you start sharing`
                    );
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

            // Start IndexedDB recording session - convert Vue reactive objects to plain objects
            try {
                await RecordingDB.startRecordingSession({
                    mode,
                    notes: recordingNotes.value || '',
                    tags: selectedTagIds.value ? [...selectedTagIds.value] : [], // Convert reactive array to plain array
                    asrOptions: {
                        language: asrLanguage.value || '',
                        min_speakers: asrMinSpeakers.value || '',
                        max_speakers: asrMaxSpeakers.value || ''
                    },
                    mimeType
                });
            } catch (dbError) {
                console.warn('[Recording] IndexedDB persistence failed, continuing without persistence:', dbError);
            }

            recorder.ondataavailable = async (event) => {
                if (event.data.size > 0) {
                    audioChunks.value.push(event.data);

                    // Save chunk to IndexedDB for crash recovery
                    try {
                        await RecordingDB.saveChunk(event.data, currentChunkIndex);
                        await RecordingDB.updateRecordingMetadata({
                            duration: recordingTime.value,
                            notes: recordingNotes.value || ''
                        });
                        currentChunkIndex++;
                    } catch (dbError) {
                        // Don't spam console - recording continues in memory regardless
                    }
                }
            };

            recorder.onstop = () => {
                const blob = new Blob(audioChunks.value, { type: mimeType });
                audioBlobURL.value = URL.createObjectURL(blob);
                stopSizeMonitoring();
            };

            mediaRecorder.value = recorder;
            recorder.start(5000); // 5-second chunks for less overhead while still enabling crash recovery
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
    const uploadRecordedAudio = async () => {
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
            folder_id: selectedFolderId.value,
            preserveOptions: true, // Prevents startUpload from overwriting recording's options
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

        // Clear IndexedDB session after successful queue
        try {
            await RecordingDB.clearRecordingSession();
        } catch (dbError) {
            console.warn('[Recording] Failed to clear IndexedDB session:', dbError);
        }

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

    // Upload recorded audio in incognito mode
    const uploadRecordedAudioIncognito = async () => {
        if (!audioBlobURL.value) {
            setGlobalError("No recorded audio to upload.");
            return;
        }

        // Check if incognito state is available
        if (!incognitoProcessing || !incognitoRecording) {
            console.warn('[Incognito] Incognito state not available, falling back to normal upload');
            uploadRecordedAudio();
            return;
        }

        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        const recordedFile = new File(audioChunks.value, `recording-${timestamp}.webm`, { type: 'audio/webm' });

        incognitoProcessing.value = true;
        processingMessage.value = 'Processing recording in incognito mode...';
        processingProgress.value = 10;
        progressPopupMinimized.value = false;
        progressPopupClosed.value = false;

        try {
            const formData = new FormData();
            formData.append('file', recordedFile);

            // Add ASR options
            if (asrLanguage.value) {
                formData.append('language', asrLanguage.value);
            }
            if (asrMinSpeakers.value && asrMinSpeakers.value !== '') {
                formData.append('min_speakers', asrMinSpeakers.value.toString());
            }
            if (asrMaxSpeakers.value && asrMaxSpeakers.value !== '') {
                formData.append('max_speakers', asrMaxSpeakers.value.toString());
            }

            // Request auto-summarization
            formData.append('auto_summarize', 'true');

            processingMessage.value = 'Uploading recording for incognito processing...';
            processingProgress.value = 20;

            console.log('[Incognito] Uploading recorded audio');

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

            // Clear IndexedDB session
            try {
                await RecordingDB.clearRecordingSession();
            } catch (dbError) {
                console.warn('[Recording] Failed to clear IndexedDB session:', dbError);
            }

            // Clear recording state
            discardRecording();

            processingProgress.value = 100;
            processingMessage.value = 'Incognito recording ready!';

            // Auto-select the incognito recording and switch to detail view
            selectedRecording.value = incognitoData;
            currentView.value = 'detail';

            // Reset incognito mode toggle
            incognitoMode.value = false;

            // Show toast
            showToast('Incognito recording processed - data will be lost when tab closes', 'fa-user-secret');

            console.log('[Incognito] Recording processing complete');

        } catch (error) {
            console.error('[Incognito] Recording processing failed:', error);
            setGlobalError(`Incognito processing failed: ${error.message}`);
        } finally {
            incognitoProcessing.value = false;
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

        // Clear IndexedDB session
        try {
            await RecordingDB.clearRecordingSession();
        } catch (dbError) {
            console.warn('[Recording] Failed to clear IndexedDB session:', dbError);
        }

        await releaseWakeLock();
        await hideRecordingNotification();

        // Return to upload view
        currentView.value = 'upload';
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

    // Recover recording from IndexedDB
    const recoverRecordingFromDB = async () => {
        try {
            const recovered = await RecordingDB.recoverRecording();
            if (!recovered) {
                return null;
            }

            // Restore chunks
            audioChunks.value = recovered.chunks;

            // Create blob URL
            const blob = new Blob(recovered.chunks, { type: recovered.metadata.mimeType });
            audioBlobURL.value = URL.createObjectURL(blob);

            // Restore metadata
            recordingMode.value = recovered.metadata.mode;
            recordingNotes.value = recovered.metadata.notes;
            selectedTagIds.value = recovered.metadata.tags;
            recordingTime.value = recovered.metadata.duration;

            if (recovered.metadata.asrOptions) {
                asrLanguage.value = recovered.metadata.asrOptions.language || '';
                asrMinSpeakers.value = recovered.metadata.asrOptions.min_speakers || '';
                asrMaxSpeakers.value = recovered.metadata.asrOptions.max_speakers || '';
            }

            console.log('[Recording] Successfully recovered recording from IndexedDB');
            return recovered.metadata;
        } catch (error) {
            console.error('[Recording] Failed to recover recording:', error);
            return null;
        }
    };

    // No initialization needed - system audio detection is handled by computed property
    const initializeAudio = async () => {
        // Placeholder for future initialization if needed
    };

    return {
        startRecording,
        stopRecording,
        discardRecording,
        uploadRecordedAudio,
        uploadRecordedAudioIncognito,
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
        initializeAudio,
        recoverRecordingFromDB,
        checkForRecoverableRecording: RecordingDB.checkForRecoverableRecording,
        clearRecordingSession: RecordingDB.clearRecordingSession
    };
}
