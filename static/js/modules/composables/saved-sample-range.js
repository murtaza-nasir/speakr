import { SavedSamplePlayback, rangeOverlaps, rangeText, playbackErrorKey } from '../utils/saved-voice-samples.js';

export function useSavedSampleRange(state, utils) {
    const {ref, computed, watch} = Vue;
    const savedSampleRange=ref(null), savedSampleInspection=ref(false), savedSampleError=ref('');
    let generation=0, returnFocus=null;
    const control=new SavedSamplePlayback(data=>{
        if(data.state==='failed') {
            clearSavedSampleRange(); savedSampleError.value=data.code;
            utils.showToast?.(window.i18n?.t(playbackErrorKey(data.code)));
        }
    });
    const clearSavedSampleRange=(restore=true)=>{
        const focus=savedSampleInspection.value && restore!==false ? returnFocus : null;
        returnFocus=null;
        generation++; control.stop();
        document.querySelectorAll('[data-testid=saved-range-audio]').forEach(a=>{a.pause();a.removeAttribute('src');a.load();});
        savedSampleRange.value=null; savedSampleInspection.value=false; savedSampleError.value='';
        if(focus?.isConnected)focus.focus();
    };
    const savedSampleRangeText=computed(()=>savedSampleRange.value ? rangeText(savedSampleRange.value) : '');
    const savedSampleTimeline=computed(()=>{
        const r=savedSampleRange.value; if(!r)return {left:0,width:0};
        const record=state.selectedRecording.value;
        const total=Math.max(r.end_ms,Number(record?.audio_duration||record?.duration||0)*1000);
        return {left:r.start_ms/total*100,width:(r.end_ms-r.start_ms)/total*100};
    });
    const savedSampleIntersects=segment=>rangeOverlaps(segment,savedSampleRange.value);
    const savedSampleRowIntersects=segment=>rangeOverlaps({start_time:segment.start,end_time:segment.end},savedSampleRange.value);
    const openSavedSampleRange=async(speakerId,sampleId,recordingId)=>{
        clearSavedSampleRange(false); const token=generation;
        const previousFocus=document.activeElement;
        const ready=await control.start(speakerId,sampleId);
        if(token!==generation || !ready)return;
        if(ready.recording_id!==Number(recordingId) || Number(state.selectedRecording.value?.id)!==ready.recording_id) {
            clearSavedSampleRange(); savedSampleError.value='access'; utils.showToast?.(window.i18n?.t('manualVoice.playbackErrors.access')); return;
        }
        savedSampleRange.value=ready;
        let segments;
        try {segments=JSON.parse(state.selectedRecording.value.transcription);}catch {}
        const index=Array.isArray(segments) && segments.every(s=>Number.isFinite(s.start_time)&&Number.isFinite(s.end_time)) ? segments.findIndex(s=>rangeOverlaps(s,ready)) : -1;
        if(index>=0) {
            try {await utils.openAtSegment(index);} catch {savedSampleInspection.value=true;}
            if(token!==generation || Number(state.selectedRecording.value?.id)!==ready.recording_id)return;
            if(!state.showAsrEditorModal.value)savedSampleInspection.value=true;
        } else savedSampleInspection.value=true;
        if(savedSampleInspection.value)returnFocus=previousFocus;
        await utils.nextTick();
        if(token!==generation || Number(state.selectedRecording.value?.id)!==ready.recording_id)return;
        document.querySelector(savedSampleInspection.value ? '[data-testid=saved-range-inspection] [role=dialog]' : '[data-testid=saved-range-selection]')?.focus();
    };
    const handleSavedInspectionKeydown=event=>{
        event.stopPropagation();
        if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s')event.preventDefault();
        if(event.key==='Escape'){event.preventDefault();clearSavedSampleRange();}
        if(event.key==='Tab') {
            const fields=[...event.currentTarget.querySelectorAll('button:not(:disabled),a[href],audio[controls]')];
            if(!fields.length)return;
            const index=fields.indexOf(document.activeElement);
            if(event.shiftKey ? index<=0 : index===fields.length-1 || index<0){
                event.preventDefault();fields[event.shiftKey ? fields.length-1 : 0].focus();
            }
        }
    };
    const onSavedRangeAudioError=event=>{
        if(!savedSampleRange.value || !event?.target?.isConnected || event.target.getAttribute('src')!==savedSampleRange.value.audioUrl)return;
        clearSavedSampleRange();savedSampleError.value='unavailable';
        utils.showToast?.(window.i18n?.t('manualVoice.playbackErrors.unavailable'));
    };
    const pauseOtherSampleAudio=event=>document.querySelectorAll('audio,video').forEach(a=>{if(a!==event.target)a.pause();});
    watch(()=>state.selectedRecording.value?.id,()=>clearSavedSampleRange(false));
    watch(()=>state.showAsrEditorModal.value,(open,previous)=>{if(!open&&previous)clearSavedSampleRange();});
    window.addEventListener('pagehide',()=>clearSavedSampleRange(false));
    return {savedSampleRange,savedSampleRangeText,savedSampleTimeline,savedSampleInspection,savedSampleError,
        clearSavedSampleRange,openSavedSampleRange,savedSampleIntersects,savedSampleRowIntersects,pauseOtherSampleAudio,
        handleSavedInspectionKeydown,onSavedRangeAudioError};
}
