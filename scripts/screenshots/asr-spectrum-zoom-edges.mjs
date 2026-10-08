import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {chromium} from 'playwright';
import {openRecordingByTitle} from './helpers.mjs';
const base=process.env.SPEAKR_URL||'http://127.0.0.1:8911',out='output/playwright/asr-workplace-spectrum-zoom/edges';
assert(['localhost','127.0.0.1'].includes(new URL(base).hostname)&&!['8898','8899'].includes(new URL(base).port));await mkdir(out,{recursive:true});
const browser=await chromium.launch(),context=await browser.newContext({viewport:{width:1024,height:960},locale:'ru-RU',reducedMotion:'reduce',recordVideo:{dir:out}}),page=await context.newPage();
const field=id=>page.getByTestId(id),errors=[],requests=[];page.setDefaultTimeout(120000);
page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().includes('/spectrogram'))requests.push({method:r.method(),url:r.url(),body:r.postData()});});
const view=()=>page.evaluate(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v&&{span:v.span,window:v.window,marker:v.marker,url:v.url,error:v.error};});
const settle=()=>page.waitForFunction(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v&&v.url&&!v.loading&&!v.tilesLoading;},null,{timeout:180000});
async function open(start,end){
 const f=await(await context.request.post(base+'/__asr-split-fixture',{data:{spectrogram:true,audio_duration:end>3?Math.ceil(end+1):2,segments:[{speaker:'Анна',start_time:start,end_time:end,sentence:'Реплика на границе записи.'}]}})).json();
 await page.goto(base,{waitUntil:'domcontentloaded',timeout:120000});await page.locator('h4').filter({hasText:f.title}).first().waitFor();await openRecordingByTitle(page,f.title);
 await page.locator('button[title="Редактировать транскрипт"]:visible').first().click();await field('asr-show-spectrogram').click();await settle();
}
let result={result:'FAIL'};
try{
 assert((await(await context.request.get(base+'/__asr-split-ready')).json()).isolated);
 await open(1,1.1);const tiny=(await view()).window;assert(Math.abs(tiny.start-.925)<1e-8&&Math.abs(tiny.end-1.175)<1e-8);
 for(const id of ['asr-spectrogram-zoom-in','asr-spectrogram-zoom-out','asr-spectrogram-fit'])assert(await field(id).isDisabled());
 await page.screenshot({path:out+'/tiny.png',animations:'disabled'});
 await open(1,3);assert.deepEqual((await view()).window,{start:1,end:2});assert(await field('asr-spectrogram-fit').isDisabled());
 await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.navigateSpectrogram(.25));await settle();
 await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.fitSpectrogram());await settle();assert.deepEqual((await view()).window,{start:1,end:2});
 await page.screenshot({path:out+'/eof.png',animations:'disabled'});
 await open(1,121);await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.navigateSpectrogram(.5));await settle();const detail=await view();assert.equal(detail.span,30);
 await page.route('**/spectrogram/prepare',route=>route.request().postDataJSON().span===120?route.fulfill({status:503,contentType:'application/json',body:'{"code":"timeout"}'}):route.continue());
 await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.fitSpectrogram());assert.equal((await view()).span,30);assert.deepEqual((await view()).window,detail.window);
 await page.screenshot({path:out+'/failed-fit.png',animations:'disabled'});await page.unroute('**/spectrogram/prepare');
 await field('asr-spectrogram-error').getByRole('button',{name:'Повторить',exact:true}).click();await page.waitForFunction(()=>{const v=document.querySelector('#app')._vnode.component.proxy.spectrogram;return v.span===120&&!v.loading&&!v.tilesLoading;},null,{timeout:180000});
 await page.screenshot({path:out+'/retried-full-fit.png',animations:'disabled'});
 await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.navigateSpectrogram(.25));await settle();const previous=await view();let release,accepted;
 await page.route('**/spectrogram/prepare',async route=>{if(route.request().postDataJSON().span!==120)return route.continue();const response=await route.fetch();accepted=await response.json();await new Promise(resolve=>{release=resolve;});await route.fulfill({response});});
 await field('asr-spectrogram-fit').click();await field('asr-spectrogram-status').waitFor();
 for(let i=0;i<120&&!release;i++)await page.waitForTimeout(100);assert(release);
 await field('asr-spectrogram-status').getByRole('button',{name:'Отмена',exact:true}).click();release();
 await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.spectrogram.error==='cancelled');
 for(let i=0;i<120&&!requests.some(r=>r.method==='DELETE'&&r.url.includes(accepted.id));i++)await page.waitForTimeout(100);
 assert(requests.some(r=>r.method==='DELETE'&&r.url.includes(accepted.id)));assert.deepEqual((await view()).window,previous.window);assert.equal((await view()).span,previous.span);
 await page.unroute('**/spectrogram/prepare');await page.screenshot({path:out+'/cancelled-late-fit.png',animations:'disabled'});assert.deepEqual(errors,[]);
 result={result:'PASS',tinyMinimum:true,actualEOF:true,failedFitRetry120:true,lateFitCancelled:true,lateLeaseReleased:true,errors};console.log(JSON.stringify(result));
}catch(error){result.error=error.message;await page.screenshot({path:out+'/failure.png'});throw error;}
finally{await writeFile(out+'/results.json',JSON.stringify({...result,requests},null,2));const video=page.video();await page.close();await context.close();await video.saveAs(out+'/edges.webm');await browser.close();}
