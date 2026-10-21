import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
const {chromium}=await import(process.env.SPEAKR_PLAYWRIGHT_MODULE || 'playwright');
const base=process.env.SPEAKR_URL || 'http://127.0.0.1:8921';
assert(['127.0.0.1','localhost'].includes(new URL(base).hostname));
assert(!['8898','8899'].includes(new URL(base).port), 'Use only the disposable synthetic fixture server');
const before=process.argv.includes('--before'), out='output/playwright/spectrum-frequency-ui';
await mkdir(out,{recursive:true});
let ready=false;
for(let i=0;i<30;i++){
 try{ready=(await(await fetch(base+'/__asr-split-ready')).json()).isolated===true;}catch{}
 if(ready)break;await new Promise(resolve=>setTimeout(resolve,1000));
}
assert(ready,'Disposable fixture server did not become ready');
const browser=await chromium.launch({executablePath:process.env.SPEAKR_CHROMIUM_PATH,args:['--autoplay-policy=no-user-gesture-required']});
const results=[];
async function scenario(width,dark,lowRate=false,sampleRate=16000){
 const name=(before?'before':'after')+'-'+width+'-'+(dark?'dark':'light')+(lowRate?'-low-rate':sampleRate>16000?'-wide-range':'');
 const context=await browser.newContext({viewport:{width,height:width<737?844:1080},locale:'ru-RU',serviceWorkers:'block',reducedMotion:'reduce',recordVideo:{dir:out},...(width<737?{isMobile:true,hasTouch:true}:{})});
 await context.addInitScript(d=>{localStorage.setItem('darkMode',String(d));localStorage.setItem('preferredLanguage','ru');},dark);
 const page=await context.newPage(), errors=[], writes=[], result={name,width,dark,lowRate};
 page.setDefaultTimeout(30000);
 const field=id=>page.getByTestId(id);
 page.on('pageerror',e=>errors.push(e.message));
 await page.route(/\/recording\/\d+\/(update_transcript|update_transcription|update_speakers|auto_identify_speakers)/,async route=>{writes.push(route.request().url());await route.abort();});
 const fixture=async()=>{const r=await context.request.post(base+'/__asr-split-fixture',{data:{spectrogram:true,audio_duration:18,audio_sample_rate:lowRate?8000:sampleRate,audio_channels:lowRate?1:2,segments:[{speaker:'Анна',start_time:0,end_time:8,sentence:'Первая реплика. Ответ второго человека.'},{speaker:'Борис',start_time:9,end_time:12,sentence:'Другая реплика.'}]}});assert(r.ok());return r.json();};
 const open=async id=>{
  if(id)await page.goto(base+'/recordings/'+id,{waitUntil:'domcontentloaded',timeout:90000});
  if(width<737)await page.locator('[data-mobile-bottom-nav] button').filter({hasText:'Транскрипция'}).click();
  await page.locator(width<737?'button[title="Редактировать транскрипт"]:visible':'button[title="Определить спикеров"]:visible').click();
  if(width<737)await field('asr-editor').getByRole('tab',{name:'Транскрипт',exact:true}).click();
  await field('workspace-edit-0').click();await field('asr-show-spectrogram').click();await settled();
 };
 const view=()=>page.evaluate(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return {frequency:v.frequency,max:v.maxFrequency,channels:v.channels,marker:v.marker,span:v.span};});
 const settled=async()=>{
  await page.waitForFunction(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v?.url&&!v.loading&&!v.tilesLoading&&!v.error;},null,{timeout:120000});
  await field('asr-spectrogram-panel').scrollIntoViewIfNeeded();
 };
 const close=()=>field('asr-editor').locator('.modal-header .modal-close').click();
 const geometry=()=>page.evaluate(()=>{
  const q=id=>document.querySelector('[data-testid="'+id+'"]'), r=id=>{const x=q(id).getBoundingClientRect();return {x:x.x,y:x.y,width:x.width,height:x.height};};
  const editor=q('workspace-editor');return {axis:r('asr-spectrogram-axis'),plot:r('asr-spectrogram-plot'),control:r('asr-spectrogram-frequency'),panel:r('asr-spectrogram-panel'),editorLeft:editor.getBoundingClientRect().left,overflow:editor.scrollWidth>editor.clientWidth+1};
 });
 try{
  const first=await fixture();await open(first.id);assert.equal((await view()).frequency,'8000');
  result.initialGeometry=await geometry();
  await field('asr-spectrogram-frequency').selectOption('4000');await settled();assert.equal((await view()).frequency,'4000');
  await page.screenshot({path:out+'/'+name+'.png'});await close();await open();
  if(before){
   assert.equal((await view()).frequency,'8000','Baseline must reproduce reset on modal reopen');
   await page.reload({waitUntil:'domcontentloaded'});await open();assert.equal((await view()).frequency,'8000');
   result.result='BASELINE_REPRODUCED';result.preferenceReset=true;
  }else{
   assert.equal((await view()).frequency,'4000');
   if(width<737)await field('asr-editor').getByRole('tab',{name:'Транскрипт',exact:true}).click();
   await field('workspace-edit-1').click();await field('asr-show-spectrogram').click();await settled();assert.equal((await view()).frequency,'4000');
   await close();const second=await fixture();const originalTranscript=(await(await context.request.get(base+'/api/recordings/'+second.id)).json()).transcription;await open(second.id);assert.equal((await view()).frequency,'4000');
   await close();await page.reload({waitUntil:'domcontentloaded'});await open();assert.equal((await view()).frequency,'4000');
   assert.equal(await page.evaluate(()=>localStorage.getItem('speakrSpectrogramFrequency')),'4000');
   const g=await geometry();assert(Math.abs(g.control.y-g.plot.y)<1,'Control must start at upper plot edge');assert(!g.overflow);assert(g.axis.width<=56);assert(g.plot.x-g.axis.x<=60);result.finalGeometry=g;
   const marker=(await view()).marker;await field('asr-spectrogram-frequency').focus();await field('asr-spectrogram-frequency').press('ArrowDown');await field('asr-spectrogram-frequency').press('Enter');await settled();
   assert.equal((await view()).frequency,'8000');assert.equal((await view()).marker,marker);assert(await field('asr-editor').locator('audio').evaluate(e=>e.paused));
   if(lowRate){
    assert.equal((await view()).max,4000);assert.equal((await view()).channels,1);assert.equal(await field('asr-spectrogram-upper-frequency').innerText(),'4 kHz');
    for(const id of ['asr-spectrogram-frequency','asr-spectrogram-plot'])assert.equal(await field(id).evaluate(e=>document.getElementById(e.getAttribute('aria-describedby')).textContent),'4 kHz');
   }
   await field('asr-spectrogram-frequency').selectOption('full');await settled();
   assert.equal((await view()).frequency,'full');assert.equal((await view()).max,lowRate?4000:sampleRate/2);assert.equal((await view()).channels,lowRate?1:2);
   await close();await page.reload({waitUntil:'domcontentloaded'});await open();assert.equal((await view()).frequency,'full');
   await page.screenshot({path:out+'/'+name+'-full.png'});
   const upper=field('asr-spectrogram-upper-frequency');assert(await upper.evaluate(e=>e.scrollWidth<=e.clientWidth+1));
   if(sampleRate===44100)assert.equal(await upper.innerText(),'22.05 kHz');
   await field('asr-spectrogram-zoom-in').click();await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.spectrogram.span===4);await settled();assert(await field('asr-spectrogram-fit').isEnabled());
   await field('asr-spectrogram-next').click();await settled();await field('asr-spectrogram-fit').click();
   await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.spectrogram.span===8&&document.querySelector('[data-testid="asr-spectrogram-fit"]').disabled);await settled();assert(await field('asr-spectrogram-fit').isDisabled());
   await field('asr-spectrogram-time').fill('4');assert.equal((await view()).marker,4);
   await field('asr-spectrogram-play').click();await page.waitForFunction(()=>!document.querySelector('[data-testid="asr-editor"] audio').paused,null,{timeout:15000});
   await field('asr-spectrogram-play').click();await page.waitForFunction(()=>document.querySelector('[data-testid="asr-editor"] audio').paused,null,{timeout:15000});
   await page.screenshot({path:out+'/'+name+'-tools.png'});
   assert.equal((await(await context.request.get(base+'/api/recordings/'+second.id)).json()).transcription,originalTranscript);
   result.result='PASS';result.persistence=true;result.keyboard=true;result.tools=true;
  }
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);result.errors=errors;console.log(JSON.stringify(result));
 }catch(e){result.error=e.message;result.errors=errors;await page.screenshot({path:out+'/'+name+'-failure.png'});throw e;}
 finally{results.push(result);const video=page.video();await page.close();await context.close();await video.saveAs(out+'/'+name+'.webm');}
}
try{if(before)await scenario(1440,true);else{for(const [w,d,l,s]of [[1440,true,false,16000],[1440,false,true,16000],[390,true,false,16000],[320,false,false,44100]])await scenario(w,d,l,s);}}
finally{await writeFile(out+'/'+(before?'before':'after')+'-results.json',JSON.stringify(results,null,2));await browser.close();}
