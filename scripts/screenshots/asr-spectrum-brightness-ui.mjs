import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
const {chromium}=await import(process.env.SPEAKR_PLAYWRIGHT_MODULE || 'playwright');
const base=process.env.SPEAKR_URL || 'http://127.0.0.1:8921';
assert(['localhost','127.0.0.1'].includes(new URL(base).hostname));
assert(!['8898','8899'].includes(new URL(base).port),'Disposable synthetic server only');
const before=process.argv.includes('--before'),out='output/playwright/spectrum-brightness-ui';
await mkdir(out,{recursive:true});
let ready=false;
for(let i=0;i<30&&!ready;i++){
 try{ready=(await(await fetch(base+'/__asr-split-ready')).json()).isolated===true;}catch{}
 if(!ready)await new Promise(resolve=>setTimeout(resolve,1000));
}
assert(ready,'Disposable fixture server unavailable');
const browser=await chromium.launch({executablePath:process.env.SPEAKR_CHROMIUM_PATH,args:['--autoplay-policy=no-user-gesture-required']});
const results=[];
async function scenario(width,dark,amplitude=.002){
 const name=(before?'before':'after')+'-'+width+'-'+(dark?'dark':'light')+(amplitude===1?'-loud':''), result={name,width,dark,amplitude};
 const context=await browser.newContext({viewport:{width,height:width<737?844:1080},locale:'ru-RU',serviceWorkers:'block',reducedMotion:'reduce',recordVideo:{dir:out},...(width<737?{isMobile:true,hasTouch:true}:{})});
 await context.addInitScript(d=>{localStorage.setItem('darkMode',String(d));localStorage.setItem('preferredLanguage','ru');},dark);
 const page=await context.newPage(),field=id=>page.getByTestId(id),errors=[],writes=[],prepares=[];
 page.setDefaultTimeout(30000);
 page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>{if(r.url().endsWith('/spectrogram/prepare'))prepares.push(r.postDataJSON());});
 await page.route(/\/recording\/\d+\/(update_transcript|update_transcription|update_speakers|auto_identify_speakers)/,async r=>{writes.push(r.request().url());await r.abort();});
 const view=()=>page.evaluate(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return {gain:v.gainDb,applied:v.appliedGainDb,draft:v.draftGainDb,frequency:v.frequency,window:{...v.window},span:v.span,marker:v.marker};});
 const settled=()=>page.waitForFunction(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v?.url&&!v.loading&&!v.tilesLoading&&!v.error;});
 const applied=n=>page.waitForFunction(n=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v?.appliedGainDb===n&&!v.loading&&!v.tilesLoading&&!v.error;},n);
 const counter=async()=>(await(await context.request.get(base+'/__asr-spectrum-counters')).json()).renderCount;
 const open=async id=>{
  if(id)await page.goto(base+'/recordings/'+id,{waitUntil:'domcontentloaded',timeout:90000});
  if(width<737)await page.locator('[data-mobile-bottom-nav] button').filter({hasText:'Транскрипция'}).click();
  await page.locator(width<737?'button[title="Редактировать транскрипт"]:visible':'button[title="Определить спикеров"]:visible').click();
  if(width<737)await field('asr-editor').getByRole('tab',{name:'Транскрипт',exact:true}).click();
  await field('workspace-edit-0').click();await field('asr-show-spectrogram').click();await settled();
 };
 const close=()=>field('asr-editor-close').click();
 const setGain=async n=>{await field('asr-spectrogram-gain').fill(String(n));await field('asr-spectrogram-gain').dispatchEvent('change');await applied(n);};
 const screenshot=async suffix=>{await field('asr-spectrogram-plot').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/'+name+suffix+'.png'});};
 try{
  const fixture=async()=>{const r=await context.request.post(base+'/__asr-split-fixture',{data:{spectrogram:true,audio_duration:18,audio_amplitude:amplitude,segments:[{speaker:'Анна',start_time:0,end_time:8,sentence:'Тихая реплика. Ответ второго человека.'},{speaker:'Борис',start_time:9,end_time:12,sentence:'Другая реплика.'}]}});assert(r.ok());return r.json();};
  const recording=await fixture();const original=(await(await context.request.get(base+'/api/recordings/'+recording.id)).json()).transcription;await open(recording.id);
  await screenshot('-0');
  if(before){assert.equal(await field('asr-spectrogram-gain').count(),0);result.result='BASELINE_QUIET';}
  else{
   assert.equal((await view()).applied,0);assert(await field('asr-spectrogram-gain-reset').isDisabled());
   await field('asr-spectrogram-gain').scrollIntoViewIfNeeded();const slider=await field('asr-spectrogram-gain').boundingBox();
   const count=prepares.length;await page.mouse.move(slider.x+8,slider.y+slider.height/2);await page.mouse.down();
   await page.mouse.move(slider.x+slider.width/2,slider.y+slider.height/2,{steps:8});
   assert.equal((await view()).draft,20);assert.equal(prepares.length,count,'Pointer input must not start preparation before release');
   await page.mouse.up();await applied(20);assert.equal(prepares.length,count+1);
   await screenshot('-20');assert.equal(await field('asr-spectrogram-gain').getAttribute('aria-valuetext'),'+20 дБ');
   await field('asr-spectrogram-zoom-in').click();await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.spectrogram.span===4);await settled();
   await field('asr-spectrogram-next').click();await settled();await field('asr-spectrogram-time').fill('4');const selected=await view();
   await setGain(30);assert.deepEqual((await view()).window,selected.window);assert.equal((await view()).marker,4);assert.equal((await view()).span,4);
   const renders=await counter();await field('asr-spectrogram-previous').click();await settled();await field('asr-spectrogram-next').click();await settled();assert.equal(await counter(),renders);
   await field('asr-spectrogram-fit').click();await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.spectrogram.span===8);await settled();
   await field('asr-spectrogram-gain').focus();await field('asr-spectrogram-gain').press('ArrowRight');await applied(35);assert.equal((await view()).marker,4);
   // Retained image must remain labelled with its applied level on a failed new request.
   await page.route('**/spectrogram/prepare',r=>r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({code:'timeout'})}));
   await field('asr-spectrogram-gain').fill('40');await field('asr-spectrogram-gain').dispatchEvent('change');await field('asr-spectrogram-error').waitFor();
   assert.equal((await view()).applied,35);assert.match(await field('asr-spectrogram-applied-gain').innerText(),/35/);assert.equal((await view()).gain,40);
   await page.unroute('**/spectrogram/prepare');await field('asr-spectrogram-error').getByRole('button').click();await applied(40);
   await field('asr-spectrogram-gain-reset').click();await applied(0);assert(await field('asr-spectrogram-gain-reset').isDisabled());
   await setGain(20);await field('asr-spectrogram-frequency').selectOption('4000');
   await page.waitForFunction(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v?.frequency==='4000'&&v.maxFrequency===4000&&v.appliedGainDb===20&&!v.loading&&!v.tilesLoading&&!v.error;});
   await screenshot('-final');
   result.geometry=await field('asr-spectrogram-brightness-controls').evaluate(e=>({width:e.getBoundingClientRect().width,overflow:e.scrollWidth>e.clientWidth+1}));assert(!result.geometry.overflow);
   assert(await field('asr-spectrogram-gain').evaluate(e=>e.getBoundingClientRect().height>=44));
   if(width<737)await field('asr-editor').getByRole('tab',{name:'Транскрипт',exact:true}).click();
   await field('workspace-edit-1').click();await field('asr-show-spectrogram').click();await settled();assert.equal((await view()).applied,20);
   await close();await open();assert.equal((await view()).applied,20);await close();
   const next=await fixture();await open(next.id);assert.equal((await view()).applied,20);
   await close();await page.reload({waitUntil:'domcontentloaded'});await open();assert.equal((await view()).applied,20);
   assert.equal(await page.evaluate(()=>localStorage.getItem('speakrSpectrogramGainDb')),'20');
   await field('asr-spectrogram-play').click();await page.waitForFunction(()=>!document.querySelector('[data-testid="asr-editor"] audio').paused);await field('asr-spectrogram-play').click();await page.waitForFunction(()=>document.querySelector('[data-testid="asr-editor"] audio').paused);
   // Exercise split in the shared draft, without saving either recording.
   await field('asr-spectrogram-time').fill('4');const text=field('asr-segment-text');await text.focus();
   await text.evaluate(e=>{const p=e.value.indexOf('. ')+2;e.setSelectionRange(p,p);e.dispatchEvent(new Event('select',{bubbles:true}));});
   await text.press('Control+Enter');await field('asr-split-preview').waitFor();await field('asr-confirm-split').click();
   await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.editingSegments.length===3);
   assert.deepEqual(await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.editingSegments.slice(0,2).map(s=>[s.start_time,s.end_time])),[[0,4],[4,8]]);
   result.split=true;
   result.result='PASS';result.preparations=prepares.length;result.panRenderDelta=0;result.persistence=true;result.keyboard=true;result.tools=true;result.failedLevelLabel=true;
  }
  assert.equal((await(await context.request.get(base+'/api/recordings/'+recording.id)).json()).transcription,original);assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);result.errors=errors;console.log(JSON.stringify(result));
 }catch(e){result.error=e.message;result.errors=errors;await page.screenshot({path:out+'/'+name+'-failure.png'});throw e;}
 finally{results.push(result);const video=page.video();await page.close();await context.close();await video.saveAs(out+'/'+name+'.webm');}
}
try{if(before)await scenario(1440,true);else if(process.argv.includes('--split-check'))await scenario(1440,true);else for(const [w,d,a]of [[1440,true,.002],[1440,false,1],[390,true,.002],[320,false,.002]])await scenario(w,d,a);}
finally{await writeFile(out+'/'+(before?'before':process.argv.includes('--split-check')?'split-check':'after')+'-results.json',JSON.stringify(results,null,2));await browser.close();}
