import {beforeEach,afterEach,it,expect,vi} from 'vitest';
import {useSavedSampleRange} from './saved-sample-range.js';
let state,ui,utils,watchers;
const id='11111111-1111-4111-8111-111111111111',ref=value=>({value});
beforeEach(()=>{
 vi.useFakeTimers();watchers=[];
 vi.stubGlobal('Vue',{ref,computed:fn=>({get value(){return fn();}}),watch:(_,cb)=>watchers.push(cb)});
 vi.stubGlobal('window',{addEventListener:vi.fn(),i18n:{t:k=>k}});
 vi.stubGlobal('document',{querySelector:()=>null,querySelectorAll:()=>[]});
 state={selectedRecording:ref({id:4,transcription:'plain original',duration:110}),showAsrEditorModal:ref(false)};
 utils={showToast:vi.fn(),openAtSegment:vi.fn(async()=>state.showAsrEditorModal.value=true),nextTick:async()=>{}};
 vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,json:async()=>({preview_id:id,lease:'a'.repeat(32),state:'ready',recording_id:4,start_ms:70000,end_ms:94000,expires_at:Date.now()+10000})})));
 ui=useSavedSampleRange(state,utils);
});
afterEach(()=>{ui.clearSavedSampleRange();vi.useRealTimers();vi.unstubAllGlobals();});
it('opens a read-only inspection for plain text and preserves recording and dirty draft',async()=>{
 state.draft=ref({sentence:'unsaved'});const before=JSON.stringify(state);
 await ui.openSavedSampleRange(2,id,4);
 expect(ui.savedSampleInspection.value).toBe(true);expect(utils.openAtSegment).not.toHaveBeenCalled();
 expect(ui.savedSampleRangeText.value).toBe('00:01:10.000–00:01:34.000');expect(JSON.stringify(state)).toBe(before);
});
it('opens the first overlapping row while keeping exact independent bounds across all rows',async()=>{
 state.selectedRecording.value.transcription=JSON.stringify([{start_time:0,end_time:35},{start_time:35,end_time:72.5},{start_time:72.5,end_time:94}]);
 const before=state.selectedRecording.value.transcription;await ui.openSavedSampleRange(2,id,4);
 expect(utils.openAtSegment).toHaveBeenCalledWith(1);expect(ui.savedSampleRowIntersects({start:72.5,end:94})).toBe(true);
 expect(state.selectedRecording.value.transcription).toBe(before);
});
it('clears stale selection and releases only this opening when recording changes',async()=>{
 await ui.openSavedSampleRange(2,id,4);state.selectedRecording.value.id=5;watchers[0]();
 expect(ui.savedSampleRange.value).toBeNull();expect(ui.savedSampleInspection.value).toBe(false);
 expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining('lease='),expect.objectContaining({method:'DELETE'}));
});
it('rejects a link pointing to a different recording without applying any selection',async()=>{
 await ui.openSavedSampleRange(2,id,8);
 expect(ui.savedSampleRange.value).toBeNull();expect(utils.openAtSegment).not.toHaveBeenCalled();expect(ui.savedSampleError.value).toBe('access');
});
it('focuses and traps the read-only fallback, closes on Escape and restores its opener',async()=>{
 const opener={isConnected:true,focus:vi.fn()},panel={focus:vi.fn()},first={focus:vi.fn()},last={focus:vi.fn()};
 document.activeElement=opener;document.querySelector=()=>panel;
 await ui.openSavedSampleRange(2,id,4);expect(panel.focus).toHaveBeenCalledOnce();
 document.activeElement=last;
 const event={key:'Tab',stopPropagation:vi.fn(),preventDefault:vi.fn(),currentTarget:{querySelectorAll:()=>[first,last]}};
 ui.handleSavedInspectionKeydown(event);expect(first.focus).toHaveBeenCalledOnce();expect(event.preventDefault).toHaveBeenCalledOnce();
 ui.handleSavedInspectionKeydown({...event,key:'Escape'});expect(ui.savedSampleInspection.value).toBe(false);expect(opener.focus).toHaveBeenCalledOnce();
});
it('clears a ready range and empty inspection on detected audio denial and releases its lease',async()=>{
 await ui.openSavedSampleRange(2,id,4);const src=ui.savedSampleRange.value.audioUrl;ui.onSavedRangeAudioError({target:{isConnected:true,getAttribute:()=>src}});
 expect(ui.savedSampleRange.value).toBeNull();expect(ui.savedSampleInspection.value).toBe(false);
 expect(ui.savedSampleError.value).toBe('unavailable');expect(utils.showToast).toHaveBeenCalledWith('manualVoice.playbackErrors.unavailable');
 expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining('lease='),expect.objectContaining({method:'DELETE'}));
});
it('does not let a stale audio error clear the newly opened sample lease',async()=>{
 await ui.openSavedSampleRange(2,id,4);const old=ui.savedSampleRange.value.audioUrl;
 fetch.mockResolvedValue({ok:true,json:async()=>({preview_id:id,lease:'b'.repeat(32),state:'ready',recording_id:4,start_ms:70000,end_ms:94000,expires_at:Date.now()+10000})});
 await ui.openSavedSampleRange(2,id,4);const current=ui.savedSampleRange.value;
 ui.onSavedRangeAudioError({target:{isConnected:true,getAttribute:()=>old}});
 expect(ui.savedSampleRange.value).toBe(current);expect(ui.savedSampleInspection.value).toBe(true);
 expect(utils.showToast).not.toHaveBeenCalled();
});
it('does not focus a stale opening after a deferred nextTick',async()=>{
 let release;const pending=new Promise(resolve=>release=resolve),focus=vi.fn();utils.nextTick=()=>pending;document.querySelector=()=>({focus});
 const opening=ui.openSavedSampleRange(2,id,4);
 await vi.waitFor(()=>expect(ui.savedSampleInspection.value).toBe(true));
 state.selectedRecording.value.id=5;watchers[0]();release();await opening;expect(focus).not.toHaveBeenCalled();
});
