// Inquire mode and the upload dialog.
//
// Three shots: the agentic Inquire answer (activity line + citation chips +
// sources), the upload dialog with a queued file, and the same dialog with
// the per-upload transcription/diarization options expanded.

import { go, settle, clickVisible, openRecordingByTitle } from '../helpers.mjs';

/** A sample file that exists in the repo; queued but never uploaded. */
const SAMPLE_FILE = '/home/murtaza/whispertranscribe/temp/german_sample.mp3';

/** Recording opened behind the upload dialog so the backdrop isn't empty. */
const BACKDROP_RECORDING = 'Ford Recalls Engineers';

/** The question asked in the Inquire shot — fixed, so re-runs are comparable. */
const QUESTION = 'What did we discuss about the MR rate within SEC? Cite where.';

/**
 * Ask the Inquire agent a question and wait for the finished answer: the
 * agent runs live tool steps, so this polls until the loading card and every
 * spinner are gone, the sources footer has been rendered, and the answer text
 * has stopped growing.
 */
async function askInquire(page, question, timeoutMs = 180000) {
    const box = page.locator('textarea:visible').first();
    await box.click();
    await box.fill(question);
    await page.keyboard.press('Enter');

    const deadline = Date.now() + timeoutMs;
    let previous = null;
    let stableFor = 0;
    while (Date.now() < deadline) {
        const state = await page.evaluate(() => {
            const visible = (el) => !!el.offsetParent;
            const answers = [...document.querySelectorAll('.chat-message.ai-message')].filter(visible);
            const last = answers[answers.length - 1];
            return {
                busy: [...document.querySelectorAll('.spinner, .fa-spin')].filter(visible).length > 0,
                sources: !!document.querySelector('.citation-refs'),
                activity: !!document.querySelector('.chat-message .fa-list-check'),
                length: last ? last.innerText.length : 0,
            };
        });
        if (!state.busy && state.sources && state.activity && state.length > 0) {
            stableFor = state.length === previous ? stableFor + 1 : 0;
            previous = state.length;
            if (stableFor >= 2) return;
        } else {
            stableFor = 0;
            previous = state.length;
        }
        await page.waitForTimeout(1500);
    }
    throw new Error('Inquire answer did not finish in time');
}

/**
 * Frame the latest answer in the message pane: top-aligned when it fits,
 * otherwise scrolled to the end so the sources footer stays visible.
 */
async function frameLatestAnswer(page) {
    await page.evaluate(() => {
        const answers = [...document.querySelectorAll('.chat-message.ai-message')];
        const msg = answers[answers.length - 1];
        if (!msg) return;
        let scroller = msg.parentElement;
        while (scroller && scroller.scrollHeight <= scroller.clientHeight + 4) scroller = scroller.parentElement;
        if (!scroller) return; // everything already fits on screen
        const offset = msg.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
        const max = scroller.scrollHeight - scroller.clientHeight;
        scroller.scrollTop = msg.offsetHeight > scroller.clientHeight ? max : Math.min(Math.max(offset - 24, 0), max);
    });
    await page.waitForTimeout(400);
}

/** Open the upload dialog from the header and queue a file without uploading. */
async function openUploadDialogWithFile(page) {
    await go(page, '/');
    await openRecordingByTitle(page, BACKDROP_RECORDING);
    if (!(await clickVisible(page, 'button:has-text("New Recording")'))) {
        throw new Error('New Recording button not found');
    }
    await settle(page, 800);
    await page.setInputFiles('input[type=file]', SAMPLE_FILE);
    // Wait for the queue row to show the probed size/duration, not just the name.
    await page.waitForFunction(
        () => /\(\d[\d.]* MB, /.test(document.body.innerText),
        { timeout: 20000 },
    ).catch(() => {});
    await settle(page, 600);
}

/** Scroll an element inside the modal body into a comfortable position. */
async function scrollModalTo(page, selector) {
    await page.evaluate((sel) => {
        const target = document.querySelector(sel);
        const body = document.querySelector('.modal-body');
        if (!target || !body) return;
        const offset = target.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop;
        body.scrollTop = Math.min(Math.max(offset - 24, 0), body.scrollHeight - body.clientHeight);
    }, selector);
    await page.waitForTimeout(400);
}

export default [
    {
        name: 'inquire-semantic-search',
        description: 'Inquire Mode: ask questions across all your recordings, filtered by tag, speaker, or date',
        theme: { dark: true, scheme: 'purple' },
        run: async (page) => {
            await go(page, '/inquire');
            await askInquire(page, QUESTION);
            await settle(page, 800);
            await frameLatestAnswer(page);
        },
    },
    {
        name: 'inquire-agent-activity',
        description: 'Agentic Inquire (beta): the agent searches, lists, and reads iteratively - every step is recorded and expandable on the answer',
        theme: { dark: true, scheme: 'teal' },
        run: async (page) => {
            await go(page, '/inquire');
            await askInquire(page, 'Which recordings mention the incognito feature, and what was decided about it?');
            await settle(page, 600);
            // Expand the activity timeline on the latest answer.
            await page.evaluate(() => {
                const lines = [...document.querySelectorAll('.chat-message')]
                    .flatMap((m) => [...m.querySelectorAll('span')])
                    .filter((el) => /steps? ·/.test(el.textContent));
                const last = lines[lines.length - 1];
                last?.closest('[class]')?.click();
            });
            await page.waitForTimeout(600);
            await frameLatestAnswer(page);
        },
    },
    {
        name: 'inquire-privacy-settings',
        description: 'Per-user Inquire privacy: you decide whether the AI may read your summaries and private notes - transcripts only, if you prefer',
        theme: { dark: false, scheme: 'blue' },
        run: async (page) => {
            await go(page, '/account#prompts');
            await settle(page, 800);
            await page.evaluate(() => {
                const el = [...document.querySelectorAll('span')].find((x) => x.textContent.trim() === 'Inquire Privacy');
                el?.scrollIntoView({ block: 'center' });
            });
            await page.waitForTimeout(400);
        },
    },
    {
        name: 'upload-modal',
        description: 'Uploading a file',
        theme: { dark: true, scheme: 'emerald' },
        run: async (page) => {
            await openUploadDialogWithFile(page);
        },
    },
    {
        name: 'upload-asr-options',
        description: 'Per-upload transcription and diarization options',
        theme: { dark: true, scheme: 'rose' },
        run: async (page) => {
            await openUploadDialogWithFile(page);
            // Expand the "Options" progressive-disclosure group, then the
            // "Transcription Options" panel inside it.
            await page.evaluate(() => {
                document.querySelectorAll('details.upload-options-group').forEach((d) => { d.open = true; });
            });
            await page.waitForTimeout(400);
            if (!(await clickVisible(page, 'button:has-text("Transcription Options")'))) {
                throw new Error('Transcription Options disclosure not found');
            }
            await settle(page, 800);
            // Fill the fields so the panel shows real values rather than
            // placeholders (these are per-upload only, nothing is saved).
            const prompt = page.locator('.disclosure-panel textarea:visible').first();
            await prompt.fill('This is a SEC/Data Science team meeting about merge request throughput.');
            const hotwords = page.locator('.disclosure-panel input[type="text"]:visible').first();
            await hotwords.fill('Speakr, WhisperX, diarization');
            const numbers = page.locator('.disclosure-panel input[type="number"]:visible');
            await numbers.nth(0).fill('2');
            await numbers.nth(1).fill('5');
            await settle(page, 500);
            await scrollModalTo(page, '.disclosure-panel');
        },
    },
];
