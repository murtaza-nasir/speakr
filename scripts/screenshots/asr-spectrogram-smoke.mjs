import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { BASE_URL, VIEWPORT, MOBILE_VIEWPORT, openRecordingByTitle } from './helpers.mjs';

assert(['localhost','127.0.0.1'].includes(new URL(BASE_URL).hostname) && !['8899','8898'].includes(new URL(BASE_URL).port));
const out=path.resolve(process.env.SPEAKR_SPECTROGRAM_OUT || 'temp/asr-spectrogram/ui');
await mkdir(out,{recursive:true});
let ready=false;
for(let i=0;i<60&&!ready;i++) {
    try {const r=await fetch(`${BASE_URL}/__asr-split-ready`,{signal:AbortSignal.timeout(2000)});ready=r.ok&&(await r.json()).fixture==='asr-segment-split';} catch {}
    if(!ready)await new Promise(resolve=>setTimeout(resolve,250));
}
assert(ready,'Disposable fixture server must be ready before UI scenarios');
const browser=await chromium.launch();
const results=[];
const only=process.argv.find(arg=>arg.startsWith('--only='))?.slice(7);
const row=(p,i)=>p.locator(`[data-segment-index="${i}"]`);
const field=(p,i)=>row(p,i).locator('textarea');
async function caret(p,i) {
    await field(p,i).evaluate(el=>{const cut=el.value.indexOf('Ответ');el.focus();el.setSelectionRange(cut,cut);el.dispatchEvent(new Event('select',{bubbles:true}));});
}
async function plot(p,i) {
    await row(p,i).locator('[data-testid="asr-show-spectrogram"]').click();
    await p.locator('[data-testid="asr-spectrogram-plot"]').waitFor();
    await p.locator('[data-testid="asr-spectrogram-plot"] img').evaluate(el=>el.decode());
}
async function save(p,context,id) {
    const post=p.waitForResponse(r=>r.url().endsWith(`/recording/${id}/update_transcription`)&&r.request().method()==='POST');
    await p.locator('[data-testid="asr-editor"] button').filter({hasText:/^Сохранить$/}).click();
    assert((await post).ok());
    const data=await (await context.request.get(`${BASE_URL}/api/recordings/${id}`)).json();
    return JSON.parse(data.transcription);
}
async function scenario(name,run,options={}) {
    if(only && only!==name)return;
    const context=await browser.newContext({viewport:options.mobile?MOBILE_VIEWPORT:VIEWPORT,locale:'ru-RU',
        ...(options.mobile?{isMobile:true,hasTouch:true}:{}),recordVideo:{dir:out,size:options.mobile?MOBILE_VIEWPORT:VIEWPORT}});
    const page=await context.newPage(); const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    try {
        const ready=await context.request.get(`${BASE_URL}/__asr-split-ready`);
        assert(ready.ok()&&(await ready.json()).fixture==='asr-segment-split');
        const response=await context.request.post(`${BASE_URL}/__asr-split-fixture`,{data:{spectrogram:true,long:!!options.long,autosave:!!options.autosave}});
        assert(response.ok()); const fixture=await response.json(); assert(fixture.isolated);
        await page.goto(BASE_URL,{waitUntil:'domcontentloaded'});
        await page.locator('h4').filter({hasText:fixture.title}).first().waitFor();
        if(options.mobile) await page.locator('header button').first().click();
        await openRecordingByTitle(page,fixture.title);
        if(options.mobile) await page.locator('[data-mobile-bottom-nav] button').filter({hasText:'Транскрипция'}).click();
        await page.locator('button[title="Редактировать транскрипт"]:visible').first().click();
        await page.locator('.asr-editor-table').waitFor();
        await run(page,context,fixture.id);
        assert.deepEqual(errors,[]);
        await page.screenshot({path:path.join(out,`${name}.png`),animations:'disabled'});
        results.push({name,result:'PASS',errors});console.log(`PASS ${name}`);
    } catch(error) {
        await page.screenshot({path:path.join(out,`${name}-failure.png`),animations:'disabled'});
        results.push({name,result:'FAIL',error:error.stack,errors});console.error(`FAIL ${name}: ${error.message}`);
    } finally {
        const video=page.video();await page.close();await context.close();await video.saveAs(path.join(out,`${name}.webm`));
    }
}
try {
    await scenario('time-marker-save-reload',async(p,c,id)=>{
        await caret(p,0); await plot(p,0);
        assert.equal(await p.locator('[data-testid="asr-spectrogram-panel"] img').count(),1);
        await p.locator('[data-testid="asr-spectrogram-plot"]').click({position:{x:100,y:30}});
        assert(await row(p,0).locator('[data-testid="asr-split-segment"]').isEnabled(),'Plot click retains caret');
        await p.locator('[data-testid="asr-spectrogram-time"]').fill('3.5');
        await p.screenshot({path:path.join(out,'open-stereo-marker.png'),animations:'disabled'});
        await row(p,0).locator('[data-testid="asr-split-segment"]').click();
        assert.equal(await p.locator('[data-testid="asr-spectrogram-panel"]').count(),0);
        await row(p,1).locator('td:first-child input').fill('Борис');
        const data=await save(p,c,id);
        assert.equal(data.length,221);assert.equal(data[0].end_time,3.5);assert.equal(data[1].start_time,3.5);
        assert.equal(data[1].speaker,'Борис');
        await p.reload({waitUntil:'domcontentloaded'});await openRecordingByTitle(p,(await (await c.request.get(`${BASE_URL}/api/recordings/${id}`)).json()).title);
        await p.locator('button[title="Редактировать транскрипт"]:visible').first().click();
        assert.equal(await row(p,0).locator('input[type="number"]').nth(1).inputValue(),'3.5');
    });
    await scenario('playback-and-window-cache',async(p)=>{
        let requests=0;p.on('request',r=>{if(r.url().includes('/spectrogram?'))requests++;});
        await plot(p,0);
        await p.locator('[data-testid="asr-spectrogram-time"]').fill('7.8');
        await p.locator('[data-testid="asr-spectrogram-play-marker"]').click();
        await p.waitForFunction(()=>document.querySelector('[data-testid="asr-editor"] audio').paused);
        assert(await p.locator('[data-testid="asr-editor"] audio').evaluate(el=>el.currentTime>=8&&el.currentTime<8.6));
        await p.locator('[data-testid="asr-spectrogram-zoom-in"]').click();await p.waitForTimeout(500);
        await p.locator('[data-testid="asr-spectrogram-plot"]').waitFor();
        await p.locator('[data-testid="asr-spectrogram-zoom-out"]').click();await p.waitForTimeout(200);
        assert.equal(requests,2,'Original window reused from modal cache');
    });
    await scenario('long-virtual-window',async(p,c,id)=>{
        await p.locator('[data-testid="asr-editor-scroll"]').evaluate(el=>{el.scrollTop=150*44;el.dispatchEvent(new Event('scroll'));});
        await field(p,150).waitFor(); await caret(p,150); await plot(p,150);
        assert((await p.locator('[data-testid="asr-spectrogram-window"]').innerText()).includes('1560.00'));
        await p.locator('[data-testid="asr-spectrogram-next"]').click();
        await p.waitForFunction(()=>document.querySelector('[data-testid="asr-spectrogram-window"]')?.textContent.includes('1590.00'));
        await p.locator('[data-testid="asr-spectrogram-time"]').fill('1575');
        await caret(p,150);await field(p,150).press('Control+Enter');
        const data=await save(p,c,id);assert.equal(data[150].end_time,1575);assert.equal(data[151].start_time,1575);
    },{long:true});
    await scenario('errors-stale-and-manual-fallback',async(p)=>{
        await p.route('**/spectrogram?*',r=>r.fulfill({status:422,contentType:'application/json',body:JSON.stringify({code:'media'})}));
        await caret(p,0); await row(p,0).locator('[data-testid="asr-show-spectrogram"]').click();
        await p.locator('[data-testid="asr-spectrogram-error"]').waitFor();
        assert.equal(await field(p,0).inputValue(),'Первая фраза 0. Ответ другого человека 0.');
        await p.unroute('**/spectrogram?*');
        await p.locator('[data-testid="asr-spectrogram-error"] button').click();await p.locator('[data-testid="asr-spectrogram-plot"]').waitFor();
        await p.locator('[data-testid="asr-spectrogram-time"]').fill('0');
        assert(await row(p,0).locator('[data-testid="asr-split-segment"]').isDisabled());
        await p.locator('[data-testid="asr-spectrogram-panel"] button').filter({hasText:'Сбросить отметку'}).click();
        assert(await row(p,0).locator('[data-testid="asr-split-segment"]').isEnabled());
        await row(p,0).locator('input[type="number"]').nth(1).fill('9');
        await p.waitForFunction(()=>document.querySelector('[data-testid="asr-spectrogram-panel"]')?.textContent.includes('9.00'));
        await row(p,0).locator('[data-testid="asr-split-segment"]').click();
        assert.equal(await p.locator('[data-testid="asr-spectrogram-panel"]').count(),0);
    });
    await scenario('autosave-marked-split',async(p,c,id)=>{
        await plot(p,0);await p.locator('[data-testid="asr-spectrogram-time"]').fill('6.25');await caret(p,0);await field(p,0).press('Control+Enter');
        await p.waitForTimeout(2300);
        const data=await (await c.request.get(`${BASE_URL}/api/recordings/${id}`)).json();
        assert.equal(JSON.parse(data.transcription)[0].end_time,6.25);
    },{autosave:true});
    await scenario('mobile-spectral-split',async(p,c,id)=>{
        await p.locator('[data-testid="asr-editor-scroll"]').evaluate(el=>el.scrollLeft=el.scrollWidth);
        await plot(p,0);
        const panel=await p.locator('[data-testid="asr-spectrogram-panel"]').boundingBox();assert(panel.x>=0&&panel.width<=390);
        await p.locator('[data-testid="asr-spectrogram-time"]').fill('4.2');
        await p.screenshot({path:path.join(out,'mobile-open.png'),animations:'disabled'});
        await caret(p,0);
        await field(p,0).press('Control+Enter');
        const data=await save(p,c,id);assert.equal(data[0].end_time,4.2);
    },{mobile:true});
} finally {
    await browser.close();await writeFile(path.join(out,'results.json'),JSON.stringify(results,null,2));
}
if(results.some(r=>r.result==='FAIL'))process.exitCode=1;
