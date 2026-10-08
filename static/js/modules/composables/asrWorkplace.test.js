import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {boundaryExtension,serializeAsrDraft,useAsrWorkplace} from './asrWorkplace.js';
const ref=value=>({value});
let state,utils,tools,callbacks,editor,audio,watchers;
const segments=()=>[{speaker:'A',sentence:'First.',start_time:0,end_time:8},{speaker:'A',speaker_id:1,sentence:'Reply.',start_time:10,end_time:18,extra:{keep:true}},{speaker:'B',sentence:'Last.',start_time:20,end_time:28}];
beforeEach(()=>{
 watchers=[];vi.stubGlobal('Vue',{ref,computed:getter=>({get value(){return getter();}}),watch:(source,callback)=>watchers.push({source,callback})});
 audio={paused:true,duration:100,currentTime:0,pause:vi.fn(),play:vi.fn(async()=>{}),volume:1,muted:false};
 vi.stubGlobal('document',{querySelector:selector=>selector.includes('audio')?audio:null});vi.stubGlobal('window',{i18n:{t:key=>key}});
 state={editingSegments:ref(segments()),selectedRecording:ref({id:11,audio_ready:true,audio_duration:100}),showAsrEditorModal:ref(true),editorAutosave:ref(false),asrEditorRef:ref({scrollTop:0})};
 utils={nextTick:async()=>{},scrollAsrEditorToIndex:vi.fn(),showToast:vi.fn(),resetModalAudioState:vi.fn()};
 tools={spectrogram:ref(null),closeSpectrogram:vi.fn(),closeSegmentTranscription:vi.fn(),releaseSpectrogramPlayback:vi.fn(),spectrogramBoundary:()=>4};
 callbacks={close:vi.fn(),clearSplitSelection:vi.fn(),persist:vi.fn(async p=>({transcription:p}))};
 editor=useAsrWorkplace(state,utils,tools,callbacks);editor.start(1);
});
afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();});
describe('workplace draft contracts',()=>{
 it('a late rejected play cannot clear the resumed range or show a stale error',async()=>{
  let reject;audio.pause.mockImplementation(()=>{audio.paused=true;});
  audio.play.mockImplementationOnce(()=>{audio.paused=false;return new Promise((_,r)=>{reject=r;});}).mockImplementation(async()=>{audio.paused=false;});
  const first=editor.playAsrSpectrum(false);audio.currentTime=12;await editor.playAsrSpectrum(false);
  await editor.playAsrSpectrum(false);reject(new Error('interrupted'));await first;
  expect(utils.showToast).not.toHaveBeenCalled();audio.currentTime=18;editor.handleAsrWorkplaceTimeUpdate({target:audio});expect(audio.paused).toBe(true);
 });
 it('spectrum command toggles pause and resumes without resetting its segment context',async()=>{
  audio.play.mockImplementation(async()=>{audio.paused=false;});audio.pause.mockImplementation(()=>{audio.paused=true;});
  await editor.playAsrSpectrum(false);expect(audio.currentTime).toBe(10);
  audio.currentTime=13.5;expect(editor.asrSpectrumAction(false)).toBe('asrWorkplace.pause');
  await editor.playAsrSpectrum(false);expect(audio.paused).toBe(true);expect(audio.currentTime).toBe(13.5);
  expect(editor.asrSpectrumAction(false)).toBe('asrSpectrogram.resume');
  await editor.playAsrSpectrum(false);expect(audio.paused).toBe(false);expect(audio.currentTime).toBe(13.5);
 });
 it('marker command shares upper transport and changing command starts its own range',async()=>{
  tools.spectrogramBoundary=()=>14;audio.play.mockImplementation(async()=>{audio.paused=false;});audio.pause.mockImplementation(()=>{audio.paused=true;});
  await editor.playAsrSpectrum(false);audio.currentTime=12;
  await editor.playAsrSpectrum(true);expect(audio.currentTime).toBe(14);expect(editor.asrPlaybackMode.value).toBe('marker');
  audio.currentTime=16;await editor.playAsrWorkplace();expect(audio.paused).toBe(true);
  expect(editor.asrSpectrumAction(true)).toBe('asrSpectrogram.resume');await editor.playAsrSpectrum(true);expect(audio.currentTime).toBe(16);
  audio.currentTime=18;editor.handleAsrWorkplaceTimeUpdate({target:audio});expect(audio.paused).toBe(true);
  await editor.playAsrSpectrum(true);expect(audio.currentTime).toBe(14);
 });
 it('an active spectrum command remains a pause action after audio/marker invalidation',async()=>{
  tools.spectrogramBoundary=()=>14;audio.play.mockImplementation(async()=>{audio.paused=false;});audio.pause.mockImplementation(()=>{audio.paused=true;});
  await editor.playAsrSpectrum(true);tools.spectrogramBoundary=()=>NaN;state.selectedRecording.value.audio_deleted_at='deleted';
  expect(editor.asrSpectrumAction(true)).toBe('asrWorkplace.pause');await editor.playAsrSpectrum(true);expect(audio.paused).toBe(true);
 });
 it('extends one bound using current neighbors and preserves text/metadata/siblings',()=>{
  const before=state.editingSegments.value.map(s=>({...s}));
  expect(editor.extendAsrBoundary('start_time')).toBe(true);expect(editor.selected.value.start_time).toBe(8);
  state.editingSegments.value[2].start_time=21;expect(editor.extendAsrBoundary('end_time')).toBe(true);
  expect(editor.selected.value).toEqual({...before[1],start_time:8,end_time:21});expect(state.editingSegments.value[0]).toEqual(before[0]);
 });
 it('uses recording duration at last segment and never previous-session media metadata',async()=>{
  await editor.select(2);expect(editor.extendAsrBoundary('end_time')).toBe(true);expect(editor.selected.value.end_time).toBe(100);
  const detached={duration:500};editor.handleAsrWorkplaceMetadata({target:detached});expect(editor.asrDuration.value).toBe(100);
  state.selectedRecording.value={id:12};editor.start(2);expect(editor.asrDuration.value).toBeNull();expect(editor.extendAsrBoundary('end_time')).toBe(false);
 });
 it.each([[0,'start_time','noPrevious'],[1,'start_time','noGap'],[1,'end_time','noGap']])('rejects first and overlap (%s,%s)',(index,field,error)=>{
  const list=segments();if(index===1){list[0].end_time=11;list[2].start_time=17;}
  expect(boundaryExtension(list,index,field,100).error).toBe(error);
 });
 it('rejects read-only/unknown/invalid and can repair a zero-range without shrinking',()=>{
  expect(boundaryExtension(segments(),1,'start_time',100,false).error).toBe('readOnly');
  expect(boundaryExtension(segments(),2,'end_time',null).error).toBe('unknownDuration');
  const list=segments();list[1].start_time=list[1].end_time=10;
  expect(boundaryExtension(list,1,'start_time',100).target).toBe(8);
  list[0].end_time=NaN;expect(boundaryExtension(list,1,'start_time',100).error).toBe('invalidBounds');
 });
 it('acknowledges server links and ignores JSON whitespace/key-order differences',async()=>{
  editor.selected.value.sentence='New';const normalized=segments();normalized[1].sentence='New';normalized[1].speaker_id=2;
  callbacks.persist.mockResolvedValue({transcription:JSON.stringify(normalized,null,2)});
  expect(await editor.save(true)).toBe(true);expect(editor.selected.value.speaker_id).toBe(2);expect(editor.asrDirty.value).toBe(false);
  await editor.save(true);expect(callbacks.persist).toHaveBeenCalledOnce();
 });
 it('retains edits made during a save and permits only a single pending request',async()=>{
  editor.selected.value.sentence='Sent';let finish;callbacks.persist.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
  const a=editor.save(false);const b=editor.save(true);editor.selected.value.sentence='Newer';
  finish({transcription:serializeAsrDraft(segments().map((s,i)=>i===1?{...s,sentence:'Sent'}:s))});await a;await b;
  expect(editor.selected.value.sentence).toBe('Newer');expect(editor.asrDirty.value).toBe(true);expect(callbacks.close).not.toHaveBeenCalled();expect(callbacks.persist).toHaveBeenCalledOnce();
 });
 it('does not acknowledge or close a newer session after a late save',async()=>{
  editor.selected.value.sentence='Old request';let finish;callbacks.persist.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
  const pending=editor.save(false);state.selectedRecording.value={id:12};state.editingSegments.value=segments();editor.start(0);
  finish({transcription:'[]'});await pending;expect(editor.asrSaveState.value).toBe('saved');expect(editor.selected.value.sentence).toBe('First.');expect(callbacks.close).not.toHaveBeenCalled();
 });
 it('keeps failed saves open with the draft and error status',async()=>{
  editor.selected.value.sentence='Keep';callbacks.persist.mockResolvedValue(false);expect(await editor.save(false)).toBe(false);
  expect(editor.asrSaveState.value).toBe('error');expect(editor.selected.value.sentence).toBe('Keep');expect(callbacks.close).not.toHaveBeenCalled();
 });
 it('suspends autosave on close decision and discard cancels the pending timer',async()=>{
  vi.useFakeTimers();state.editorAutosave.value=true;editor.selected.value.sentence='Pending';
  watchers[0].callback();editor.requestClose();expect(editor.asrCloseDecision.value).toBe(true);expect(editor.asrEditingLocked.value).toBe(true);
  await vi.advanceTimersByTimeAsync(2500);expect(callbacks.persist).not.toHaveBeenCalled();editor.discardAsrDraft();expect(callbacks.close).toHaveBeenCalledOnce();
 });
 it('selection retains a draft and does not interrupt whole-recording playback',async()=>{
  editor.selected.value.sentence='Keep';editor.setAsrPlaybackMode('recording');await editor.playAsrWorkplace();
  await editor.select(2);expect(state.editingSegments.value[1].sentence).toBe('Keep');expect(audio.pause).not.toHaveBeenCalled();expect(editor.asrDirty.value).toBe(true);
 });
 it('bounded playback starts at selected range and stops at the actual end',async()=>{
  await editor.playAsrWorkplace();expect(audio.currentTime).toBe(10);audio.currentTime=18;editor.handleAsrWorkplaceTimeUpdate({target:audio});expect(audio.pause).toHaveBeenCalledOnce();
 });
 it('preserves pause/resume and paused seek within the same playback range',async()=>{
  await editor.playAsrWorkplace();audio.currentTime=12.5;await editor.playAsrWorkplace();expect(audio.currentTime).toBe(12.5);
  editor.seekAsrWorkplace({target:{value:'13.5'}});await editor.playAsrWorkplace();expect(audio.currentTime).toBe(13.5);
  await editor.select(2);await editor.playAsrWorkplace();expect(audio.currentTime).toBe(20);
 });
 it('does not start marker playback without a valid marker inside the selected segment',async()=>{
  editor.setAsrPlaybackMode('marker');expect(editor.asrCanPlayMarker.value).toBe(false);
  await editor.playAsrWorkplace();expect(audio.play).not.toHaveBeenCalled();
 });
 it('pauses bounded playback on an absolute seek outside the segment without clamping the seek',async()=>{
  await editor.playAsrWorkplace();editor.seekAsrWorkplace({target:{value:'4'}});
  expect(audio.pause).toHaveBeenCalled();expect(audio.currentTime).toBe(4);
  await editor.playAsrWorkplace();expect(audio.currentTime).toBe(10);
 });
});
