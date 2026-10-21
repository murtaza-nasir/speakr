import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';
import {BASE_URL,VIEWPORT,MOBILE_VIEWPORT,openRecordingByTitle} from './helpers.mjs';
assert(['localhost','127.0.0.1'].includes(new URL(BASE_URL).hostname)&&!['8898','8899'].includes(new URL(BASE_URL).port));
const out=path.resolve('temp/asr-segment-tools/ui');await mkdir(out,{recursive:true});
let ready=false;
for(let i=0;i<120&&!ready;i++){try{const r=await fetch(`${BASE_URL}/__asr-split-ready`,{signal:AbortSignal.timeout(1500)});ready=r.ok&&(await r.json()).isolated;}catch{}if(!ready)await new Promise(resolve=>setTimeout(resolve,250));}
assert(ready,'Disposable fixture server must be ready');
const browser=await chromium.launch(),results=[];
const only=process.argv.find(a=>a.startsWith('--only='))?.slice(7);
const row=(p,i)=>p.locator(`[data-segment-index="${i}"]`),text=(p,i)=>row(p,i).locator('textarea');
const panel=p=>p.locator('[data-testid="asr-retranscription-panel"]');
const proposal=p=>p.locator('[data-testid="asr-retranscription-proposal"]');
async function saved(c,id){return JSON.parse((await(await c.request.get(`${BASE_URL}/api/recordings/${id}`)).json()).transcription);}
async function generate(p,i=0){await row(p,i).locator('[data-testid="asr-retranscribe"]').click();await proposal(p).waitFor({timeout:30000});}
async function scenario(name,run,options={}) {
 if(only&&only!==name)return;
 const c=await browser.newContext({viewport:options.mobile?MOBILE_VIEWPORT:VIEWPORT,locale:'ru-RU',recordVideo:{dir:out},...(options.mobile?{isMobile:true,hasTouch:true}:{})});
 await c.addInitScript(d=>localStorage.setItem('darkMode',d?'true':'false'),!!options.dark);
 const p=await c.newPage(),errors=[];p.on('pageerror',e=>errors.push(e.message));
 try {
  const ready=await c.request.get(`${BASE_URL}/__asr-split-ready`);assert((await ready.json()).isolated);
  const f=await(await c.request.post(`${BASE_URL}/__asr-split-fixture`,{data:{spectrogram:true,...options}})).json();assert(f.isolated);
  await p.goto(BASE_URL);await p.locator('h4').filter({hasText:f.title}).first().waitFor();
  if(options.mobile)await p.locator('header button').first().click();
  await openRecordingByTitle(p,f.title);if(options.mobile)await p.locator('[data-mobile-bottom-nav] button').filter({hasText:'Транскрипция'}).click();
  await p.locator('button[title="Редактировать транскрипт"]:visible').first().click();await p.locator('.asr-editor-table').waitFor();
  await run(p,c,f.id);assert.deepEqual(errors,[]);await p.screenshot({path:path.join(out,name+'.png')});results.push({name,result:'PASS'});console.log('PASS '+name);
 } catch(e){await p.screenshot({path:path.join(out,name+'-failure.png')});results.push({name,result:'FAIL',error:e.stack,errors});console.error('FAIL '+name+': '+e.message);}
 finally {const video=p.video();await p.close();await c.close();await video.saveAs(path.join(out,name+'.webm'));}
}
try {
 await scenario('long-segment-limit-guidance',async(p)=>{
  await row(p,0).locator('input[type="number"]').nth(1).fill('301');
  const action=row(p,0).locator('[data-testid="asr-retranscribe"]');assert(await action.isDisabled());assert((await action.getAttribute('title')).includes('сначала разделите'));
  await row(p,0).locator('input[type="number"]').nth(1).fill('8');assert(await action.isEnabled());
 });
 await scenario('three-workers-job-busy-cancel',async(p,c,id)=>{
  const csrf=await p.locator('meta[name="csrf-token"]').getAttribute('content'),url=`${BASE_URL}/api/recordings/${id}/segment-transcriptions`,headers={'X-CSRFToken':csrf,'Connection':'close'};
  const start=await c.request.post(url,{data:{start:0,end:8},headers});assert.equal(start.status(),202);const job=(await start.json()).job_id;
  const busy=await c.request.post(url,{data:{start:0,end:8},headers});assert.equal(busy.status(),429);
  const pids=new Set([start.headers()['x-synthetic-worker']]);
  for(let i=0;i<6&&pids.size<3;i++){const polls=await Promise.all(Array.from({length:8},()=>c.request.get(url+'/'+job,{headers})));for(const poll of polls){assert.equal(poll.status(),200);pids.add(poll.headers()['x-synthetic-worker']);assert(['running','done'].includes((await poll.json()).status));}}
  assert.equal(pids.size,3,'All three HTTP workers see the same job');const cancel=await c.request.delete(url+'/'+job,{headers});assert.equal(cancel.status(),200);assert.equal((await cancel.json()).status,'cancelled');await p.waitForTimeout(1000);
 },{asr_mode:'delay'});
 await scenario('frequency-marker-caret-cache',async(p)=>{
  let requests=0;p.on('request',r=>{if(r.url().includes('/spectrogram?'))requests++;});
  await text(p,0).evaluate(e=>{e.focus();const n=e.value.indexOf('Ответ');e.setSelectionRange(n,n);e.dispatchEvent(new Event('select',{bubbles:true}));});
  await row(p,0).locator('[data-testid="asr-show-spectrogram"]').click();await p.locator('[data-testid="asr-spectrogram-plot"]').waitFor();
  await p.locator('[data-testid="asr-spectrogram-time"]').fill('3.5');
  for(const value of ['2000','4000','full','8000']){await p.locator('[data-testid="asr-spectrogram-frequency"]').selectOption(value);await p.locator('[data-testid="asr-spectrogram-plot"]').waitFor();assert.equal(await p.locator('[data-testid="asr-spectrogram-time"]').inputValue(),'3.5');assert((await p.locator('[data-testid="asr-spectrogram-axis"]').innerText()).includes(value==='full'?'8':String(+value/1000)));}
  assert.equal(requests,4);assert(await row(p,0).locator('[data-testid="asr-split-segment"]').isEnabled());
 });
 await scenario('proposal-review-save-reload',async(p,c,id)=>{
  const before=await saved(c,id);await generate(p);assert.deepEqual(await saved(c,id),before);assert.equal(await text(p,0).inputValue(),before[0].sentence);
  await proposal(p).fill('Исправленный текст.');await p.locator('[data-testid="asr-retranscription-apply"]').click();
  assert.equal(await text(p,0).inputValue(),'Исправленный текст.');
  const response=p.waitForResponse(r=>r.url().endsWith(`/recording/${id}/update_transcription`)&&r.request().method()==='POST');
  await p.locator('[data-testid="asr-editor"] button').filter({hasText:/^Сохранить$/}).click();assert((await response).ok());
  const after=await saved(c,id);assert.deepEqual(after[0],{...before[0],sentence:'Исправленный текст.'});assert.deepEqual(after.slice(1),before.slice(1));
  await p.reload();const data=await saved(c,id);assert.equal(data[0].sentence,'Исправленный текст.');
  const capture=await(await c.request.get(`${BASE_URL}/__asr-segment-capture`)).json();assert.equal(capture.duration,8);assert.equal(capture.channels,2);assert.equal(capture.params.diarize,'false');assert.equal(capture.params.enable_diarization,'false');assert.equal(capture.params.model,'large-v3');
 });
 await scenario('unsaved-split-autosave',async(p,c,id)=>{
  await text(p,0).evaluate(e=>{e.focus();const n=e.value.indexOf('Ответ');e.setSelectionRange(n,n);e.dispatchEvent(new Event('select',{bubbles:true}));});await text(p,0).press('Control+Enter');
  const start=+(await row(p,1).locator('input[type="number"]').nth(0).inputValue()),end=+(await row(p,1).locator('input[type="number"]').nth(1).inputValue());
  await generate(p,1);const capture=await(await c.request.get(`${BASE_URL}/__asr-segment-capture`)).json();assert(Math.abs(capture.duration-(end-start))<.01);
  await p.locator('[data-testid="asr-retranscription-apply"]').click();await p.waitForTimeout(2400);assert.equal((await saved(c,id))[1].sentence,'Повторно распознанный сегмент.');
 },{autosave:true});
 await scenario('stale-cancel-close',async(p,c,id)=>{
  const before=await saved(c,id);await row(p,0).locator('[data-testid="asr-retranscribe"]').click();await panel(p).waitFor();await text(p,0).fill('Ручное исправление');await p.waitForTimeout(1500);
  assert.equal(await proposal(p).count(),0);assert.deepEqual(await saved(c,id),before);await panel(p).getByRole('button',{name:'Закрыть',exact:true}).click();assert.equal(await panel(p).count(),0);
 },{asr_mode:'delay'});
 await scenario('empty-safe-retry',async(p)=>{await row(p,0).locator('[data-testid="asr-retranscribe"]').click();await p.locator('[data-testid="asr-retranscription-error"]').waitFor({timeout:30000});assert.equal(await proposal(p).count(),0);assert.equal(await text(p,0).inputValue(),'Первая фраза 0. Ответ другого человека 0.');},{asr_mode:'empty'});
 await scenario('long-entire-segment',async(p,c)=>{
  await p.locator('[data-testid="asr-editor-scroll"]').evaluate(e=>{e.scrollTop=150*44;e.dispatchEvent(new Event('scroll'));});await text(p,150).waitFor();await row(p,150).locator('[data-testid="asr-show-spectrogram"]').click();await p.locator('[data-testid="asr-spectrogram-plot"]').waitFor();await generate(p,150);
  const capture=await(await c.request.get(`${BASE_URL}/__asr-segment-capture`)).json();assert.equal(capture.duration,140);
 },{long:true,dark:true});
 await scenario('mobile-dark-proposal',async(p)=>{await p.locator('[data-testid="asr-editor-scroll"]').evaluate(e=>e.scrollLeft=e.scrollWidth);await row(p,0).locator('[data-testid="asr-show-spectrogram"]').click();await p.locator('[data-testid="asr-spectrogram-plot"]').waitFor();await generate(p);const box=await panel(p).boundingBox();assert(box.x>=0&&box.width<=390);assert(await p.locator('[data-testid="asr-editor-scroll"]').evaluate(e=>e.clientHeight>44));await proposal(p).scrollIntoViewIfNeeded();await p.screenshot({path:path.join(out,'mobile-open-proposal.png')});await p.locator('[data-testid="asr-retranscription-apply"]').click();assert.equal(await text(p,0).inputValue(),'Повторно распознанный сегмент.');},{mobile:true,dark:true});
} finally {await browser.close();await writeFile(path.join(out,only?`results-${only}.json`:'results.json'),JSON.stringify(results,null,2));}
if(results.some(r=>r.result==='FAIL'))process.exitCode=1;
