/**
 * Audio recording composable
 * Handles microphone/system audio recording with visualizers and wake lock
 */

import * as RecordingDB from '../db/recording-persistence.js';
import * as IncognitoStorage from '../db/incognito-storage.js';
import * as ServerSessions from '../db/server-recording-sessions.js';

export function useAudio(state, utils) {
    const {
        isRecording, mediaRecorder, audioContext, analyser, micAnalyser, systemAnalyser,
        audioChunks, recordingTime, recordingInterval, recordingMode, audioBlobURL,
        estimatedFileSize, actualBitrate, recordingNotes, recordingQuality,
        maxRecordingMB, fileSizeWarningShown, sizeCheckInterval, recordingDisclaimer,
        showRecordingDisclaimerModal, pendingRecordingMode, currentView, showUploadModal, showSystemAudioHelp, disableAudioProcessing,
        selectedMicDeviceId, selectedSecondaryDeviceId,
        isDarkMode, wakeLock, animationFrameId,
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
    // Carries resume info ({sessionId, mimeType, startIndex, priorSeconds,
    // priorBytes}) across the disclaimer detour when resuming an existing
    // server session.
    let pendingResumeContext = null;
    // Bytes already uploaded to the server before a resume, so the live size
    // estimate reflects the WHOLE recording, not just the new segment.
    let serverResumePriorBytes = 0;

    // Phase B: server-side chunk streaming (#287 c/d). Feature-flagged via
    // the page-level dataset attribute `data-server-recording-chunks`
    // (rendered by Flask from ENABLE_SERVER_RECORDING_CHUNKS). When the
    // flag is on, every MediaRecorder chunk is also POSTed to the server
    // via createUploader; on Stop+Upload, finalizeSession runs instead of
    // the legacy single-shot upload path.
    //
    // None of this is exposed on the shared state surface yet; the UI to
    // monitor sync backlog lands in Phase C.
    let serverSessionId = null;
    let serverSessionUploader = null;
    let serverSessionMimeType = 'audio/webm';
    let serverSessionLastError = null;

    function _serverRecordingChunksEnabled() {
        const el = document.getElementById('app');
        if (!el || !el.dataset) return false;
        return (el.dataset.serverRecordingChunks || '').toLowerCase() === 'true';
    }

    // MediaRecorder timeslice in ms, from the page dataset (env
    // RECORDING_CHUNK_SECONDS, default 5). Controls chunk emit + upload
    // cadence. Clamped to [1, 60] seconds to match the server.
    function _recordingChunkMs() {
        const el = document.getElementById('app');
        const raw = el && el.dataset ? el.dataset.recordingChunkSeconds : '';
        const secs = parseInt(raw, 10);
        return (Number.isFinite(secs) && secs >= 1 && secs <= 60 ? secs : 5) * 1000;
    }

    function _resetServerSessionState() {
        serverSessionId = null;
        serverSessionUploader = null;
        serverSessionLastError = null;
    }

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
    const startRecording = async (mode = 'microphone', resumeContext = null) => {
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
                    // Open the platform-aware help modal so the user
                    // gets per-OS guidance instead of a bare toast.
                    if (showSystemAudioHelp) showSystemAudioHelp.value = true;
                    showToast('No audio track came through — see the help guide for per-OS setup.', 'fa-exclamation-triangle');
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
            pendingResumeContext = resumeContext;
            return;
        }

        await startRecordingInternal(mode, resumeContext);
    };

    // Accept recording disclaimer and start recording
    const acceptRecordingDisclaimer = async () => {
        showRecordingDisclaimerModal.value = false;
        const resumeContext = pendingResumeContext;
        pendingResumeContext = null;
        await startRecordingInternal(pendingRecordingMode.value || 'microphone', resumeContext);
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
        pendingResumeContext = null;
    };

    // Internal start recording function. resumeContext (optional) continues an
    // existing server session after a page reload: a fresh MediaRecorder keeps
    // POSTing chunks to the same session id, and its header chunk starts a new
    // segment that the server-side assembly concatenates onto the prior audio.
    const startRecordingInternal = async (mode, resumeContext = null) => {
        try {
            recordingMode.value = mode;
            audioChunks.value = [];
            // On resume, continue the on-screen timer and size estimate from
            // where the prior segment left off so both reflect the WHOLE
            // recording, not just the new segment.
            recordingTime.value = (resumeContext && resumeContext.priorSeconds) || 0;
            serverResumePriorBytes = (resumeContext && resumeContext.priorBytes) || 0;
            estimatedFileSize.value = serverResumePriorBytes;
            fileSizeWarningShown.value = false;

            // Initialize IndexedDB session
            currentChunkIndex = 0;

            let stream;
            let combinedStream;

            if (mode === 'microphone') {
                if (!canRecordAudio.value) {
                    throw new Error('Microphone recording is not available. Make sure you are using HTTPS.');
                }
                // When the user is routing system audio in via a
                // monitor source / virtual audio device, the default
                // echoCancellation + noiseSuppression + autoGainControl
                // processing trio aggressively gates the stream to
                // silence after about a second because the algorithm
                // classifies sustained speech/music audio as noise.
                // Flag-controlled via disableAudioProcessing, exposed
                // as a toggle in the upload modal next to the mic
                // button and persisted in localStorage.
                const skipProc = disableAudioProcessing && disableAudioProcessing.value;
                const buildConstraints = (deviceId) => {
                    const c = {
                        echoCancellation: !skipProc,
                        noiseSuppression: !skipProc,
                        autoGainControl: !skipProc,
                        sampleRate: 48000
                    };
                    if (deviceId) c.deviceId = { exact: deviceId };
                    return c;
                };

                const primaryDeviceId = (selectedMicDeviceId && selectedMicDeviceId.value) || '';
                const secondaryDeviceId = (selectedSecondaryDeviceId && selectedSecondaryDeviceId.value) || '';
                const wantsMix = !!secondaryDeviceId && secondaryDeviceId !== primaryDeviceId;

                // Primary stream (the user's mic, or whatever they
                // explicitly picked as the primary input).
                const micStreamA = await navigator.mediaDevices.getUserMedia({
                    audio: buildConstraints(primaryDeviceId)
                });

                // Now that the user has granted mic permission,
                // device labels populate — re-scan for virtual audio
                // routing devices (BlackHole / VB-Cable / monitor
                // sources) so the upload modal can offer them. Also
                // refresh the full input-device list for the picker
                // (post-permission the labels are real names instead
                // of opaque IDs).
                if (utils.refreshVirtualAudioDevices) utils.refreshVirtualAudioDevices();
                if (utils.refreshInputAudioDevices)   utils.refreshInputAudioDevices();

                audioContext.value = new (window.AudioContext || window.webkitAudioContext)();

                if (wantsMix) {
                    // Mix-mode: capture a second getUserMedia stream
                    // from the chosen secondary device, then merge
                    // both into a single MediaStream via Web Audio so
                    // the rest of the pipeline (MediaRecorder, the
                    // visualizer analyser) sees one consolidated
                    // stream. Falls back to single-stream recording
                    // if the secondary capture fails (e.g. the device
                    // disappeared since the picker was populated).
                    let micStreamB;
                    try {
                        micStreamB = await navigator.mediaDevices.getUserMedia({
                            audio: buildConstraints(secondaryDeviceId)
                        });
                    } catch (mixErr) {
                        console.warn('[Recording] Secondary input unavailable, falling back to primary only:', mixErr);
                        if (utils.showToast) utils.showToast(
                            'Secondary input unavailable — recording primary only.',
                            'fa-exclamation-triangle'
                        );
                    }

                    if (micStreamB) {
                        const mixer = audioContext.value.createGain();
                        audioContext.value.createMediaStreamSource(micStreamA).connect(mixer);
                        audioContext.value.createMediaStreamSource(micStreamB).connect(mixer);
                        const dest = audioContext.value.createMediaStreamDestination();
                        mixer.connect(dest);
                        stream = dest.stream;
                        activeStreams.value = [micStreamA, micStreamB];
                        analyser.value = audioContext.value.createAnalyser();
                        analyser.value.fftSize = 256;
                        mixer.connect(analyser.value);
                    } else {
                        stream = micStreamA;
                        activeStreams.value = [micStreamA];
                        const src = audioContext.value.createMediaStreamSource(stream);
                        analyser.value = audioContext.value.createAnalyser();
                        analyser.value.fftSize = 256;
                        src.connect(analyser.value);
                    }
                } else {
                    // Single-stream path (preserved from the original
                    // behaviour). The MediaRecorder consumes micStreamA
                    // directly so there's no needless Web Audio hop.
                    stream = micStreamA;
                    activeStreams.value = [stream];
                    const src = audioContext.value.createMediaStreamSource(stream);
                    analyser.value = audioContext.value.createAnalyser();
                    analyser.value.fftSize = 256;
                    src.connect(analyser.value);
                }

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
                    // Open the platform-aware help modal so the user
                    // sees the per-OS setup instructions instead of
                    // just a generic error toast. The thrown error
                    // still surfaces as a toast so the failure is
                    // acknowledged.
                    if (showSystemAudioHelp) showSystemAudioHelp.value = true;
                    throw new Error(
                        'No audio track came through with the screen share. ' +
                        'Make sure you ticked "Share system audio" / "Share tab audio" ' +
                        'in the share dialog. The help guide that just opened has ' +
                        'platform-specific instructions.'
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
                // Honour the disableAudioProcessing flag here too —
                // see comment at the microphone-only path above.
                const skipProcBoth = disableAudioProcessing && disableAudioProcessing.value;
                const micStream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: !skipProcBoth,
                        noiseSuppression: !skipProcBoth,
                        autoGainControl: !skipProcBoth,
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
                    // Open the platform-aware help modal so the user
                    // sees per-OS setup instead of a generic error.
                    if (showSystemAudioHelp) showSystemAudioHelp.value = true;
                    throw new Error(
                        'No audio track came through with the screen share. ' +
                        'Make sure you ticked "Share system audio" / "Share tab audio" ' +
                        'in the share dialog. The help guide that just opened has ' +
                        'platform-specific instructions.'
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

            // If server-side chunk streaming is enabled (#287 c/d), open the
            // session up front so the very first ondataavailable can post
            // straight to the server. A failure here logs and falls back to
            // local-only recording — the user's audio is never blocked on
            // a network round-trip.
            if (_serverRecordingChunksEnabled()) {
                try {
                    let startIndex = 1;
                    if (resumeContext && resumeContext.sessionId) {
                        // RESUME: reuse the existing session; the new
                        // MediaRecorder's chunks append after what the server
                        // already has (its header chunk opens a new segment).
                        serverSessionId = resumeContext.sessionId;
                        serverSessionMimeType = resumeContext.mimeType || mimeType.split(';')[0];
                        startIndex = (resumeContext.startIndex && resumeContext.startIndex > 1)
                            ? resumeContext.startIndex : 1;
                        console.log('[Recording] Resuming server session', serverSessionId, 'from chunk', startIndex);
                    } else {
                        serverSessionMimeType = mimeType.split(';')[0]; // strip codecs= suffix
                        const session = await ServerSessions.createSession(serverSessionMimeType);
                        serverSessionId = session.session_id;
                        console.log('[Recording] Opened server session', serverSessionId);
                    }
                    serverSessionUploader = ServerSessions.createUploader(serverSessionId, {
                        startIndex,
                        onError: (info) => {
                            serverSessionLastError = info.error;
                            if (info.droppedFromQueue) {
                                console.warn('[Recording] dropped chunk after max retries:', info);
                            }
                        },
                    });
                } catch (e) {
                    serverSessionLastError = e;
                    console.warn('[Recording] Could not open server session; falling back to local-only:', e);
                    _resetServerSessionState();
                }
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

                    // Server-side streaming (Phase B of #287 c/d). The
                    // uploader handles ordering + retries internally; we
                    // fire-and-forget here so MediaRecorder is never
                    // blocked on the network. Failures are surfaced via
                    // serverSessionUploader.lastError().
                    if (serverSessionUploader) {
                        serverSessionUploader.enqueue(event.data);

                        // Storage dedupe (#287 task 5): when the server is the
                        // durable copy and keeping up, keep only a small rolling
                        // IndexedDB buffer instead of every chunk — avoids the
                        // IndexedDB quota blowing out on hours-long recordings.
                        // Guard: only prune when the backlog is smaller than the
                        // window we keep, so a not-yet-uploaded chunk is never
                        // dropped from the local fallback. If the server falls
                        // behind / is unreachable, the backlog grows past the
                        // window and we keep the FULL buffer as the safety net.
                        const ROLLING_KEEP = 5;
                        if (serverSessionUploader.getBacklog() < ROLLING_KEEP) {
                            RecordingDB.pruneOldChunks(ROLLING_KEEP).catch(() => {});
                        }
                    }
                }
            };

            recorder.onstop = () => {
                const blob = new Blob(audioChunks.value, { type: mimeType });
                audioBlobURL.value = URL.createObjectURL(blob);
                stopSizeMonitoring();
            };

            mediaRecorder.value = recorder;
            // Timeslice is configurable (RECORDING_CHUNK_SECONDS, default 5s):
            // smaller = finer crash recovery, larger = less server load.
            recorder.start(_recordingChunkMs());
            isRecording.value = true;
            // Switch to recording view immediately so pending wake-lock/notification awaits don't block Safari rendering
            currentView.value = 'recording';
            // Dismiss the upload modal while the recording view is on
            // screen so the two don't compete for the same surface.
            showUploadModal.value = false;

            // Start timer. Phase C of #287 (c)(d): hours-based hard ceiling
            // replaces the size-based auto-stop for server-streamed
            // recordings. Reads the cap from the page-level dataset
            // attribute so admins can tune it via env var; defaults to 8h
            // to backstop runaway recordings while allowing genuine
            // long-form meetings/lectures.
            const appEl = document.getElementById('app');
            const maxHoursAttr = appEl?.dataset?.recordingMaxHours;
            const recordingMaxSeconds = Math.max(60, parseFloat(maxHoursAttr || '8') * 3600);
            recordingInterval.value = setInterval(() => {
                recordingTime.value++;
                if (recordingTime.value >= recordingMaxSeconds) {
                    stopRecording();
                    showToast(
                        (utils.t && utils.t('toasts.recordingMaxDurationReached'))
                            || `Recording reached the maximum duration (${(recordingMaxSeconds / 3600).toFixed(1)}h) and was stopped automatically.`,
                        'fa-stop-circle',
                        7000
                    );
                }
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

        // Get selected tags as objects and create a DEEP copy to prevent reactivity issues
        const selectedTagsTemp = selectedTagIds.value.map(tagId => {
            const tag = state.availableTags.value.find(t => t.id == tagId);
            return tag || null;
        }).filter(Boolean);

        // Deep clone to completely break reactivity chain - JSON parse/stringify removes all proxies
        const selectedTags = JSON.parse(JSON.stringify(selectedTagsTemp));

        // Server-side streaming path (Phase B of #287 c/d): if the recording
        // was streamed chunk-by-chunk to the server, drain the uploader and
        // call finalize. The backend stitches the chunks via ffmpeg concat
        // demux into the final audio file, then enqueues a transcribe job.
        // The legacy in-memory single-shot path below stays as fallback for
        // recordings that were captured before this feature was enabled.
        if (serverSessionId && serverSessionUploader) {
            try {
                await serverSessionUploader.drain();
                // No title here on purpose: the in-app recorder has no title
                // field, so we must NOT fabricate one. The server resolves the
                // title through the shared resolve_upload_title helper — an
                // absent title becomes a recognised placeholder and the
                // recording gets an AI title, exactly like a drag-drop upload.
                const metadata = {
                    notes: recordingNotes.value || null,
                    folder_id: selectedFolderId.value || null,
                    tags: selectedTags,
                    language: asrLanguage.value || null,
                    min_speakers: asrMinSpeakers.value || null,
                    max_speakers: asrMaxSpeakers.value || null,
                };
                const result = await ServerSessions.finalizeSession(serverSessionId, metadata);
                showToast?.((utils.t && utils.t('toasts.recordingFinalized')) || 'Recording uploaded for processing', 'fa-cloud-upload-alt');

                // Tear down local state the same way the legacy path does.
                if (audioBlobURL.value) URL.revokeObjectURL(audioBlobURL.value);
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
                try { await RecordingDB.clearRecordingSession(); } catch (_) { /* ignore */ }
                _resetServerSessionState();
                // The recording is already finalized server-side — there is
                // nothing left to "finish" in the upload modal. Drop back to
                // the main view and refresh the sidebar + processing-queue
                // panel so the queued recording appears immediately, the same
                // way a drag-drop upload does (instead of only after a manual
                // page refresh).
                currentView.value = null;
                showUploadModal.value = false;
                try { await utils.onServerRecordingQueued?.(); } catch (_) { /* non-fatal */ }
                return result;
            } catch (e) {
                console.error('[Recording] finalize failed; falling back to single-shot upload:', e);
                setGlobalError((utils.t && utils.t('errors.recordingFinalizeFallback')) || `Server-side stitch failed (${e.message}); uploading as a single file instead.`);
                // Fall through to the legacy upload path below.
            }
        }

        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        const _rawRecordedFile = new File(audioChunks.value, `recording-${timestamp}.webm`, { type: 'audio/webm' });
        // Prevent Vue from wrapping the binary File in a reactive proxy. See
        // upload.js for rationale (issue #280).
        const recordedFile = (typeof Vue !== 'undefined' && Vue.markRaw)
            ? Vue.markRaw(_rawRecordedFile)
            : _rawRecordedFile;

        // Add to upload queue. The recording session in IndexedDB is
        // intentionally NOT cleared here (issue #287(b)). It is the user's
        // crash-recovery copy; we only clear it once the upload has reached
        // the server successfully. The clientId on the queue item is what the
        // upload-success handler uses to find and clear the matching session.
        const queueClientId = `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
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
            clientId: queueClientId,
            fromInProgressRecording: true,  // marker: upload-success handler clears RecordingDB session
            error: null,
            willAutoSummarize: false // Server will tell us via SUMMARIZING status
        });

        // Release the in-memory audio resources so the recording view does not
        // keep showing the old waveform, but DO NOT clear the IndexedDB session
        // (that happens only on upload success — see upload.js).
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

        // Return to upload modal so the user can finish the upload form.
        currentView.value = null;
        showUploadModal.value = true;

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
        const _rawRecordedFile = new File(audioChunks.value, `recording-${timestamp}.webm`, { type: 'audio/webm' });
        // Prevent Vue from wrapping the binary File in a reactive proxy. See
        // upload.js for rationale (issue #280).
        const recordedFile = (typeof Vue !== 'undefined' && Vue.markRaw)
            ? Vue.markRaw(_rawRecordedFile)
            : _rawRecordedFile;

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

            // Clear recording state (must await so currentView='upload' completes
            // before we override it with 'detail', otherwise the deferred
            // currentView='upload' fires after 'detail' and the view watcher
            // clears incognito data thinking we navigated away)
            await discardRecording();

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

        // If a server-side session was open (Phase B of #287 c/d), abort it
        // so the chunks on disk are reaped immediately rather than waiting
        // for the cleanup sweep.
        if (serverSessionId) {
            try {
                await ServerSessions.abortSession(serverSessionId);
            } catch (e) {
                console.warn('[Recording] Could not abort server session during discard:', e);
            }
            _resetServerSessionState();
        }

        // Clear IndexedDB session
        try {
            await RecordingDB.clearRecordingSession();
        } catch (dbError) {
            console.warn('[Recording] Failed to clear IndexedDB session:', dbError);
        }

        await releaseWakeLock();
        await hideRecordingNotification();

        // Return to upload modal so the user can finish the upload form.
        currentView.value = null;
        showUploadModal.value = true;
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

        // Include bytes already uploaded before a resume so the estimate (and
        // the derived bitrate) reflect the whole recording, not just the new
        // segment. serverResumePriorBytes is 0 for a fresh recording.
        const totalSize = serverResumePriorBytes
            + audioChunks.value.reduce((sum, chunk) => sum + chunk.size, 0);
        estimatedFileSize.value = totalSize;

        if (recordingTime.value > 0) {
            actualBitrate.value = (totalSize * 8) / recordingTime.value;
        }

        // Phase C of #287 (c)(d): the 200 MB cap used to be a hard auto-stop
        // because the entire blob was held in browser RAM and would crash
        // the tab past a certain size. When server-side chunk streaming is
        // active that constraint goes away — chunks flush to the server as
        // they are produced. We still surface a soft warning at the same
        // threshold so users know they are recording a large file, but the
        // hard auto-stop is replaced by an absolute hours-based ceiling
        // (`RECORDING_MAX_HOURS`, default 8) so a runaway recording from a
        // misclick still has a backstop.
        const sizeMB = totalSize / (1024 * 1024);
        const warningThresholdMB = maxRecordingMB.value * 0.8;

        if (sizeMB > warningThresholdMB && !fileSizeWarningShown.value) {
            fileSizeWarningShown.value = true;
            showToast(
                (utils.t && utils.t('toasts.recordingSizeSoftWarning', { size: formatFileSize(totalSize) }))
                    || `Recording is ${formatFileSize(totalSize)}. Consider stopping when convenient.`,
                'fa-exclamation-triangle',
                5000
            );
        }

        // Server-streaming path: no client-side hard size cap. The absolute
        // ceiling is hours-based and lives in the recording-time tick below.
        if (serverSessionUploader) {
            return;
        }

        // Legacy single-shot path: keep the hard auto-stop at the configured
        // size so the in-memory blob does not run the tab out of RAM.
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
