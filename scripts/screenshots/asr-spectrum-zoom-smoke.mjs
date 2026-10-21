import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {chromium} from 'playwright';
import {openRecordingByTitle} from './helpers.mjs';
const before=process.argv.includes('--before'),base=process.env.SPEAKR_URL||`http://127.0.0.1:${before?8909:8910}`,out='output/playwright/asr-workplace-spectrum-zoom';
assert(['localhost','127.0.0.1'].includes(new URL(base).hostname)&&!['8898','8899'].includes(new URL(base).port));await mkdir(out,{recursive:true});
const browser=await chromium.launch();
const counter=async()=>{const result=await(await fetch(base+'/__asr-spectrum-counters')).json();assert(result.isolated);return result.renderCount;};
async function scenario(width,length){
 const context=await browser.newContext({viewport:{width,height:960},reducedMotion:'reduce',locale:'ru-RU',recordVideo:{dir:out},...(width<737?{isMobile:true,hasTouch:true}:{})}),page=await context.newPage(),errors=[],requests=[];
 const id=name=>page.getByTestId(name);page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().includes('/spectrogram'))requests.push({method:r.method(),url:r.url(),body:r.postData()});});
 page.setDefaultTimeout(120000);
 const name=`${before?'before':'after'}-${width}-${length}`,result={phase:before?'before':'after',width,length,result:'FAIL'};
 const settled=()=>page.waitForFunction(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v&&!v.loading&&!v.tilesLoading&&v.url;},null,{timeout:180000});
 try{
  assert((await(await context.request.get(base+'/__asr-split-ready')).json()).isolated);
  const fixture=await(await context.request.post(base+'/__asr-split-fixture',{data:{spectrogram:true,audio_duration:Math.ceil(length+2),segments:[{speaker:'Анна',start_time:1,end_time:1+length,sentence:'Первая реплика. Ответ второго человека.'}]}})).json();
  await page.goto(base,{waitUntil:'domcontentloaded',timeout:120000});await page.locator('h4').filter({hasText:fixture.title}).first().waitFor();
  if(width<737)await page.locator('header button').first().tap();await openRecordingByTitle(page,fixture.title);
  if(width<737)await page.locator('[data-mobile-bottom-nav] button').filter({hasText:'Транскрипция'}).tap();
  await page.locator('button[title="Редактировать транскрипт"]:visible').first().click();await id('asr-show-spectrogram').click();await settled();
  await id('asr-spectrogram-panel').scrollIntoViewIfNeeded();
  if(before){assert(await id('asr-spectrogram-zoom-out').isEnabled());assert.equal(await id('asr-spectrogram-fit').count(),0);await page.screenshot({path:`${out}/${name}.png`,animations:'disabled'});result.originalDefect=true;}
  else{
   assert.equal(await id('asr-spectrogram-zoom-out').isDisabled(),length<=60);
   const audio=page.locator('[data-testid="asr-editor"] audio');await id('asr-spectrogram-time').fill(String(1+length/2));
   if(length>=120){await id('asr-spectrogram-play').click();await page.waitForFunction(()=>!document.querySelector('[data-testid="asr-editor"] audio').paused);}
   const playing=await audio.evaluate(e=>e.currentTime);await id('asr-spectrogram-zoom-in').click();await settled();
   if(length>=120){assert(await audio.evaluate((e,t)=>!e.paused&&e.currentTime>=t,playing));await id('asr-spectrogram-play').click();}
   const paused=await audio.evaluate(e=>e.currentTime);assert(await audio.evaluate(e=>e.paused));
   await id('asr-spectrogram-next').click();await settled();await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.canFitSpectrogram());await id('asr-spectrogram-fit').click();await page.waitForFunction(n=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return Math.abs(v.span-n)<.001&&!v.loading&&!v.tilesLoading;},Math.max(.25,length),{timeout:180000});await settled();
   let view=await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.spectrogram);
   assert.equal(view.window.start,1);assert(Math.abs(view.window.end-(1+length))<.001);assert.equal(view.marker,1+length/2);
   assert.equal(await audio.evaluate(e=>e.currentTime),paused);assert(await id('asr-spectrogram-fit').isDisabled());assert(await id('asr-spectrogram-zoom-out').isDisabled());assert(await id('asr-spectrogram-zoom-in').isEnabled());
   const scroll=await id('asr-spectrogram-scroll').evaluate(e=>({width:e.clientWidth,total:e.scrollWidth}));assert(scroll.total<=scroll.width+1);
   await id('asr-spectrogram-panel').scrollIntoViewIfNeeded();await page.screenshot({path:`${out}/${name}-fit.png`,animations:'disabled'});

   await id('asr-spectrogram-zoom-in').click();await page.waitForFunction(n=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v.span<n&&!v.loading&&!v.tilesLoading;},length,{timeout:180000});const count=requests.filter(r=>r.url.endsWith('/prepare')&&r.method==='POST'&&!JSON.parse(r.body).existing_id).length;const fitRenderBefore=await counter();await id('asr-spectrogram-fit').click();await page.waitForFunction(n=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return Math.abs(v.span-n)<.001&&!v.loading&&!v.tilesLoading;},Math.max(.25,length),{timeout:180000});await settled();assert.equal(await counter(),fitRenderBefore);result.retainedFitRenderDelta=0;
   assert.equal(requests.filter(r=>r.url.endsWith('/prepare')&&r.method==='POST'&&!JSON.parse(r.body).existing_id).length,count);
   if(length===6){for(let i=0;i<5;i++){if(await id('asr-spectrogram-zoom-in').isEnabled()){const target=await page.evaluate(()=>Math.max(.25,document.querySelector('#app')._vnode.component.proxy.spectrogram.span/2));await id('asr-spectrogram-zoom-in').click();await page.waitForFunction(n=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v.span===n&&!v.loading&&!v.tilesLoading;},target,{timeout:180000});await settled();}}
    assert(await id('asr-spectrogram-zoom-in').isDisabled());view=await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.spectrogram);assert.equal(view.span,.25);
    await page.screenshot({path:`${out}/${name}-minimum.png`,animations:'disabled'});
   }
   assert(await id('asr-editor').evaluate(e=>e.scrollWidth<=e.clientWidth+1));result.fullOverview=true;result.playingThroughZoom=length>=120;result.pausedTimePreserved=true;result.retainedFitGenerationDelta=0;result.markerPreserved=true;
  }
  assert.deepEqual(errors,[]);result.result='PASS';result.errors=errors;console.log(JSON.stringify(result));
 }catch(error){result.error=error.message;result.errors=errors;await page.screenshot({path:`${out}/${name}-failure.png`});throw error;}
 finally{await writeFile(`${out}/${name}-results.json`,JSON.stringify({...result,requests},null,2));const video=page.video();await page.close();await context.close();await video.saveAs(`${out}/${name}.webm`);}
}
try{if(before)await scenario(1024,6);else{for(const [width,length]of (process.argv.includes('--remaining')?[[1024,150.3],[390,120],[320,120]]:[[1024,6],[1024,120],[1024,150.3],[390,120],[320,120]]))await scenario(width,length);}}finally{await browser.close();}
