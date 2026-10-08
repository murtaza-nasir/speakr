import assert from 'node:assert/strict';
import {writeFile,mkdir} from 'node:fs/promises';
const {chromium}=await import(process.env.SPEAKR_PLAYWRIGHT_MODULE || 'playwright');
const browser=await chromium.launch({executablePath:process.env.SPEAKR_CHROMIUM_PATH});
const segments=Array.from({length:1200},(_,i)=>({speaker:`SPEAKER_0${i%3}`,start_time:i*2,end_time:i*2+1.8,sentence:('Длинная реплика для сравнения полного списка и изменения имени. ').repeat(5)+i}));
const results=[];
try{
 for(const [name,base] of [['before',process.env.SPEAKR_BASELINE_URL||'http://127.0.0.1:8918'],['after',process.env.SPEAKR_URL||'http://127.0.0.1:8919']]){
  assert(['127.0.0.1','localhost'].includes(new URL(base).hostname)&&!['8898','8899'].includes(new URL(base).port));
  const c=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});
  const f=await(await c.request.post(base+'/__asr-split-fixture',{data:{segments,autosave:false,spectrogram:false}})).json();assert(f.isolated);
  const p=await c.newPage();await p.goto(base);await p.locator('h4').filter({hasText:f.title}).first().click();
  await p.waitForFunction(()=>!!document.querySelector('#app')._vnode.component.proxy.selectedRecording?.transcription);
  const times=await p.evaluate(async name=>{
   const app=document.querySelector('#app')._vnode.component.proxy;
   const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
   const measure=async operation=>{const start=performance.now();await operation();await Vue.nextTick();await frame();return performance.now()-start;};
   const open=await measure(()=>app.openSpeakerModal());
   const typing=[],filter=[],scroll=[],selection=[],mode=[];
   for(let i=0;i<12;i++){
    typing.push(await measure(()=>app.speakerMap.SPEAKER_00.name='Анна '+i));
    filter.push(await measure(()=>{app.selectSpeaker(i%2?'SPEAKER_01':'SPEAKER_02');}));
    scroll.push(await measure(()=>{app.speakerModalTranscriptRef.scrollTop=(i%2)*app.speakerModalTranscriptRef.scrollHeight;}));
   }
   // Compare the actual persistent user transitions. Before: close the
   // speaker screen, open ASR, then return. After: switch the shared panes.
   if(name==='before'){app.closeSpeakerModal();await app.openAsrEditorModal();}
   else await app.openSegmentEditor(0);
   await Vue.nextTick();await frame();
   for(let i=0;i<12;i++){
    selection.push(await measure(()=>app.selectAsrSegment(i%2?300:600)));
    mode.push(await measure(async()=>{
     if(name==='before'){
      app.closeAsrEditorModal();await Vue.nextTick();await app.openSpeakerModal();
      await Vue.nextTick();app.closeSpeakerModal();await Vue.nextTick();await app.openAsrEditorModal();
     }else{
      app.backToWorkspaceSpeakers();await Vue.nextTick();await app.openSegmentEditor(app.asrSelectedIndex);
     }
    }));
   }
   const median=values=>values.slice(2).sort((a,b)=>a-b)[Math.floor((values.length-2)/2)];
   return {open,typing:median(typing),filter:median(filter),scroll:median(scroll),selection:median(selection),mode:median(mode),segments:app.editingSegments.length};
  },name);
  assert.equal(times.segments,1200);results.push({name,...times});await p.close();await c.close();
 }
 const comparison=Object.fromEntries(['typing','filter','scroll','selection','mode'].map(key=>[key,{before:results[0][key],after:results[1][key],ratio:results[1][key]/results[0][key]}]));
 await mkdir('output/playwright/unified-workspace',{recursive:true});
 await writeFile('output/playwright/unified-workspace/performance.json',JSON.stringify({results,comparison,viewport:'1440x1000',fixture:'1200 full long utterances',threshold:1.2,warmup:2,trials:12,mode:'ASR -> speakers -> ASR roundtrip',opening:'First opening includes the new workspace_context request; reported separately from persistent interactions.'},null,2));
 console.log(JSON.stringify(comparison));
 assert(Object.values(comparison).every(value=>value.ratio<=1.2),'Persistent interaction latency regression exceeds 20%');
}finally{await browser.close();}
