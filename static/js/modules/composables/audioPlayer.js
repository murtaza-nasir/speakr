/**
 * Audio Player Composable
 *
 * Centralized audio playback functionality for consistent behavior across the app.
 * This module handles:
 * - Playback state (playing, paused, loading)
 * - Time tracking (current time, duration)
 * - Volume/mute control
 * - Seeking with progress bar support
 * - Server-side duration support (for formats like WebM that don't report duration)
 *
 * Usage:
 *   const player = useAudioPlayer(ref, computed);
 *   // In template: @loadedmetadata="player.handleLoadedMetadata"
 *   // When recording changes: player.setServerDuration(recording.audio_duration)
 */

export function useAudioPlayer(ref, computed) {
    // --- State ---
    const isPlaying = ref(false);
    const currentTime = ref(0);
    const duration = ref(0);
    const isMuted = ref(false);
    const isLoading = ref(false);
    const volume = ref(1.0);

    // Progress bar drag state
    const isDragging = ref(false);
    const dragPreviewPercent = ref(0);

    // Track if we have a reliable server-side duration
    let hasServerDuration = false;

    // --- Computed ---
    const progressPercent = computed(() => {
        // Use preview position while dragging for smooth UI
        if (isDragging.value) {
            return dragPreviewPercent.value;
        }
        if (!duration.value) return 0;
        return (currentTime.value / duration.value) * 100;
    });

    // Preview time display while dragging
    const displayCurrentTime = computed(() => {
        if (isDragging.value && duration.value) {
            return (dragPreviewPercent.value / 100) * duration.value;
        }
        return currentTime.value;
    });

    // --- Duration Management ---

    /**
     * Set duration from server-side ffprobe value.
     * This is more reliable than browser metadata for some formats (WebM, etc.)
     */
    const setServerDuration = (serverDuration) => {
        if (serverDuration && isFinite(serverDuration) && serverDuration > 0) {
            duration.value = serverDuration;
            hasServerDuration = true;
        } else {
            hasServerDuration = false;
        }
    };

    /**
     * Try to set duration from browser, only if we don't have a server-side value.
     * Browser duration can be Infinity for some WebM files.
     */
    const trySetBrowserDuration = (browserDuration) => {
        if (hasServerDuration) {
            // Don't overwrite reliable server-side duration
            return;
        }
        if (browserDuration && isFinite(browserDuration) && browserDuration > 0) {
            duration.value = browserDuration;
        }
    };

    // --- Event Handlers ---

    const handlePlayPause = (event) => {
        isPlaying.value = !event.target.paused;
    };

    const handleLoadedMetadata = (event) => {
        trySetBrowserDuration(event.target.duration);
        isLoading.value = false;
    };

    const handleDurationChange = (event) => {
        // WebM and some formats may initially report Infinity duration
        // This handler catches when the actual duration becomes available
        trySetBrowserDuration(event.target.duration);
    };

    const handleTimeUpdate = (event) => {
        currentTime.value = event.target.currentTime;

        // Fallback: if duration wasn't set yet, try to get it now
        if (!duration.value || duration.value === 0) {
            trySetBrowserDuration(event.target.duration);
        }
    };

    const handleEnded = () => {
        isPlaying.value = false;
        currentTime.value = 0;
    };

    const handleWaiting = () => {
        isLoading.value = true;
    };

    const handleCanPlay = (event) => {
        isLoading.value = false;

        // Fallback: try to get duration if not set yet
        if (!duration.value || duration.value === 0) {
            trySetBrowserDuration(event.target.duration);
        }
    };

    const handleVolumeChange = (event) => {
        volume.value = event.target.volume;
        isMuted.value = event.target.muted;
    };

    // --- Actions ---

    /**
     * Get the audio element. Override this for custom element selection.
     */
    let getAudioElement = () => {
        return document.querySelector('audio[ref="audioPlayerElement"]') ||
               document.querySelector('audio');
    };

    /**
     * Set custom audio element getter.
     */
    const setAudioElementGetter = (getter) => {
        getAudioElement = getter;
    };

    const play = () => {
        const audio = getAudioElement();
        if (audio) {
            audio.play().catch(err => console.warn('Play failed:', err));
        }
    };

    const pause = () => {
        const audio = getAudioElement();
        if (audio) {
            audio.pause();
        }
    };

    const togglePlayback = () => {
        const audio = getAudioElement();
        if (!audio) return;

        if (audio.paused) {
            audio.play().catch(err => console.warn('Play failed:', err));
        } else {
            audio.pause();
        }
    };

    const seekTo = (time) => {
        const audio = getAudioElement();
        if (!audio || !isFinite(time)) return;

        const maxTime = isFinite(audio.duration) ? audio.duration : time;
        audio.currentTime = Math.max(0, Math.min(time, maxTime));
    };

    const seekByPercent = (percent) => {
        const audio = getAudioElement();
        if (!audio || !duration.value || !isFinite(duration.value)) return;

        const time = (percent / 100) * duration.value;
        audio.currentTime = time;
    };

    const setVolume = (value) => {
        const audio = getAudioElement();
        if (audio) {
            audio.volume = Math.max(0, Math.min(1, value));
            volume.value = audio.volume;
        }
    };

    const toggleMute = () => {
        const audio = getAudioElement();
        if (!audio) return;

        if (audio.muted || audio.volume === 0) {
            audio.muted = false;
            if (audio.volume === 0) {
                audio.volume = 0.5;
            }
            isMuted.value = false;
        } else {
            audio.muted = true;
            isMuted.value = true;
        }
    };

    // --- Progress Bar Drag Support ---

    const startProgressDrag = (event) => {
        const bar = event.currentTarget.querySelector('.h-2') || event.currentTarget;
        const rect = bar.getBoundingClientRect();
        const isTouch = event.type === 'touchstart';

        const getPercent = (evt) => {
            const clientX = isTouch ? evt.touches[0].clientX : evt.clientX;
            return Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
        };

        // Start dragging - show preview
        isDragging.value = true;
        dragPreviewPercent.value = getPercent(event);

        const onMove = (evt) => {
            evt.preventDefault();
            const clientX = isTouch ? evt.touches[0].clientX : evt.clientX;
            dragPreviewPercent.value = Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
        };

        const onUp = () => {
            document.removeEventListener(isTouch ? 'touchmove' : 'mousemove', onMove);
            document.removeEventListener(isTouch ? 'touchend' : 'mouseup', onUp);
            // Seek to final position on release
            seekByPercent(dragPreviewPercent.value);
            isDragging.value = false;
        };

        document.addEventListener(isTouch ? 'touchmove' : 'mousemove', onMove, { passive: false });
        document.addEventListener(isTouch ? 'touchend' : 'mouseup', onUp);
    };

    // --- Utility ---

    const formatTime = (seconds) => {
        if (!seconds || isNaN(seconds)) return '0:00';
        const hours = Math.floor(seconds / 3600);
        const mins = Math.floor((seconds % 3600) / 60);
        const secs = Math.floor(seconds % 60);
        if (hours > 0) {
            return `${hours}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        }
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    };

    /**
     * Reset all player state (call when changing recordings)
     */
    const reset = () => {
        isPlaying.value = false;
        currentTime.value = 0;
        duration.value = 0;
        isMuted.value = false;
        isLoading.value = false;
        hasServerDuration = false;
    };

    /**
     * Initialize with a recording object.
     * Automatically uses server-side duration if available.
     */
    const initWithRecording = (recording) => {
        reset();
        if (recording && recording.audio_duration) {
            setServerDuration(recording.audio_duration);
        }
    };

    return {
        // State
        isPlaying,
        currentTime,
        duration,
        isMuted,
        isLoading,
        volume,
        isDragging,
        dragPreviewPercent,

        // Computed
        progressPercent,
        displayCurrentTime,

        // Duration management
        setServerDuration,
        trySetBrowserDuration,

        // Event handlers (wire these to <audio> element)
        handlePlayPause,
        handleLoadedMetadata,
        handleDurationChange,
        handleTimeUpdate,
        handleEnded,
        handleWaiting,
        handleCanPlay,
        handleVolumeChange,

        // Actions
        play,
        pause,
        togglePlayback,
        seekTo,
        seekByPercent,
        setVolume,
        toggleMute,
        startProgressDrag,
        setAudioElementGetter,

        // Utility
        formatTime,
        reset,
        initWithRecording
    };
}

/**
 * Create a standalone audio player instance.
 * Use this for pages that don't have Vue's ref/computed (like share.html).
 */
export function createStandalonePlayer(Vue) {
    const { ref, computed } = Vue;
    return useAudioPlayer(ref, computed);
}
