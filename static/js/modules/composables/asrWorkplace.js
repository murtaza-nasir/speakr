/** Local editor session: selection, draft acknowledgement, transport and boundary commands. */
export function serializeAsrDraft(segments) {
    return JSON.stringify(segments.map(({ id, showSuggestions, filteredSpeakers, ...rest }) => rest),
        (_key, value) => value && typeof value === 'object' && !Array.isArray(value) ?
            Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])) : value);
}

export function boundaryExtension(segments, index, field, duration, editable = true) {
    const segment = segments[index];
    if (!editable) return { error: 'readOnly' };
    if (!segment) return { error: 'invalidBounds' };
    const start = field === 'start_time';
    if (start && index === 0) return { error: 'noPrevious' };
    const last = !start && index === segments.length - 1;
    if (last && !(Number.isFinite(duration) && duration > 0)) return { error: 'unknownDuration' };
    const target = start ? segments[index - 1]?.end_time : last ? duration : segments[index + 1]?.start_time;
    const s = segment.start_time, e = segment.end_time;
    if (![target, s, e].every(Number.isFinite) || s < 0 || target < 0 ||
        (Number.isFinite(duration) && target > duration) ||
        (start ? target >= e : target <= s)) return { error: 'invalidBounds' };
    if (start ? target >= s - 1e-8 : target <= e + 1e-8) return { error: 'noGap' };
    return { target };
}

const locationMemory = new Map();
export function useAsrWorkplace(state, utils, tools, callbacks) {
    const { ref, computed, watch } = Vue;
    const segments = state.editingSegments;
    const selected = ref(null), splitPreview = ref(null), closeDecision = ref(false), deleteCandidate = ref(null);
    const listExpanded = ref(false), saveState = ref('saved'), saveError = ref('');
    const playbackMode = ref('segment'), volume = ref(1), muted = ref(false);
    const metadataDuration = ref(null), confirmed = ref('');
    let session = 0, recordingId = null, savePromise = null, autosaveTimer = null, playEnd = null, playContext = null;
    const selectedIndex = computed(() => segments.value.indexOf(selected.value));
    const serializedDraft = computed(() => serializeAsrDraft(segments.value));
    const fingerprint = () => callbacks.fingerprint?.(serializedDraft.value) ?? serializedDraft.value;
    const dirty = computed(() => fingerprint() !== confirmed.value);
    const editable = computed(() => !utils.workspace?.loading.value && state.selectedRecording.value?.can_edit !== false &&
        state.selectedRecording.value?.is_read_only !== true);
    const locked = computed(() => !editable.value || closeDecision.value || !!deleteCandidate.value);
    let decisionFocus = null;
    const focusDecision = async () => {
        decisionFocus = document.activeElement;
        await utils.nextTick();
        document.querySelector('[data-testid="asr-keep-editing"], [data-testid="asr-delete-cancel"]')?.focus();
    };
    const restoreDecisionFocus = async () => {
        await utils.nextTick();
        if (decisionFocus?.isConnected) decisionFocus.focus();
        decisionFocus = null;
    };
    const decisionKeydown = event => {
        if (event.key === 'Escape') {
            event.preventDefault(); event.stopPropagation();
            if (deleteCandidate.value) cancelDelete(); else keepEditing();
        } else if (event.key === 'Tab') {
            const buttons = [...event.currentTarget.querySelectorAll('button:not(:disabled)')];
            if (!buttons.length) return;
            const index = buttons.indexOf(document.activeElement);
            event.preventDefault(); buttons[(index + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
        }
    };
    const duration = computed(() => {
        const stored = state.selectedRecording.value?.audio_duration ?? state.selectedRecording.value?.duration;
        return metadataDuration.value || (Number.isFinite(stored) && stored > 0 ? stored : null);
    });
    const audio = () => document.querySelector('[data-testid="asr-editor"] audio, [data-testid="asr-editor"] video');
    const clearTimer = () => { clearTimeout(autosaveTimer); autosaveTimer = null; };
    let playAttempt = 0;
    const cancelBoundedPlayback = () => {
        playAttempt++;
        if (playEnd !== null) audio()?.pause();
        playEnd = null;
    };
    const remember = () => {
        if (recordingId != null) locationMemory.set(recordingId, {
            index: selectedIndex.value, scroll: state.asrEditorRef?.value?.scrollTop || 0
        });
    };
    const scheduleAutosave = () => {
        clearTimer();
        if (state.showAsrEditorModal.value && state.editorAutosave?.value && editable.value &&
            dirty.value && !closeDecision.value && !deleteCandidate.value && !savePromise) {
            const token = session;
            autosaveTimer = setTimeout(() => { if (token === session) save(true, true); }, 2000);
        }
    };
    const select = async index => {
        if (closeDecision.value || deleteCandidate.value || !segments.value[index]) return;
        const next = segments.value[index];
        if (next !== selected.value) {
            cancelBoundedPlayback();
            playContext = null;
            if (tools.segmentTranscription?.value?.status === 'done')
                utils.showToast(window.i18n?.t('asrWorkplace.proposalDiscarded'));
            tools.closeSpectrogram(false, false);
            tools.closeSegmentTranscription();
            callbacks.clearSplitSelection(); splitPreview.value = null;
            selected.value = next;
        }
        listExpanded.value = false;
        await utils.nextTick(); utils.scrollAsrEditorToIndex?.(index);
    };
    const start = index => {
        session++; recordingId = state.selectedRecording.value.id;
        playContext = null;
        savePromise = null; clearTimer(); metadataDuration.value = null;
        utils.resetModalAudioState?.();
        confirmed.value = fingerprint();
        saveState.value = 'saved'; saveError.value = ''; closeDecision.value = false;
        deleteCandidate.value = null;
        splitPreview.value = null; listExpanded.value = false; playbackMode.value = 'segment';
        const remembered = locationMemory.get(recordingId);
        const target = index ?? remembered?.index ?? 0;
        selected.value = segments.value[Math.max(0, Math.min(target, segments.value.length - 1))] || null;
    };
    const stop = () => {
        remember(); session++; recordingId = null; savePromise = null;
        clearTimer(); cancelBoundedPlayback(); audio()?.pause();
        playContext = null;
        selected.value = null; splitPreview.value = null; closeDecision.value = false; metadataDuration.value = null;
        deleteCandidate.value = null;
    };
    const save = async (keepOpen = true, automatic = false) => {
        if (!editable.value || deleteCandidate.value || !state.showAsrEditorModal.value) return false;
        if (savePromise) { await savePromise; return false; }
        if (!dirty.value && !callbacks.needsSave?.(automatic)) { if (!keepOpen) callbacks.close(); return true; }
        clearTimer();
        const payload = serializeAsrDraft(segments.value), id = recordingId, token = session;
        saveState.value = 'saving'; saveError.value = '';
        savePromise = callbacks.persist(payload, id, () => session === token && recordingId === id, automatic);
        const pending = savePromise;
        let success;
        try { success = await pending; } catch { success = false; }
        if (session !== token || recordingId !== id) return !!success;
        savePromise = null;
        if (!success) {
            saveState.value = 'error'; saveError.value = 'saveFailed'; return false;
        }
        const stored = serializeAsrDraft(JSON.parse(success.transcription ?? payload));
        if (success.applyNormalization !== false && serializeAsrDraft(segments.value) === payload && stored !== payload) {
            const normalized = JSON.parse(stored);
            if (Array.isArray(normalized) && normalized.length === segments.value.length) {
                normalized.forEach((item, i) => {
                    delete segments.value[i].speaker_id;
                    Object.assign(segments.value[i], item);
                });
            }
        }
        confirmed.value = success.fingerprint ?? stored; saveState.value = dirty.value ? 'dirty' : 'saved';
        if (!keepOpen && !dirty.value) callbacks.close();
        else scheduleAutosave();
        return true;
    };
    const requestClose = () => {
        clearTimer();
        if (deleteCandidate.value) return;
        if (dirty.value || savePromise) { closeDecision.value = true; focusDecision(); }
        else callbacks.close();
    };
    const keepEditing = () => { closeDecision.value = false; restoreDecisionFocus(); scheduleAutosave(); };
    const discard = () => { if (!savePromise) callbacks.close(); };
    const requestDelete = index => {
        if (locked.value || !segments.value[index]) return;
        clearTimer(); deleteCandidate.value = segments.value[index]; focusDecision();
    };
    const cancelDelete = () => { deleteCandidate.value = null; restoreDecisionFocus(); scheduleAutosave(); };
    const confirmDelete = () => {
        const index = segments.value.indexOf(deleteCandidate.value);
        deleteCandidate.value = null;
        if (index >= 0 && editable.value) callbacks.remove(index);
        restoreDecisionFocus(); scheduleAutosave();
    };
    const extension = field => boundaryExtension(segments.value, selectedIndex.value, field, duration.value, !locked.value);
    const extend = field => {
        const result = extension(field);
        if (result.error) return false;
        cancelBoundedPlayback();
        if (playbackMode.value === 'marker') playbackMode.value = 'segment';
        splitPreview.value = null;
        selected.value[field] = result.target;
        // The spectrum/job composables observe bounds and invalidate their old range.
        return true;
    };
    const loadedMetadata = event => {
        const element = event.target;
        if (!state.showAsrEditorModal.value || recordingId !== state.selectedRecording.value?.id ||
            element !== audio()) return;
        metadataDuration.value = Number.isFinite(element.duration) && element.duration > 0 ? element.duration : null;
        element.volume = volume.value; element.muted = muted.value;
    };
    const canPlay = computed(() => !!state.showAsrEditorModal.value &&
        !!(state.selectedRecording.value?.audio_ready || state.selectedRecording.value?.audio_available || state.selectedRecording.value?.audio_path) &&
        !state.selectedRecording.value?.audio_deleted_at && !state.selectedRecording.value?.incognito);
    const canPlayMarker = computed(() => {
        const segment = selected.value, marker = segment && tools.spectrogramBoundary(segment);
        return !!segment && Number.isFinite(marker) && marker > segment.start_time && marker < segment.end_time;
    });
    const play = async () => {
        utils.releaseSpeakerSample?.();
        const element = audio();
        if (!element) return;
        if (!element.paused) { playAttempt++; element.pause(); return; }
        if (!canPlay.value) return;
        tools.releaseSpectrogramPlayback(); playEnd = null;
        const segment = selected.value;
        if (playbackMode.value !== 'recording') {
            if (!segment || !Number.isFinite(segment.start_time) || !Number.isFinite(segment.end_time) ||
                segment.start_time < 0 || segment.end_time <= segment.start_time) return;
            const marker = tools.spectrogramBoundary(segment);
            if (playbackMode.value === 'marker' && !canPlayMarker.value) return;
            const lower = playbackMode.value === 'marker' ? marker : segment.start_time;
            const upper = Math.min(playbackMode.value === 'fromHere' ? Infinity : segment.end_time, duration.value || Infinity);
            if (playContext?.segment !== segment || playContext?.mode !== playbackMode.value ||
                element.currentTime < lower || element.currentTime >= upper) element.currentTime = lower;
            playContext = {segment, mode: playbackMode.value};
            if (playbackMode.value !== 'fromHere') playEnd = Math.min(segment.end_time, duration.value || Infinity);
        }
        const attempt = ++playAttempt;
        try { await element.play(); } catch (error) {
            if (attempt !== playAttempt || element !== audio() || !state.showAsrEditorModal.value) return;
            playEnd = null;
            if (error.name !== 'AbortError') utils.showToast(window.i18n?.t('asrSpectrogram.error_media'));
        }
    };
    const timeUpdate = event => {
        if (playEnd !== null && event.target.currentTime >= playEnd) {
            event.target.pause(); playEnd = null;
        }
    };
    const setVolume = event => { volume.value = Number(event.target.value); if (audio()) audio().volume = volume.value; };
    const toggleMute = () => { muted.value = !muted.value; if (audio()) audio().muted = muted.value; };
    const setPlaybackMode = value => { cancelBoundedPlayback(); tools.releaseSpectrogramPlayback(); playContext = null; playbackMode.value = value; };
    const spectrumMode = fromMarker => fromMarker ? 'marker' : 'segment';
    const spectrumPlaying = fromMarker => {
        // Media events invalidate this getter; the native element is authoritative.
        state.modalAudioIsPlaying?.value;
        return playbackMode.value === spectrumMode(fromMarker) && !!audio() && !audio().paused;
    };
    const spectrumAction = fromMarker => {
        if (spectrumPlaying(fromMarker)) return 'asrWorkplace.pause';
        state.modalAudioCurrentTime?.value;
        const segment = selected.value, mode = spectrumMode(fromMarker), element = audio();
        const lower = fromMarker ? tools.spectrogramBoundary(segment) : segment?.start_time;
        if (element && playContext?.segment === segment && playContext.mode === mode &&
            playbackMode.value === mode && Number.isFinite(lower) && element.currentTime > lower &&
            element.currentTime < Math.min(segment.end_time, duration.value || Infinity)) return 'asrSpectrogram.resume';
        return fromMarker ? 'asrSpectrogram.playMarker' : 'asrSpectrogram.playSegment';
    };
    const playSpectrum = async fromMarker => {
        const mode = spectrumMode(fromMarker);
        if (playbackMode.value !== mode) {
            // A different command starts its own range; a repeated command toggles it.
            audio()?.pause();
            setPlaybackMode(mode);
        }
        return play();
    };
    const seek = event => {
        if (audio() && duration.value) {
            const target = Number(event.target.value), segment = selected.value;
            const lower = playbackMode.value === 'marker' ? tools.spectrogramBoundary(segment) : segment?.start_time;
            if (['segment', 'marker'].includes(playbackMode.value) &&
                !(Number.isFinite(lower) && target >= lower && target < segment?.end_time)) {
                cancelBoundedPlayback(); tools.releaseSpectrogramPlayback(); audio().pause();
            }
            audio().currentTime = target;
            playContext = {segment: selected.value, mode: playbackMode.value};
        }
    };
    watch?.(fingerprint, () => {
        if (!state.showAsrEditorModal.value) return;
        if (saveState.value !== 'saving') saveState.value = dirty.value ? 'dirty' : 'saved';
        const preview = splitPreview.value;
        if (preview && (selected.value !== preview.segment || serializeAsrDraft([preview.segment]) !== preview.snapshot)) splitPreview.value = null;
        scheduleAutosave();
    });
    watch?.(() => state.selectedRecording.value?.id, (id, oldId) => {
        if (id !== oldId && state.showAsrEditorModal.value && id !== recordingId) callbacks.close();
    });
    watch?.(() => [selected.value?.start_time, selected.value?.end_time], () => {
        cancelBoundedPlayback();
        playContext = null;
        if (playbackMode.value === 'marker') playbackMode.value = 'segment';
    });
    watch?.(() => tools.spectrogram.value?.marker, () => {
        splitPreview.value = null;
        if (playbackMode.value === 'marker' && !canPlayMarker.value) {
            cancelBoundedPlayback(); playContext = null; playbackMode.value = 'segment';
        }
    });
    watch?.(() => state.editorAutosave?.value, scheduleAutosave);
    return {
        start, stop, select, save, requestClose, remember, selected, selectedIndex, locked, editable,
        splitPreview, serialize: serializeAsrDraft,
        asrSelectedSegment: selected, asrSelectedIndex: selectedIndex, asrListExpanded: listExpanded,
        asrDirty: dirty, asrSaveState: saveState, asrSaveError: saveError, asrCloseDecision: closeDecision,
        asrDeleteCandidate: deleteCandidate, requestAsrDelete: requestDelete, cancelAsrDelete: cancelDelete,
        confirmAsrDelete: confirmDelete, handleAsrDecisionKeydown: decisionKeydown,
        asrEditingLocked: locked, asrCanEdit: editable, asrSplitPreview: splitPreview,
        asrPlaybackMode: playbackMode, asrVolume: volume, asrMuted: muted, asrDuration: duration, asrCanPlay: canPlay, asrCanPlayMarker: canPlayMarker,
        selectAsrSegment: select, navigateAsrSegment: delta => select(selectedIndex.value + delta),
        asrBoundaryExtension: extension, extendAsrBoundary: extend,
        keepAsrEditing: keepEditing, discardAsrDraft: discard,
        handleAsrWorkplaceMetadata: loadedMetadata, handleAsrWorkplaceTimeUpdate: timeUpdate,
        playAsrWorkplace: play, playAsrSpectrum: playSpectrum,
        releaseAsrWorkplacePlayback: () => { cancelBoundedPlayback(); playContext = null; tools.releaseSpectrogramPlayback(); },
        asrSpectrumPlaying: spectrumPlaying, asrSpectrumAction: spectrumAction,
        setAsrVolume: setVolume, toggleAsrMute: toggleMute,
        setAsrPlaybackMode: setPlaybackMode, seekAsrWorkplace: seek
    };
}
