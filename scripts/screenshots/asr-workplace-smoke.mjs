import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';
import {openRecordingByTitle} from './helpers.mjs';
const baseline=process.argv.includes('--baseline'),only=process.argv.find(a=>a.startsWith('--only='))?.slice(7);
const base=process.env.SPEAKR_URL || `http://127.0.0.1:${baseline?8902:8901}`;
assert(['localhost','127.0.0.1'].includes(new URL(base).hostname)&&!['8898','8899'].includes(new URL(base).port));
const out=path.resolve(process.env.SPEAKR_WORKPLACE_OUT||'output/playwright/asr-workplace');await mkdir(out,{recursive:true});
let ready=false;
for(let attempt=0;attempt<30;attempt++){
 try{ready=(await (await fetch(base+'/__asr-split-ready')).json()).isolated===true;if(ready)break;}catch{}
 await new Promise(resolve=>setTimeout(resolve,1000));
}
assert(ready,'Disposable fixture server must be ready before running browser scenarios');
const browser=await chromium.launch(),results=[];
const modal=p=>p.locator('[data-testid="asr-editor"]'),field=(p,id)=>p.locator(`[data-testid="${id}"]`),text=p=>field(p,'asr-segment-text');
async function saved(c,id){return JSON.parse((await(await c.request.get(`${base}/api/recordings/${id}`)).json()).transcription);}
async function caret(p){await text(p).evaluate(e=>{e.focus();const n=e.value.indexOf('Ответ');e.setSelectionRange(n,n);e.dispatchEvent(new Event('select',{bubbles:true}));});}
async function choose(p,index,touch=false){
 const picker=field(p,'asr-mobile-picker');if(await picker.isVisible() && await picker.getAttribute('aria-expanded')==='false')await picker[touch?'tap':'click']();
 await field(p,'asr-editor-scroll').evaluate((e,i)=>{e.scrollTop=i*76;e.dispatchEvent(new Event('scroll'));},index);
 await p.locator(`[data-list-index="${index}"]`)[touch?'tap':'click']();await field(p,'asr-detail').waitFor();
 assert.equal(await field(p,'asr-detail').getAttribute('data-segment-index'),String(index));
}
async function overflow(p){assert(await modal(p).evaluate(e=>e.scrollWidth<=e.clientWidth+1));const bad=await modal(p).locator('button:visible,input:visible,textarea:visible,select:visible').evaluateAll(es=>es.filter(e=>{const r=e.getBoundingClientRect();return r.x < -1 || r.right > innerWidth+1;}).map(e=>e.outerHTML.slice(0,100)));assert.deepEqual(bad,[]);}
async function scenario(name,run,options={}){
 if(only&&only!==name)return;
 const c=await browser.newContext({viewport:{width:options.width||1024,height:options.width<737?844:900},locale:'ru-RU',serviceWorkers:'block',...(process.env.SPEAKR_REDUCED_MOTION?{reducedMotion:'reduce'}:{}),recordVideo:{dir:out},...(options.width<737?{isMobile:true,hasTouch:true}:{} )});
 await c.addInitScript(d=>localStorage.setItem('darkMode',d?'true':'false'),!!options.dark);
 const p=await c.newPage(),errors=[],writes=[];p.on('pageerror',e=>errors.push(e.message));p.on('request',r=>{if(r.url().endsWith('/update_transcription'))writes.push(r.postDataJSON());});
 try{
  const ready=await c.request.get(base+'/__asr-split-ready');assert((await ready.json()).isolated);
  const f=await(await c.request.post(base+'/__asr-split-fixture',{data:{spectrogram:true,...options}})).json();assert(f.isolated);
  await p.goto(base,{waitUntil:'domcontentloaded',timeout:60000});await p.locator('h4').filter({hasText:f.title}).first().waitFor();
  if(options.width<737)await p.locator('header button').first().tap();
  await openRecordingByTitle(p,f.title);
  if(options.width<737)await p.locator('[data-mobile-bottom-nav] button').filter({hasText:'Транскрипция'}).tap();
  await p.locator('button[title="Редактировать транскрипт"]:visible').first()[options.width<737?'tap':'click']();
  await (baseline?p.locator('.asr-editor-table'):field(p,'asr-mobile-picker').or(field(p,'asr-detail')).first()).waitFor({state:'attached'});
  await run(p,c,f.id,writes);assert.deepEqual(errors,[]);if(!baseline && await modal(p).isVisible())await overflow(p);
  await p.screenshot({path:path.join(out,name+'.png')});results.push({name,result:'PASS',writes:writes.length});console.log('PASS '+name);
 }catch(e){await p.screenshot({path:path.join(out,name+'-failure.png')});results.push({name,result:'FAIL',error:e.stack,errors});console.error('FAIL '+name+': '+e.message);}
 finally{const video=p.video();await p.close();await c.close();await video.saveAs(path.join(out,name+'.webm'));}
}
try{
 if(baseline){
  await scenario('before-workplace',async(p)=>{
   const row=p.locator('[data-segment-index="0"]');await row.locator('[data-testid="asr-show-spectrogram"]').click();await field(p,'asr-spectrogram-plot').waitFor();
   await row.locator('[data-testid="asr-retranscribe"]').click();await field(p,'asr-retranscription-proposal').waitFor({timeout:60000});
  });
 }else{
  await scenario('split-preview-save-reopen',async(p,c,id,writes)=>{
   const before=await saved(c,id);await caret(p);await text(p).press('Control+Enter');await field(p,'asr-split-preview').waitFor();assert.deepEqual(await saved(c,id),before);
   await field(p,'asr-split-speaker-0').fill('Анна');await field(p,'asr-split-speaker-1').fill('Борис');assert.deepEqual(await saved(c,id),before);
   await field(p,'asr-confirm-split').click();assert.equal(await field(p,'asr-detail').getAttribute('data-segment-index'),'1');assert(await field(p,'asr-segment-speaker').evaluate(e=>e===document.activeElement));
   await field(p,'asr-segment-speaker').fill('Борис');await field(p,'asr-save').click();await p.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();
   const after=await saved(c,id);assert.equal(after.length,before.length+1);assert.equal(after[1].speaker,'Борис');assert.deepEqual(after.slice(2),before.slice(1));assert.equal(writes.length,1);
   await field(p,'asr-save').click();await p.waitForTimeout(150);assert.equal(writes.length,1);
   await modal(p).getByRole('button',{name:'Закрыть',exact:true}).last().click();await p.locator('button[title="Редактировать транскрипт"]:visible').first().click();assert.equal(await field(p,'asr-segment-speaker').inputValue(),'Борис');
  });
  await scenario('spectrum-marker-frequency-preview',async(p)=>{
   let requests=0;p.on('request',r=>{if(r.url().endsWith('/spectrogram/prepare')&&!r.postDataJSON()?.existing_id)requests++;});await caret(p);await field(p,'asr-show-spectrogram').click();await field(p,'asr-spectrogram-plot').waitFor();
   await field(p,'asr-spectrogram-time').fill('3.5');for(const v of ['2000','4000','full','8000']){
    const accepted=p.waitForResponse(r=>r.url().endsWith('/spectrogram/prepare')&&r.request().postDataJSON()?.frequency===v,{timeout:120000});
    await field(p,'asr-spectrogram-frequency').selectOption(v);await accepted;
    await p.waitForFunction(value=>{const s=document.querySelector('#app')._vnode.component.proxy.spectrogram;return s.frequency===value&&!s.loading&&!s.tilesLoading&&s.url;},v,{timeout:120000});
    assert.equal(await field(p,'asr-spectrogram-time').inputValue(),'3.5');
   }
   assert.equal(requests,4);await field(p,'asr-spectrogram-plot').focus();await field(p,'asr-spectrogram-plot').press('ArrowRight');assert.equal(await field(p,'asr-spectrogram-time').inputValue(),'3.51');
   await field(p,'asr-spectrogram-panel').scrollIntoViewIfNeeded();await p.screenshot({path:path.join(out,'desktop-spectrum.png')});
   await field(p,'asr-spectrogram-zoom-in').click();await field(p,'asr-spectrogram-plot').waitFor();await field(p,'asr-spectrogram-next').click();await field(p,'asr-spectrogram-plot').waitFor();await field(p,'asr-spectrogram-next').click();await field(p,'asr-spectrogram-plot').waitFor();assert.equal(await field(p,'asr-spectrogram-time').inputValue(),'3.51');
   await field(p,'asr-spectrogram-panel').getByRole('button',{name:'Сбросить отметку',exact:true}).click();assert.equal(await field(p,'asr-spectrogram-time').inputValue(),'');await field(p,'asr-spectrogram-time').fill('3.51');
   await field(p,'asr-split-segment').click();await field(p,'asr-split-preview').waitFor();assert((await field(p,'asr-split-preview').innerText()).includes('по метке'));await field(p,'asr-confirm-split').click();assert.equal(await field(p,'asr-start').inputValue(),'3.51');
   await p.route('**/spectrogram/prepare',r=>r.fulfill({status:503,contentType:'application/json',body:'{"code":"unavailable"}'}));await field(p,'asr-show-spectrogram').click();await field(p,'asr-spectrogram-error').waitFor();await p.unroute('**/spectrogram/prepare');await field(p,'asr-spectrogram-error').getByRole('button',{name:'Повторить',exact:true}).click();await field(p,'asr-spectrogram-plot').waitFor();
  });
  await scenario('segment-proposal-apply-and-stale',async(p,c,id)=>{
   await field(p,'asr-retranscribe').click();const cancelled=p.waitForResponse(r=>r.request().method()==='DELETE' && r.url().includes('/segment-transcriptions/'));await field(p,'asr-retranscription-panel').getByRole('button',{name:'Отмена',exact:true}).click();const cancellation=await cancelled;assert.equal(await field(p,'asr-retranscription-panel').count(),0);assert(await field(p,'asr-retranscribe').evaluate(e=>e===document.activeElement));
   // DELETE acknowledges the request; the supervisor releases its lease after stopping the worker.
   let terminal=false;for(let i=0;i<50;i++){const status=await(await c.request.get(cancellation.url())).json();if(status.status==='cancelled'){terminal=true;break;}await p.waitForTimeout(100);}assert(terminal);
   const before=await saved(c,id);await field(p,'asr-retranscribe').click();await field(p,'asr-retranscription-proposal').waitFor({timeout:30000});assert.deepEqual(await saved(c,id),before);
   assert(await text(p).evaluate(e=>e.readOnly));
   await field(p,'asr-retranscription-panel').getByRole('button',{name:'Закрыть',exact:true}).click();assert(await field(p,'asr-retranscribe').evaluate(e=>e===document.activeElement));
   await field(p,'asr-retranscribe').click();await field(p,'asr-retranscription-proposal').waitFor({timeout:30000});
   await field(p,'asr-retranscription-proposal').fill('Правильный текст.');await field(p,'asr-retranscription-apply').click();assert.equal(await text(p).inputValue(),'Правильный текст.');
   await field(p,'asr-retranscribe').click();await field(p,'asr-retranscription-proposal').waitFor({timeout:30000});
   await modal(p).getByRole('button',{name:'Закрыть',exact:true}).last().click();await field(p,'asr-close-decision').waitFor();assert(await field(p,'asr-retranscription-proposal').isDisabled());assert(await p.locator('.sw-content').evaluate(e=>e.inert));
   await field(p,'asr-keep-editing').press('Tab');assert(await field(p,'asr-close-decision').evaluate(e=>e.contains(document.activeElement)));await field(p,'asr-close-decision').press('Escape');
   await field(p,'asr-extend-end').click();await field(p,'asr-retranscription-error').waitFor();assert.equal(await field(p,'asr-retranscription-proposal').count(),0);assert.equal(await text(p).inputValue(),'Правильный текст.');
   await field(p,'asr-retranscription-panel').getByRole('button',{name:'Повторить',exact:true}).click();await field(p,'asr-retranscription-proposal').waitFor({timeout:30000});
  });
  await scenario('extensions-current-draft-save',async(p,c,id)=>{
   assert(await field(p,'asr-extend-start').isDisabled());await choose(p,1);await field(p,'asr-extend-start').click();assert.equal(await field(p,'asr-start').inputValue(),'8');await field(p,'asr-extend-end').click();assert.equal(await field(p,'asr-end').inputValue(),'20');assert(await field(p,'asr-extend-start').isDisabled());assert(await field(p,'asr-extend-end').isDisabled());
   await choose(p,219);assert.equal(await field(p,'asr-extend-end').innerText(),'До конца записи');await field(p,'asr-extend-end').click();assert.equal(await field(p,'asr-end').inputValue(),'2200');await field(p,'asr-save').click();await p.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();const after=await saved(c,id);assert.equal(after[1].start_time,8);assert.equal(after[1].end_time,20);assert.equal(after.at(-1).end_time,2200);
  });
  await scenario('save-failure-edit-during-save',async(p,c,id)=>{
   await text(p).fill('Черновик');await p.route('**/update_transcription',r=>r.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'fixture failure'})}));await field(p,'asr-save').click();await p.getByTestId('asr-save-status').filter({hasText:'Не удалось'}).waitFor();assert.equal(await text(p).inputValue(),'Черновик');assert(await modal(p).isVisible());
   await p.unroute('**/update_transcription');await p.route('**/update_transcription',async r=>{await new Promise(resolve=>setTimeout(resolve,500));await r.continue();});await field(p,'asr-save').click();await p.getByTestId('asr-save-status').filter({hasText:'Сохранение…'}).waitFor();await text(p).fill('Более новая правка');await p.getByTestId('asr-save-status').filter({hasText:'несохранённые'}).waitFor();assert.equal((await saved(c,id))[0].sentence,'Черновик');assert.equal(await text(p).inputValue(),'Более новая правка');await p.unroute('**/update_transcription');await field(p,'asr-save').click();await p.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();assert.equal((await saved(c,id))[0].sentence,'Более новая правка');
   await text(p).fill('Сохранить и закрыть');await p.locator('.sw-save-group summary').click();await field(p,'asr-save-close').click();await modal(p).waitFor({state:'hidden'});assert.equal((await saved(c,id))[0].sentence,'Сохранить и закрыть');
  });
  await scenario('autosave-close-discard-no-write',async(p,c,id,writes)=>{
   const before=await saved(c,id);await text(p).fill('Discard');await modal(p).getByRole('button',{name:'Закрыть',exact:true}).last().click();await field(p,'asr-close-decision').waitFor();await p.waitForTimeout(2200);assert.equal(writes.length,0);await field(p,'asr-discard').click();await p.waitForTimeout(300);assert.deepEqual(await saved(c,id),before);
   await p.locator('button[title="Редактировать транскрипт"]:visible').first().click();await text(p).fill('Autosaved');await modal(p).getByRole('button',{name:'Закрыть',exact:true}).last().click();await field(p,'asr-keep-editing').click();await p.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor({timeout:6000});assert.equal(writes.length,1);assert.equal((await saved(c,id))[0].sentence,'Autosaved');
  },{autosave:true});
  await scenario('audio-modes-speed-volume',async(p)=>{
   const a=modal(p).locator('audio');await a.evaluate(e=>new Promise(resolve=>{if(e.readyState>=1)resolve();else e.addEventListener('loadedmetadata',resolve,{once:true});}));
   assert.equal(await modal(p).locator('audio').count(),1);assert(await field(p,'asr-play-mode').locator('option[value="marker"]').evaluate(e=>e.disabled),await field(p,'asr-play-mode').locator('option[value="marker"]').evaluate(e=>e.outerHTML));for(const mode of ['segment','fromHere','recording']){await field(p,'asr-play-mode').selectOption(mode);}
   await field(p,'asr-show-spectrogram').click();await field(p,'asr-spectrogram-plot').waitFor({timeout:60000});await field(p,'asr-spectrogram-time').fill('3.5');
   await field(p,'asr-spectrogram-play').click();await p.waitForTimeout(100);assert(await a.evaluate(e=>!e.paused));await a.evaluate(e=>e.pause());
   await field(p,'asr-spectrogram-play-marker').click();await p.waitForTimeout(100);assert(await a.evaluate(e=>!e.paused && e.currentTime>=3.5));await a.evaluate(e=>e.pause());
   await field(p,'asr-spectrogram-panel').getByRole('button',{name:'Закрыть',exact:true}).click();assert(await field(p,'asr-show-spectrogram').evaluate(e=>e===document.activeElement));
   for(const speed of ['0.5','0.75','1','1.25','1.5','1.75','2','2.5','3']){await field(p,'asr-speed').selectOption(speed);assert.equal(await a.evaluate(e=>e.playbackRate),+speed);}
   await field(p,'asr-speed').selectOption('1');await field(p,'asr-play-mode').selectOption('segment');await field(p,'asr-play').click();assert(await a.evaluate(e=>!e.paused));
   await a.evaluate(e=>{e.currentTime=2;e.pause();});await field(p,'asr-play').click();assert(await a.evaluate(e=>e.currentTime>=2 && !e.paused));await a.evaluate(e=>e.pause());await field(p,'asr-seek').fill('4');await field(p,'asr-play').click();assert(await a.evaluate(e=>e.currentTime>=4 && !e.paused));
   await a.evaluate(e=>{e.currentTime=8;e.dispatchEvent(new Event('timeupdate'));});assert(await a.evaluate(e=>e.paused));
   await field(p,'asr-play-mode').selectOption('recording');await field(p,'asr-play').click();await choose(p,1);assert(await a.evaluate(e=>!e.paused));await a.evaluate(e=>e.pause());
   await field(p,'asr-play-mode').selectOption('fromHere');await field(p,'asr-play').click();assert(await a.evaluate(e=>e.currentTime>=10));await a.evaluate(e=>{e.currentTime=18.5;e.dispatchEvent(new Event('timeupdate'));});assert(await a.evaluate(e=>!e.paused));await a.evaluate(e=>e.pause());
   await field(p,'asr-play-mode').selectOption('segment');await field(p,'asr-play').click();await field(p,'asr-seek').fill('4');assert(await a.evaluate(e=>e.paused && e.currentTime===4));await field(p,'asr-play').click();assert(await a.evaluate(e=>!e.paused && e.currentTime>=10));await a.evaluate(e=>e.pause());
   await p.locator('.sw-volume').fill('0.4');assert.equal(await a.evaluate(e=>e.volume),.4);await p.getByRole('button',{name:'Выключить звук',exact:true}).click();assert(await a.evaluate(e=>e.muted));
  });
  await scenario('empty-speakers-no-write-crud',async(p,c,id,writes)=>{
   await modal(p).getByRole('button',{name:'Спикеры',exact:true}).first().click();const names=p.locator('.modal-panel--sm').filter({has:p.getByRole('heading',{name:'Редактировать спикеров'})});await names.waitFor();await names.getByRole('button',{name:'Add Speaker'}).click();await names.locator('input').fill('Новое имя');await names.getByRole('button',{name:'Сохранить',exact:true}).click();assert.equal(writes.length,0);assert.deepEqual(await saved(c,id),[]);
   await modal(p).getByRole('button',{name:'Добавить сегмент',exact:true}).click();assert.equal(await field(p,'asr-segment-speaker').inputValue(),'Новое имя');assert(await text(p).evaluate(e=>e===document.activeElement));await modal(p).getByText('Ещё',{exact:true}).click();await modal(p).getByRole('button',{name:'Удалить сегмент',exact:true}).click();await field(p,'asr-delete-decision').waitFor();await field(p,'asr-delete-cancel').press('Escape');assert.equal(await field(p,'asr-detail').count(),1);await modal(p).getByRole('button',{name:'Удалить сегмент',exact:true}).click();await field(p,'asr-delete-confirm').click();assert.equal(await field(p,'asr-detail').count(),0);
  },{empty:true,spectrogram:false});
  await scenario('late-speaker-dialog-no-write',async(p,c,id,writes)=>{
   await p.route('**/speakers',async r=>{await new Promise(resolve=>setTimeout(resolve,1000));await r.continue();});
   await modal(p).getByRole('button',{name:'Спикеры',exact:true}).first().click();await modal(p).getByRole('button',{name:'Закрыть',exact:true}).last().click();await p.waitForTimeout(1500);
   assert.equal(await p.getByRole('heading',{name:'Редактировать спикеров',exact:true}).count(),0);assert.equal(writes.length,0);
   await p.locator('button[title="Редактировать транскрипт"]:visible').first().click();await text(p).fill('Черновик под диалогом');
   await modal(p).getByRole('button',{name:'Спикеры',exact:true}).first().click();await modal(p).getByRole('button',{name:'Закрыть',exact:true}).last().click();await field(p,'asr-close-decision').waitFor();await p.waitForTimeout(1500);
   assert.equal(await p.getByRole('heading',{name:'Редактировать спикеров',exact:true}).count(),0);assert.equal(writes.length,0);assert.equal(await text(p).inputValue(),'Черновик под диалогом');await field(p,'asr-keep-editing').click();
  });
  await scenario('speaker-map-and-add-below',async(p,c,id,writes)=>{
   const before=await saved(c,id);await modal(p).getByRole('button',{name:'Спикеры',exact:true}).first().click();const names=field(p,'asr-speaker-names');await names.waitFor();
   await names.locator('input').nth(0).fill('Борис');await names.locator('input').nth(1).fill('Вера');await p.getByRole('button',{name:'Вера',exact:true}).click();
   await names.getByRole('button',{name:'Add Speaker'}).click();await field(p,'asr-speaker-remove-2').click();assert.equal(await names.locator('input').count(),2);
   await names.getByRole('button',{name:'Сохранить',exact:true}).click();assert.equal(await field(p,'asr-segment-speaker').inputValue(),'Борис');await choose(p,1);assert.equal(await field(p,'asr-segment-speaker').inputValue(),'Вера');assert.deepEqual(await saved(c,id),before);assert.equal(writes.length,0);
   await choose(p,0);await modal(p).getByText('Ещё',{exact:true}).click();await modal(p).getByRole('button',{name:'Добавить сегмент ниже',exact:true}).click();assert.equal(await field(p,'asr-detail').getAttribute('data-segment-index'),'1');assert(await text(p).evaluate(e=>e===document.activeElement));assert.equal(await field(p,'asr-start').inputValue(),'8');assert.equal(await field(p,'asr-end').inputValue(),'10');
   await text(p).fill('Новый сегмент');await text(p).press('Control+s');await field(p,'asr-save-status').filter({hasText:'Сохранено'}).waitFor();assert.equal(writes.length,1);assert.deepEqual((await saved(c,id)).map(s=>s.speaker),['Борис','Борис','Вера']);
  },{segments:[{speaker:'Анна',start_time:0,end_time:8,sentence:'Первая фраза. Ответ другого человека.'},{speaker:'Борис',start_time:10,end_time:18,sentence:'Ещё одна фраза.'}],database_speakers:['Борис','Вера']});
  await scenario('long-virtual-list',async(p)=>{assert(await p.locator('[data-list-index]').count()<60);await choose(p,1499);assert(await p.locator('[data-list-index]').count()<60);assert((await text(p).inputValue()).includes('1499'));await text(p).fill('Длинный текст '.repeat(100));await field(p,'asr-segment-speaker').fill('Длинное имя '.repeat(30));},{count:1500,dark:true});
  await scenario('entry-scroll-and-text-sizing',async(p)=>{
   await modal(p).getByRole('button',{name:'Закрыть',exact:true}).last().click();await p.locator('.speaker-segment[data-segment-index="10"], .transcript-segment[data-segment-index="10"]').first().dblclick();
   assert.equal(await field(p,'asr-detail').getAttribute('data-segment-index'),'10');assert(await p.locator('[data-list-index="10"]').evaluate(e=>e.classList.contains('sw-highlight')));
   await field(p,'asr-editor-scroll').evaluate(e=>{e.scrollTop=7600;e.dispatchEvent(new Event('scroll'));});const top=await field(p,'asr-editor-scroll').evaluate(e=>e.scrollTop);
   await modal(p).getByRole('button',{name:'Закрыть',exact:true}).last().click();await p.locator('button[title="Редактировать транскрипт"]:visible').first().click();await p.waitForFunction(expected=>document.querySelector('[data-testid="asr-editor-scroll"]').scrollTop===expected,top);
   assert.equal(await field(p,'asr-detail').getAttribute('data-segment-index'),'10');const height=await text(p).evaluate(e=>e.getBoundingClientRect().height);await text(p).fill('Длинная русская фраза. '.repeat(200));assert(await text(p).evaluate(e=>e.getBoundingClientRect().height)>height);
  });
  for(const width of [390,320])await scenario(`mobile-${width}-tools`,async(p)=>{
   await p.evaluate(()=>{window.__workplaceTouches=0;document.addEventListener('touchstart',()=>window.__workplaceTouches++);});
   await choose(p,1,true);await field(p,'asr-extend-start').tap();assert.equal(await field(p,'asr-start').inputValue(),'8');await field(p,'asr-show-spectrogram').tap();await field(p,'asr-spectrogram-plot').waitFor();
   const plot=field(p,'asr-spectrogram-plot');await plot.tap();const marker=Number(await field(p,'asr-spectrogram-time').inputValue());assert(marker>8 && marker<18);assert.equal(await field(p,'asr-spectrogram-marker').count(),1);
   await field(p,'asr-spectrogram-panel').scrollIntoViewIfNeeded();await p.screenshot({path:path.join(out,`mobile-${width}-spectrum.png`)});
   await field(p,'asr-retranscribe').tap();await field(p,'asr-retranscription-proposal').waitFor({timeout:30000});await field(p,'asr-retranscription-panel').scrollIntoViewIfNeeded();await overflow(p);await p.screenshot({path:path.join(out,`mobile-${width}-proposal.png`)});await field(p,'asr-retranscription-apply').tap();assert.equal(await text(p).inputValue(),'Повторно распознанный сегмент.');
   await modal(p).getByRole('button',{name:'Закрыть',exact:true}).last().tap();await field(p,'asr-close-decision').waitFor();await field(p,'asr-keep-editing').tap();assert.equal(await field(p,'asr-close-decision').count(),0);assert.equal(await field(p,'asr-detail').getAttribute('data-segment-index'),'1');assert(await p.evaluate(()=>window.__workplaceTouches>=8));
  },{width,dark:width===320});
 }
}finally{await browser.close();await writeFile(path.join(out,baseline?'results-before.json':only?`results-${only}.json`:'results.json'),JSON.stringify(results,null,2));}
if(results.some(r=>r.result==='FAIL'))process.exitCode=1;
