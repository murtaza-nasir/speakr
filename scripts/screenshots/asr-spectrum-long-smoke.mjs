import assert from 'node:assert/strict';
import {mkdir,writeFile,readFile} from 'node:fs/promises';
import {chromium} from 'playwright';
import {openRecordingByTitle} from './helpers.mjs';
const base='http://127.0.0.1:8906',out='output/playwright/asr-workplace-spectrum-refinements';
await mkdir(out,{recursive:true});
const only=process.argv.find(a=>a.startsWith('--only='))?.slice(7);
const browser=await chromium.launch(),results=only?JSON.parse(await readFile(out+'/long-results.json','utf8')).filter(r=>String(r.width)!==only):[];
const app=()=>document.querySelector('#app')._vnode.component.proxy;
async function scenario(width){
 const c=await browser.newContext({viewport:{width,height:960},locale:'ru-RU',reducedMotion:'reduce',recordVideo:{dir:out},...(width<737?{isMobile:true,hasTouch:true}:{})}),p=await c.newPage();
 const field=id=>p.getByTestId(id),errors=[],requests=[];p.on('pageerror',e=>errors.push(e.message));p.on('request',r=>{if(r.url().includes('/spectrogram'))requests.push({method:r.method(),url:r.url()});});
 let result={width,result:'FAIL'};
 try{
  assert((await(await c.request.get(base+'/__asr-split-ready')).json()).isolated);
  const f=await(await c.request.post(base+'/__asr-split-fixture',{data:{spectrogram:true,segments:[{speaker:'Анна',start_time:1,end_time:121,sentence:'Первая реплика. Вторая реплика другого человека.'}]}})).json();
  await p.goto(base,{waitUntil:'domcontentloaded',timeout:60000});await p.locator('h4').filter({hasText:f.title}).first().waitFor();if(width<737)await p.locator('header button').first().tap();await openRecordingByTitle(p,f.title);if(width<737)await p.locator('[data-mobile-bottom-nav] button').filter({hasText:'Транскрипция'}).tap();await p.locator('button[title="Редактировать транскрипт"]:visible').first()[width<737?'tap':'click']();await field('asr-show-spectrogram')[width<737?'tap':'click']();
  await field('asr-spectrogram-plot').waitFor({timeout:180000});
  // Invoke the same zoom command once at the approved minimum, avoiding seven redundant preparations.
  const started=Date.now();await p.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.navigateSpectrogram(.25/60));
  await field('asr-spectrogram-status').waitFor({state:'hidden',timeout:180000});await field('asr-spectrogram-plot').waitFor();
  const prepareMs=Date.now()-started,state=await p.evaluate(()=>{const a=document.querySelector('#app')._vnode.component.proxy;return {view:a.spectrogram,usage:a.spectrogramResourceUsage()};});
  assert.equal(state.view.channels,2);assert.equal(state.view.span,.25);assert.equal(state.view.representation.end,121);
  await field('asr-spectrogram-time').fill('60.5');const audio=p.locator('[data-testid="asr-editor"] audio');await field('asr-spectrogram-play-marker')[width<737?'tap':'click']();await p.waitForFunction(()=>!document.querySelector('[data-testid="asr-editor"] audio').paused);
  await field('asr-spectrogram-play-marker')[width<737?'tap':'click']();const pausedTime=await audio.evaluate(e=>e.currentTime);assert(await audio.evaluate(e=>e.paused));
  await field('asr-spectrogram-play-marker')[width<737?'tap':'click']();const playingStart=await audio.evaluate(e=>e.currentTime);
  const requestStart=requests.length,panTimes=[];
  for(let i=0;i<10;i++){const t=Date.now();await field('asr-spectrogram-next')[width<737?'tap':'click']();await p.waitForFunction(()=>!document.querySelector('#app')._vnode.component.proxy.spectrogram.tilesLoading);panTimes.push(Date.now()-t);}
  assert(await audio.evaluate((e,time)=>!e.paused&&e.currentTime>=time,playingStart));await field('asr-spectrogram-play-marker')[width<737?'tap':'click']();const pausedAfterPan=await audio.evaluate(e=>e.currentTime);
  const scroll=field('asr-spectrogram-scroll');await scroll.evaluate(e=>{e.scrollLeft=e.scrollWidth-e.clientWidth;});
  await p.waitForFunction(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return !v.tilesLoading&&v.window.end>120.99;});
  const end=await field('asr-spectrogram-window').innerText();assert(end.includes('121.00'));assert.equal(await field('asr-spectrogram-time').inputValue(),'60.5');assert.equal(await audio.evaluate(e=>e.currentTime),pausedAfterPan);
  await field('asr-spectrogram-panel').scrollIntoViewIfNeeded();await p.screenshot({path:`${out}/long-${width}-end.png`});
  await scroll.evaluate(e=>{e.scrollLeft=0;});await p.waitForFunction(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return !v.tilesLoading&&v.window.start<1.01;});
  if(width<737){
   const session=await c.newCDPSession(p),box=await scroll.boundingBox(),x=box.x+box.width*.8,y=box.y+box.height*.4;
   await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
   for(let i=1;i<=5;i++)await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:x-i*box.width*.1,y}]});
   await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
   await p.waitForFunction(()=>document.querySelector('[data-testid="asr-spectrogram-scroll"]').scrollLeft>0);
   assert.equal(await field('asr-spectrogram-time').inputValue(),'60.5');
  }
  const shifted=requests.slice(requestStart);assert.equal(shifted.filter(r=>r.url.endsWith('/spectrogram/prepare')||r.url.includes('/spectrogram?')).length,0);
  const usage=await p.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.spectrogramResourceUsage());assert(usage.compressedBytes<=64*1024**2);assert(usage.decodedBytes<=16*1024**2);assert(await p.getByTestId('asr-editor').evaluate(e=>e.scrollWidth<=e.clientWidth+1));
  if(width===1024){await p.setViewportSize({width:900,height:960});assert.equal(await field('asr-spectrogram-time').inputValue(),'60.5');assert.equal(await audio.evaluate(e=>e.currentTime),pausedAfterPan);}
  await p.screenshot({path:`${out}/long-${width}-start.png`});assert.deepEqual(errors,[]);
  result={...result,result:'PASS',stereo:true,seconds:120,span:.25,prepareMs,panMs:panTimes,panPrepareCount:0,end,usage,touch:width<737,markerPreserved:true,playingThroughPan:true,pausedPositionPreserved:true,errors};
  console.log(JSON.stringify(result));
 }catch(error){result.error=error.message;result.view=await p.evaluate(()=>document.querySelector('#app')?._vnode?.component?.proxy?.spectrogram);result.requests=requests;await p.screenshot({path:`${out}/long-${width}-failure.png`});throw error;}
 finally{results.push(result);await p.close();const video=p.video();await c.close();await video.saveAs(`${out}/long-${width}.webm`);await writeFile(`${out}/long-results.json`,JSON.stringify(results,null,2));}
}
try{for(const width of [1024,390,320])if(!only||only===String(width))await scenario(width);}finally{await browser.close();}
