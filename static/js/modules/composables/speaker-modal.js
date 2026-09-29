/**
 * Identify Speakers modal: list, filter, name fields and player (#395).
 *
 * Saving, auto-identify, voice suggestions and the per-line edits stay in
 * speakers.js; this composable owns what the rebuilt modal shows and how the
 * user moves through it. It reads the stored transcription directly instead
 * of processedTranscription, so typing a name re-renders only that speaker's
 * rows, not every segment of the recording.
 */

import {
    parseModalSegments, computeSpeakerStats, orderSpeakers, splitMinorSpeakers,
    buildNameOptions, findSavedSpelling, playingSegmentPosition, turnStarts,
    formatClock, mergeSpeakerSegments, MINOR_SPEAKER_SECONDS,
} from '../utils/speaker-modal-model.js';

const SORT_KEY = 'speakerModalSort';
const SORT_MODES = ['duration', 'appearance', 'name'];
const EMPTY = { isJson: false, segments: [], plainText: '' };

export function useSpeakerModal(state, utils) {
    const { ref, computed, watch, nextTick } = Vue;
    const {
        showSpeakerModal, selectedRecording, speakerMap, speakerColorMap, modalSpeakers,
        speakerModalTab, voiceSuggestions, editedTranscriptData, playerVolume, audioIsMuted,
        modalAudioCurrentTime, modalPlaybackRate, playbackSpeeds, speakerModalTranscriptRef,
    } = state;
    const { showToast } = utils;
    const t = (key, params) => (window.i18n ? window.i18n.t(key, params) : key);
    const tc = (key, count, params) => (window.i18n ? window.i18n.tc(key, count, params) : key);

    let storedSort = null;
    try { storedSort = localStorage.getItem(SORT_KEY); } catch (e) { /* storage blocked */ }

    const speakerModalPlayer = ref(null);
    const speakerSort = ref(SORT_MODES.includes(storedSort) ? storedSort : 'duration');
    const selectedSpeaker = ref(null);
    const onlySelectedSpeaker = ref(true);
    const showMinorSpeakers = ref(false);
    const currentTurn = ref(0);
    const savedSpeakers = ref([]);
    const nameSearchResults = ref({});
    const openNameField = ref(null);
    const nameOptionIndex = ref(-1);
    // Names as they were when the list was last ordered. Sorting by name, and
    // deciding who counts as a minor speaker, uses this snapshot so a row
    // does not jump away while its name is being typed.
    const nameSnapshot = ref({});

    let sampleStopAt = null;
    let searchTimer = null;
    let searchAbort = null;
    let blurTimer = null;

    const nameOf = (id) => speakerMap.value[id]?.name || '';
    const labelOf = (id) => nameOf(id).trim() || id;
    const snapshotNames = () => {
        const snap = {};
        for (const id of modalSpeakers.value) snap[id] = nameOf(id).trim();
        nameSnapshot.value = snap;
    };

    // ---------------------------------------------------------------- data

    const speakerModalData = computed(() =>
        showSpeakerModal.value ? parseModalSegments(selectedRecording.value?.transcription) : EMPTY);

    const speakerStats = computed(() => computeSpeakerStats(speakerModalData.value.segments));

    const orderedSpeakers = computed(() =>
        orderSpeakers(modalSpeakers.value, speakerStats.value, speakerSort.value, id => nameSnapshot.value[id]));

    const speakerGroupsSplit = computed(() =>
        splitMinorSpeakers(orderedSpeakers.value, speakerStats.value, MINOR_SPEAKER_SECONDS, id => !!nameSnapshot.value[id]));

    const mainSpeakers = computed(() => speakerGroupsSplit.value.main);
    const minorSpeakers = computed(() => speakerGroupsSplit.value.minor);
    const minorSpeakersSeconds = computed(() =>
        minorSpeakers.value.reduce((sum, id) => sum + (speakerStats.value.bySpeaker[id]?.seconds || 0), 0));

    // One flat list for the template: main speakers, then the minor group's
    // header, then (when expanded) the minor speakers.
    const speakerListEntries = computed(() => {
        const rows = mainSpeakers.value.map(id => ({ key: id, id, minor: false }));
        if (minorSpeakers.value.length) {
            rows.push({ key: '__minor__', header: true });
            if (showMinorSpeakers.value) {
                minorSpeakers.value.forEach(id => rows.push({ key: id, id, minor: true }));
            }
        }
        return rows;
    });

    const canPlayModalAudio = computed(() =>
        !!selectedRecording.value && !selectedRecording.value.audio_deleted_at && !selectedRecording.value.incognito);

    const visibleModalSegments = computed(() => {
        const segs = speakerModalData.value.segments;
        if (!selectedSpeaker.value || !onlySelectedSpeaker.value) return segs;
        return segs.filter(s => s.speakerId === selectedSpeaker.value);
    });

    const selectedSpeakerTurns = computed(() =>
        selectedSpeaker.value && !onlySelectedSpeaker.value
            ? turnStarts(visibleModalSegments.value, selectedSpeaker.value)
            : []);

    const playingSegmentIndex = computed(() => {
        const segs = speakerModalData.value.segments;
        const pos = playingSegmentPosition(segs, modalAudioCurrentTime.value);
        return pos === -1 ? -1 : segs[pos].index;
    });

    const speakerColor = (id) => speakerMap.value[id]?.color || speakerColorMap.value[id] || 'speaker-color-1';

    const speakerMeta = (id) => {
        const agg = speakerStats.value.bySpeaker[id];
        if (!agg) return t('speakerModal.noSegmentsYet');
        if (!speakerStats.value.hasTimes) return tc('speakerModal.segmentCount', agg.segments, { count: agg.segments });
        return t('speakerModal.speakingTime', { time: formatClock(agg.seconds), share: agg.share });
    };

    const speakerShare = (id) => speakerStats.value.bySpeaker[id]?.share || 0;

    // --------------------------------------------------------- name fields

    const loadSavedSpeakers = async () => {
        try {
            const response = await fetch('/speakers/search?top=1');
            savedSpeakers.value = response.ok ? await response.json() : [];
        } catch (e) {
            savedSpeakers.value = [];
        }
    };

    const searchSavedSpeakers = (id, query) => {
        clearTimeout(searchTimer);
        if (searchAbort) searchAbort.abort();
        const q = (query || '').trim();
        if (!q) {
            nameSearchResults.value = { ...nameSearchResults.value, [id]: [] };
            return;
        }
        searchTimer = setTimeout(async () => {
            const controller = new AbortController();
            searchAbort = controller;
            try {
                const response = await fetch(`/speakers/search?q=${encodeURIComponent(q)}`, { signal: controller.signal });
                const results = response.ok ? await response.json() : [];
                if (!controller.signal.aborted) {
                    nameSearchResults.value = { ...nameSearchResults.value, [id]: Array.isArray(results) ? results : [] };
                }
            } catch (e) {
                // Aborted by a newer keystroke, or offline: keep the last results.
            }
        }, 200);
    };

    const nameOptions = (id) => {
        const typed = nameOf(id);
        const taken = modalSpeakers.value.filter(other => other !== id).map(nameOf).filter(Boolean);
        return buildNameOptions({
            voice: voiceSuggestions.value[id] || [],
            saved: [...(nameSearchResults.value[id] || []), ...savedSpeakers.value],
            query: typed,
            takenNames: taken,
        }).filter(opt => opt.name.trim() !== typed.trim());
    };

    const savedSpellingFor = (id) => {
        const known = [...savedSpeakers.value, ...(nameSearchResults.value[id] || [])].map(s => s.name);
        return findSavedSpelling(nameOf(id), known);
    };

    const topVoiceMatch = (id) => {
        if (speakerMap.value[id]?.isMe || nameOf(id).trim()) return null;
        return (voiceSuggestions.value[id] || [])[0] || null;
    };

    const onNameFocus = (id) => {
        clearTimeout(blurTimer);
        openNameField.value = id;
        nameOptionIndex.value = -1;
        selectSpeaker(id, { keep: true });
    };

    const onNameInput = (id) => {
        openNameField.value = id;
        nameOptionIndex.value = -1;
        searchSavedSpeakers(id, nameOf(id));
    };

    const onNameBlur = () => {
        blurTimer = setTimeout(() => {
            openNameField.value = null;
            nameOptionIndex.value = -1;
            snapshotNames();
        }, 150);
    };

    const chooseName = (id, name) => {
        if (!speakerMap.value[id]) return;
        speakerMap.value[id].name = name;
        openNameField.value = null;
        nameOptionIndex.value = -1;
        snapshotNames();
    };

    const onNameKeydown = (id, event) => {
        const options = nameOptions(id);
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            if (!options.length) return;
            event.preventDefault();
            openNameField.value = id;
            const last = options.length - 1;
            const i = nameOptionIndex.value;
            nameOptionIndex.value = event.key === 'ArrowDown'
                ? (i < last ? i + 1 : 0)
                : (i > 0 ? i - 1 : last);
        } else if (event.key === 'Enter') {
            if (openNameField.value === id && nameOptionIndex.value >= 0 && options[nameOptionIndex.value]) {
                event.preventDefault();
                chooseName(id, options[nameOptionIndex.value].name);
            } else {
                openNameField.value = null;
                snapshotNames();
            }
        } else if (event.key === 'Escape') {
            if (openNameField.value === id) {
                event.preventDefault();
                event.stopPropagation();
                openNameField.value = null;
                nameOptionIndex.value = -1;
            }
        }
    };

    // ------------------------------------------------------- list & filter

    const scrollTranscriptTo = (index, block = 'start') => {
        nextTick(() => {
            const container = speakerModalTranscriptRef.value;
            if (!container) return;
            if (index === null) { container.scrollTop = 0; return; }
            const el = container.querySelector(`[data-segment-index="${index}"]`);
            if (el) el.scrollIntoView({ block, behavior: 'auto' });
        });
    };

    const selectSpeaker = (id, { keep = false, openTranscript = false } = {}) => {
        if (!keep && selectedSpeaker.value === id) {
            clearSelectedSpeaker();
            return;
        }
        const changed = selectedSpeaker.value !== id;
        selectedSpeaker.value = id;
        currentTurn.value = 0;
        if (changed) {
            if (onlySelectedSpeaker.value) scrollTranscriptTo(null);
            else {
                const first = selectedSpeakerTurns.value[0];
                if (first !== undefined) scrollTranscriptTo(visibleModalSegments.value[first].index, 'center');
            }
        }
        if (openTranscript) speakerModalTab.value = 'transcript';
    };

    const clearSelectedSpeaker = () => {
        selectedSpeaker.value = null;
        currentTurn.value = 0;
    };

    const showAllSegments = () => {
        onlySelectedSpeaker.value = false;
        const first = selectedSpeakerTurns.value[0];
        if (first !== undefined) scrollTranscriptTo(visibleModalSegments.value[first].index, 'center');
    };

    const showOnlySelectedSpeaker = () => {
        onlySelectedSpeaker.value = true;
        scrollTranscriptTo(null);
    };

    const goToTurn = (delta) => {
        const turns = selectedSpeakerTurns.value;
        if (!turns.length) return;
        currentTurn.value = (currentTurn.value + delta + turns.length) % turns.length;
        scrollTranscriptTo(visibleModalSegments.value[turns[currentTurn.value]].index, 'center');
    };

    const setSpeakerSort = (mode) => {
        if (!SORT_MODES.includes(mode)) return;
        snapshotNames();
        speakerSort.value = mode;
        try { localStorage.setItem(SORT_KEY, mode); } catch (e) { /* storage blocked */ }
    };

    /**
     * Stage a merge: every segment of `sourceIds` moves to `targetId`, and
     * the sources leave the list. Nothing is saved until Save.
     */
    const mergeSpeakersInto = (sourceIds, targetId) => {
        if (!targetId || !selectedRecording.value?.transcription) return;
        const sources = sourceIds.filter(id => id && id !== targetId);
        if (!sources.length) return;
        let data;
        try {
            data = JSON.parse(selectedRecording.value.transcription);
        } catch (e) {
            return;
        }
        if (!Array.isArray(data)) return;
        const { data: merged, moved } = mergeSpeakerSegments(data, sources, targetId);
        editedTranscriptData.value = merged;
        selectedRecording.value.transcription = JSON.stringify(merged);

        const removed = new Set(sources);
        modalSpeakers.value = modalSpeakers.value.filter(id => !removed.has(id));
        const map = { ...speakerMap.value };
        sources.forEach(id => delete map[id]);
        speakerMap.value = map;
        if (removed.has(selectedSpeaker.value)) selectedSpeaker.value = targetId;
        snapshotNames();
        if (moved > 0) {
            showToast(tc('speakerModal.mergedSpeakers', sources.length, { count: sources.length, name: labelOf(targetId) }), 'fa-object-group');
        }
    };

    // --------------------------------------------------------------- player

    const player = () => speakerModalPlayer.value;

    const toggleModalPlayback = () => {
        const el = player();
        if (!el) return;
        if (el.paused) { sampleStopAt = null; el.play(); } else el.pause();
    };

    const playFrom = (seconds, stopAt = null) => {
        const el = player();
        if (!el || seconds === null || !isFinite(seconds)) return;
        el.currentTime = Math.max(0, seconds);
        sampleStopAt = stopAt;
        el.play();
    };

    const playModalSegment = (segment) => playFrom(segment.start);

    const playSpeakerSample = (id) => {
        const agg = speakerStats.value.bySpeaker[id];
        if (!agg) return;
        const seg = speakerModalData.value.segments[agg.longestIndex];
        if (seg) playFrom(seg.start, seg.end);
    };

    const onModalTimeUpdate = (event) => {
        const el = event.target;
        modalAudioCurrentTime.value = el.currentTime;
        if (sampleStopAt !== null && el.currentTime >= sampleStopAt) {
            sampleStopAt = null;
            el.pause();
        }
    };

    const seekModalPlayer = (event, duration) => {
        const el = player();
        if (!el || !duration) return;
        const rect = event.currentTarget.getBoundingClientRect();
        const pct = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
        sampleStopAt = null;
        el.currentTime = pct * duration;
    };

    const setModalVolume = (value) => {
        const volume = Math.min(1, Math.max(0, parseFloat(value)));
        if (!isFinite(volume)) return;
        playerVolume.value = volume;
        const el = player();
        if (el) {
            el.volume = volume;
            el.muted = volume === 0;
        }
        audioIsMuted.value = volume === 0;
        try { localStorage.setItem('playerVolume', String(volume)); } catch (e) { /* storage blocked */ }
    };

    const toggleModalMute = () => {
        const el = player();
        if (!el) return;
        el.muted = !el.muted;
        audioIsMuted.value = el.muted;
    };

    const cycleSpeakerModalRate = () => {
        const speeds = playbackSpeeds;
        const next = speeds[(speeds.indexOf(modalPlaybackRate.value) + 1) % speeds.length];
        modalPlaybackRate.value = next;
        const el = player();
        if (el) el.playbackRate = next;
    };

    // ------------------------------------------------------------ lifecycle

    watch(showSpeakerModal, (open) => {
        if (open) {
            selectedSpeaker.value = null;
            onlySelectedSpeaker.value = true;
            showMinorSpeakers.value = false;
            currentTurn.value = 0;
            openNameField.value = null;
            nameOptionIndex.value = -1;
            nameSearchResults.value = {};
            modalAudioCurrentTime.value = 0;
            sampleStopAt = null;
            nextTick(snapshotNames);
            loadSavedSpeakers();
            scrollTranscriptTo(null);
        } else {
            const el = player();
            if (el) el.pause();
            clearTimeout(searchTimer);
            if (searchAbort) searchAbort.abort();
        }
    });

    return {
        speakerModalPlayer,
        speakerSort, setSpeakerSort,
        speakerModalData, speakerStats, orderedSpeakers, mainSpeakers, minorSpeakers, minorSpeakersSeconds, showMinorSpeakers,
        speakerListEntries, canPlayModalAudio, minorSpeakerSeconds: MINOR_SPEAKER_SECONDS,
        visibleModalSegments, selectedSpeaker, onlySelectedSpeaker, selectedSpeakerTurns, currentTurn,
        playingSegmentIndex, speakerColor, speakerMeta, speakerShare, labelOf,
        selectSpeaker, clearSelectedSpeaker, showAllSegments, showOnlySelectedSpeaker, goToTurn,
        mergeSpeakersInto,
        openNameField, nameOptionIndex, nameOptions, savedSpellingFor, topVoiceMatch,
        onNameFocus, onNameInput, onNameBlur, onNameKeydown, chooseName,
        toggleModalPlayback, playModalSegment, playSpeakerSample, onModalTimeUpdate, seekModalPlayer,
        setModalVolume, toggleModalMute, cycleSpeakerModalRate,
        formatClock,
    };
}
