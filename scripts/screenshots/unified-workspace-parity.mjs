import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
const {chromium}=await import(process.env.SPEAKR_PLAYWRIGHT_MODULE || 'playwright');
const base=process.env.SPEAKR_URL || 'http://127.0.0.1:8919';
assert(['localhost','127.0.0.1'].includes(new URL(base).hostname) && !['8898','8899'].includes(new URL(base).port));
const out='output/playwright/unified-workspace';await mkdir(out,{recursive:true});
const browser=await chromium.launch({executablePath:process.env.SPEAKR_CHROMIUM_PATH});
const results=[];
const segment=(speaker,start,end,sentence='Первая фраза. Ответ другого человека.')=>({speaker,start_time:start,end_time:end,sentence,extra:{preserve:true}});
async function scenario(name,options,run){
 const context=await browser.newContext({viewport:{width:options.width || (options.mobile?390:1440),height:options.mobile?844:1000},serviceWorkers:'block',locale:'ru-RU',recordVideo:{dir:out}});
 const fixture=await(await context.request.post(base+'/__asr-split-fixture',{data:{count:8,spectrogram:true,audio_duration:90,...options}})).json();assert(fixture.isolated);
 const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const app=callback=>page.evaluate(callback);
 try{
  await page.goto(base,{waitUntil:'domcontentloaded',timeout:120000});await page.locator('h4').filter({hasText:fixture.title}).first().waitFor({state:'attached'});
  if(options.mobile)await page.locator('header button').first().click();
  await page.locator('h4').filter({hasText:fixture.title}).first().click();
  await page.waitForFunction(()=>!!document.querySelector('#app')._vnode.component.proxy.selectedRecording?.transcription);
  await run(page,context,fixture,app);
  assert.deepEqual(errors,[]);await page.screenshot({path:`${out}/${name}.png`});results.push({name,status:'PASS'});
 }catch(error){await page.screenshot({path:`${out}/${name}-failure.png`});results.push({name,status:'FAIL',error:error.stack,errors});throw error;}
 finally{const video=page.video();await page.close();await context.close();await video.saveAs(`${out}/${name}.webm`);}
}
const open=async(page)=>{await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.openSpeakerModal());await page.getByTestId('workspace-speakers').waitFor();await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.asrCanEdit);};
try{
 for (const width of [390,320]) await scenario('mobile-two-modes-'+width,{mobile:true,width},async(page)=>{
  await open(page);await page.getByTestId('workspace-speaker-Анна').locator('.spk-row-head').click();
  await page.getByTestId('workspace-transcript').waitFor();await page.getByTestId('workspace-edit-0').click();
  await page.getByTestId('asr-segment-text').fill('Правка на телефоне.');
  await page.screenshot({path:out+`/mobile-editor-${width}.png`});
  assert(await page.getByTestId('asr-editor').evaluate(e=>e.scrollWidth<=e.clientWidth+1));
  await page.getByTestId('workspace-back').click();await page.getByTestId('workspace-edit-0').click();
  assert.equal(await page.getByTestId('asr-segment-text').inputValue(),'Правка на телефоне.');
  await page.getByTestId('asr-save').click();await page.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();
 });
 await scenario('tablet-rtl-two-modes',{width:1024,segments:[segment('Анна',0,8,'مرحبا بالعالم. Это русская реплика.')],count:1},async(page)=>{
  await open(page);await page.getByTestId('workspace-edit-0').click();
  assert(await page.getByTestId('asr-editor').evaluate(e=>e.scrollWidth<=e.clientWidth+1));
  assert.equal(await page.getByTestId('asr-segment-text').getAttribute('dir'),'auto');
  assert((await page.getByTestId('asr-segment-text').inputValue()).startsWith('مرحبا'));
  await page.getByTestId('workspace-back').click();assert(await page.getByTestId('workspace-speakers').isVisible());
 });
 await scenario('autosave-and-summary-once',{autosave:true},async(page,context,fixture)=>{
  const writes=[];page.on('request',r=>{if(r.url().endsWith('/update_transcript'))writes.push(r.postDataJSON());});
  await open(page);await page.getByTestId('workspace-speaker-Анна').locator('.spk-name input').fill('Борис');
  await page.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor({timeout:8000});
  assert.equal(writes.length,1);assert.equal(writes[0].regenerate_summary,false);
  await page.getByTestId('asr-save').click();await page.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();
  assert.equal(writes.length,2);assert.equal(writes[1].regenerate_summary,true);
  await page.getByTestId('asr-save').click();await page.waitForTimeout(200);assert.equal(writes.length,2);
  assert(JSON.parse((await(await context.request.get(base+`/api/recordings/${fixture.id}`)).json()).transcription).every(s=>s.speaker==='Борис'));
 });
 await scenario('speaker-tools-and-minor-merge',{segments:[segment('SPEAKER_00',0,40),segment('SPEAKER_01',40,75),segment('SPEAKER_02',75,76)],database_speakers:['Анна','Борис']},async(page)=>{
  await page.route('**/speakers/suggestions/*',r=>r.fulfill({json:{success:true,suggestions:{SPEAKER_00:[{name:'Анна',similarity:.95}]}}}));
  await open(page);await page.getByTestId('workspace-speaker-SPEAKER_00').locator('.spk-voice-pill').click();
  assert.equal(await page.getByTestId('workspace-speaker-SPEAKER_00').locator('.spk-name input').inputValue(),'Анна');
  await page.locator('.spk-minor-toggle').click();await page.getByTestId('workspace-speaker-SPEAKER_02').waitFor();
  await page.locator('.spk-minor-header select').selectOption('SPEAKER_01');
  await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.editingSegments[2].speaker==='SPEAKER_01');
  await page.getByTestId('workspace-speaker-SPEAKER_00').locator('.spk-me input').check();
  assert(await page.getByTestId('workspace-speaker-SPEAKER_00').locator('.spk-name input').isDisabled());
  await page.getByTestId('workspace-speaker-SPEAKER_00').locator('.spk-me input').uncheck();
  await page.route('**/auto_identify_speakers',r=>{assert(r.request().postDataJSON().transcript_data[2].speaker==='SPEAKER_01');return r.fulfill({json:{success:true,speaker_map:{SPEAKER_00:'Анна',SPEAKER_01:'Борис'}}});});
  await page.getByTestId('workspace-identify').click();
  await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.speakerMap.SPEAKER_01.name==='Борис');
  await page.getByTestId('workspace-speaker-SPEAKER_00').locator('.spk-row-head').click();
  await page.getByTestId('workspace-edit-0').click();
  assert.equal(await page.getByTestId('asr-segment-speaker').inputValue(),'Анна');
  await page.getByTestId('asr-segment-speaker').click();await page.getByTestId('asr-segment-speaker').fill('');
  assert((await page.locator('[role="option"]').allTextContents()).some(s=>s.includes('Борис')));
 });
 await scenario('real-segment-asr-boundaries-delete',{segments:[segment('Анна',1,8),segment('Борис',10,18),segment('Анна',20,28)]},async(page)=>{
  await open(page);await page.getByTestId('workspace-edit-1').click();
  await page.getByTestId('asr-extend-start').click();assert.equal(await page.getByTestId('asr-start').inputValue(),'8');
  await page.getByTestId('asr-extend-end').click();assert.equal(await page.getByTestId('asr-end').inputValue(),'20');
  await page.getByTestId('asr-retranscribe').click();await page.getByTestId('asr-retranscription-proposal').waitFor({timeout:60000});
  await page.getByTestId('asr-retranscription-proposal').fill('Проверенный новый текст.');await page.getByTestId('asr-retranscription-apply').click();
  assert.equal(await page.getByTestId('asr-segment-text').inputValue(),'Проверенный новый текст.');
  await page.locator('.sw-more summary').click();await page.getByTestId('workspace-editor').getByRole('button',{name:'Удалить сегмент',exact:true}).click();
  await page.getByTestId('asr-delete-cancel').click();assert.equal(await page.getByTestId('asr-detail').getAttribute('data-segment-index'),'1');
 });
 await scenario('mixed-label-provenance-real-vue',{segments:[segment('SPEAKER_00',0,8),segment('SPEAKER_01',10,18)],speaker_embeddings:{SPEAKER_00:Array(256).fill(.1),SPEAKER_01:Array(256).fill(.2)}},async(page,context,fixture)=>{
  const writes=[];page.on('request',r=>{if(r.url().endsWith('/update_transcript'))writes.push(r.postDataJSON());});
  await open(page);await page.getByTestId('workspace-edit-0').click();
  const text=page.getByTestId('asr-segment-text');await text.evaluate(e=>{e.focus();e.setSelectionRange(e.value.indexOf('Ответ'),e.value.indexOf('Ответ'));e.dispatchEvent(new Event('select',{bubbles:true}));});
  await text.press('Control+Enter');await page.getByTestId('asr-split-speaker-0').fill('Анна');await page.getByTestId('asr-split-speaker-1').fill('Борис');await page.getByTestId('asr-confirm-split').click();
  await page.getByTestId('asr-save').click();await page.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();
  assert.deepEqual(writes[0].workspace_effects.invalidated_voice_labels,['SPEAKER_00']);
  const metadata=(await(await context.request.get(base+`/recording/${fixture.id}/workspace_context`)).json());assert.deepEqual(metadata.voice_labels,['SPEAKER_01']);
 });
 await scenario('readonly-incognito-plaintext',{count:2},async(page)=>{
  await page.evaluate(()=>{const a=document.querySelector('#app')._vnode.component.proxy;a.selectedRecording.can_edit=false;});await openReadonly(page);
  assert(await page.getByTestId('asr-save').isDisabled());assert(await page.getByTestId('workspace-speaker-Анна').locator('.spk-name input').isDisabled());
  await page.evaluate(()=>{const a=document.querySelector('#app')._vnode.component.proxy;a.closeAsrEditorModal();a.selectedRecording.can_edit=true;a.selectedRecording.incognito=true;a.openAsrEditorAtSegment(0);});
  await page.getByTestId('asr-detail').waitFor();const writes=[];page.on('request',r=>{if(r.method()==='POST'&&/update_|auto_identify/.test(r.url()))writes.push(r.url());});
  await page.getByTestId('asr-segment-text').fill('Инкогнито.');await page.getByTestId('asr-save').click();await page.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();
  assert.equal(writes.length,0);
  await page.evaluate(()=>{const a=document.querySelector('#app')._vnode.component.proxy;a.closeAsrEditorModal();a.selectedRecording.transcription='[Анна]: Исходный текст.';a.openTranscriptionEditor();});
  await page.evaluate(async()=>{const a=document.querySelector('#app')._vnode.component.proxy;a.editingTranscriptionContent='[Анна]: Локальный текст.';await a.saveTranscription();});
  assert.equal(writes.length,0);assert((await page.evaluate(()=>JSON.parse(sessionStorage.getItem('speakr_incognito_recording')).transcription)).includes('Локальный'));
 });
 await scenario('legacy-untimed-json',{segments:[{speaker:'SPEAKER_00',sentence:'Старая запись без временных меток.',metadata:{keep:true}}]},async(page,context,fixture)=>{
  await open(page);await page.getByTestId('workspace-speaker-SPEAKER_00').locator('.spk-name input').fill('Анна');
  await page.getByTestId('asr-save').click();await page.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();
  const stored=JSON.parse((await(await context.request.get(base+`/api/recordings/${fixture.id}`)).json()).transcription);
  assert.equal(stored[0].speaker,'Анна');assert.equal(stored[0].metadata.keep,true);assert(!('start_time' in stored[0]));
  await page.evaluate(()=>{const a=document.querySelector('#app')._vnode.component.proxy;a.closeAsrEditorModal();a.openSpeakerModal();});
  await page.getByTestId('workspace-speaker-Анна').waitFor();
 });
 await scenario('shared-video-player',{video:true,count:2,audio_duration:20},async(page)=>{
  await open(page);const video=page.getByTestId('asr-editor').locator('video');await video.waitFor();
  await page.getByTestId('asr-play').click();await page.waitForFunction(()=>!document.querySelector('[data-testid="asr-editor"] video').paused);
  await page.getByTestId('asr-play').click();await page.waitForFunction(()=>document.querySelector('[data-testid="asr-editor"] video').paused);
  await page.getByTestId('asr-speed').selectOption('1.5');assert.equal(await video.evaluate(e=>e.playbackRate),1.5);
  await page.getByTestId('asr-editor').getByRole('button',{name:'Скрыть видео',exact:true}).click();assert(await video.evaluate(e=>e.classList.contains('tw-video-collapsed')));
  await page.getByTestId('workspace-edit-0').click();assert.equal(await page.getByTestId('asr-editor').locator('audio').count(),0);
  await page.getByTestId('workspace-back').click();assert.equal(await page.getByTestId('asr-editor').locator('video').count(),1);
 });
 await scenario('empty-add-delete-save',{segments:[]},async(page,context,fixture)=>{
  await open(page);await page.getByTestId('asr-editor').getByRole('button',{name:'Добавить в конец',exact:true}).click();
  await page.getByTestId('asr-segment-text').fill('Первая новая реплика.');await page.getByTestId('asr-segment-speaker').fill('Анна');
  await page.getByTestId('asr-save').click();await page.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();
  let stored=JSON.parse((await(await context.request.get(base+`/api/recordings/${fixture.id}`)).json()).transcription);assert.equal(stored[0].speaker,'Анна');
  await page.locator('.sw-more summary').click();await page.getByTestId('workspace-editor').getByRole('button',{name:'Удалить сегмент',exact:true}).click();await page.getByTestId('asr-delete-confirm').click();
  await page.getByTestId('asr-save').click();await page.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();
  stored=JSON.parse((await(await context.request.get(base+`/api/recordings/${fixture.id}`)).json()).transcription);assert.deepEqual(stored,[]);
 });
 async function openReadonly(page){await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.openSpeakerModal());await page.getByTestId('workspace-speakers').waitFor();}
}finally{await writeFile(out+'/parity-results.json',JSON.stringify(results,null,2));await browser.close();}
