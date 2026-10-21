import {beforeEach,afterEach,it,expect,vi} from 'vitest';
import {useAsrSegmentTranscription} from './asrSegmentTranscription.js';
let state,asr,watchers,beforeApply;
const ref=value=>{let current=value && typeof value==='object'?new Proxy(value,{}):value;return {get value(){return current;},set value(v){current=v&&typeof v==='object'?new Proxy(v,{}):v;}};};
const reply=(data,ok=true)=>({ok,json:async()=>data});
beforeEach(()=>{
    vi.useFakeTimers();watchers=[];
    vi.stubGlobal('Vue',{ref,watch:(source,cb)=>watchers.push(cb)});
    vi.stubGlobal('document',{querySelector:vi.fn(()=>null)});
    state={editingSegments:ref([{start_time:10,end_time:18,sentence:'Old',speaker:'A'},{start_time:20,end_time:28,sentence:'Other',speaker:'B'}]),
        selectedRecording:ref({id:9,audio_available:true,can_edit:true}),showAsrEditorModal:ref(true)};
    vi.stubGlobal('fetch',vi.fn(async(url,options)=>options?.method==='POST'?reply({job_id:'job',status:'running'}):
        options?.method==='DELETE'?reply({status:'cancelled'}):reply({status:'done',text:'New',start:10,end:18})));
    beforeApply=vi.fn();asr=useAsrSegmentTranscription(state,{nextTick:async()=>{},beforeApply});
});
afterEach(()=>{asr.closeSegmentTranscription();vi.useRealTimers();vi.unstubAllGlobals();vi.restoreAllMocks();});
it('generates a proposal through reactive refs without modifying draft until apply',async()=>{
    const other={...state.editingSegments.value[1]},segment=state.editingSegments.value[0];
    await asr.retranscribeSegment(0);
    expect(asr.segmentTranscription.value.status).toBe('done');expect(segment.sentence).toBe('Old');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({start:10,end:18});
    asr.segmentTranscription.value.proposal='Manually reviewed';await asr.applySegmentTranscription();
    expect(beforeApply).toHaveBeenCalledOnce();expect(segment).toMatchObject({sentence:'Manually reviewed',speaker:'A',start_time:10,end_time:18});
    expect(state.editingSegments.value[1]).toEqual(other);
});
it('keeps object target across index insertion and preserves a renamed speaker',async()=>{
    const object=state.editingSegments.value[0];await asr.retranscribeSegment(0);
    state.editingSegments.value.unshift({sentence:'Inserted'});object.speaker='Renamed';watchers.at(-1)();
    expect(asr.canApplySegmentTranscription()).toBe(true);await asr.applySegmentTranscription();
    expect(object.sentence).toBe('New');expect(object.speaker).toBe('Renamed');expect(state.editingSegments.value[0].sentence).toBe('Inserted');
});
it.each(['sentence','start_time','end_time'])('rejects stale %s instead of replacing manual edits',async key=>{
    await asr.retranscribeSegment(0);state.editingSegments.value[0][key]=key==='sentence'?'Manual':17;watchers.at(-1)();
    expect(asr.segmentTranscription.value.error).toBe('stale');expect(await asr.applySegmentTranscription()).toBe(false);
});
it('discards delayed response after record switch and cancels known task',async()=>{
    let resolve;fetch.mockImplementation(async(url,options)=>options?.method==='POST'?reply({job_id:'job'}):options?.method==='DELETE'?reply({}):new Promise(r=>resolve=r));
    const pending=asr.retranscribeSegment(0);await Promise.resolve();await Promise.resolve();await Promise.resolve();
    state.selectedRecording.value={id:10};watchers[0]();resolve(reply({status:'done',text:'Late',start:10,end:18}));await pending;
    expect(asr.segmentTranscription.value).toBeNull();expect(state.editingSegments.value[0].sentence).toBe('Old');
    expect(fetch.mock.calls.some(([,opts])=>opts?.method==='DELETE')).toBe(true);
});
it('cancel while running stops polling and leaves text intact',async()=>{
    fetch.mockImplementation(async(url,opts)=>opts?.method==='POST'?reply({job_id:'job'}):reply({status:'running'}));
    await asr.retranscribeSegment(0);asr.closeSegmentTranscription();const calls=fetch.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10000);expect(fetch).toHaveBeenCalledTimes(calls);expect(state.editingSegments.value[0].sentence).toBe('Old');
});
it('missing/empty/error result never becomes an empty replacement',async()=>{
    fetch.mockImplementation(async(url,opts)=>opts?.method==='POST'?reply({job_id:'job'}):reply({status:'done',text:' ',start:10,end:18}));
    await asr.retranscribeSegment(0);expect(asr.segmentTranscription.value.error).toBe('provider');expect(asr.canApplySegmentTranscription()).toBe(false);
});
it('permission/audio/range guards and busy status preserve draft',async()=>{
    state.selectedRecording.value.can_edit=false;expect(asr.canTranscribeSegment(0)).toBe(false);
    state.selectedRecording.value.can_edit=true;state.editingSegments.value[0].end_time=400;expect(asr.canTranscribeSegment(0)).toBe(false);
    state.editingSegments.value[0].end_time=18;fetch.mockResolvedValue(reply({code:'busy'},false));
    await asr.retranscribeSegment(0);expect(asr.segmentTranscription.value.error).toBe('busy');expect(state.editingSegments.value[0].sentence).toBe('Old');
});
