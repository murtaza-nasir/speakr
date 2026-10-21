import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {chromium} from 'playwright';
import {openRecordingByTitle} from './helpers.mjs';
const before=process.argv.includes('--before'),base=process.env.SPEAKR_URL||`http://127.0.0.1:${before?8905:8906}`;
assert(['8905','8906'].includes(new URL(base).port));
const out='output/playwright/asr-workplace-spectrum-refinements';await mkdir(out,{recursive:true});
const browser=await chromium.launch(),c=await browser.newContext({viewport:{width:1024,height:960},reducedMotion:'reduce',locale:'ru-RU',recordVideo:{dir:out}}),p=await c.newPage();
const field=id=>p.getByTestId(id),requests=[],errors=[];p.on('pageerror',e=>errors.push(e.message));p.on('request',r=>{if(r.url().includes('/spectrogram'))requests.push({url:r.url(),method:r.method()});});
let result;
try{
 assert((await(await c.request.get(base+'/__asr-split-ready')).json()).isolated);
 const f=await(await c.request.post(base+'/__asr-split-fixture',{data:{spectrogram:true,count:2}})).json();assert(f.isolated);
 await p.goto(base,{waitUntil:'domcontentloaded',timeout:60000});await openRecordingByTitle(p,f.title);
 await p.locator('button[title="Редактировать транскрипт"]:visible').first().click();await field('asr-show-spectrogram').click();await field('asr-spectrogram-plot').waitFor({timeout:60000});
 const audio=p.locator('[data-testid="asr-editor"] audio');await field('asr-spectrogram-play').click();await p.waitForFunction(()=>!document.querySelector('[data-testid="asr-editor"] audio').paused);
 await field('asr-spectrogram-panel').scrollIntoViewIfNeeded();await p.waitForFunction(()=>document.querySelector('[data-testid="asr-editor"] audio').currentTime>.4);const firstLabel=await field('asr-spectrogram-play').innerText();const firstTime=await audio.evaluate(e=>e.currentTime);
 await p.screenshot({path:out+(before?'/before-playing.png':'/after-playing.png')});await field('asr-spectrogram-play').click();
 if(before)await p.waitForFunction(()=>!document.querySelector('[data-testid="asr-editor"] audio').paused);
 const paused=await audio.evaluate(e=>e.paused),afterTime=await audio.evaluate(e=>e.currentTime);
 if(!before){assert(paused);assert(afterTime>=firstTime);await p.screenshot({path:out+'/after-paused.png'});await p.waitForTimeout(500);assert.equal(await audio.evaluate(e=>e.currentTime),afterTime);await field('asr-play').click();assert(await audio.evaluate((e,time)=>!e.paused && e.currentTime>=time,afterTime));await field('asr-spectrogram-play').click();assert(await audio.evaluate(e=>e.paused));}
 await audio.evaluate(e=>e.pause());await field('asr-spectrogram-zoom-in').click();
 await p.waitForFunction(()=>{const e=document.querySelector('[data-testid="asr-spectrogram-window"]');const s=e&&[...e.querySelectorAll('span')].map(x=>parseFloat(x.textContent));return s?.length===2&&Math.abs(s[1]-s[0]-4)<.01;});await field('asr-spectrogram-plot').waitFor({timeout:60000});
 const windowBefore=await field('asr-spectrogram-window').innerText(),count=requests.length;await field('asr-spectrogram-next').click();
 await p.waitForFunction(old=>document.querySelector('[data-testid="asr-spectrogram-window"]')?.innerText!==old,windowBefore);
 await field('asr-spectrogram-plot').waitFor({timeout:60000});
 const shiftedRequests=requests.slice(count),generatedOnPan=shiftedRequests.filter(r=>r.url.endsWith('/spectrogram/prepare')||(!r.url.includes('/preparations/') && r.method==='GET')).length;
 result={phase:before?'before':'after',result:'FAIL',firstLabel,firstTime,afterTime,paused,generatedOnPan,requests,errors};
 if(!before)assert.equal(generatedOnPan,0);else assert(!paused && generatedOnPan>0,'Both original defects must be reproduced');
 await p.screenshot({path:out+(before?'/before-pan.png':'/after-pan.png')});
 result.result='PASS';
 assert.deepEqual(errors,[]);
}catch(e){await p.screenshot({path:out+'/failure.png'});throw e;}
finally{await p.close();const video=p.video();await c.close();await video.saveAs(out+(before?'/before.webm':'/after.webm'));await browser.close();if(result)await writeFile(out+(before?'/before-results.json':'/after-results.json'),JSON.stringify(result,null,2));}
