/**
 * Audio Player composable
 * Handles audio playback functionality
 */

import { ref, computed, watch } from 'vue';

export function usePlayer() {
    // State
    const isPlaying = ref(false);
    const currentTime = ref(0);
    const duration = ref(0);
    const playbackRate = ref(1.0);
    const audioElement = ref(null);

    // Computed
    const progress = computed(() => {
        if (!duration.value) return 0;
        return (currentTime.value / duration.value) * 100;
    });

    const formattedCurrentTime = computed(() => {
        return formatTime(currentTime.value);
    });

    const formattedDuration = computed(() => {
        return formatTime(duration.value);
    });

    // Methods
    const formatTime = (seconds) => {
        if (!seconds || isNaN(seconds)) return '0:00';
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    };

    const initPlayer = (audio) => {
        audioElement.value = audio;

        if (!audio) return;

        audio.addEventListener('loadedmetadata', () => {
            duration.value = audio.duration;
        });

        audio.addEventListener('timeupdate', () => {
            currentTime.value = audio.currentTime;
        });

        audio.addEventListener('play', () => {
            isPlaying.value = true;
        });

        audio.addEventListener('pause', () => {
            isPlaying.value = false;
        });

        audio.addEventListener('ended', () => {
            isPlaying.value = false;
            currentTime.value = 0;
        });
    };

    const play = () => {
        if (audioElement.value) {
            audioElement.value.play();
        }
    };

    const pause = () => {
        if (audioElement.value) {
            audioElement.value.pause();
        }
    };

    const togglePlayPause = () => {
        if (isPlaying.value) {
            pause();
        } else {
            play();
        }
    };

    const seek = (time) => {
        if (audioElement.value) {
            audioElement.value.currentTime = time;
            currentTime.value = time;
        }
    };

    const seekPercent = (percent) => {
        if (audioElement.value && duration.value) {
            const time = (percent / 100) * duration.value;
            seek(time);
        }
    };

    const skip = (seconds) => {
        if (audioElement.value) {
            const newTime = Math.max(0, Math.min(duration.value, currentTime.value + seconds));
            seek(newTime);
        }
    };

    const setPlaybackRate = (rate) => {
        playbackRate.value = rate;
        if (audioElement.value) {
            audioElement.value.playbackRate = rate;
        }
    };

    const reset = () => {
        if (audioElement.value) {
            audioElement.value.pause();
            audioElement.value.currentTime = 0;
        }
        isPlaying.value = false;
        currentTime.value = 0;
        duration.value = 0;
    };

    return {
        // State
        isPlaying,
        currentTime,
        duration,
        playbackRate,
        audioElement,

        // Computed
        progress,
        formattedCurrentTime,
        formattedDuration,

        // Methods
        initPlayer,
        play,
        pause,
        togglePlayPause,
        seek,
        seekPercent,
        skip,
        setPlaybackRate,
        reset
    };
}
