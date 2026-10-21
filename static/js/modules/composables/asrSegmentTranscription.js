/** Reviewable ASR proposals, tied to the actual unsaved segment object. */
export function useAsrSegmentTranscription(state, utils) {
    const { editingSegments, selectedRecording, showAsrEditorModal } = state;
    const segmentTranscription = Vue.ref(null);
    let activeObject = null, token = 0, controller = null, timer = null;
    const csrf = () => document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
    const endpoint = view => `/api/recordings/${view.recordingId}/segment-transcriptions${view.jobId ? '/' + view.jobId : ''}`;
    const editable = () => selectedRecording.value?.can_edit !== false && selectedRecording.value?.is_read_only !== true && utils.canEditDraft?.() !== false;
    const canTranscribeSegment = index => {
        const segment = editingSegments.value?.[index], recording = selectedRecording.value;
        return editable() && !!segment && Number.isFinite(segment.start_time) && Number.isFinite(segment.end_time) &&
            segment.start_time >= 0 && segment.end_time - segment.start_time >= .25 && segment.end_time - segment.start_time <= 300 &&
            !!(recording?.audio_available || recording?.audio_ready || recording?.audio_path) && !recording?.audio_deleted_at && !recording?.incognito &&
            segmentTranscription.value?.status !== 'running';
    };
    const sameDraft = view => !!view && showAsrEditorModal.value && selectedRecording.value?.id === view.recordingId &&
        editingSegments.value.includes(activeObject) && activeObject.start_time === view.start && activeObject.end_time === view.end &&
        activeObject.sentence === view.original;
    const cancelJob = view => {
        if (view?.jobId) fetch(endpoint(view), {method:'DELETE',headers:{'X-CSRFToken':csrf()}}).catch(()=>{});
    };
    const closeSegmentTranscription = (restoreFocus = false) => {
        const segment = activeObject;
        const index = editingSegments.value.indexOf(activeObject), view = segmentTranscription.value;
        token++; controller?.abort(); controller = null; clearTimeout(timer); timer = null;
        cancelJob(view); segmentTranscription.value = null; activeObject = null;
        if (restoreFocus && index >= 0) {
            utils.scrollAsrEditorToIndex?.(index);
            utils.nextTick().then(()=> {
                if (utils.isSelectedSegment?.(segment) !== false)
                    document.querySelector('[data-testid="asr-editor"] [data-testid="asr-retranscribe"]')?.focus({preventScroll:true});
            });
        }
    };
    const current = (view, requestToken) => view === segmentTranscription.value && requestToken === token;
    const fail = (view, code) => {
        view.status = 'failed';
        view.error = ['bounds','forbidden','missing','remote','busy','media','channels','empty','size','changed','provider','timeout','worker','cancelled','stale'].includes(code) ? code : 'unavailable';
    };
    const poll = async (view, requestToken) => {
        if (!current(view, requestToken)) return;
        if (!sameDraft(view)) { cancelJob(view); fail(view,'stale'); return; }
        try {
            const response = await fetch(endpoint(view),{signal:controller.signal});
            const data = await response.json();
            if (!current(view,requestToken)) return;
            if (!sameDraft(view)) { cancelJob(view); fail(view,'stale'); return; }
            if (!response.ok) { fail(view,data.code); return; }
            if (data.status === 'done') {
                if (typeof data.text !== 'string' || !data.text.trim() || data.start !== view.start || data.end !== view.end) { fail(view,'provider'); return; }
                view.proposal = data.text; view.status = 'done';
            } else if (data.status === 'running') {
                timer = setTimeout(()=>poll(view,requestToken), Date.now()-view.started > 10000 ? 2000 : 1000);
            } else fail(view,data.code || 'cancelled');
        } catch (error) { if (current(view,requestToken) && error.name !== 'AbortError') fail(view,'unavailable'); }
    };
    const retranscribeSegment = async index => {
        if (!canTranscribeSegment(index)) return;
        closeSegmentTranscription();
        activeObject = editingSegments.value[index];
        segmentTranscription.value = {recordingId:selectedRecording.value.id, start:activeObject.start_time, end:activeObject.end_time,
            original:activeObject.sentence, speaker:activeObject.speaker, proposal:'', status:'running', error:'', jobId:null, started:Date.now()};
        const view = segmentTranscription.value, requestToken = token;
        controller = new AbortController();
        try {
            const response = await fetch(endpoint(view), {method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},
                // Keep the short start handshake alive so a late accepted job can be cancelled by ID.
                body:JSON.stringify({start:view.start,end:view.end})});
            const data = await response.json();
            // A late accepted start still needs cancellation even after the panel closed.
            if (typeof data.job_id === 'string') view.jobId = data.job_id;
            if (!current(view,requestToken)) { cancelJob(view); return; }
            if (!response.ok) { fail(view,data.code); return; }
            if (!view.jobId) { fail(view,'provider'); return; }
            return poll(view,requestToken);
        } catch (error) { if (current(view,requestToken) && error.name !== 'AbortError') fail(view,'unavailable'); }
    };
    const canApplySegmentTranscription = () => sameDraft(segmentTranscription.value) && editable() &&
        segmentTranscription.value.status === 'done' && !!segmentTranscription.value.proposal.trim();
    const applySegmentTranscription = async () => {
        if (!canApplySegmentTranscription()) return false;
        const segment = activeObject, text = segmentTranscription.value.proposal.trim();
        utils.beforeApply?.();
        segment.sentence = text;
        const index = editingSegments.value.indexOf(segment);
        closeSegmentTranscription();
        await utils.nextTick(); utils.scrollAsrEditorToIndex?.(index);
        await utils.nextTick();
        const textarea = document.querySelector(`[data-segment-index="${index}"] [data-testid="asr-segment-text"]`);
        textarea?.focus({preventScroll:true});
        return true;
    };
    const retrySegmentTranscription = () => {
        const index = editingSegments.value.indexOf(activeObject);
        closeSegmentTranscription();
        if (index >= 0) return retranscribeSegment(index);
    };
    Vue.watch?.(() => JSON.stringify([showAsrEditorModal.value, selectedRecording.value?.id, selectedRecording.value?.audio_path, selectedRecording.value?.audio_deleted_at]),()=>closeSegmentTranscription());
    Vue.watch?.(() => [segmentTranscription.value, activeObject?.sentence, activeObject?.start_time, activeObject?.end_time, activeObject?.speaker,
        activeObject && editingSegments.value.includes(activeObject)],()=>{
        const view = segmentTranscription.value;
        if (!view) return;
        if (!editingSegments.value.includes(activeObject)) {closeSegmentTranscription();return;}
        view.speaker = activeObject.speaker;
        if (!sameDraft(view)) {clearTimeout(timer);cancelJob(view);controller?.abort();fail(view,'stale');}
    });
    return {segmentTranscription,canTranscribeSegment,retranscribeSegment,closeSegmentTranscription,
        retrySegmentTranscription,canApplySegmentTranscription,applySegmentTranscription};
}
