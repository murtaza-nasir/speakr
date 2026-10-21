/** Independent recording range; never reads or writes transcript draft objects. */
export const secondsToMilliseconds = seconds => Number.isFinite(seconds) ? Math.round(seconds * 1000) : null;
export function formatManualRangeTime(ms) {
    if (!Number.isSafeInteger(ms) || ms < 0) return '';
    const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60, s = Math.floor(ms / 1000) % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`;
}
export function parseManualRangeTime(text) {
    const match = /^(\d+):([0-5]\d):([0-5]\d)\.(\d{3})$/.exec(String(text).trim());
    if (!match) return null;
    const value = Number(match[1]) * 3600000 + Number(match[2]) * 60000 + Number(match[3]) * 1000 + Number(match[4]);
    return Number.isSafeInteger(value) ? value : null;
}

export function useManualVoiceSamples(state, utils = {}) {
    const {ref, computed, watch} = Vue;
    const manualVoiceOpen = ref(false), manualStart = ref('00:00:00.000'), manualEnd = ref('00:00:15.000');
    const manualSpeakerId = ref(''), manualSpeakers = ref([]), manualSamples = ref([]), manualJob = ref(null);
    const manualError = ref(''), manualCheckUrl = ref(''), manualLoading = ref(false), manualListError = ref('');
    const manualSourceAudio = ref(null), manualPreparedAudio = ref(null), manualRangePlaying = ref(false);
    const manualDurationMs = ref(null);
    let generation = 0, opening = 0, pollTimer = null, deadlineTimer = null, expiryTimer = null, playbackTimer = null, pollController = null;
    let lastRecordingId = state.selectedRecording.value?.id;
    const csrf = () => document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
    const recording = () => state.selectedRecording.value;
    const editable = () => !!recording()?.id && recording()?.can_edit !== false && recording()?.is_read_only !== true;
    const audible = () => !!recording()?.id && !recording()?.incognito && !recording()?.audio_deleted_at &&
        !!(recording()?.audio_available || recording()?.audio_ready || recording()?.audio_path);
    const context = () => JSON.stringify([recording()?.id, manualSpeakerId.value, manualStart.value, manualEnd.value]);
    const endpoint = job => `/recordings/${job.recordingId}/manual_voice_samples/preparations/${job.jobId}`;
    const current = job => !!job && manualVoiceOpen.value && job === manualJob.value && job.generation === generation && job.context === context();
    const manualRange = computed(() => ({start_ms:parseManualRangeTime(manualStart.value), end_ms:parseManualRangeTime(manualEnd.value)}));
    const validRange = () => {
        const {start_ms:start, end_ms:end} = manualRange.value;
        return start !== null && end !== null && start < end && end - start <= 300000 &&
            (manualDurationMs.value === null || end <= manualDurationMs.value);
    };
    const manualBusy = computed(() => ['preparing', 'committing'].includes(manualJob.value?.state));
    const canListenManualRange = computed(() => audible() && validRange() && manualJob.value?.state !== 'committing');
    const manualAvailability = computed(() => !audible() ? 'unavailableAudio' : !editable() ? 'readOnly' : !validRange() ? 'errors.bounds' : '');
    const canPrepareManualVoice = computed(() => manualVoiceOpen.value && editable() && audible() && validRange() &&
        manualSpeakers.value.some(s => String(s.id) === String(manualSpeakerId.value)) && !manualBusy.value);
    const canCommitManualVoice = computed(() => current(manualJob.value) && editable() && audible() &&
        manualJob.value?.state === 'ready' && Date.now() < manualJob.value.expiresAt);
    const manualPreparedAudioUrl = computed(() => canCommitManualVoice.value ? `${endpoint(manualJob.value)}/audio` : '');
    const safeCode = code => ['bounds','access','unsupported','insufficient_speech','multiple_speakers','provider','space','changed','expired','limit',
        'unavailable','cancelled','timeout','busy','consumed'].includes(code) ? code : 'unavailable';
    const stopManualRange = () => {
        clearTimeout(playbackTimer); playbackTimer = null; manualSourceAudio.value?.pause(); manualRangePlaying.value = false;
    };
    const cancelRemote = job => {
        if (job?.jobId && job.state !== 'committed') fetch(endpoint(job), {method:'DELETE', headers:{'X-CSRFToken':csrf()}}).catch(() => {});
    };
    const invalidateManualVoice = () => {
        generation++; clearTimeout(pollTimer); clearTimeout(deadlineTimer); clearTimeout(expiryTimer);
        pollController?.abort(); pollController = null; stopManualRange(); manualPreparedAudio.value?.pause();
        cancelRemote(manualJob.value); manualJob.value = null; manualError.value = ''; manualCheckUrl.value = '';
    };
    const fail = (job, code, data = {}) => {
        if (!current(job)) return;
        clearTimeout(pollTimer); clearTimeout(deadlineTimer); clearTimeout(expiryTimer);
        job.state = code === 'cancelled' ? 'cancelled' : 'failed'; manualError.value = safeCode(code);
        // Only the established local administrative check may be offered; never follow provider data URLs.
        manualCheckUrl.value = typeof data.check_url === 'string' && /^\/admin(?:[/?#]|$)/.test(data.check_url) ? data.check_url : '';
        manualPreparedAudio.value?.pause();
    };
    const loadManualSamples = async () => {
        const speakerId = manualSpeakerId.value, token = generation;
        manualSamples.value = []; manualListError.value = '';
        if (!speakerId) return;
        try {
            const response = await fetch(`/speakers/${speakerId}/manual_voice_samples`), data = await response.json();
            if (token !== generation || speakerId !== manualSpeakerId.value || !manualVoiceOpen.value) return;
            if (!response.ok) {manualListError.value = safeCode(data.code); return;}
            manualSamples.value = Array.isArray(data.samples) ? data.samples : [];
        } catch { if (token === generation) manualListError.value = 'unavailable'; }
    };
    const openManualVoice = async () => {
        invalidateManualVoice(); manualVoiceOpen.value = true; manualLoading.value = true;
        manualSpeakers.value = [];
        // People belong to this panel opening; range edits invalidate only preparations.
        const token = ++opening, recordingId = recording()?.id;
        const activeOpening = () => token === opening && manualVoiceOpen.value && recordingId === recording()?.id;
        const start = Math.max(0, secondsToMilliseconds(state.showAsrEditorModal?.value ? (state.modalAudioCurrentTime?.value || 0) : 0));
        const duration = secondsToMilliseconds(Number(recording()?.audio_duration ?? recording()?.duration));
        manualDurationMs.value = duration > 0 ? duration : null;
        manualStart.value = formatManualRangeTime(start);
        manualEnd.value = formatManualRangeTime(manualDurationMs.value === null ? start + 15000 : Math.min(start + 15000, manualDurationMs.value));
        try {
            const response = await fetch('/speakers'), data = await response.json();
            if (!activeOpening()) return;
            manualSpeakers.value = response.ok && Array.isArray(data) ? data : [];
            if (!response.ok) manualError.value = 'unavailable';
            if (!manualSpeakers.value.some(s => String(s.id) === String(manualSpeakerId.value))) manualSpeakerId.value = '';
        } catch { if (activeOpening()) manualError.value = 'unavailable'; }
        finally { if (activeOpening()) manualLoading.value = false; }
        if (!activeOpening()) return;
        await loadManualSamples();
        await utils.nextTick?.();
        if (!activeOpening()) return;
        document.querySelector('[data-testid="manual-voice-start"]')?.focus?.();
    };
    const closeManualVoice = () => {
        const wasOpen = manualVoiceOpen.value;
        opening++;
        invalidateManualVoice(); manualVoiceOpen.value = false; manualSamples.value = [];
        if (wasOpen) utils.nextTick?.(() => document.querySelector('[data-testid="manual-voice-open"]')?.focus?.());
    };
    const handleManualKeydown = event => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') event.preventDefault();
        if (event.key === 'Escape' && manualJob.value?.state !== 'committing') {event.preventDefault(); closeManualVoice();}
        if (event.key === 'Tab') {
            const fields = [...event.currentTarget.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href],audio[controls]')];
            if (!fields.length) return;
            const index = fields.indexOf(document.activeElement);
            if (event.shiftKey ? index <= 0 : index === fields.length - 1) {
                event.preventDefault(); fields[event.shiftKey ? fields.length - 1 : 0].focus();
            }
        }
    };
    const changeManualContext = () => {invalidateManualVoice(); return loadManualSamples();};
    const poll = async job => {
        if (!current(job)) {cancelRemote(job); return;}
        if (job.state !== 'preparing') return;
        try {
            const response = await fetch(endpoint(job), {signal:pollController.signal}), data = await response.json();
            if (!current(job)) {cancelRemote(job); return;}
            if (job.state !== 'preparing') return;
            if (!response.ok) {fail(job, data.code, data); return;}
            if (data.state === 'ready') {
                if (data.range?.start_ms !== job.start_ms || data.range?.end_ms !== job.end_ms ||
                    !Number.isFinite(data.speech_ms) || !data.space_id) {fail(job, 'provider'); cancelRemote(job); return;}
                clearTimeout(deadlineTimer); job.state = 'ready'; job.speech_ms = data.speech_ms; job.space_id = data.space_id;
                const serverExpiry = data.expires_at ? Date.parse(data.expires_at) : NaN;
                job.expiresAt = Number.isFinite(serverExpiry) ? serverExpiry : Date.now() + 900000;
                expiryTimer = setTimeout(() => fail(job, 'expired'), Math.max(0, job.expiresAt - Date.now()));
            } else if (data.state === 'preparing') pollTimer = setTimeout(() => poll(job), 1000);
            else fail(job, data.code || data.state || 'provider', data);
        } catch (error) { if (error.name !== 'AbortError') fail(job, 'unavailable'); }
    };
    const prepareManualVoice = async () => {
        if (!canPrepareManualVoice.value) return;
        invalidateManualVoice();
        const range = manualRange.value;
        manualJob.value = {...range, recordingId:recording().id, speakerId:Number(manualSpeakerId.value),
            generation, context:context(), state:'preparing', jobId:null};
        const job = manualJob.value;
        pollController = new AbortController();
        deadlineTimer = setTimeout(() => {fail(job, 'timeout'); pollController?.abort(); cancelRemote(job);}, 300000);
        try {
            // Keep the acceptance handshake alive: a late accepted job must be cancelled by its UUID.
            const response = await fetch(`/recordings/${job.recordingId}/manual_voice_samples/prepare`, {
                method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},
                body:JSON.stringify({speaker_id:job.speakerId, start_ms:job.start_ms, end_ms:job.end_ms})});
            const data = await response.json();
            if (typeof data.job_id === 'string') job.jobId = data.job_id;
            if (!current(job) || job.state !== 'preparing') {cancelRemote(job); return;}
            if (!response.ok) {fail(job, data.code, data); return;}
            if (!job.jobId) {fail(job, 'provider'); return;}
            await poll(job);
        } catch {fail(job, 'unavailable');}
    };
    const cancelManualVoice = () => {invalidateManualVoice(); manualError.value = 'cancelled';};
    const commitManualVoice = async () => {
        if (!canCommitManualVoice.value) return false;
        const job = manualJob.value; job.state = 'committing'; manualError.value = '';
        try {
            const response = await fetch(`/speakers/${job.speakerId}/manual_voice_samples`, {method:'POST',
                headers:{'Content-Type':'application/json','X-CSRFToken':csrf()}, body:JSON.stringify({job_id:job.jobId})});
            const data = await response.json();
            if (!current(job)) return false;
            if (!response.ok) {fail(job, data.code, data); return false;}
            clearTimeout(expiryTimer); job.state = 'committed'; manualPreparedAudio.value?.pause();
            await loadManualSamples(); utils.showToast?.(window.i18n?.t('manualVoice.added') || 'Voice sample added', 'success');
            return true;
        } catch {
            // The server may have consumed the job before the connection failed; retry its receipt.
            if (current(job) && Date.now() < job.expiresAt) {job.state = 'ready'; manualError.value = 'unavailable';}
            else fail(job, 'expired');
            return false;
        }
    };
    const deleteManualVoice = async sample => {
        const speakerId = manualSpeakerId.value;
        if (!speakerId || !sample?.id || sample.removing) return;
        sample.removing = true;
        try {
            const response = await fetch(`/speakers/${speakerId}/manual_voice_samples/${encodeURIComponent(sample.id)}`,
                {method:'DELETE', headers:{'X-CSRFToken':csrf()}});
            const data = await response.json();
            if (!response.ok) {manualListError.value = safeCode(data.code); return;}
            if (speakerId === manualSpeakerId.value) await loadManualSamples();
        } catch {manualListError.value = 'unavailable';} finally {sample.removing = false;}
    };
    const onManualMetadata = event => {manualDurationMs.value = secondsToMilliseconds(event.target.duration);};
    const onManualTimeUpdate = event => {
        if (manualRangePlaying.value && event.target.currentTime >= manualRange.value.end_ms / 1000) stopManualRange();
    };
    const playManualRange = async () => {
        if (manualRangePlaying.value) {stopManualRange(); return;}
        if (!audible() || !validRange()) return;
        const audio = manualSourceAudio.value;
        if (!audio) return;
        utils.pauseOtherAudio?.(); manualPreparedAudio.value?.pause();
        const {start_ms:start, end_ms:end} = manualRange.value;
        audio.currentTime = start / 1000; manualRangePlaying.value = true;
        try {await audio.play();} catch {stopManualRange(); manualError.value = 'unavailable'; return;}
        // timeupdate is sparse; a timer bounds the preview even between browser events.
        const checkEnd = () => {
            if (!manualRangePlaying.value) return;
            if (audio.currentTime >= end / 1000) stopManualRange(); else playbackTimer = setTimeout(checkEnd, 25);
        };
        playbackTimer = setTimeout(checkEnd, 25);
    };
    const manualSourceLink = sample => sample.source_available && sample.recording_id ? `/recordings/${sample.recording_id}` : '';
    const onManualPreparedError = () => {
        const job = manualJob.value;
        if (current(job) && job.state === 'ready') {fail(job, 'unavailable'); cancelRemote(job);}
    };
    watch?.(() => JSON.stringify([recording()?.id, editable(), audible()]), () => {
        const changedRecording = lastRecordingId !== recording()?.id; lastRecordingId = recording()?.id;
        if (!manualVoiceOpen.value) return;
        if (changedRecording) closeManualVoice();
        else {invalidateManualVoice(); manualError.value = 'access'; loadManualSamples();}
    });
    watch?.(() => state.showAsrEditorModal?.value, open => {if (!open && manualVoiceOpen.value) closeManualVoice();});
    Vue.onUnmounted?.(closeManualVoice);
    return {manualVoiceOpen, manualStart, manualEnd, manualSpeakerId, manualSpeakers, manualSamples, manualJob, manualError, manualCheckUrl,
        manualLoading, manualListError, manualSourceAudio, manualPreparedAudio, manualRangePlaying, manualRange, manualBusy, canListenManualRange, manualAvailability,
        canPrepareManualVoice, canCommitManualVoice, manualPreparedAudioUrl, openManualVoice, closeManualVoice, changeManualContext,
        prepareManualVoice, cancelManualVoice, commitManualVoice, deleteManualVoice, playManualRange, stopManualRange, onManualTimeUpdate,
        onManualMetadata, manualSourceLink, formatManualRangeTime, handleManualKeydown, onManualPreparedError};
}
