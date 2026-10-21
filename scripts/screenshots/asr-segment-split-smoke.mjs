// Run against asr-split-dev-server.py in a disposable container, never a live service.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { BASE_URL, VIEWPORT, MOBILE_VIEWPORT, openRecordingByTitle } from './helpers.mjs';

const url = new URL(BASE_URL);
assert(['127.0.0.1', 'localhost'].includes(url.hostname), 'Smoke requires a local disposable dev server');
assert(url.port && url.port !== '8899', 'Refusing the installed Speakr port');
const baseline = process.argv.includes('--baseline');
const only = process.argv.find(arg => arg.startsWith('--only='))?.slice(7);
const out = path.resolve(process.env.SPEAKR_SPLIT_OUT || 'temp/asr-split');
await mkdir(out, { recursive: true });
// Docker publishes the port before Flask finishes initialization. Wait only for readiness,
// without retrying a failed product scenario or sending writes to an unverified service.
let ready = false;
for (let attempt = 0; attempt < 30 && !ready; attempt++) {
    try {
        const response = await fetch(`${BASE_URL}/__asr-split-ready`, { signal: AbortSignal.timeout(2000) });
        ready = response.ok && (await response.json()).fixture === 'asr-segment-split';
    } catch { /* The disposable server is still starting. */ }
    if (!ready) await new Promise(resolve => setTimeout(resolve, 250));
}
assert(ready, 'Disposable-server readiness guard failed');
const browser = await chromium.launch();
const failures = [];
const results = [];

const row = (page, index) => page.locator(`.asr-editor-table tr[data-segment-index="${index}"]`);
const text = (page, index) => row(page, index).locator('textarea');
const speaker = (page, index) => row(page, index).locator('td:first-child input');
const split = (page, index) => row(page, index).locator('[data-testid="asr-split-segment"]');

async function caret(page, index, position, end = position) {
    await text(page, index).evaluate((el, [p, e]) => {
        el.focus(); el.setSelectionRange(p, e); el.dispatchEvent(new Event('select', { bubbles: true }));
    }, [position, end]);
}

async function openEditor(page) {
    await page.locator('button[title="Редактировать транскрипт"]:visible').first().click();
    await page.locator('.asr-editor-table').waitFor();
}

async function savedSegments(context, id) {
    const response = await context.request.get(`${BASE_URL}/api/recordings/${id}`);
    assert(response.ok(), `Read-back HTTP ${response.status()}`);
    const data = await response.json();
    return JSON.parse((data.recording || data).transcription);
}

async function fixture(context, autosave = false) {
    const ready = await context.request.get(`${BASE_URL}/__asr-split-ready`);
    assert(ready.ok() && (await ready.json()).fixture === 'asr-segment-split', 'Disposable-server guard failed');
    const response = await context.request.post(`${BASE_URL}/__asr-split-fixture`, { data: { autosave } });
    assert(response.ok(), `Fixture HTTP ${response.status()}`);
    const data = await response.json();
    assert(data.isolated === true, 'Fixture must be isolated');
    return data;
}

async function scenario(name, run, { mobile = false, autosave = false } = {}) {
    if (only && only !== name) return;
    const context = await browser.newContext({
        viewport: mobile ? MOBILE_VIEWPORT : VIEWPORT,
        locale: 'ru-RU', timezoneId: 'Europe/Moscow',
        ...(mobile ? { isMobile: true, hasTouch: true } : {}),
        recordVideo: { dir: out, size: mobile ? MOBILE_VIEWPORT : VIEWPORT }
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', e => { if (e.type() === 'error') errors.push(e.text()); });
    try {
        const data = await fixture(context, autosave);
        await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
        await page.locator('h4').filter({ hasText: data.title }).first().waitFor();
        if (mobile) await page.locator('header button').first().click();
        await openRecordingByTitle(page, data.title);
        if (mobile) await page.locator('[data-mobile-bottom-nav] button').filter({ hasText: 'Транскрипция' }).click();
        await openEditor(page);
        if (!baseline) assert(await page.locator('[data-testid="asr-split-hint"]').isVisible());
        await run(page, context, data.id);
        // The app may probe optional APIs; only actual JS/renderer failures are acceptable here.
        assert.deepEqual(errors, [], 'Browser console/page errors');
        await page.screenshot({ path: path.join(out, `${name}.png`), animations: 'disabled' });
        results.push({ name, result: 'PASS', errors });
        console.log(`PASS ${name}`);
    } catch (error) {
        failures.push({ name, error: error.stack, errors });
        await page.screenshot({ path: path.join(out, `${name}-failure.png`), animations: 'disabled' });
        console.error(`FAIL ${name}: ${error.message}`);
    } finally {
        const video = page.video();
        await page.close();
        await context.close();
        await video.saveAs(path.join(out, `${name}.webm`));
    }
}

try {
    if (baseline) {
        await scenario('baseline-add-below', async page => {
            assert.equal(await split(page, 0).count(), 0, 'Baseline must use the original template');
            const original = await text(page, 0).inputValue();
            await row(page, 0).locator('button[title="Добавить сегмент ниже"]').click();
            assert.equal(await text(page, 0).inputValue(), original);
            assert.equal(await text(page, 1).inputValue(), '');
            assert.equal(await row(page, 1).locator('input[type="number"]').first().inputValue(), '8');
        });
    } else {
        await scenario('button-save-reload', async (page, context, id) => {
            const original = await savedSegments(context, id);
            assert.equal(await split(page, 0).isDisabled(), true);
            const p = (await text(page, 0).inputValue()).indexOf('Ответ');
            await caret(page, 0, p);
            assert.equal(await split(page, 0).isEnabled(), true);
            await split(page, 0).click();
            await speaker(page, 1).waitFor();
            await page.waitForFunction(() => document.activeElement?.dataset.testid === 'asr-segment-speaker');
            assert.equal(await text(page, 0).inputValue(), 'Первая фраза 0.');
            assert.equal(await text(page, 1).inputValue(), 'Ответ другого человека 0.');
            assert.equal(await speaker(page, 1).evaluate(el => el.selectionEnd - el.selectionStart), 4);
            await speaker(page, 1).fill('Борис');
            const response = page.waitForResponse(r => r.url().endsWith(`/recording/${id}/update_transcription`) && r.request().method() === 'POST');
            await page.locator('[data-testid="asr-editor"] button').filter({ hasText: /^Сохранить$/ }).click();
            assert((await response).ok());
            const saved = await savedSegments(context, id);
            assert.equal(saved.length, 221);
            assert.equal(saved[1].speaker, 'Борис');
            assert.equal(saved[0].end_time, saved[1].start_time);
            assert.equal(saved[0].start_time, 0);
            assert.equal(saved[1].end_time, 8);
            assert.deepEqual(saved.slice(2), original.slice(1));
            assert.deepEqual(Object.keys(saved[0]).sort(), ['end_time', 'sentence', 'speaker', 'start_time']);
            await page.reload({ waitUntil: 'domcontentloaded' });
            await page.locator('h4').filter({ hasText: `ASR split smoke fixture ${id}` }).first().waitFor();
            if (!(await page.locator('button[title="Редактировать транскрипт"]:visible').count())) {
                const recording = await context.request.get(`${BASE_URL}/api/recordings/${id}`);
                await openRecordingByTitle(page, (await recording.json()).title);
            }
            await openEditor(page);
            assert.equal(await text(page, 1).inputValue(), 'Ответ другого человека 0.');
            assert.equal(await speaker(page, 1).inputValue(), 'Борис');
        });

        await scenario('keyboard-virtual-scroll', async (page, context, id) => {
            const before = await savedSegments(context, id);
            const scroller = page.locator('[data-testid="asr-editor-scroll"]');
            await scroller.evaluate(el => { el.scrollTop = 150 * 44; });
            await text(page, 150).waitFor();
            const sentence = await text(page, 150).inputValue();
            await caret(page, 150, sentence.indexOf('Ответ'));
            await text(page, 150).press('Control+Enter');
            await page.waitForFunction(() => document.activeElement?.value === 'Анна' && document.activeElement?.closest('tr')?.dataset.segmentIndex === '151');
            await speaker(page, 151).fill('Борис');
            await caret(page, 151, 6);
            await text(page, 151).press('Meta+Enter');
            await text(page, 152).waitFor();
            assert.equal(await text(page, 151).inputValue(), 'Ответ');
            assert.equal(await text(page, 152).inputValue(), 'другого человека 150.');
            const response = page.waitForResponse(r => r.url().endsWith(`/recording/${id}/update_transcription`) && r.request().method() === 'POST');
            await speaker(page, 152).press('Control+s');
            assert((await response).ok());
            const saved = await savedSegments(context, id);
            assert.equal(saved.length, 222);
            assert.deepEqual(saved.slice(0, 150), before.slice(0, 150));
            assert.deepEqual(saved.slice(153), before.slice(151));
        });

        await scenario('selection-tab-and-errors', async page => {
            const original = await text(page, 0).inputValue();
            await caret(page, 0, 0);
            assert(await split(page, 0).isDisabled());
            await caret(page, 0, original.length);
            assert(await split(page, 0).isDisabled());
            await caret(page, 0, 4, 10);
            await text(page, 0).press('Control+Enter');
            assert.equal(await text(page, 0).inputValue(), original);
            await caret(page, 0, original.indexOf('Ответ'));
            await row(page, 0).locator('input[type="number"]').last().fill('0');
            assert(await split(page, 0).isDisabled());
            await row(page, 0).locator('input[type="number"]').last().fill('8');
            await caret(page, 0, original.indexOf('Ответ'));
            // Tab past play and add-below to the split action: selection must survive blur.
            await text(page, 0).press('Tab');
            await page.keyboard.press('Tab');
            await page.keyboard.press('Tab');
            assert.equal(await page.evaluate(() => document.activeElement?.dataset.testid), 'asr-split-segment');
            await page.keyboard.press('Enter');
            assert.equal(await text(page, 1).inputValue(), 'Ответ другого человека 0.');
            await text(page, 1).press('End');
            await text(page, 1).press('Enter');
            assert((await text(page, 1).inputValue()).endsWith('\n'));
        });

        await scenario('autosave', async (page, context, id) => {
            await caret(page, 0, (await text(page, 0).inputValue()).indexOf('Ответ'));
            const response = page.waitForResponse(r => r.url().endsWith(`/recording/${id}/update_transcription`) && r.request().method() === 'POST');
            await split(page, 0).click();
            assert((await response).ok());
            assert.equal((await savedSegments(context, id)).length, 221);
        }, { autosave: true });

        await scenario('mobile-split', async page => {
            await caret(page, 0, (await text(page, 0).inputValue()).indexOf('Ответ'));
            await page.locator('[data-testid="asr-editor-scroll"]').evaluate(el => { el.scrollLeft = el.scrollWidth; });
            await page.screenshot({ path: path.join(out, 'mobile-split-actions.png'), animations: 'disabled' });
            await split(page, 0).tap();
            assert.equal(await text(page, 1).inputValue(), 'Ответ другого человека 0.');
            assert.equal(await speaker(page, 1).inputValue(), 'Анна');
        }, { mobile: true });
    }
} finally {
    await browser.close();
    await writeFile(path.join(out, baseline ? 'baseline-results.json' : 'results.json'), JSON.stringify({ results, failures }, null, 2));
}
assert.equal(failures.length, 0, `${failures.length} UI smoke scenarios failed; see ${out}`);
