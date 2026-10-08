import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
const {chromium} = await import(process.env.SPEAKR_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.SPEAKR_URL || 'http://127.0.0.1:8918';
assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname) && !['8898', '8899'].includes(new URL(base).port));
const baseline = process.argv.includes('--baseline');
const out = 'output/playwright/unified-workspace';
await mkdir(out, {recursive:true});
let ready=false;
for(let i=0;i<30;i++){try{ready=(await (await fetch(base+'/__asr-split-ready')).json()).isolated;}catch{}if(ready)break;await new Promise(r=>setTimeout(r,1000));}
assert(ready);
const browser = await chromium.launch({executablePath:process.env.SPEAKR_CHROMIUM_PATH});
const context = await browser.newContext({viewport:{width:1440,height:1000},locale:'ru-RU',serviceWorkers:'block',recordVideo:{dir:out}});
const fixture = await (await context.request.post(base+'/__asr-split-fixture', {data:{count:12,spectrogram:true,audio_duration:120,autosave:false}})).json();
assert(fixture.isolated);
const page = await context.newPage(), errors=[];
page.on('pageerror', e=>errors.push(e.message));
try {
    await page.goto(base,{waitUntil:'domcontentloaded',timeout:120000});
    await page.locator('h4').filter({hasText:fixture.title}).first().click();
    await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.selectedRecording?.transcription);
    await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.openSpeakerModal());
    await page.locator('.spk-modal:visible').waitFor();
    await page.screenshot({path:out+(baseline?'/before-speakers.png':'/after-speakers.png')});
    if (baseline) {
        await page.evaluate(()=>{const app=document.querySelector('#app')._vnode.component.proxy; app.closeSpeakerModal(); app.openAsrEditorAtSegment(0);});
        await page.getByTestId('asr-detail').waitFor();
        await page.screenshot({path:out+'/before-editor.png'});
    } else {
        await page.getByTestId('workspace-speaker-Анна').locator('.spk-row-head').click();
        await page.getByTestId('workspace-transcript').evaluate(e=>e.dataset.identity='kept');
        await page.getByTestId('workspace-edit-0').click();
        assert(!await page.getByTestId('workspace-speakers').isVisible());
        assert(await page.getByTestId('workspace-editor').isVisible());
        const text=page.getByTestId('asr-segment-text'), original=await text.inputValue();
        await text.fill(original+' Правка.');
        await page.getByTestId('workspace-back').click();
        await page.getByTestId('workspace-edit-0').click();
        assert.equal(await text.inputValue(),original+' Правка.');
        assert.equal(await page.getByTestId('workspace-transcript').getAttribute('data-identity'),'kept');
        await page.screenshot({path:out+'/after-editor.png'});
        await text.evaluate(e=>{e.focus();const n=e.value.indexOf('Ответ');e.setSelectionRange(n,n);e.dispatchEvent(new Event('select',{bubbles:true}));});
        await text.press('Control+Enter'); await page.getByTestId('asr-split-preview').waitFor();
        await page.getByTestId('asr-split-speaker-0').fill('Анна');
        await page.getByTestId('asr-split-speaker-1').fill('Борис');
        await page.getByTestId('asr-confirm-split').click();
        assert(await page.locator('[data-list-index="0"]').isVisible());
        assert(await page.locator('[data-list-index="1"]').isVisible());
        await page.getByTestId('workspace-back').click();
        assert(await page.getByTestId('workspace-speakers').isVisible());
        assert(await page.locator('[data-list-index="1"]').isVisible());
        await page.getByTestId('asr-save').click();
        await page.getByTestId('asr-save-status').filter({hasText:'Сохранено'}).waitFor();
        const saved=await (await context.request.get(base+`/api/recordings/${fixture.id}`)).json();
        const segments=JSON.parse(saved.transcription); assert.equal(segments.length,13);
        assert.deepEqual(segments.slice(0,2).map(s=>s.speaker),['Анна','Борис']);
        await page.screenshot({path:out+'/after-split.png'});
        await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.closeAsrEditorModal());
        await page.evaluate(()=>document.querySelector('#app')._vnode.component.proxy.openAsrEditorAtSegment(1));
        await page.getByTestId('asr-detail').waitFor();
        assert.equal(await page.getByTestId('asr-segment-speaker').inputValue(),'Борис');
        // Cached spectrum controls and bounded playback still use the shared player.
        await page.getByTestId('asr-show-spectrogram').click();
        await page.getByTestId('asr-spectrogram-plot').waitFor({timeout:90000});
        assert(await page.getByTestId('asr-spectrogram-zoom-out').isDisabled());
        await page.getByTestId('asr-spectrogram-zoom-in').click();
        await page.getByTestId('asr-spectrogram-fit').click();
        await page.getByTestId('asr-spectrogram-play').click();
        await page.waitForFunction(()=>!document.querySelector('[data-testid="asr-editor"] audio').paused);
        await page.getByTestId('asr-spectrogram-play').click();
        await page.waitForFunction(()=>document.querySelector('[data-testid="asr-editor"] audio').paused);
        await page.screenshot({path:out+'/after-spectrum.png'});
    }
    assert.deepEqual(errors, []);
} catch(error) {
    await page.screenshot({path:out+'/failure.png'}); console.log(errors); throw error;
} finally {
    const video = page.video(); await page.close(); await context.close();
    await video.saveAs(out+(baseline?'/before.webm':'/after.webm')); await browser.close();
}
