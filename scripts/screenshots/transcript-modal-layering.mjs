import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
const {chromium}=await import(process.env.SPEAKR_PLAYWRIGHT_MODULE||'playwright');
const base=process.env.SPEAKR_URL||'http://127.0.0.1:8919';
assert(['localhost','127.0.0.1'].includes(new URL(base).hostname)&&!['8898','8899'].includes(new URL(base).port));
const out='output/playwright/modal-layering';await mkdir(out,{recursive:true});
const common=process.env.SPEAKR_MODAL_COMMON==='1';
const browser=await chromium.launch();const results=[];
try{
 for(const [width,dark] of [[1440,false],[390,true]]){
  const context=await browser.newContext({viewport:{width,height:width<500?844:1000},locale:'ru-RU',serviceWorkers:'block',recordVideo:{dir:out}});
  const fixture=await(await context.request.post(base+'/__asr-split-fixture',{data:{count:60,autosave:false,spectrogram:true,audio_duration:90,database_speakers:['Анна','Борис']}})).json();assert(fixture.isolated);
  const page=await context.newPage(),errors=[],writes=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.method()==='POST'&&/\/(update_transcript|update_speakers|update_transcription)$/.test(new URL(r.url()).pathname))writes.push(r.url());});
  const app=fn=>page.evaluate(fn);
  const auxiliary=()=>page.locator('#app > .modal-overlay:not([data-testid="asr-editor"])');
  const hit=async locator=>{
   await locator.waitFor({state:'visible'});
   await locator.scrollIntoViewIfNeeded();
   // Bottom-sheet transforms can still be animating when visible becomes true.
   await locator.click({trial:true,timeout:5000});
   const value=await locator.evaluate(e=>{const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,top=document.elementFromPoint(x,y);return {hit:!!top&&(e===top||e.contains(top)),top:top?.outerHTML.slice(0,180),x,y};});
   assert(value.hit,'Popup is covered: '+JSON.stringify(value));return value;
  };
  const snapshot=()=>app(()=>{const a=document.querySelector('#app')._vnode.component.proxy;return {draft:JSON.stringify(a.editingSegments),selected:a.asrSelectedIndex,mode:a.workspaceMode,speaker:a.selectedSpeaker,onlySelected:a.onlySelectedSpeaker,scroll:a.speakerModalTranscriptRef?.scrollTop??0};});
  const clean=()=>app(()=>{const a=document.querySelector('#app')._vnode.component.proxy;for(const flag of ['showAddSpeakerModal','showEditModal','showDeleteModal','showEditTagsModal','showDateTimePicker','showSharesListModal','showUploadModal','showEditSpeakersModal','showColorSchemeModal','showSystemAudioHelp','manualVoiceOpen'])if(flag in a)a[flag]=false;document.querySelectorAll('#organizerModals > .modal-overlay').forEach(e=>e.classList.add('hidden'));document.querySelectorAll('#toastContainer .toast-message').forEach(e=>e.remove());});
  async function check(name,run){
   try{await run();results.push({name,width,dark,status:'PASS'});}
   catch(error){await page.screenshot({path:`${out}/${width}-${name}-failure.png`});results.push({name,width,dark,status:'FAIL',error:error.stack});}
   finally{await clean();}
   await writeFile(out+'/result.json',JSON.stringify(results,null,2));
  }
  try{
   await page.goto(base,{waitUntil:'domcontentloaded',timeout:120000});
   if(dark)await app(()=>document.documentElement.classList.add('dark'));
   if(width<500)await page.locator('header button').first().click();
   await page.locator('h4').filter({hasText:fixture.title}).first().click();
   await page.waitForFunction(()=>!!document.querySelector('#app')._vnode.component.proxy.selectedRecording?.transcription);
   await app(()=>document.querySelector('#app')._vnode.component.proxy.openSpeakerModal());
   await page.getByTestId('workspace-speakers').waitFor();
   await app(()=>{const a=document.querySelector('#app')._vnode.component.proxy;a.editingSegments[0].sentence='Несохранённый черновик для проверки слоёв.';a.selectSpeaker('Анна');a.onlySelectedSpeaker=true;});
   await check('add-speaker-ordinary-click',async()=>{
    const before=await snapshot();await page.locator('.spk-add').click();
    const input=auxiliary().locator('input[type="text"]').first();await hit(input);await input.fill('Изолированное имя');
    await page.screenshot({path:`${out}/${width}-add-speaker.png`});await auxiliary().locator('.modal-close').click();
    assert.deepEqual(await snapshot(),before);
   });
   await app(()=>document.querySelector('#app')._vnode.component.proxy.openSegmentEditor(3));
   await page.getByTestId('asr-segment-text').waitFor();
   const cases=[['metadata',a=>a.openEditModal(a.selectedRecording)],['tags',a=>a.openEditTagsModal(a.selectedRecording)],['date-time',a=>{a.showDateTimePicker=true;}],['delete-confirmation',a=>a.confirmDelete(a.selectedRecording)],['shares',a=>{a.showSharesListModal=true;}],['upload',a=>{a.showUploadModal=true;}],['colors',a=>{a.showColorSchemeModal=true;}],['audio-help',a=>{a.showSystemAudioHelp=true;}]];
   for(const [name,open] of cases)await check(name+'-above-editor',async()=>{
    const before=await snapshot();await page.evaluate(source=>{const a=document.querySelector('#app')._vnode.component.proxy;return (0,eval)('('+source+')')(a);},open.toString());
    const close=auxiliary().locator('.modal-close').first();await hit(close);await close.click();assert.deepEqual(await snapshot(),before);
   });
   await check('child-shortcut-does-not-save-editor',async()=>{
    await app(()=>document.querySelector('#app')._vnode.component.proxy.openEditModal(document.querySelector('#app')._vnode.component.proxy.selectedRecording));
    const input=auxiliary().locator('input:not([type]),input[type="text"]').first();await input.focus();
    const count=writes.length;await page.keyboard.press('Control+s');await page.waitForTimeout(400);
    assert.equal(writes.length,count,'Ctrl+S in child dialog saved underlying transcript');
   });
   if(common){assert.equal(await page.getByTestId('manual-voice-open').count(),0);results.push({name:'manual-voice-and-toast',width,status:'NA',reason:'Common C01-C11 layer has no manual-voice C12-C15 panel'});}
   else await check('manual-voice-and-toast',async()=>{
    await page.getByTestId('manual-voice-open').click();await hit(page.getByTestId('manual-voice-close'));
    await app(async()=>{const {showToast}=await import('/static/js/modules/utils/toast.js');showToast('Проверка уведомления над диалогом','fa-info-circle',15000);});
    const toast=page.locator('#toastContainer .toast-message').last();await toast.waitFor();
    await page.waitForFunction(()=>{const e=document.querySelector('#toastContainer .toast-message');if(!e)return false;const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&getComputedStyle(e).opacity==='1';});await hit(toast);
    await page.screenshot({path:`${out}/${width}-manual-toast.png`});await toast.click();await toast.waitFor({state:'detached'});await page.getByTestId('manual-voice-close').click();
   });
   for(const kind of ['Tag','Folder'])await check('upload-create-'+kind.toLowerCase(),async()=>{
    assert(await app(()=>!!window.SpeakrOrganizer),'Organizer must be available in this fixture');
    await app(()=>{document.querySelector('#app')._vnode.component.proxy.showUploadModal=true;});
    await page.evaluate(kind=>document.querySelector('#app')._vnode.component.proxy[kind==='Tag'?'createTagFromUploadSearch':'createFolderFromUpload'](),kind);
    const input=page.locator('#'+kind.toLowerCase()+'Name');await hit(input);await input.fill('Изолированный тест');
    await page.screenshot({path:`${out}/${width}-organizer-${kind}.png`});await page.locator('#close'+kind+'ModalBtn').click();
    await hit(auxiliary().locator('.modal-close').first());await auxiliary().locator('.modal-close').first().click();
   });
   await check('speaker-suggestions-above-child',async()=>{
    // The ordinary names command intentionally returns to the shared speaker pane.
    // Seed its retained dialog only to characterize the body-teleported layer.
    await app(async()=>{const a=document.querySelector('#app')._vnode.component.proxy;a.editingSpeakersList=[{original:'Анна',current:'Анна'}];a.showEditSpeakersModal=true;await Vue.nextTick();a.editingSpeakerSuggestions={0:[{id:999,name:'Борис'}]};});
    const option=page.locator('body > [class~="z-[100]"] button').first();await hit(option);await option.click();
    await auxiliary().locator('.modal-close').click();
   });
   await check('editor-confirmation-still-works',async()=>{
    await app(()=>document.querySelector('#app')._vnode.component.proxy.requestAsrDelete(3));
    await hit(page.getByTestId('asr-delete-cancel'));await page.getByTestId('asr-delete-cancel').click();
   });
   await check('editor-shortcut-still-saves',async()=>{
    await app(()=>document.querySelector('#app')._vnode.component.proxy.openSegmentEditor(3));
    await page.getByTestId('asr-segment-text').fill('Обычное сохранение из самого редактора.');
    const saved=page.waitForRequest(r=>r.method()==='POST'&&r.url().endsWith('/update_transcript'),{timeout:5000});
    await page.keyboard.press('Control+s');await saved;
   });
   await check('closed-editor-original-layer',async()=>{
    await app(()=>{const a=document.querySelector('#app')._vnode.component.proxy;a.showAsrEditorModal=false;a.openEditModal(a.selectedRecording);});
    const overlay=auxiliary();assert.equal(await overlay.evaluate(e=>getComputedStyle(e).zIndex),'50');await hit(overlay.locator('.modal-close'));await overlay.locator('.modal-close').click();
   });
   assert.deepEqual(errors,[],'Unexpected Vue/browser errors');
  }finally{const video=page.video();await page.close();await context.close();await video.saveAs(`${out}/${width}-${dark?'dark':'light'}.webm`);}
 }
}finally{await browser.close();await writeFile(out+'/result.json',JSON.stringify(results,null,2));}
console.log(JSON.stringify(results.map(({error,...r})=>r)));assert(results.every(r=>r.status==='PASS'||r.status==='NA'),'Transcript modal layering regression');
