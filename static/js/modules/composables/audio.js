/**
 * Audio recording composable
 * Handles microphone/system audio recording with visualizers and wake lock
 */

import * as RecordingDB from '../db/recording-persistence.js';
import * as IncognitoStorage from '../db/incognito-storage.js';
import * as ServerSessions from '../db/server-recording-sessions.js';
import {
    findExternalAudioInputCandidates,
    getAudioInputCandidateSignature,
    shouldAutoOfferExternalAudioInput
} from '../utils/platform.js';

export function buildMicrophoneConstraints(deviceId = '', disableAudioProcessing = false) {
    const constraints = {
        echoCancellation: !disableAudioProcessing,
        noiseSuppression: !disableAudioProcessing,
        autoGainControl: !disableAudioProcessing,
        sampleRate: 48000
    };
    if (deviceId) constraints.deviceId = { exact: deviceId };
    return constraints;
}

export function isUnavailableAudioInputError(error) {
    return error?.name === 'NotFoundError' || error?.name === 'OverconstrainedError';
}

export async function acquireMicrophoneStream(mediaDevices, options = {}) {
    const deviceId = options.deviceId || '';
    const disableAudioProcessing = !!options.disableAudioProcessing;

    try {
        const stream = await mediaDevices.getUserMedia({
            audio: buildMicrophoneConstraints(deviceId, disableAudioProcessing)
        });
        return { stream, requestedDeviceId: deviceId, usedFallback: false, originalError: null };
    } catch (error) {
        if (!deviceId || !isUnavailableAudioInputError(error)) throw error;

        const stream = await mediaDevices.getUserMedia({
            audio: buildMicrophoneConstraints('', disableAudioProcessing)
        });
        return { stream, requestedDeviceId: deviceId, usedFallback: true, originalError: error };
    }
}

export function useAudio(state, utils) {
    const {
        isRecording, isPaused, mediaRecorder, audioContext, analyser, micAnalyser, systemAnalyser,
        audioChunks, recordingTime, recordingInterval, recordingMode, audioBlobURL,
        estimatedFileSize, actualBitrate, recordingNotes, recordingQuality,
        maxRecordingMB, fileSizeWarningShown, sizeCheckInterval, isServerStreamedRecording, isFinalizingRecording, recordingDisclaimer,
        showRecordingDisclaimerModal, pendingRecordingMode, currentView, showUploadModal, showSystemAudioHelp, disableAudioProcessing,
        recordSystemVideo, recordingVideoActive, videoRetentionEnabled,
        inputAudioDevices, selectedMicDeviceId, selectedSecondaryDeviceId,
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

    const isPreparingRecording = Vue.ref(false);
    const pendingMicrophoneSelection = Vue.ref(null);
    let pendingMicrophoneSelectionResolver = null;
    let dismissedMicrophoneCandidateSignature = '';
    const dismissedCandidateStorageKey = 'dismissedExternalMicCandidateSignature';

    try {
        dismissedMicrophoneCandidateSignature = sessionStorage.getItem(dismissedCandidateStorageKey) || '';
    } catch (_) { /* sessionStorage may be unavailable in private contexts */ }

    const rememberDismissedMicrophoneCandidates = (signature) => {
        dismissedMicrophoneCandidateSignature = signature || '';
        try {
            sessionStorage.setItem(dismissedCandidateStorageKey, dismissedMicrophoneCandidateSignature);
        } catch (_) { /* keep the in-memory fallback */ }
    };

    const waitForMicrophoneSelection = (candidates, activeDeviceId) => new Promise((resolve) => {
        const signature = getAudioInputCandidateSignature(candidates);
        pendingMicrophoneSelection.value = {
            candidates,
            activeDeviceId: activeDeviceId || '',
            candidateSignature: signature
        };
        pendingMicrophoneSelectionResolver = resolve;
    });

    const resolveMicrophoneSelection = (result) => {
        const resolver = pendingMicrophoneSelectionResolver;
        pendingMicrophoneSelectionResolver = null;
        pendingMicrophoneSelection.value = null;
        if (resolver) resolver(result);
    };

    const confirmPendingMicrophoneSelection = () => {
        const deviceId = (selectedMicDeviceId && selectedMicDeviceId.value) || '';
        const pending = pendingMicrophoneSelection.value;
        if (!deviceId && pending?.candidateSignature) {
            rememberDismissedMicrophoneCandidates(pending.candidateSignature);
        }
        resolveMicrophoneSelection(deviceId
            ? { action: 'select', deviceId }
            : { action: 'continue', deviceId: '' });
    };

    const cancelPendingMicrophoneSelection = () => {
        resolveMicrophoneSelection({ action: 'cancel', deviceId: '' });
    };

    if (typeof Vue !== 'undefined' && Vue.watch && showUploadModal) {
        Vue.watch(showUploadModal, (visible) => {
            if (!visible && pendingMicrophoneSelection.value) {
                cancelPendingMicrophoneSelection();
            }
        });
    }

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
    // Mime type of the recording currently in memory (set at start, restored
    // from IndexedDB metadata on crash recovery). The legacy single-shot
    // upload path uses it to build the File with the right container type and
    // extension — a video capture must not be uploaded as `audio/webm`.
    let currentRecordingMimeType = 'audio/webm';

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
        if (isServerStreamedRecording) isServerStreamedRecording.value = false;
    }

    // Pick the best MediaRecorder container for the capture. Video captures
    // prefer WebM (VP9 then VP8, both with Opus) because Chromium — the only
    // engine that can share tab/system audio — muxes it natively; video/mp4
    // is the Safari-family fallback. Audio captures keep the existing
    // audio/webm;codecs=opus preference.
    function _pickRecorderMime(wantsVideo) {
        const candidates = wantsVideo
            ? ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4']
            : ['audio/webm;codecs=opus', 'audio/webm'];
        for (const c of candidates) {
            if (MediaRecorder.isTypeSupported(c)) return c;
        }
        return wantsVideo ? null : 'audio/webm';
    }

    // Video bitrate cap for tab/screen captures, from the page dataset
    // (RECORDING_VIDEO_KBPS, default 2500). Screen content compresses well;
    // 2.5 Mbps keeps an hour of capture near 1 GB instead of browser-default
    // rates that can triple that.
    function _videoBitsPerSecond() {
        const el = document.getElementById('app');
        const raw = el && el.dataset ? el.dataset.recordingVideoKbps : '';
        const kbps = parseInt(raw, 10);
        return (Number.isFinite(kbps) && kbps >= 100 && kbps <= 50000 ? kbps : 2500) * 1000;
    }

    // Attach the live display stream to the recording view's preview element.
    // The <video> renders after currentView flips to 'recording', so retry
    // briefly until it exists.
    function _attachVideoPreview(stream, attempt = 0) {
        const el = document.getElementById('recordingVideoPreview');
        if (el) {
            el.srcObject = stream;
            el.play().catch(() => {});
        } else if (attempt < 20) {
            setTimeout(() => _attachVideoPreview(stream, attempt + 1), 100);
        }
    }

    // Fix duration/seeking on the finished-recording review players.
    //
    // A raw MediaRecorder blob is a "live" container: no duration metadata
    // and no seek index, so <audio>/<video> report duration=Infinity — the
    // length doesn't display and the seek bar is dead. (The server-side
    // stitch fixes this for the STORED file with an ffmpeg remux pass; this
    // is the client-side equivalent for the pre-upload review.) The standard
    // fix: seek far past the end, which forces the browser to scan the
    // stream, compute the real duration, and build its seek index; then snap
    // back to the start. Wired to @loadedmetadata on both review players.
    const normalizeLiveMediaDuration = (event) => {
        const el = event && event.target;
        if (!el || Number.isFinite(el.duration)) return;
        const restore = () => {
            el.removeEventListener('timeupdate', restore);
            el.removeEventListener('seeked', restore);
            if (el.currentTime > 0) el.currentTime = 0;
        };
        el.addEventListener('timeupdate', restore);
        el.addEventListener('seeked', restore);
        try {
            el.currentTime = Number.MAX_SAFE_INTEGER;
        } catch (_) {
            restore();
        }
    };

    // Detach the live stream from the preview element (called on stop — the
    // tracks are ended at that point). recordingVideoActive stays true so the
    // finished-recording review pane knows to render a <video> player for the
    // blob instead of the audio bar; it resets on discard/upload.
    function _detachVideoPreview() {
        const el = document.getElementById('recordingVideoPreview');
        if (el) el.srcObject = null;
    }

    function _clearVideoPreview() {
        _detachVideoPreview();
        if (recordingVideoActive) recordingVideoActive.value = false;
    }

    // Close the recording AudioContext and drop the analyser nodes. Browsers
    // cap concurrent AudioContexts (~6 in Chromium); without this, a handful
    // of record→stop cycles without a page reload exhausts the pool and
    // `new AudioContext()` throws, killing recording until reload. Called on
    // stop, discard, and the start-error path.
    function _releaseAudioContext() {
        if (audioContext.value) {
            try { audioContext.value.close(); } catch (_) { /* already closed */ }
            audioContext.value = null;
        }
        analyser.value = null;
        if (micAnalyser) micAnalyser.value = null;
        if (systemAnalyser) systemAnalyser.value = null;
    }

    // Stop any tracks still held on the pre-disclosure display stream. The
    // display capture is acquired up front (Firefox transient-activation
    // requirement) and parked in pendingDisplayStream; if start then fails
    // before it's consumed, the browser's "sharing your screen" indicator
    // would otherwise stay on with the tracks never released.
    function _stopPendingDisplayStream() {
        if (pendingDisplayStream) {
            try { pendingDisplayStream.getTracks().forEach(t => t.stop()); } catch (_) {}
            pendingDisplayStream = null;
        }
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

    const refreshAudioDeviceLists = async () => {
        if (utils.refreshVirtualAudioDevices) utils.refreshVirtualAudioDevices();
        if (utils.refreshInputAudioDevices) return await utils.refreshInputAudioDevices();
        return (inputAudioDevices && inputAudioDevices.value) || [];
    };

    const showSelectedMicrophoneUnavailable = () => {
        const message = (utils.t && utils.t('recording.selectedMicUnavailable'))
            || 'The selected microphone is no longer available. Using the system default.';
        showToast(message, 'fa-exclamation-triangle');
    };

    const requestMicrophonePermission = async () => {
        if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
            showToast(
                (utils.t && utils.t('recording.micPermissionUnavailable'))
                    || 'Microphone access is not available. Make sure you are using HTTPS.',
                'error'
            );
            return;
        }

        let permissionStream = null;
        try {
            permissionStream = await navigator.mediaDevices.getUserMedia({ audio: true });
            await refreshAudioDeviceLists();
        } catch (error) {
            const denied = error?.name === 'NotAllowedError' || error?.name === 'SecurityError';
            showToast(
                (utils.t && utils.t(denied ? 'recording.microphonePermissionDenied' : 'recording.micPermissionFailed'))
                    || (denied
                        ? 'Microphone permission was denied. Allow access in your browser settings and try again.'
                        : 'Could not access the microphone. Check that it is connected and not in use.'),
                'error'
            );
        } finally {
            if (permissionStream) permissionStream.getTracks().forEach(track => track.stop());
        }
    };

    // Start recording
    // IMPORTANT: For Firefox, getDisplayMedia MUST be the first async call from user gesture
    const startRecording = async (mode = 'microphone', resumeContext = null) => {
        if (isPreparingRecording.value || isRecording.value) return;
        isPreparingRecording.value = true;

        try {
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
        } finally {
            isPreparingRecording.value = false;
        }
    };

    // Accept recording disclaimer and start recording
    const acceptRecordingDisclaimer = async () => {
        if (isPreparingRecording.value || isRecording.value) return;
        isPreparingRecording.value = true;
        showRecordingDisclaimerModal.value = false;
        const resumeContext = pendingResumeContext;
        pendingResumeContext = null;
        try {
            await startRecordingInternal(pendingRecordingMode.value || 'microphone', resumeContext);
        } finally {
            isPreparingRecording.value = false;
        }
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
            recordingVideoActive.value = false;
            if (isPaused) isPaused.value = false;
            audioChunks.value = [];
            // On resume, continue the on-screen timer and size estimate from
            // where the prior segment left off so both reflect the WHOLE
            // recording, not just the new segment.
            recordingTime.value = (resumeContext && resumeContext.priorSeconds) || 0;
            serverResumePriorBytes = (resumeContext && resumeContext.priorBytes) || 0;
            estimatedFileSize.value = serverResumePriorBytes;
            fileSizeWarningShown.value = false;

            // A resumed recording already has chunks on the server, so it
            // cannot become incognito: processing it in incognito would send
            // only the locally-held new segment (silent partial audio) and
            // discard the prior one. Force the toggle off — relevant when
            // INCOGNITO_MODE_DEFAULT is on or the user flipped it earlier.
            if (resumeContext && resumeContext.sessionId && incognitoMode && incognitoMode.value) {
                incognitoMode.value = false;
                showToast(
                    (utils.t && utils.t('toasts.resumedRecordingNotIncognito'))
                        || 'Incognito was turned off: this resumed recording was already streaming to the server.',
                    'fa-user-secret',
                    7000
                );
            }

            // Initialize IndexedDB session
            currentChunkIndex = 0;

            let stream;
            let combinedStream;
            // Opt-in tab/window/screen video capture (#303): only meaningful
            // for the display-capture modes, and only when the server keeps
            // video streams (VIDEO_RETENTION) — otherwise the pipeline would
            // strip the video right back out at transcription time.
            const wantsVideo = (mode === 'system' || mode === 'both')
                && !!(recordSystemVideo && recordSystemVideo.value)
                && !!(videoRetentionEnabled && videoRetentionEnabled.value);
            // The display video track we keep for the recorder (null when the
            // user did not opt in, or no video mime is supported).
            let capturedVideoTrack = null;

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
                let primaryDeviceId = (selectedMicDeviceId && selectedMicDeviceId.value) || '';

                // Acquire first, then enumerate. Browsers reveal useful
                // device labels only after the normal microphone permission
                // grant initiated by the user's click.
                let primaryResult = await acquireMicrophoneStream(navigator.mediaDevices, {
                    deviceId: primaryDeviceId,
                    disableAudioProcessing: skipProc
                });
                let micStreamA = primaryResult.stream;
                activeStreams.value = [micStreamA];

                if (primaryResult.usedFallback) {
                    primaryDeviceId = '';
                    if (selectedMicDeviceId) selectedMicDeviceId.value = '';
                    showSelectedMicrophoneUnavailable();
                }

                const refreshedInputs = await refreshAudioDeviceLists();
                const activeDeviceId = micStreamA.getAudioTracks()[0]?.getSettings?.().deviceId || '';

                // Mobile browsers generally lack the desktop browser's own
                // microphone chooser. Only offer Speakr's inline fallback on
                // mobile, when no explicit selection is active and the browser
                // exposes a confidently separate physical input. Desktop keeps
                // its native chooser without a second automatic selection step.
                // Never offer during a resume: a continued session must keep
                // its original input, and the prompt UI lives inside the
                // upload modal, which a crash-recovery resume may not have
                // open — awaiting the selection there would hang forever
                // with the record buttons stuck disabled.
                if (!primaryDeviceId && !resumeContext && shouldAutoOfferExternalAudioInput()) {
                    const candidates = findExternalAudioInputCandidates(refreshedInputs, activeDeviceId);
                    const candidateSignature = getAudioInputCandidateSignature(candidates);
                    if (candidates.length > 0
                        && candidateSignature
                        && candidateSignature !== dismissedMicrophoneCandidateSignature) {
                        const selection = await waitForMicrophoneSelection(candidates, activeDeviceId);
                        if (selection.action === 'cancel') {
                            micStreamA.getTracks().forEach(track => track.stop());
                            activeStreams.value = [];
                            return;
                        }

                        if (selection.action === 'select' && selection.deviceId) {
                            primaryDeviceId = selection.deviceId;
                            if (primaryDeviceId !== activeDeviceId) {
                                micStreamA.getTracks().forEach(track => track.stop());
                                activeStreams.value = [];
                                primaryResult = await acquireMicrophoneStream(navigator.mediaDevices, {
                                    deviceId: primaryDeviceId,
                                    disableAudioProcessing: skipProc
                                });
                                micStreamA = primaryResult.stream;
                                activeStreams.value = [micStreamA];
                                if (primaryResult.usedFallback) {
                                    primaryDeviceId = '';
                                    if (selectedMicDeviceId) selectedMicDeviceId.value = '';
                                    showSelectedMicrophoneUnavailable();
                                }
                            }
                        }
                    }
                }

                const secondaryDeviceId = (selectedSecondaryDeviceId && selectedSecondaryDeviceId.value) || '';
                const wantsMix = !!secondaryDeviceId && secondaryDeviceId !== primaryDeviceId;
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
                            audio: buildMicrophoneConstraints(secondaryDeviceId, skipProc)
                        });
                    } catch (mixErr) {
                        console.warn('[Recording] Secondary input unavailable, falling back to primary only:', mixErr);
                        if (isUnavailableAudioInputError(mixErr) && selectedSecondaryDeviceId) {
                            selectedSecondaryDeviceId.value = '';
                            refreshAudioDeviceLists();
                        }
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

                // Keep the display video track when the user opted into video
                // capture; otherwise stop it immediately as before.
                if (wantsVideo && stream.getVideoTracks().length > 0) {
                    capturedVideoTrack = stream.getVideoTracks()[0];
                    stream.getVideoTracks().slice(1).forEach(track => track.stop());
                } else {
                    stream.getVideoTracks().forEach(track => track.stop());
                }
                const audioOnlyStream = new MediaStream([audioTrack]);
                stream = capturedVideoTrack
                    ? new MediaStream([capturedVideoTrack, audioTrack])
                    : audioOnlyStream;
                activeStreams.value = [stream];

                audioContext.value = new (window.AudioContext || window.webkitAudioContext)();
                const source = audioContext.value.createMediaStreamSource(audioOnlyStream);
                analyser.value = audioContext.value.createAnalyser();
                analyser.value.fftSize = 256;
                source.connect(analyser.value);

            } else if (mode === 'both') {
                if (!canRecordAudio.value || !canRecordSystemAudio.value) {
                    throw new Error('Recording is not available. Make sure you are using HTTPS.');
                }
                // Honour the chosen primary input and the processing flag
                // here too. Display capture was already acquired first in
                // startRecording(), preserving Firefox's transient activation.
                const skipProcBoth = disableAudioProcessing && disableAudioProcessing.value;
                const selectedDeviceId = (selectedMicDeviceId && selectedMicDeviceId.value) || '';
                const micResult = await acquireMicrophoneStream(navigator.mediaDevices, {
                    deviceId: selectedDeviceId,
                    disableAudioProcessing: skipProcBoth
                });
                const micStream = micResult.stream;
                activeStreams.value = [micStream];
                if (micResult.usedFallback) {
                    if (selectedMicDeviceId) selectedMicDeviceId.value = '';
                    showSelectedMicrophoneUnavailable();
                }
                await refreshAudioDeviceLists();

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

                // Keep the display video track when the user opted into video
                // capture; otherwise stop the video tracks as before.
                if (wantsVideo && displayStream.getVideoTracks().length > 0) {
                    capturedVideoTrack = displayStream.getVideoTracks()[0];
                    displayStream.getVideoTracks().slice(1).forEach(track => track.stop());
                } else {
                    displayStream.getVideoTracks().forEach(track => track.stop());
                }

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
                // With video capture on, the recorder consumes the display
                // video track plus the Web-Audio mixed mic+system track.
                stream = capturedVideoTrack
                    ? new MediaStream([capturedVideoTrack, ...combinedStream.getAudioTracks()])
                    : combinedStream;
            }

            // Determine best mime type. A video capture needs a video
            // container; if the browser can't mux one, fall back to
            // audio-only rather than failing the recording.
            let mimeType = _pickRecorderMime(!!capturedVideoTrack);
            if (capturedVideoTrack && !mimeType) {
                console.warn('[Recording] No supported video container; falling back to audio-only');
                showToast(
                    (utils.t && utils.t('toasts.videoRecordingUnsupported'))
                        || 'This browser cannot record video — capturing audio only.',
                    'fa-exclamation-triangle'
                );
                capturedVideoTrack.stop();
                capturedVideoTrack = null;
                stream = new MediaStream(stream.getAudioTracks());
                mimeType = _pickRecorderMime(false);
            }
            currentRecordingMimeType = mimeType;

            const recorderOptions = { mimeType };
            if (capturedVideoTrack) {
                recorderOptions.videoBitsPerSecond = _videoBitsPerSecond();
            }
            const recorder = new MediaRecorder(stream, recorderOptions);

            // Live preview of the captured surface in the recording view.
            if (capturedVideoTrack) {
                recordingVideoActive.value = true;
                _attachVideoPreview(new MediaStream([capturedVideoTrack]));
            }

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
                    mimeType,
                    incognito: !!(incognitoMode && incognitoMode.value)
                });
            } catch (dbError) {
                console.warn('[Recording] IndexedDB persistence failed, continuing without persistence:', dbError);
            }

            // If server-side chunk streaming is enabled (#287 c/d), open the
            // session up front so the very first ondataavailable can post
            // straight to the server. A failure here logs and falls back to
            // local-only recording — the user's audio is never blocked on
            // a network round-trip.
            //
            // Incognito recordings never open a session: the whole point of
            // incognito is that audio does not touch server storage until the
            // explicit process-without-saving upload, so they stay on the
            // in-browser path (RAM + IndexedDB, 200 MB cap) regardless of the
            // streaming flag. The resume guard above already cleared the
            // toggle for resumed server sessions, so this cannot strand a
            // half-uploaded session.
            if (_serverRecordingChunksEnabled() && !(incognitoMode && incognitoMode.value)) {
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
                    // Mirror the streaming state into a ref so the recording
                    // view can hide the size-limit warning UI (#332), which
                    // only applies to the in-RAM legacy path.
                    if (isServerStreamedRecording) isServerStreamedRecording.value = true;
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
            // Warn once at 80% of the ceiling (matching the size warning's
            // threshold convention) so a long recording never just cuts off
            // by surprise. `>=` plus a flag, not `===`: a recovered/resumed
            // recording restores recordingTime past the mark in one jump and
            // then gets the warning on its first tick.
            let durationWarningShown = false;
            recordingInterval.value = setInterval(() => {
                // Freeze the elapsed-time counter while paused (#338). The
                // interval keeps running so the max-duration closure survives a
                // pause/resume; it just skips ticking.
                if (isPaused && isPaused.value) return;
                recordingTime.value++;
                if (!durationWarningShown && recordingTime.value >= recordingMaxSeconds * 0.8) {
                    durationWarningShown = true;
                    const minutesLeft = Math.max(1, Math.round((recordingMaxSeconds - recordingTime.value) / 60));
                    showToast(
                        (utils.t && utils.t('toasts.recordingMaxDurationApproaching', { minutes: minutesLeft }))
                            || `Recording will stop automatically in about ${minutesLeft} minutes (maximum duration reached).`,
                        'fa-exclamation-triangle',
                        7000
                    );
                }
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
            // Also release the display stream grabbed before the failure point
            // (e.g. 'both' mode where the mic getUserMedia rejects after the
            // screen share was already acquired) and the AudioContext.
            _stopPendingDisplayStream();
            _clearVideoPreview();
            _releaseAudioContext();
        }
    };

    // Stop recording
    // Pause an in-progress recording. MediaRecorder.pause() freezes the media
    // timeline, so the audio resumes contiguously with no silent gap — meeting
    // breaks are simply excluded from the recording (#338). Works on every
    // capture path (mic/system/both, legacy in-RAM and server-streamed) since
    // it operates purely at the MediaRecorder level.
    const pauseRecording = () => {
        const recorder = mediaRecorder.value;
        if (!recorder || !isRecording.value || (isPaused && isPaused.value)) return;
        if (recorder.state !== 'recording') return;
        try {
            recorder.pause();
        } catch (e) {
            console.warn('[Recording] pause() failed:', e);
            return;
        }
        if (isPaused) isPaused.value = true;
        // The capture tracks stay live while paused, so freeze the level meter —
        // otherwise it would keep animating to incoming (unrecorded) audio.
        if (animationFrameId.value) {
            cancelAnimationFrame(animationFrameId.value);
            animationFrameId.value = null;
        }
        showToast(
            (utils.t && utils.t('toasts.recordingPaused')) || 'Recording paused',
            'fa-circle-pause',
            3000
        );
    };

    // Resume a paused recording: MediaRecorder continues the same stream, so the
    // assembled file stays a single valid container (no new header).
    const resumeRecording = () => {
        const recorder = mediaRecorder.value;
        if (!recorder || !isRecording.value || !(isPaused && isPaused.value)) return;
        if (recorder.state !== 'paused') return;
        try {
            recorder.resume();
        } catch (e) {
            console.warn('[Recording] resume() failed:', e);
            return;
        }
        if (isPaused) isPaused.value = false;
        drawVisualizers();  // restart the level meter
        showToast(
            (utils.t && utils.t('toasts.recordingResumed')) || 'Recording resumed',
            'fa-circle-play',
            3000
        );
    };

    // Stop recording
    const stopRecording = async () => {
        if (mediaRecorder.value && isRecording.value) {
            mediaRecorder.value.stop();
            isRecording.value = false;
            if (isPaused) isPaused.value = false;
            _detachVideoPreview();

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

            // Close the AudioContext so repeated record→stop cycles don't
            // exhaust the browser's context pool.
            _releaseAudioContext();

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

    // Upload recorded audio (inner implementation — call uploadRecordedAudio,
    // which adds the re-entrancy guard and button state around this).
    const _uploadRecordedAudioInner = async (opts = {}) => {
        if (!audioBlobURL.value) {
            setGlobalError("No recorded audio to upload.");
            return;
        }
        // Optional merge intent: when present, the server routes the stitched
        // clip straight into a merge (no standalone transcription). See
        // finalizeRecordingMerge / mergeRecordedWithExisting.
        const mergeIntent = opts.mergeIntent || null;

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
                if (mergeIntent) {
                    metadata.merge_intent = mergeIntent;
                }
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
                _clearVideoPreview();
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
        const recordedMime = (currentRecordingMimeType || 'audio/webm').split(';')[0];
        const recordedExt = recordedMime === 'video/mp4' ? 'mp4' : 'webm';
        const _rawRecordedFile = new File(audioChunks.value, `recording-${timestamp}.${recordedExt}`, { type: recordedMime });
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
        _clearVideoPreview();
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

    // Public entry point: re-entrancy guard + button state around the inner
    // upload. Draining the chunk backlog before finalize can take many
    // seconds with no other visible change, so users double-click — and
    // every extra call used to send another finalize for the same session,
    // minting duplicate recordings. The server is idempotent about replayed
    // finalizes now, but the first line of defense is not sending them:
    // ignore clicks while one upload is in flight and let the template
    // disable the buttons via isFinalizingRecording.
    const uploadRecordedAudio = async (opts = {}) => {
        if (isFinalizingRecording && isFinalizingRecording.value) {
            console.log('[Recording] Upload already in progress; ignoring duplicate request');
            return;
        }
        if (isFinalizingRecording) isFinalizingRecording.value = true;
        try {
            return await _uploadRecordedAudioInner(opts);
        } finally {
            if (isFinalizingRecording) isFinalizingRecording.value = false;
        }
    };

    // Split-button action: open the merge modal in "recording" mode BEFORE
    // finalizing, so the user picks which existing recording(s) to merge this
    // clip into and in what order. On confirm the modal calls
    // finalizeRecordingMerge, which finalizes the session carrying that intent —
    // the server stitches the clip and routes it straight into the merge, so the
    // clip is never transcribed on its own (only the combined recording is).
    const mergeRecordedWithExisting = async () => {
        const t = (k, p) => (utils.t ? utils.t(k, p) : k);
        if (!audioBlobURL.value) {
            setGlobalError(t('mergeRecordings.noClip') || 'No recorded audio to merge.');
            return;
        }
        if (!serverSessionId) {
            // Legacy single-shot recordings have no server session to attach the
            // intent to. Tell the user to upload and merge from the list.
            showToast?.(t('mergeRecordings.uploadedMergeManually'), 'fa-info-circle', 6000);
            return;
        }
        if (utils.openMergeForRecording) {
            // Pass the clip's recording-view notes so it can be offered as a
            // notes source in the merge modal.
            utils.openMergeForRecording({ clipNotes: recordingNotes.value || '' });
        }
    };

    // Called by the merge modal (recording mode) on confirm: finalize the
    // recording session with the merge intent. `orderedSpec` is the ordered
    // source list with the string '__self__' marking this clip's position.
    const finalizeRecordingMerge = async (orderedSpec, { deleteOriginals = true, title = undefined, notesSource = undefined } = {}) => {
        const mergeIntent = {
            order: orderedSpec,
            delete_originals: !!deleteOriginals,
            title: title || undefined,
        };
        // notesSource: '__self__' | <id> | null. Only include the key when the
        // caller specified one, so its absence means "use the backend default".
        if (notesSource !== undefined) {
            mergeIntent.notes_source = notesSource;
        }
        return uploadRecordedAudio({ mergeIntent });
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

        // The recording may have streamed to a server session before the user
        // chose incognito in the review pane (incognito recordings never OPEN
        // a session, but the toggle can be flipped after a normal recording
        // finishes). Honor the choice: delete the server-side chunks up front,
        // before processing, rather than only after success via
        // discardRecording. Refuse outright if part of the audio exists ONLY
        // on the server (resumed session) — incognito-processing just the
        // local segment would silently truncate the recording. That state
        // should be unreachable (the resume path clears the toggle), so this
        // is defense-in-depth.
        if (serverSessionId) {
            if (serverResumePriorBytes > 0) {
                setGlobalError('This resumed recording cannot be processed in incognito because its earlier audio exists only on the server. Use the normal upload instead.');
                return;
            }
            try {
                await ServerSessions.abortSession(serverSessionId);
            } catch (e) {
                console.warn('[Incognito] Could not abort server session before incognito processing:', e);
            }
            _resetServerSessionState();
        }

        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        const recordedMime = (currentRecordingMimeType || 'audio/webm').split(';')[0];
        const recordedExt = recordedMime === 'video/mp4' ? 'mp4' : 'webm';
        const _rawRecordedFile = new File(audioChunks.value, `recording-${timestamp}.${recordedExt}`, { type: recordedMime });
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
            // discardRecording() re-opened the upload modal (its normal
            // return-to-form behaviour); close it here so it doesn't cover the
            // finished incognito recording. Every other incognito path does this.
            if (showUploadModal) showUploadModal.value = false;

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
            // Reset the shared processing-progress state so it isn't left
            // pinned at 100% / "ready" for the next operation.
            processingProgress.value = 0;
            processingMessage.value = '';
        }
    };

    // Discard recording
    const discardRecording = async () => {
        _clearVideoPreview();
        _stopPendingDisplayStream();
        _releaseAudioContext();
        // Stop any still-live capture tracks (e.g. discarding while the tab
        // stayed on the finished-review pane with tracks not yet stopped).
        if (activeStreams.value.length > 0) {
            activeStreams.value.forEach(stream => {
                try { stream.getTracks().forEach(track => track.stop()); } catch (_) {}
            });
            activeStreams.value = [];
        }
        if (audioBlobURL.value) {
            URL.revokeObjectURL(audioBlobURL.value);
        }
        audioBlobURL.value = null;
        audioChunks.value = [];
        isRecording.value = false;
        if (isPaused) isPaused.value = false;
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
        // No new chunks arrive while paused (#338); leave the estimate frozen.
        if (isPaused && isPaused.value) return;

        // Include bytes already uploaded before a resume so the estimate (and
        // the derived bitrate) reflect the whole recording, not just the new
        // segment. serverResumePriorBytes is 0 for a fresh recording.
        const totalSize = serverResumePriorBytes
            + audioChunks.value.reduce((sum, chunk) => sum + chunk.size, 0);
        estimatedFileSize.value = totalSize;

        if (recordingTime.value > 0) {
            actualBitrate.value = (totalSize * 8) / recordingTime.value;
        }

        // Phase C of #287 (c)(d): the 200 MB cap exists because on the legacy
        // path the entire blob is held in browser RAM and would crash the tab
        // past a certain size. When server-side chunk streaming is active that
        // constraint goes away — chunks flush to the server as they are
        // produced, so NO size-based warning or stop applies (#332: warning
        // users about a limit that does not exist was alarming them). The
        // ceiling in streaming mode is hours-based (`RECORDING_MAX_HOURS`,
        // default 8, warned about and enforced in the recording-time tick) so
        // a runaway recording from a misclick still has a backstop.
        if (serverSessionUploader) {
            return;
        }

        // Legacy single-shot path: soft-warn at 80% of the cap, then hard
        // auto-stop at the cap so the in-memory blob does not run the tab
        // out of RAM.
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

            // Create blob URL. Also restore the recording's mime type so the
            // legacy upload path builds the File with the right container
            // (a recovered video capture must not be renamed audio/webm).
            currentRecordingMimeType = recovered.metadata.mimeType || 'audio/webm';
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

            // Restore the incognito state the recording was left in, so a
            // crashed incognito recording is offered back as incognito instead
            // of silently becoming a normal (permanently stored) one.
            if (incognitoMode && enableIncognitoMode && enableIncognitoMode.value) {
                incognitoMode.value = !!recovered.metadata.incognito;
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

    // Keep the crash-recovery session's incognito flag in sync with the
    // toggle. It can be flipped in the review pane after recording stops
    // (and the modal toggle can flip it with no session — then this is a
    // harmless no-op), so a crash after the flip still recovers into the
    // mode the user last chose.
    if (typeof Vue !== 'undefined' && Vue.watch && incognitoMode) {
        Vue.watch(incognitoMode, (v) => {
            RecordingDB.updateRecordingMetadata({ incognito: !!v });
        });
    }

    return {
        startRecording,
        requestMicrophonePermission,
        confirmPendingMicrophoneSelection,
        cancelPendingMicrophoneSelection,
        isPreparingRecording,
        pendingMicrophoneSelection,
        pauseRecording,
        resumeRecording,
        stopRecording,
        discardRecording,
        normalizeLiveMediaDuration,
        uploadRecordedAudio,
        mergeRecordedWithExisting,
        finalizeRecordingMerge,
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
