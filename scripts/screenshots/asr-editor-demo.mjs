import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';
import {openRecordingByTitle} from './helpers.mjs';

// Only synthetic fixture servers, never a normal Speakr instance.
const base = process.env.SPEAKR_URL || 'http://127.0.0.1:8913';
assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname));
assert(!['8898', '8899'].includes(new URL(base).port));
const before = process.argv.includes('--before');
const mobile = process.argv.includes('--mobile');
const legacyBrowser = process.argv.includes('--without-segmenter');
const out = path.resolve(process.env.SPEAKR_DEMO_OUT || 'output/playwright/asr-editor-demo');
await mkdir(out, {recursive: true});
const name = legacyBrowser ? 'without-segmenter' : before ? 'before' : mobile ? 'mobile' : 'after';
const browser = await chromium.launch();
const context = await browser.newContext({
    viewport: mobile ? {width: 390, height: 844} : {width: 1440, height: 1080},
    locale: 'en-US', reducedMotion: 'reduce', serviceWorkers: 'block',
    recordVideo: {dir: out, size: mobile ? {width: 390, height: 844} : {width: 960, height: 720}},
    ...(mobile ? {isMobile: true, hasTouch: true} : {})
});
await context.addInitScript(without => {
    localStorage.setItem('preferredLanguage', 'en');
    localStorage.setItem('darkMode', 'true');
    if (without) Object.defineProperty(Intl, 'Segmenter', {value: undefined});
}, legacyBrowser);
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const field = id => page.getByTestId(id);
try {
    assert.equal((await (await context.request.get(base + '/__asr-split-ready')).json()).isolated, true);
    const fixture = await (await context.request.post(base + '/__asr-split-fixture', {data: {
        spectrogram: true, audio_duration: 16,
        segments: [
            {speaker: 'SPEAKER_00', start_time: 0, end_time: 8, sentence: 'Welcome to the demo. Thanks, let us get started.'},
            {speaker: 'SPEAKER_01', start_time: 9, end_time: 12, sentence: 'We can correct one utterance at a time.'},
            {speaker: 'SPEAKER_00', start_time: 13, end_time: 15, sentence: 'This recording and its audio are synthetic.'}
        ]
    }})).json();
    assert.equal(fixture.isolated, true);
    await page.goto(base, {waitUntil: 'domcontentloaded', timeout: 60000});
    if (mobile) await page.locator('header button').first().tap();
    await openRecordingByTitle(page, fixture.title);
    if (mobile) await page.locator('[data-mobile-bottom-nav] button').filter({hasText: 'Transcription'}).tap();
    await page.locator('[title="Edit transcript"]:visible').first().click();
    await page.getByText('Edit ASR Transcription', {exact: true}).waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(1000);
    if (before) {
        await page.screenshot({path: path.join(out, 'before.png')});
        await page.waitForTimeout(1200);
    } else {
        await field('asr-detail').waitFor();
        assert.equal(await field('asr-segment-text').inputValue(), 'Welcome to the demo. Thanks, let us get started.');
        await page.screenshot({path: path.join(out, name + '-workspace.png')});
        if (legacyBrowser) {
            assert(await field('asr-split-segment').isDisabled());
            assert.match(await field('asr-split-segment').getAttribute('title'), /Update your browser/);
            await field('asr-segment-text').fill('Editing still works in this browser.');
            await field('asr-save').click();
            await field('asr-save-status').filter({hasText: 'Saved'}).waitFor();
        } else {
            await field('asr-show-spectrogram').click();
            await field('asr-spectrogram-plot').waitFor({timeout: 120000});
            await field('asr-spectrogram-panel').scrollIntoViewIfNeeded();
            await field('asr-spectrogram-time').fill('3.5');
            await page.screenshot({path: path.join(out, name + '-spectrum.png')});
            assert(await field('asr-spectrogram-zoom-out').isDisabled());
            assert(await field('asr-spectrogram-fit').isDisabled());
            if (!mobile) {
                await field('asr-spectrogram-play').click();
                await page.waitForTimeout(600);
                assert.match(await field('asr-spectrogram-play').innerText(), /Pause/);
                await field('asr-spectrogram-play').click();
                await field('asr-spectrogram-zoom-in').click();
                await page.waitForFunction(() => {
                    const view = document.querySelector('#app')._vnode.component.proxy.spectrogram;
                    return view.url && !view.loading && !view.tilesLoading;
                });
                const count = async () => (await (await context.request.get(base + '/__asr-spectrum-counters')).json()).renderCount;
                const renders = await count();
                await field('asr-spectrogram-next').click();
                await field('asr-spectrogram-fit').click();
                await page.waitForFunction(() => {
                    const view = document.querySelector('#app')._vnode.component.proxy.spectrogram;
                    return view.url && !view.loading && !view.tilesLoading &&
                        view.window.start === 0 && view.window.end === 8;
                });
                assert.equal(await count(), renders);
                assert.equal(await field('asr-spectrogram-time').inputValue(), '3.5');
                await field('asr-segment-text').evaluate(element => {
                    element.focus(); const at = element.value.indexOf('Thanks');
                    element.setSelectionRange(at, at); element.dispatchEvent(new Event('select', {bubbles: true}));
                });
                await field('asr-split-segment').click();
                await field('asr-split-preview').waitFor();
                await page.screenshot({path: path.join(out, 'split-preview.png')});
                await field('asr-confirm-split').click();
                assert.equal(await field('asr-start').inputValue(), '3.5');
                await field('asr-segment-speaker').fill('SPEAKER_01');
                await field('asr-save').click();
                await field('asr-save-status').filter({hasText: 'Saved'}).waitFor();
                const saved = JSON.parse((await (await context.request.get(`${base}/api/recordings/${fixture.id}`)).json()).transcription);
                assert.equal(saved.length, 4); assert.equal(saved[1].speaker, 'SPEAKER_01');
                assert.equal(saved[0].end_time, saved[1].start_time);
                await page.waitForTimeout(600);
            }
        }
    }
    assert.deepEqual(errors, []);
    await writeFile(path.join(out, name + '-results.json'), JSON.stringify({result: 'PASS', synthetic: true, errors}, null, 2));
} finally {
    const video = page.video();
    await context.close();
    if (video) await video.saveAs(path.join(out, name + '.webm'));
    await browser.close();
}
