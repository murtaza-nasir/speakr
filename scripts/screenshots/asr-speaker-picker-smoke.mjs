import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';
import {openRecordingByTitle} from './helpers.mjs';

const base = process.env.SPEAKR_URL || 'http://127.0.0.1:8911';
assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname));
assert(!['8898', '8899'].includes(new URL(base).port), 'Synthetic fixture only');
const baseline = process.argv.includes('--baseline');
const out = path.resolve(process.env.SPEAKR_PICKER_OUT || 'output/playwright/asr-speaker-picker');
await mkdir(out, {recursive: true});
assert((await (await fetch(base + '/__asr-split-ready')).json()).isolated);
const browser = await chromium.launch(process.env.SPEAKR_CHROMIUM ? {executablePath: process.env.SPEAKR_CHROMIUM} : {});
const results = [];
for (const width of baseline ? [1024] : [1024, 320]) {
    const context = await browser.newContext({viewport: {width, height: 900}, locale: 'ru-RU', reducedMotion: 'reduce',
        serviceWorkers: 'block', recordVideo: {dir: out}, ...(width < 737 ? {isMobile: true, hasTouch: true} : {})});
    const page = await context.newPage(), errors = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {if (request.url().endsWith('/update_transcription')) writes.push(request.postDataJSON());});
    const field = id => page.getByTestId(id);
    const segments = ['Анна', 'Борис', 'Вера'].map((speaker, i) => ({speaker, start_time: i * 10, end_time: i * 10 + 8,
        sentence: 'Первая фраза. Ответ другого человека.', extra: {keep: i}}));
    try {
        const fixture = await (await context.request.post(base + '/__asr-split-fixture',
            {data: {segments, database_speakers: ['Анна', 'Борис', 'Вера']}})).json();
        assert(fixture.isolated);
        await page.goto(base, {waitUntil: 'domcontentloaded'});
        await page.locator('h4').filter({hasText: fixture.title}).first().waitFor();
        if (width < 737) await page.locator('header button').first().tap();
        await openRecordingByTitle(page, fixture.title);
        if (width < 737) await page.locator('[data-mobile-bottom-nav] button').filter({hasText: 'Транскрипция'}).tap();
        await page.locator('button[title="Редактировать транскрипт"]:visible').first().click();
        await field('asr-segment-speaker').waitFor();
        await field('asr-segment-speaker').focus();
        await field('asr-segment-speaker').press('ArrowDown');
        await page.screenshot({path: path.join(out, `open-${width}.png`)});
        assert.equal(writes.length, 0, 'Opening the picker must not save');
        await field('asr-segment-speaker').press('ArrowDown');
        await field('asr-segment-speaker').press('Enter');
        assert.equal(await field('asr-segment-speaker').inputValue(), 'Борис', 'Choose another speaker without erasing current name');
        if (!baseline) {
            // Mouse/touch opens the complete list, even with a populated name.
            const picker = field('asr-segment-speaker').locator('..');
            await picker.getByRole('button').click();
            assert.deepEqual(await picker.getByRole('option').allTextContents(), ['Анна', 'Борис', 'Вера']);
            await picker.getByRole('option', {name: 'Вера', exact: true}).click();
            assert.equal(await field('asr-segment-speaker').inputValue(), 'Вера');
            await field('asr-segment-speaker').fill('Новое имя');
            await field('asr-segment-speaker').press('Escape');
            await picker.getByRole('button').click();
            assert((await picker.getByRole('option').allTextContents()).includes('Новое имя'));
            await field('asr-segment-speaker').press('Escape');
            assert.equal(await picker.getByRole('listbox').count(), 0);
            await field('asr-segment-speaker').focus();
            await field('asr-start').click();
            assert.equal(await picker.getByRole('listbox').count(), 0, 'Blur closes suggestions');
            await field('asr-segment-text').evaluate(element => {
                element.focus(); element.setSelectionRange(14, 14); element.dispatchEvent(new Event('select', {bubbles: true}));
            });
            await field('asr-split-segment').click();
            await field('asr-split-preview').waitFor();
            for (const [i, name] of ['Анна', 'Борис'].entries()) {
                const input = field('asr-split-speaker-' + i), control = input.locator('..');
                await control.getByRole('button').click();
                assert((await control.getByRole('option').allTextContents()).includes(name));
                await control.getByRole('option', {name, exact: true}).click();
            }
            await field('asr-confirm-split').click();
            assert.equal(await field('asr-segment-speaker').inputValue(), 'Борис');
            assert.equal(await field('asr-segment-speaker').locator('..').getByRole('listbox').count(), 0, 'New selected object resets picker');
            await field('asr-save').click();
            await page.waitForFunction(() => document.querySelector('#app')._vnode.component.proxy.asrSaveState === 'saved');
            const saved = JSON.parse((await (await context.request.get(base + '/api/recordings/' + fixture.id)).json()).transcription);
            assert.equal(saved[0].speaker, 'Анна'); assert.equal(saved[1].speaker, 'Борис');
            assert(saved[0].speaker_id); assert(saved[1].speaker_id); assert.notEqual(saved[0].speaker_id, saved[1].speaker_id);
            assert.deepEqual(saved[0].extra, {keep: 0}); assert.deepEqual(saved[1].extra, {keep: 0});
            assert.deepEqual(saved.slice(2).map(s => ({speaker: s.speaker, sentence: s.sentence, extra: s.extra})),
                segments.slice(1).map(s => ({speaker: s.speaker, sentence: s.sentence, extra: s.extra})));
            assert.equal(writes.length, 1);
            await page.getByTestId('asr-editor').getByRole('button', {name: 'Закрыть', exact: true}).last().click();
            await page.locator('button[title="Редактировать транскрипт"]:visible').first().click();
            assert.equal(await field('asr-segment-speaker').inputValue(), 'Борис');
            await field('asr-segment-speaker').locator('..').getByRole('button').click();
            await page.screenshot({path: path.join(out, `after-${width}.png`)});
            assert(await page.getByTestId('asr-editor').evaluate(e => e.scrollWidth <= e.clientWidth + 1));
            const overflow = await page.getByTestId('asr-editor').locator('button:visible,input:visible,[role=option]:visible').evaluateAll(elements => elements
                .filter(e => e.getBoundingClientRect().left < -1 || e.getBoundingClientRect().right > innerWidth + 1).map(e => e.outerHTML));
            assert.deepEqual(overflow, []);
        }
        assert.deepEqual(errors, []);
        results.push({width, result: 'PASS', writes: writes.length, errors});
    } catch (error) {
        await page.screenshot({path: path.join(out, `failure-${width}.png`)});
        results.push({width, result: 'FAIL', error: error.message, errors});
    } finally {
        const video = page.video(); await page.close(); await context.close();
        await video.saveAs(path.join(out, `${baseline ? 'before' : 'after'}-${width}.webm`));
    }
}
await browser.close();
await writeFile(path.join(out, 'results.json'), JSON.stringify(results, null, 2));
console.log(JSON.stringify(results));
assert(results.every(result => result.result === 'PASS'));
