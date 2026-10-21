import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { useAsrSpectrogram } from './asrSpectrogram.js';
let state, spectrum, watchers, audio, manifests, serial, lifecycle, pageLifecycle, storedFrequency, storedGain;
const ref = value => ({value});
const tileBlob = (width=1024,height=384,size=24) => {
    const bytes=new Uint8Array(size);bytes.set([137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82]);
    const view=new DataView(bytes.buffer);view.setUint32(16,width);view.setUint32(20,height);
    return new Blob([bytes],{type:'image/png'});
};
const response = (width=1024) => ({ok:true, headers:new Headers({
    'X-Spectrogram-Start':'10','X-Spectrogram-End':'18','X-Spectrogram-Duration':'100','X-Spectrogram-Channels':'2'
}), blob:async()=>tileBlob(width)});
const json = data => ({ok:true,status:200,json:async()=>data});
const ready = (start=10,end=18,span=Math.min(60,Math.max(.25,end-start)),frequency='8000',gainDb=0) => {
    const tiles=[],step=Math.min(4*span,60);
    for(let offset=start;offset<end-1e-9;offset+=step){const stop=Math.min(end,offset+step),index=tiles.length;
        tiles.push({index,start:offset,end:stop,width:Math.ceil((stop-offset)/span*1024),contentStart:offset,contentEnd:stop,url:`/api/recordings/9/spectrogram/preparations/p${serial}/tiles/${index}`});}
    const data={id:`p${serial++}`,lease:'lease',status:'ready',manifest:{start,end,duration:Math.max(130,end),segmentStart:start,segmentEnd:end,span,channels:2,maxFrequency:frequency==='full'?24000:Number(frequency),plotWidth:1024,tiles}};
    data.manifest.gainDb=gainDb;manifests.set(data.id,data);return data;
};
const serve = async (url,options={}) => {
    if(options.method==='DELETE' || url.endsWith('/lease'))return json({status:'ok'});
    if(url.endsWith('/prepare')){const body=JSON.parse(options.body);return json(body.existing_id?manifests.get(body.existing_id):ready(body.start,body.end,body.span,body.frequency,body.gainDb));}
    if(url.includes('/tiles/')){
        const [id,index]=url.split('/preparations/')[1].split('/tiles/');
        return response(manifests.get(id)?.manifest.tiles[Number(index.split('?')[0])].width || 1024);
    }
    return json(manifests.get(url.split('/preparations/')[1]?.split('?')[0]));
};
const prepares = () => fetch.mock.calls.filter(([url])=>url.endsWith('/prepare'));
const generations = () => prepares().filter(([,options])=>!JSON.parse(options.body).existing_id);
it('commits brightness once, preserves zoom/pan/marker/audio and isolates cached levels',async()=>{
    await spectrum.openSpectrogram(0);await spectrum.navigateSpectrogram(.5);await spectrum.navigateSpectrogram(1,1);
    spectrum.setSpectrogramMarker(14);const window={...spectrum.spectrogram.value.window},count=generations().length;
    spectrum.draftSpectrogramGain('20');expect(generations()).toHaveLength(count);
    expect(spectrum.spectrogram.value.draftGainDb).toBe(20);expect(spectrum.spectrogram.value.appliedGainDb).toBe(0);
    await spectrum.commitSpectrogramGain();expect(generations()).toHaveLength(count+1);
    expect(spectrum.spectrogram.value.window).toEqual(window);expect(spectrum.spectrogram.value.marker).toBe(14);
    expect(spectrum.spectrogram.value.appliedGainDb).toBe(20);expect(audio.currentTime).toBe(14);
    expect(localStorage.setItem).toHaveBeenCalledWith('speakrSpectrogramGainDb','20');
    await spectrum.commitSpectrogramGain();expect(generations()).toHaveLength(count+1);
    await spectrum.resetSpectrogramGain();expect(spectrum.spectrogram.value.appliedGainDb).toBe(0);
    expect(generations()).toHaveLength(count+1);expect(spectrum.spectrogram.value.window).toEqual(window);
});
it.each(['0','5','10','15','20','25','30','35','40'])('restores brightness %s independently of frequency',async value=>{
    storedGain=value;storedFrequency='4000';spectrum=useAsrSpectrogram(state,{nextTick:async()=>{}});
    await spectrum.openSpectrogram(0);expect(spectrum.spectrogram.value.appliedGainDb).toBe(Number(value));
    expect(spectrum.spectrogram.value.frequency).toBe('4000');expect(JSON.parse(prepares().at(-1)[1].body).gainDb).toBe(Number(value));
});
it.each([null,'NaN','-5','45','1','20.0','','true'])('ignores invalid saved gain %s',async value=>{
    storedGain=value;spectrum=useAsrSpectrogram(state,{nextTick:async()=>{}});await spectrum.openSpectrogram(0);
    expect(spectrum.spectrogram.value.appliedGainDb).toBe(0);
});
it('retains applied pixels and requested preference on gain error/cancel then retries latest',async()=>{
    await spectrum.openSpectrogram(0);const url=spectrum.spectrogram.value.url;
    fetch.mockImplementation((u,o)=>u.endsWith('/prepare')?Promise.resolve({ok:false,status:503,json:async()=>({code:'timeout'})}):serve(u,o));
    spectrum.draftSpectrogramGain('20');await spectrum.commitSpectrogramGain();
    expect(spectrum.spectrogram.value.url).toBe(url);expect(spectrum.spectrogram.value.appliedGainDb).toBe(0);
    expect(spectrum.spectrogram.value.gainDb).toBe(20);expect(storedGain).toBe('20');
    spectrum.cancelSpectrogramPreparation();fetch.mockImplementation(serve);await spectrum.retrySpectrogram();
    expect(spectrum.spectrogram.value.appliedGainDb).toBe(20);
});
it('ignores a late gain response and refuses a mismatched nonzero manifest',async()=>{
    await spectrum.openSpectrogram(0);let resolve;
    fetch.mockImplementation((u,o)=>u.endsWith('/prepare')?new Promise(r=>{resolve=r;}):serve(u,o));
    spectrum.draftSpectrogramGain('20');const pending=spectrum.commitSpectrogramGain();
    for(let i=0;i<30&&!resolve;i++)await Promise.resolve();expect(resolve).toBeTypeOf('function');
    fetch.mockImplementation(serve);spectrum.draftSpectrogramGain('30');await spectrum.commitSpectrogramGain();
    resolve(json(ready(10,18,8,'8000',20)));await pending;
    expect(spectrum.spectrogram.value.appliedGainDb).toBe(30);expect(storedGain).toBe('30');
    fetch.mockImplementation((u,o)=>u.endsWith('/prepare')?Promise.resolve(json(ready(10,18,8,'8000',0))):serve(u,o));
    spectrum.draftSpectrogramGain('40');await spectrum.commitSpectrogramGain();
    expect(spectrum.spectrogram.value.error).toBe('media');expect(spectrum.spectrogram.value.appliedGainDb).toBe(30);
});
it('invalidates an older gain response even when a tile failure throttles the latest choice',async()=>{
    state.editingSegments.value[0].end_time=130;await spectrum.openSpectrogram(0);let finish;
    fetch.mockImplementation((u,o)=>u.endsWith('/prepare')?new Promise(r=>{finish=r;}):u.includes('/tiles/1')?Promise.resolve({ok:false,status:429,json:async()=>({code:'busy'})}):serve(u,o));
    spectrum.draftSpectrogramGain('20');const pending=spectrum.commitSpectrogramGain();for(let i=0;i<30&&!finish;i++)await Promise.resolve();
    expect(finish).toBeTypeOf('function');await spectrum.navigateSpectrogram(1,1);expect(spectrum.spectrogram.value.error).toBe('busy');
    spectrum.draftSpectrogramGain('30');await spectrum.commitSpectrogramGain();finish(json(ready(10,130,60,'8000',20)));await pending;
    expect(spectrum.spectrogram.value.appliedGainDb).toBe(0);expect(spectrum.spectrogram.value.gainDb).toBe(30);expect(storedGain).toBe('30');
});
it('debounces keyboard commits and clears pending timers on close',async()=>{
    await spectrum.openSpectrogram(0);vi.useFakeTimers();const count=generations().length;
    spectrum.draftSpectrogramGain('5');spectrum.queueSpectrogramGain();spectrum.draftSpectrogramGain('10');spectrum.queueSpectrogramGain();
    await vi.advanceTimersByTimeAsync(300);expect(spectrum.spectrogram.value.appliedGainDb).toBe(10);
    expect(generations()).toHaveLength(count+1);
    spectrum.draftSpectrogramGain('15');spectrum.queueSpectrogramGain();await spectrum.closeSpectrogram();
    await vi.advanceTimersByTimeAsync(500);expect(generations()).toHaveLength(count+1);vi.useRealTimers();
});
it('cancels a pointer draft without changing preference or preparing an image',async()=>{
    await spectrum.openSpectrogram(0);const count=generations().length;
    spectrum.draftSpectrogramGain('20');spectrum.cancelSpectrogramGainDraft();await spectrum.commitSpectrogramGain();
    expect(spectrum.spectrogram.value.draftGainDb).toBe(0);expect(storedGain).toBeNull();expect(generations()).toHaveLength(count);
});
it('cancels a queued keyboard commit before a pointer drag takes over',async()=>{
    await spectrum.openSpectrogram(0);vi.useFakeTimers();const count=generations().length;
    spectrum.draftSpectrogramGain('5');spectrum.queueSpectrogramGain();spectrum.beginSpectrogramGainPointer();spectrum.draftSpectrogramGain('20');
    await vi.advanceTimersByTimeAsync(500);expect(generations()).toHaveLength(count);expect(storedGain).toBeNull();
    await spectrum.commitSpectrogramGain();expect(generations()).toHaveLength(count+1);expect(spectrum.spectrogram.value.appliedGainDb).toBe(20);vi.useRealTimers();
});
it.each([null,'0',5,NaN])('rejects invalid applied manifest gain %s while keeping default pixels',async value=>{
    await spectrum.openSpectrogram(0);
    fetch.mockImplementation((u,o)=>{if(u.endsWith('/prepare')){const d=ready(10,18,8,'4000');d.manifest.gainDb=value;return Promise.resolve(json(d));}return serve(u,o);});
    await spectrum.setSpectrogramFrequency('4000');expect(spectrum.spectrogram.value.error).toBe('media');expect(spectrum.spectrogram.value.appliedGainDb).toBe(0);
});
it('retains brightness across lifecycle transitions even if preference IO throws',async()=>{
    localStorage.getItem.mockImplementation(()=>{throw new Error('blocked');});localStorage.setItem.mockImplementation(()=>{throw new Error('quota');});
    const first=watchers.length;spectrum=useAsrSpectrogram(state,{nextTick:async()=>{}});await spectrum.openSpectrogram(0);
    spectrum.draftSpectrogramGain('20');await spectrum.commitSpectrogramGain();
    state.showAsrEditorModal.value=false;await watchers[first]();state.showAsrEditorModal.value=true;
    await spectrum.openSpectrogram(1);expect(spectrum.spectrogram.value.appliedGainDb).toBe(20);
    state.selectedRecording.value={id:10,audio_path:'/other.wav'};await watchers[first]();
    await spectrum.openSpectrogram(0);expect(spectrum.spectrogram.value.appliedGainDb).toBe(20);
});
it('disables scale boundary actions and guards them without any requests',async()=>{
    await spectrum.openSpectrogram(0);
    expect(spectrum.canZoomSpectrogram(2)).toBe(false);expect(spectrum.canZoomSpectrogram(.5)).toBe(true);
    expect(spectrum.canFitSpectrogram()).toBe(false);let count=fetch.mock.calls.length;
    await spectrum.navigateSpectrogram(2);await spectrum.fitSpectrogram();expect(fetch).toHaveBeenCalledTimes(count);
    for(let i=0;i<5;i++)await spectrum.navigateSpectrogram(.5);
    expect(spectrum.spectrogram.value.span).toBe(.25);expect(spectrum.canZoomSpectrogram(.5)).toBe(false);
    count=fetch.mock.calls.length;await spectrum.navigateSpectrogram(.5);expect(fetch).toHaveBeenCalledTimes(count);
    expect(spectrum.canFitSpectrogram()).toBe(true);
});
it('fits a fractional long range, reuses the coarse representation and preserves audio and marker',async()=>{
    state.editingSegments.value[0].end_time=160.3;await spectrum.openSpectrogram(0);
    spectrum.setSpectrogramMarker(120);audio.currentTime=20;
    await spectrum.fitSpectrogram();expect(spectrum.spectrogram.value.span).toBeCloseTo(150.3);
    expect(spectrum.spectrogram.value.window).toEqual({start:10,end:160.3});expect(spectrum.canZoomSpectrogram(2)).toBe(false);
    expect(spectrum.canFitSpectrogram()).toBe(false);expect(audio.currentTime).toBe(20);expect(audio.pause).not.toHaveBeenCalled();
    await spectrum.navigateSpectrogram(.5);await spectrum.navigateSpectrogram(1,1);const count=generations().length;
    await spectrum.fitSpectrogram();expect(generations()).toHaveLength(count);expect(spectrum.spectrogram.value.marker).toBe(120);
    expect(spectrum.spectrogram.value.window).toEqual({start:10,end:160.3});
});
it('clamps the last zoom-out step to the complete non-power-of-two range',async()=>{
    state.editingSegments.value[0].end_time=23.7;await spectrum.openSpectrogram(0);
    await spectrum.navigateSpectrogram(.5);await spectrum.navigateSpectrogram(2);
    expect(spectrum.spectrogram.value.window.start).toBe(10);expect(spectrum.spectrogram.value.window.end).toBeCloseTo(23.7);
    expect(spectrum.canZoomSpectrogram(2)).toBe(false);
});
it('retries a failed full fit rather than the still-displayed detail scale',async()=>{
    state.editingSegments.value[0].end_time=130;await spectrum.openSpectrogram(0);await spectrum.navigateSpectrogram(.5);
    const window={...spectrum.spectrogram.value.window},url=spectrum.spectrogram.value.url;
    fetch.mockImplementation((address,options)=>address.endsWith('/prepare')?Promise.resolve({ok:false,status:503,json:async()=>({code:'timeout'})}):serve(address,options));
    await spectrum.fitSpectrogram();expect(spectrum.spectrogram.value.span).toBe(30);expect(spectrum.spectrogram.value.window).toEqual(window);
    expect(spectrum.spectrogram.value.url).toBe(url);expect(spectrum.canFitSpectrogram()).toBe(true);
    fetch.mockImplementation(serve);await spectrum.retrySpectrogram();expect(spectrum.spectrogram.value.span).toBe(120);
    expect(spectrum.spectrogram.value.window).toEqual({start:10,end:130});
});
it('explicit cancellation clears a failed fit retry intent',async()=>{
    state.editingSegments.value[0].end_time=130;await spectrum.openSpectrogram(0);await spectrum.navigateSpectrogram(.5);
    fetch.mockImplementation((address,options)=>address.endsWith('/prepare')?Promise.resolve({ok:false,status:503,json:async()=>({code:'timeout'})}):serve(address,options));
    await spectrum.fitSpectrogram();spectrum.cancelSpectrogramPreparation();fetch.mockImplementation(serve);
    await spectrum.retrySpectrogram();expect(spectrum.spectrogram.value.span).toBe(30);
});
beforeEach(() => {
    watchers=[];manifests=new Map();serial=0;lifecycle=new Map();pageLifecycle=new Map();
    storedFrequency=null;storedGain=null;
    vi.stubGlobal('localStorage',{getItem:vi.fn(key=>key==='speakrSpectrogramGainDb'?storedGain:storedFrequency),setItem:vi.fn((key,value)=>{if(key==='speakrSpectrogramGainDb')storedGain=value;else storedFrequency=value;})});
    vi.stubGlobal('Vue',{ref,watch:(source,cb)=>watchers.push(cb)});
    vi.spyOn(URL,'createObjectURL').mockReturnValue('blob:spectrum');
    vi.spyOn(URL,'revokeObjectURL').mockImplementation(()=>{});
    vi.stubGlobal('fetch',vi.fn(serve));
    audio={currentTime:0,pause:vi.fn(),play:vi.fn(async()=>{}),addEventListener:vi.fn(),removeEventListener:vi.fn()};
    vi.stubGlobal('document',{hidden:false,querySelector:vi.fn(()=>audio),addEventListener:vi.fn((name,handler)=>lifecycle.set(name,handler)),removeEventListener:vi.fn((name)=>lifecycle.delete(name))});
    vi.stubGlobal('window',{addEventListener:vi.fn((name,handler)=>pageLifecycle.set(name,handler)),removeEventListener:vi.fn((name)=>pageLifecycle.delete(name))});
    state={editingSegments:ref([{start_time:10,end_time:18,speaker:'A'}, {start_time:20,end_time:28,speaker:'B'}]),
        selectedRecording:ref({id:9,audio_path:'/audio.wav'}),showAsrEditorModal:ref(true),modalAudioCurrentTime:ref(0)};
    spectrum=useAsrSpectrogram(state,{nextTick:async()=>{},showToast:vi.fn()});
});

it.each(['cancel','close','range'])('discards a late full-fit response after %s and releases its lease',async change=>{
    state.editingSegments.value[0].end_time=130;await spectrum.openSpectrogram(0);await spectrum.navigateSpectrogram(.5);
    const previous={...spectrum.spectrogram.value.window};let complete;
    fetch.mockImplementation((url,options)=>url.endsWith('/prepare')?new Promise(resolve=>{complete=resolve;}):serve(url,options));
    const pending=spectrum.fitSpectrogram();for(let i=0;i<30&&!complete;i++)await Promise.resolve();
    expect(complete).toBeTypeOf('function');const finishOld=complete;
    if(change==='cancel')spectrum.cancelSpectrogramPreparation();
    else if(change==='close')await spectrum.closeSpectrogram();
    else {fetch.mockImplementation(serve);state.editingSegments.value[0].end_time=129;await watchers.at(-1)();}
    const late=ready(10,130,120);finishOld(json(late));await pending;
    if(change==='cancel'){expect(spectrum.spectrogram.value.window).toEqual(previous);expect(spectrum.spectrogram.value.span).toBe(30);}
    else if(change==='close')expect(spectrum.spectrogram.value).toBeNull();
    else {expect(spectrum.spectrogram.value.segmentEnd).toBe(129);expect(spectrum.spectrogram.value.span).toBe(60);}
    expect(fetch.mock.calls.some(([url,options])=>url.includes(late.id)&&options?.method==='DELETE')).toBe(true);
});

it('uses the actual EOF range for full fit and guards an already fitted tiny segment',async()=>{
    state.editingSegments.value[0].end_time=150;
    fetch.mockImplementation((url,options)=>{if(url.endsWith('/prepare')){const body=JSON.parse(options.body);return json(ready(body.start,130,body.span));}return serve(url,options);});
    await spectrum.openSpectrogram(0);await spectrum.fitSpectrogram();
    expect(spectrum.spectrogram.value.window).toEqual({start:10,end:130});expect(spectrum.spectrogram.value.span).toBe(120);
    expect(spectrum.canFitSpectrogram()).toBe(false);
    await spectrum.closeSpectrogram();state.editingSegments.value[0].end_time=10.1;fetch.mockImplementation(serve);
    await spectrum.openSpectrogram(0);const calls=fetch.mock.calls.length;
    expect(spectrum.canFitSpectrogram()).toBe(false);expect(spectrum.canZoomSpectrogram(.5)).toBe(false);
    await spectrum.fitSpectrogram();await spectrum.navigateSpectrogram(.5);expect(fetch).toHaveBeenCalledTimes(calls);
});
afterEach(async()=>{await spectrum.closeSpectrogram();vi.restoreAllMocks();vi.unstubAllGlobals();});

it.each(['2000','4000','8000','full'])('restores valid browser frequency %s on a fresh session',async frequency=>{
    storedFrequency=frequency;spectrum=useAsrSpectrogram(state,{nextTick:async()=>{}});
    await spectrum.openSpectrogram(0);expect(spectrum.spectrogram.value.frequency).toBe(frequency);
    expect(JSON.parse(prepares().at(-1)[1].body).frequency).toBe(frequency);
});
it.each([null,'garbage','16000','', '4000.0'])('ignores invalid stored frequency %s',async value=>{
    storedFrequency=value;spectrum=useAsrSpectrogram(state,{nextTick:async()=>{}});
    await spectrum.openSpectrogram(0);expect(spectrum.spectrogram.value.frequency).toBe('8000');
});
it('keeps the in-memory choice through lifecycle resets when storage is unavailable',async()=>{
    localStorage.getItem.mockImplementation(()=>{throw new Error('blocked');});
    localStorage.setItem.mockImplementation(()=>{throw new Error('quota');});
    const firstWatcher=watchers.length;spectrum=useAsrSpectrogram(state,{nextTick:async()=>{}});
    await spectrum.openSpectrogram(0);expect(spectrum.spectrogram.value.frequency).toBe('8000');
    await spectrum.setSpectrogramFrequency('4000');
    for(const change of [()=>{state.showAsrEditorModal.value=false;},()=>{state.selectedRecording.value={id:10,audio_path:'/next.wav'};},()=>{state.selectedRecording.value.audio_path='/changed.wav';}]){
        change();await watchers[firstWatcher]();state.showAsrEditorModal.value=true;
        await spectrum.openSpectrogram(0);expect(spectrum.spectrogram.value.frequency).toBe('4000');
    }
});
it('persists explicit choices, restores them after recreation and ignores repeated/invalid choices',async()=>{
    await spectrum.openSpectrogram(0);await spectrum.setSpectrogramFrequency('4000');
    expect(localStorage.setItem).toHaveBeenCalledWith('speakrSpectrogramFrequency','4000');
    const count=prepares().length;await spectrum.setSpectrogramFrequency('4000');await spectrum.setSpectrogramFrequency('nope');
    expect(prepares()).toHaveLength(count);expect(localStorage.setItem).toHaveBeenCalledTimes(1);
    await spectrum.closeSpectrogram();spectrum=useAsrSpectrogram(state,{nextTick:async()=>{}});
    await spectrum.openSpectrogram(1);expect(spectrum.spectrogram.value.frequency).toBe('4000');
});
it('labels the actual low-rate cap while retaining the requested browser range',async()=>{
    fetch.mockImplementation((url,options)=>{
        if(url.endsWith('/prepare')){const b=JSON.parse(options.body);const result=ready(b.start,b.end,b.span,b.frequency);result.manifest.maxFrequency=4000;return json(result);}
        return serve(url,options);
    });
    await spectrum.openSpectrogram(0);
    expect(spectrum.spectrogram.value.frequency).toBe('8000');expect(spectrum.spectrogramFrequencyLabel()).toBe('4 kHz');
    await spectrum.setSpectrogramFrequency('full');
    expect(spectrum.spectrogramFrequencyLabel()).toBe('4 kHz');expect(storedFrequency).toBe('full');
});
it('keeps the displayed-image axis and the new preference when preparation fails',async()=>{
    await spectrum.openSpectrogram(0);
    fetch.mockImplementation((url,options)=>url.endsWith('/prepare')?Promise.resolve({ok:false,status:503,json:async()=>({code:'timeout'})}):serve(url,options));
    await spectrum.setSpectrogramFrequency('4000');
    expect(spectrum.spectrogramFrequencyLabel()).toBe('8 kHz');expect(storedFrequency).toBe('4000');
    expect(spectrum.spectrogram.value.error).toBe('timeout');
    fetch.mockImplementation(serve);await spectrum.closeSpectrogram();await spectrum.openSpectrogram(1);
    expect(spectrum.spectrogram.value.frequency).toBe('4000');expect(spectrum.spectrogramFrequencyLabel()).toBe('4 kHz');
});

it('frequency changes preserve marker/window and cache windows separately',async()=>{
    await spectrum.openSpectrogram(0);spectrum.setSpectrogramMarker(14);const range={...spectrum.spectrogram.value.window};
    await spectrum.setSpectrogramFrequency('4000');expect(spectrum.spectrogram.value.maxFrequency).toBe(4000);
    expect(spectrum.spectrogram.value.window).toEqual(range);expect(spectrum.spectrogramBoundary(state.editingSegments.value[0])).toBe(14);
    await spectrum.setSpectrogramFrequency('8000');expect(generations()).toHaveLength(2);expect(spectrum.spectrogram.value.maxFrequency).toBe(8000);
});
it('late frequency result cannot overwrite a newer axis',async()=>{
    await spectrum.openSpectrogram(0);const pending=[];fetch.mockImplementation((url,options)=>url.endsWith('/prepare')?new Promise(r=>pending.push(r)):serve(url,options));
    const narrow=spectrum.setSpectrogramFrequency('4000');
    for(let i=0;i<20 && pending.length<1;i++)await Promise.resolve();
    const full=spectrum.setSpectrogramFrequency('full');
    for(let i=0;i<20 && pending.length<2;i++)await Promise.resolve();
    pending[1](json(ready(10,18,8,'full')));await full;
    pending[0](json(ready(10,18,8,'4000')));await narrow;expect(spectrum.spectrogram.value.maxFrequency).toBe(24000);expect(spectrum.spectrogram.value.frequency).toBe('full');
    expect(storedFrequency).toBe('full');expect(spectrum.spectrogramFrequencyLabel()).toBe('24 kHz');
});

it('marker is tied to object/bounds and never reused for another segment',async()=>{
    await spectrum.openSpectrogram(0);
    spectrum.setSpectrogramMarker(16.42);
    expect(spectrum.spectrogramBoundary(state.editingSegments.value[0])).toBe(16.42);
    expect(spectrum.spectrogramBoundary(state.editingSegments.value[1])).toBeUndefined();
    state.editingSegments.value[0].end_time=17;
    expect(spectrum.spectrogramBoundary(state.editingSegments.value[0])).toBeNaN();
});
it('invalid marker prevents approximate fallback; reset explicitly restores it',async()=>{
    await spectrum.openSpectrogram(0);
    spectrum.setSpectrogramMarker(10);
    expect(spectrum.spectrogramBoundary(state.editingSegments.value[0])).toBeNaN();
    spectrum.resetSpectrogramMarker();
    expect(spectrum.spectrogramBoundary(state.editingSegments.value[0])).toBeUndefined();
});
it('retains same-object marker after text/speaker changes and index shifts',async()=>{
    await spectrum.openSpectrogram(0);
    const object=state.editingSegments.value[0];
    spectrum.setSpectrogramMarker(14);
    object.sentence='new text'; object.speaker='Other'; state.editingSegments.value.unshift({});
    watchers.at(-1)();
    expect(spectrum.spectrogram.value.speaker).toBe('Other');
    expect(spectrum.spectrogramBoundary(object)).toBe(14);
});
it('discards a late response after close before allocating a blob URL',async()=>{
    let resolve;
    fetch.mockImplementation((url,options)=>url.endsWith('/prepare')?new Promise(r=>resolve=r):serve(url,options));
    const pending=spectrum.openSpectrogram(0);
    for(let i=0;i<20 && !resolve;i++)await Promise.resolve();
    await spectrum.closeSpectrogram(); resolve(json(ready())); await pending;
    expect(spectrum.spectrogram.value).toBeNull();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
});
it('discards removed-object response and uses latest selection when requests overlap',async()=>{
    const requests=[];
    fetch.mockImplementation((url,options)=>url.endsWith('/prepare')?new Promise(resolve=>requests.push(resolve)):serve(url,options));
    const first=spectrum.openSpectrogram(0);
    for(let i=0;i<20 && requests.length<1;i++)await Promise.resolve();
    const second=spectrum.openSpectrogram(1);
    for(let i=0;i<20 && requests.length<2;i++)await Promise.resolve();
    requests[0](json(ready())); await first;
    expect(spectrum.spectrogram.value.segmentStart).toBe(20);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    state.editingSegments.value.splice(1,1);
    requests[1](json(ready(20,28))); await second;
    expect(URL.createObjectURL).not.toHaveBeenCalled();
});
it('uses one playback owner, pauses at end and cleans up listeners on close',async()=>{
    await spectrum.openSpectrogram(0);
    spectrum.setSpectrogramMarker(14);
    await spectrum.playSpectrogram(true);
    expect(audio.currentTime).toBe(14); expect(audio.play).toHaveBeenCalledOnce();
    audio.currentTime=18;
    audio.addEventListener.mock.calls.find(c=>c[0]==='timeupdate')[1]();
    expect(audio.pause).toHaveBeenCalledOnce();
    expect(audio.removeEventListener).toHaveBeenCalledWith('timeupdate',expect.any(Function));
    await spectrum.closeSpectrogram();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
});
it('blocks repeated busy requests until Retry-After and preserves draft objects',async()=>{
    const object=state.editingSegments.value[0];
    fetch.mockResolvedValue({ok:false,status:429,json:async()=>({code:'busy'})});
    await spectrum.openSpectrogram(0); await spectrum.retrySpectrogram();
    expect(fetch).toHaveBeenCalledOnce(); expect(spectrum.spectrogram.value.error).toBe('busy');
    expect(state.editingSegments.value[0]).toBe(object);
});
it('does not request deleted or incognito audio',async()=>{
    state.selectedRecording.value.audio_deleted_at='now';
    expect(spectrum.canShowSpectrogram(0)).toBe(false);
    await spectrum.openSpectrogram(0); expect(fetch).not.toHaveBeenCalled();
});
it('uses the detail API audio_available flag even when it omits audio_ready and audio_path',()=>{
    state.selectedRecording.value={id:9,audio_available:true};
    expect(spectrum.canShowSpectrogram(0)).toBe(true);
});
it('reuses cached local plots on row switching and purges every URL on modal close',async()=>{
    await spectrum.openSpectrogram(0); await spectrum.openSpectrogram(1); await spectrum.openSpectrogram(0);
    expect(generations()).toHaveLength(2);
    await spectrum.closeSpectrogram();
    expect(spectrum.spectrogramResourceUsage()).toEqual({compressedBytes:0,decodedBytes:0,representations:0});
});

it('prepares the full long range once, then pans and scrolls through only cached tile reads',async()=>{
    state.editingSegments.value[0].end_time=130;
    fetch.mockImplementation(async(url,options={})=>{
        if(url.endsWith('/prepare')) return {ok:true,status:200,json:async()=>({id:'ready',lease:'lease',status:'ready',manifest:{start:10,end:130,duration:130,segmentStart:10,segmentEnd:130,span:60,channels:2,maxFrequency:8000,plotWidth:1024,tiles:[{index:0,start:10,end:70,width:1024,contentStart:10,contentEnd:70,url:'/tile/0'},{index:1,start:70,end:130,width:1024,contentStart:70,contentEnd:130,url:'/tile/1'}]}})};
        return response();
    });
    await spectrum.openSpectrogram(0);
    expect(fetch.mock.calls.some(([url])=>url.endsWith('/prepare'))).toBe(true);
    const count=prepares().length;
    spectrum.setSpectrogramMarker(50);
    for(let i=0;i<5;i++) await spectrum.navigateSpectrogram(1,1);
    expect(spectrum.spectrogram.value.window.end).toBe(130);
    await spectrum.spectrogramScroll({currentTarget:{scrollLeft:0,clientWidth:320}});
    expect(spectrum.spectrogram.value.window.start).toBe(10);
    expect(spectrum.spectrogram.value.marker).toBe(50);
    expect(audio.pause).not.toHaveBeenCalled();
    expect(prepares()).toHaveLength(count);
});

it('keeps the ready picture and absolute marker after a failed new scale, and reuses a previous scale',async()=>{
    await spectrum.openSpectrogram(0);spectrum.setSpectrogramMarker(14);
    const first=spectrum.spectrogram.value.url;
    fetch.mockImplementation((url,options)=>url.endsWith('/prepare')?Promise.resolve({ok:false,status:413,json:async()=>({code:'limit'})}):serve(url,options));
    await spectrum.navigateSpectrogram(.5);
    expect(spectrum.spectrogram.value.url).toBe(first);expect(spectrum.spectrogram.value.window).toEqual({start:10,end:18});
    expect(spectrum.spectrogram.value.marker).toBe(14);expect(spectrum.spectrogram.value.error).toBe('limit');
    fetch.mockImplementation(serve);
    const calls=fetch.mock.calls.length;
    await spectrum.navigateSpectrogram(2);expect(fetch).toHaveBeenCalledTimes(calls);
    await spectrum.retrySpectrogram();expect(spectrum.spectrogram.value.span).toBe(4);
    await spectrum.navigateSpectrogram(2);
    expect(generations()).toHaveLength(3);expect(spectrum.spectrogram.value.error).toBe('');
});

it('reports expired cached reads without silently preparing on pan; only explicit retry rebuilds',async()=>{
    state.editingSegments.value[0].end_time=130;
    await spectrum.openSpectrogram(0);const count=prepares().length;
    fetch.mockImplementation((url,options)=>url.includes('/tiles/1')?Promise.resolve({ok:false,status:410,json:async()=>({code:'expired'})}):serve(url,options));
    await spectrum.navigateSpectrogram(1,1);
    expect(spectrum.spectrogram.value.error).toBe('expired');expect(prepares()).toHaveLength(count);
    await spectrum.navigateSpectrogram(1,-1);expect(prepares()).toHaveLength(count);
    fetch.mockImplementation(serve);await spectrum.retrySpectrogram();
    expect(prepares()).toHaveLength(count+1);expect(spectrum.spectrogram.value.error).toBe('');
});

it('renews pending and ready leases and releases them on close, including late accepted starts',async()=>{
    vi.useFakeTimers();
    try {
        const data=ready();let polls=0;
        fetch.mockImplementation((url,options)=>{
            if(url.endsWith('/prepare'))return Promise.resolve(json({...data,status:'preparing',manifest:null}));
            if(url.includes('/preparations/') && !url.includes('/tiles/') && !url.endsWith('/lease'))return Promise.resolve(json(++polls<110?{...data,status:'preparing',manifest:null}:data));
            return serve(url,options);
        });
        const pending=spectrum.openSpectrogram(0);
        await vi.advanceTimersByTimeAsync(30000);
        expect(fetch.mock.calls.filter(([url,options])=>url.endsWith('/lease')&&options.method==='POST')).toHaveLength(1);
        await vi.advanceTimersByTimeAsync(4000);await pending;
        expect(spectrum.spectrogram.value.loading).toBe(false);
        await spectrum.closeSpectrogram();
        expect(fetch.mock.calls.filter(([url,options])=>url.endsWith('/lease')&&options.method==='DELETE')).toHaveLength(1);
        expect(vi.getTimerCount()).toBe(0);
    } finally {vi.useRealTimers();}
});

it('keeps decoded images below sixteen MiB across long-detail pan and scale replacement',async()=>{
    state.editingSegments.value[0].end_time=130;
    await spectrum.openSpectrogram(0);
    for(let i=0;i<8;i++)await spectrum.navigateSpectrogram(.5);
    expect(spectrum.spectrogram.value.span).toBe(.25);
    for(let i=0;i<12;i++) {
        await spectrum.navigateSpectrogram(1,1);
        expect(spectrum.spectrogramResourceUsage().decodedBytes).toBeLessThanOrEqual(16*1024*1024);
    }
    await spectrum.spectrogramScroll({currentTarget:{scrollLeft:3584,clientWidth:1024}});
    await spectrum.setSpectrogramFrequency('4000');
    expect(spectrum.spectrogramResourceUsage().decodedBytes).toBeLessThanOrEqual(16*1024*1024);
    expect(spectrum.spectrogramTiles()).toHaveLength(2);
    await spectrum.closeSpectrogram();
    expect(spectrum.spectrogramResourceUsage()).toEqual({compressedBytes:0,decodedBytes:0,representations:0});
    expect(URL.revokeObjectURL.mock.calls.length).toBe(URL.createObjectURL.mock.calls.length);
});

it('evicts compressed tile bytes at sixty-four MiB and fetches evicted bytes without generation',async()=>{
    state.editingSegments.value[0].end_time=130;
    fetch.mockImplementation(async(url,options)=>{
        if(!url.includes('/tiles/'))return serve(url,options);
        const original=await serve(url,options),blob=await original.blob();
        const view=new DataView(await blob.arrayBuffer());
        return {ok:true,blob:async()=>tileBlob(view.getUint32(16),384,8*1024*1024)};
    });
    await spectrum.openSpectrogram(0);
    for(let i=0;i<8;i++)await spectrum.navigateSpectrogram(.5);
    const count=prepares().length;
    for(let i=0;i<30;i++) {
        await spectrum.navigateSpectrogram(1,1);
        expect(spectrum.spectrogramResourceUsage().compressedBytes).toBeLessThanOrEqual(64*1024*1024);
    }
    expect(prepares()).toHaveLength(count);
    expect(spectrum.spectrogram.value.error).toBe('');
});

it('resize changes only geometry and retains center, scale, marker and playing audio',async()=>{
    state.editingSegments.value[0].end_time=130;
    await spectrum.openSpectrogram(0);await spectrum.navigateSpectrogram(.5);
    spectrum.setSpectrogramMarker(42.01);await spectrum.navigateSpectrogram(1,1);
    const before={...spectrum.spectrogram.value.window},count=fetch.mock.calls.length;
    spectrum.spectrogramResize(320);
    expect(spectrum.spectrogram.value.window).toEqual(before);expect(spectrum.spectrogram.value.marker).toBe(42.01);
    expect(spectrum.spectrogram.value.span).toBe(30);expect(fetch).toHaveBeenCalledTimes(count);expect(audio.pause).not.toHaveBeenCalled();
    spectrum.clickSpectrogram({currentTarget:{getBoundingClientRect:()=>({left:10,top:0,width:320})},clientX:170,clientY:20});
    expect(spectrum.spectrogram.value.marker).toBe(Math.round((before.start+15)*100)/100);
});

it('cancels obsolete pending preparation without erasing the current ready scale',async()=>{
    await spectrum.openSpectrogram(0);const url=spectrum.spectrogram.value.url;
    const data=ready(10,18,4);
    fetch.mockImplementation((address,options)=>address.endsWith('/prepare')?Promise.resolve(json({...data,status:'queued',manifest:null})):serve(address,options));
    const pending=spectrum.navigateSpectrogram(.5);await Promise.resolve();await Promise.resolve();await Promise.resolve();
    spectrum.cancelSpectrogramPreparation();await pending;
    expect(spectrum.spectrogram.value.url).toBe(url);expect(spectrum.spectrogram.value.loading).toBe(false);
    expect(fetch.mock.calls.some(([address,options])=>address.endsWith(`/preparations/${data.id}/lease`)&&options.method==='DELETE')).toBe(true);
});

it('rejects cropped manifest coverage and oversize PNG while retaining the old representation',async()=>{
    await spectrum.openSpectrogram(0);const url=spectrum.spectrogram.value.url;
    const data=ready(10,18,4);data.manifest.tiles.at(-1).end=17;
    fetch.mockImplementation((address,options)=>address.endsWith('/prepare')?Promise.resolve(json(data)):serve(address,options));
    await spectrum.navigateSpectrogram(.5);
    expect(spectrum.spectrogram.value.error).toBe('media');expect(spectrum.spectrogram.value.url).toBe(url);
    fetch.mockImplementation((address,options)=>address.includes('/tiles/')?Promise.resolve({ok:true,blob:async()=>new Blob([new Uint8Array(8*1024*1024+1)],{type:'image/png'})}):serve(address,options));
    await spectrum.setSpectrogramFrequency('4000');
    expect(spectrum.spectrogram.value.error).toBe('media');expect(spectrum.spectrogram.value.url).toBe(url);
});

it('keeps the old ready picture when an explicit retry fails, and releases resources on later close',async()=>{
    await spectrum.openSpectrogram(0);const url=spectrum.spectrogram.value.url;
    fetch.mockImplementation((address,options)=>address.endsWith('/prepare')?Promise.resolve({ok:false,status:429,json:async()=>({code:'busy'})}):serve(address,options));
    await spectrum.retrySpectrogram();
    expect(spectrum.spectrogram.value.url).toBe(url);expect(spectrum.spectrogramResourceUsage().decodedBytes).toBeGreaterThan(0);
    expect(spectrum.spectrogram.value.error).toBe('busy');
    await spectrum.closeSpectrogram();expect(spectrum.spectrogramResourceUsage().compressedBytes).toBe(0);
});

it('ignores scrollbar clicks and the synthetic click following a native drag without moving marker',async()=>{
    state.editingSegments.value[0].end_time=130;await spectrum.openSpectrogram(0);
    spectrum.setSpectrogramMarker(20);
    const target={getBoundingClientRect:()=>({left:0,top:0,width:320}),querySelector:()=>({clientHeight:112,getBoundingClientRect:()=>({top:0})})};
    spectrum.clickSpectrogram({currentTarget:target,clientX:160,clientY:118});expect(spectrum.spectrogram.value.marker).toBe(20);
    await spectrum.spectrogramScroll({currentTarget:{scrollLeft:100,clientWidth:320}});
    spectrum.clickSpectrogram({currentTarget:target,clientX:160,clientY:80});expect(spectrum.spectrogram.value.marker).toBe(20);
});

it('does not allocate URLs after closing during a deferred DOM swap',async()=>{
    let unblock,hold=false;
    spectrum=useAsrSpectrogram(state,{nextTick:()=>hold?new Promise(resolve=>{unblock=resolve;}):Promise.resolve(),showToast:vi.fn()});
    fetch.mockImplementation(async(address,options)=>{const result=await serve(address,options);if(address.includes('/tiles/'))hold=true;return result;});
    const pending=spectrum.openSpectrogram(0);
    for(let i=0;i<20 && !unblock;i++)await Promise.resolve();
    expect(unblock).toBeTypeOf('function');await spectrum.closeSpectrogram();unblock();await pending;
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(spectrum.spectrogramResourceUsage()).toEqual({compressedBytes:0,decodedBytes:0,representations:0});
});

it('rejects PNG headers whose real dimensions exceed the manifest budget',async()=>{
    fetch.mockImplementation((address,options)=>address.includes('/tiles/')?Promise.resolve({ok:true,blob:async()=>tileBlob(16384,16384)}):serve(address,options));
    await spectrum.openSpectrogram(0);
    expect(spectrum.spectrogram.value.error).toBe('media');expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(spectrum.spectrogramResourceUsage().decodedBytes).toBe(0);
});

it('uses the actual scroll viewport height so the native scrollbar cannot crop bottom frequencies',async()=>{
    await spectrum.openSpectrogram(0);
    spectrum.spectrogramViewportRef({clientWidth:320,clientHeight:97,scrollLeft:0});
    expect(spectrum.spectrogramStripStyle().height).toBe('97px');
    spectrum.spectrogramResize(320,145);
    expect(spectrum.spectrogramStripStyle().height).toBe('145px');
    expect(spectrum.spectrogram.value.window).toEqual({start:10,end:18});
});

it('releases both retained and replacement bytes when closing during a same-key retry DOM swap',async()=>{
    let unblock,hold=false;
    spectrum=useAsrSpectrogram(state,{nextTick:()=>hold?new Promise(resolve=>{unblock=resolve;}):Promise.resolve(),showToast:vi.fn()});
    await spectrum.openSpectrogram(0);
    fetch.mockImplementation(async(address,options)=>{const result=await serve(address,options);if(address.includes('/tiles/'))hold=true;return result;});
    const pending=spectrum.retrySpectrogram();
    for(let i=0;i<20 && !unblock;i++)await Promise.resolve();
    expect(unblock).toBeTypeOf('function');await spectrum.closeSpectrogram();unblock();await pending;
    expect(spectrum.spectrogramResourceUsage()).toEqual({compressedBytes:0,decodedBytes:0,representations:0});
    expect(URL.revokeObjectURL.mock.calls.length).toBe(URL.createObjectURL.mock.calls.length);
});

it('renews only the active lease and releases inactive cache leases instead of pinning four scales',async()=>{
    vi.useFakeTimers();
    try {
        await spectrum.openSpectrogram(0);
        await spectrum.setSpectrogramFrequency('4000');await spectrum.setSpectrogramFrequency('2000');await spectrum.setSpectrogramFrequency('full');
        const released=fetch.mock.calls.filter(([url,options])=>url.endsWith('/lease')&&options.method==='DELETE');
        expect(released).toHaveLength(3);
        fetch.mockClear();await vi.advanceTimersByTimeAsync(30000);
        expect(fetch.mock.calls.filter(([url,options])=>url.endsWith('/lease')&&options.method==='POST')).toHaveLength(1);
        await spectrum.closeSpectrogram(false,false);
        expect(vi.getTimerCount()).toBe(0);expect(spectrum.spectrogramResourceUsage().compressedBytes).toBeGreaterThan(0);
        expect(lifecycle.size).toBe(0);expect(pageLifecycle.size).toBe(0);
    } finally {vi.useRealTimers();}
});

it('validates cache-only access after background expiry before reusing a locally cached tile',async()=>{
    state.editingSegments.value[0].end_time=130;await spectrum.openSpectrogram(0);
    const count=prepares().length;
    expect(lifecycle.get('visibilitychange')).toBeTypeOf('function');expect(pageLifecycle.get('pageshow')).toBeTypeOf('function');
    document.hidden=true;await lifecycle.get('visibilitychange')();
    fetch.mockImplementation((url,options)=>url.includes('/preparations/')&&!url.includes('/tiles/')&&!url.endsWith('/lease')?Promise.resolve({ok:false,status:410,json:async()=>({code:'expired'})}):serve(url,options));
    document.hidden=false;await lifecycle.get('visibilitychange')();
    await spectrum.navigateSpectrogram(1,1);
    expect(spectrum.spectrogram.value.error).toBe('expired');expect(prepares()).toHaveLength(count);
    expect(fetch.mock.calls.some(([url])=>url.includes('/preparations/')&&!url.includes('/tiles/')&&!url.endsWith('/lease'))).toBe(true);
});

it('keeps the previous viewport, image and marker throughout a deferred failed cache-only pan',async()=>{
    state.editingSegments.value[0].end_time=130;await spectrum.openSpectrogram(0);
    spectrum.setSpectrogramMarker(42);
    const before={...spectrum.spectrogram.value.window},url=spectrum.spectrogram.value.url,count=prepares().length;
    let rejectTile;
    fetch.mockImplementation((address,options)=>address.includes('/tiles/1')?new Promise(resolve=>{rejectTile=resolve;}):serve(address,options));
    const pending=spectrum.navigateSpectrogram(1,1);
    for(let i=0;i<20 && !rejectTile;i++)await Promise.resolve();
    expect(spectrum.spectrogram.value.window).toEqual(before);expect(spectrum.spectrogram.value.url).toBe(url);
    rejectTile({ok:false,status:503,json:async()=>({code:'unavailable'})});await pending;
    expect(spectrum.spectrogram.value.window).toEqual(before);expect(spectrum.spectrogram.value.url).toBe(url);
    expect(spectrum.spectrogram.value.marker).toBe(42);expect(prepares()).toHaveLength(count);
});

it('reacquires an inactive ready artifact by existing ID without a normal preparation or new tile generation',async()=>{
    await spectrum.openSpectrogram(0);const id=JSON.parse(prepares()[0][1].body).existing_id;
    expect(id).toBeUndefined();await spectrum.setSpectrogramFrequency('4000');
    const generated=generations().length,tiles=fetch.mock.calls.filter(([url])=>url.includes('/tiles/')).length;
    await spectrum.setSpectrogramFrequency('8000');
    expect(JSON.parse(prepares().at(-1)[1].body).existing_id).toBe('p0');
    expect(generations()).toHaveLength(generated);expect(fetch.mock.calls.filter(([url])=>url.includes('/tiles/'))).toHaveLength(tiles);
    expect(spectrum.spectrogram.value.maxFrequency).toBe(8000);
});

it('an expired inactive ready ID returns an explicit error and never falls back to a normal prepare',async()=>{
    await spectrum.openSpectrogram(0);await spectrum.setSpectrogramFrequency('4000');const url=spectrum.spectrogram.value.url,count=generations().length;
    fetch.mockImplementation((address,options)=>address.endsWith('/prepare')&&JSON.parse(options.body).existing_id?Promise.resolve({ok:false,status:410,json:async()=>({code:'expired'})}):serve(address,options));
    await spectrum.setSpectrogramFrequency('8000');
    expect(generations()).toHaveLength(count);expect(spectrum.spectrogram.value.error).toBe('expired');expect(spectrum.spectrogram.value.url).toBe(url);
});

it('a deferred pageshow validation gates cached tile reuse and keeps the prior viewport until expiry is known',async()=>{
    state.editingSegments.value[0].end_time=130;await spectrum.openSpectrogram(0);
    const before={...spectrum.spectrogram.value.window},url=spectrum.spectrogram.value.url,created=URL.createObjectURL.mock.calls.length;
    let complete;
    fetch.mockImplementation((address,options)=>address.includes('/preparations/')&&!address.includes('/tiles/')&&!address.endsWith('/lease')?new Promise(resolve=>{complete=resolve;}):serve(address,options));
    const resume=pageLifecycle.get('pageshow')();const pan=spectrum.navigateSpectrogram(1,1);
    for(let i=0;i<20 && !complete;i++)await Promise.resolve();
    expect(spectrum.spectrogram.value.window).toEqual(before);expect(spectrum.spectrogram.value.url).toBe(url);
    expect(URL.createObjectURL).toHaveBeenCalledTimes(created);
    complete({ok:false,status:410,json:async()=>({code:'expired'})});await resume;await pan;
    expect(spectrum.spectrogram.value.window).toEqual(before);expect(spectrum.spectrogram.value.error).toBe('expired');
    expect(prepares()).toHaveLength(1);
});

it('a late lifecycle validation cannot renew a released lease or recreate listeners after close',async()=>{
    await spectrum.openSpectrogram(0);let complete;
    fetch.mockImplementation((address,options)=>address.includes('/preparations/')&&!address.includes('/tiles/')&&!address.endsWith('/lease')?new Promise(resolve=>{complete=resolve;}):serve(address,options));
    const resume=pageLifecycle.get('pageshow')();await spectrum.closeSpectrogram();fetch.mockClear();complete(json(manifests.get('p0')));await resume;
    expect(fetch).not.toHaveBeenCalled();expect(lifecycle.size).toBe(0);expect(pageLifecycle.size).toBe(0);
    expect(spectrum.spectrogramResourceUsage()).toEqual({compressedBytes:0,decodedBytes:0,representations:0});
});

it('keeps old images visible during a native scrollbar cache miss and restores the old native position on failure',async()=>{
    state.editingSegments.value[0].end_time=130;await spectrum.openSpectrogram(0);
    const scroller={clientWidth:320,clientHeight:97,scrollLeft:0};spectrum.spectrogramViewportRef(scroller);await Promise.resolve();
    const before={...spectrum.spectrogram.value.window},url=spectrum.spectrogram.value.url;let complete;
    fetch.mockImplementation((address,options)=>address.includes('/tiles/1')?new Promise(resolve=>{complete=resolve;}):serve(address,options));
    scroller.scrollLeft=160;const pending=spectrum.spectrogramScroll({currentTarget:scroller});
    for(let i=0;i<20 && !complete;i++)await Promise.resolve();
    expect(spectrum.spectrogram.value.window).toEqual(before);expect(spectrum.spectrogram.value.url).toBe(url);
    expect(spectrum.spectrogramTiles()[0].left-scroller.scrollLeft).toBe(0);
    complete({ok:false,status:503,json:async()=>({code:'unavailable'})});await pending;
    expect(scroller.scrollLeft).toBe(0);expect(spectrum.spectrogram.value.window).toEqual(before);expect(spectrum.spectrogram.value.url).toBe(url);
});

it.each(['selection','recording','modal','removed'])('does not reopen a stale segment after delayed lease release and %s change',async change=>{
    let selected=state.editingSegments.value[0];
    spectrum=useAsrSpectrogram(state,{nextTick:async()=>{},showToast:vi.fn(),isSelectedSegment:segment=>segment===selected});
    await spectrum.openSpectrogram(0);const count=prepares().length;let complete,deferred=false;
    fetch.mockImplementation((address,options)=>{if(options?.method==='DELETE'&&!deferred){deferred=true;return new Promise(resolve=>{complete=resolve;});}return serve(address,options);});
    const pending=spectrum.openSpectrogram(0);
    for(let i=0;i<20 && !complete;i++)await Promise.resolve();
    if(change==='selection')selected=state.editingSegments.value[1];
    if(change==='recording')state.selectedRecording.value={id:10,audio_available:true};
    if(change==='modal')state.showAsrEditorModal.value=false;
    if(change==='removed')state.editingSegments.value.splice(0,1);
    complete(json({status:'ok'}));await pending;
    expect(spectrum.spectrogram.value).toBeNull();expect(prepares()).toHaveLength(count);expect(lifecycle.size).toBe(0);
});

it('an explicit close invalidates an open awaiting a delayed lease release',async()=>{
    await spectrum.openSpectrogram(0);let complete,deferred=false;const count=prepares().length;
    fetch.mockImplementation((address,options)=>{if(options?.method==='DELETE'&&!deferred){deferred=true;return new Promise(resolve=>{complete=resolve;});}return serve(address,options);});
    const opening=spectrum.openSpectrogram(0);for(let i=0;i<20 && !complete;i++)await Promise.resolve();
    const closing=spectrum.closeSpectrogram();complete(json({status:'ok'}));await opening;await closing;
    expect(spectrum.spectrogram.value).toBeNull();expect(prepares()).toHaveLength(count);
});

it('only the newest overlapping open can create a panel or prepare after deferred release',async()=>{
    await spectrum.openSpectrogram(0);let complete,deferred=false;const count=prepares().length;
    fetch.mockImplementation((address,options)=>{if(options?.method==='DELETE'&&!deferred){deferred=true;return new Promise(resolve=>{complete=resolve;});}return serve(address,options);});
    const first=spectrum.openSpectrogram(0);for(let i=0;i<20 && !complete;i++)await Promise.resolve();
    const second=spectrum.openSpectrogram(1);complete(json({status:'ok'}));await first;await second;
    expect(spectrum.spectrogram.value.segmentStart).toBe(20);expect(prepares()).toHaveLength(count+1);
    expect(JSON.parse(prepares().at(-1)[1].body).start).toBe(20);
    fetch.mockImplementation(serve);
});

it('a newer invalid open still invalidates an older open waiting for lease release',async()=>{
    await spectrum.openSpectrogram(0);let complete,deferred=false;const count=prepares().length;
    fetch.mockImplementation((address,options)=>{if(options?.method==='DELETE'&&!deferred){deferred=true;return new Promise(resolve=>{complete=resolve;});}return serve(address,options);});
    const first=spectrum.openSpectrogram(0);for(let i=0;i<20 && !complete;i++)await Promise.resolve();
    await spectrum.openSpectrogram(-1);complete(json({status:'ok'}));await first;
    expect(spectrum.spectrogram.value).toBeNull();expect(prepares()).toHaveLength(count);
});

it('reopens the captured selected object at its new index after rows move during delayed release',async()=>{
    const segment=state.editingSegments.value[0];state.asrEditorHighlightIndex=ref(0);
    spectrum=useAsrSpectrogram(state,{nextTick:async()=>{},showToast:vi.fn(),isSelectedSegment:object=>object===segment});
    await spectrum.openSpectrogram(0);let complete,deferred=false;
    fetch.mockImplementation((address,options)=>{if(options?.method==='DELETE'&&!deferred){deferred=true;return new Promise(resolve=>{complete=resolve;});}return serve(address,options);});
    const opening=spectrum.openSpectrogram(0);for(let i=0;i<20 && !complete;i++)await Promise.resolve();
    state.editingSegments.value.unshift({start_time:1,end_time:2,speaker:'Other'});
    complete(json({status:'ok'}));await opening;
    expect(spectrum.spectrogram.value.segmentStart).toBe(10);expect(state.asrEditorHighlightIndex.value).toBe(1);
});
