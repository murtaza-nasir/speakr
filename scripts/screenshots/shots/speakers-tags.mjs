// Speaker identification, sharing, and per-tag settings.
//
// Seven shots: the Identify Speakers modal with voice-profile matches, the
// same modal after the LLM has read the names out of the transcript, the
// share dialog, and four views of the tag editor on the account page
// (transcription defaults, summary prompt + title template, a real recipe
// prompt, and the group auto-share options).

import { go, settle, clickVisible, anchorSidebar } from '../helpers.mjs';

/**
 * Recordings are addressed by id via the /recordings/<id> deep link rather
 * than by sidebar title: two recordings in the library share a title, and
 * the deep link also removes the infinite-scroll dance from every shot.
 */
const REC = {
    // Podcast panel, tagged AI. Four speakers are already named and the rest
    // still carry diarization labels, three of which match a saved voice
    // profile — so the modal shows named rows AND suggestion pills together.
    voiceProfiles: 2279,
    // Round of manager introductions, tagged Important. Every speaker is
    // still a raw diarization label and each one says their own name in the
    // transcript, which is exactly what Auto Identify reads — so the shot
    // shows names arriving rather than contradicting existing labels.
    autoIdentify: 2302,
    // Tech podcast, tagged AI. This one already carries a public share link,
    // so the dialog shows a real link instead of an empty state and the shot
    // needs no share to be created.
    share: 2296,
};

/** Sidebar row the list is parked on so the backdrop shows tagged rows. */
const SIDEBAR_ANCHOR = 'Tiredness and Google Maps Scraping Insights';

/**
 * Park the sidebar list on a curated, tag-rich region: anchorSidebar loads
 * the row into the (infinite-scroll) list, then the row is aligned to the top
 * of the scroller by measured position — the app scrolls the list to the
 * selected recording on load, which otherwise decides the framing.
 */
async function parkSidebar(page, title) {
    await anchorSidebar(page, title);
    await page.evaluate((wanted) => {
        const h = [...document.querySelectorAll('h4')]
            .find((el) => el.textContent.includes(wanted) && el.getBoundingClientRect().height > 0);
        if (!h) return;
        const list = h.closest('.overflow-y-auto');
        const row = h.closest('[class*="border-b"]') || h.parentElement;
        if (!list || !row) return;
        list.scrollTop += row.getBoundingClientRect().top - list.getBoundingClientRect().top - 4;
    }, title);
    await page.waitForTimeout(400);
}

/** Wait until every toast has dismissed itself (they cover the top-right). */
async function waitForToasts(page, timeoutMs = 20000) {
    await page.waitForFunction(() => {
        const c = document.getElementById('toastContainer');
        return !c || c.children.length === 0;
    }, { timeout: timeoutMs }).catch(() => {});
    await page.waitForTimeout(300);
}

/** Open a recording by id and settle. */
async function openRecording(page, id) {
    await go(page, `/recordings/${id}`, 1500);
    await page.waitForFunction(
        () => !!document.querySelector('button[title="Share recording"]'),
        { timeout: 20000 },
    );
    await settle(page, 600);
}

/** Open the Identify Speakers modal and wait for the speaker rows to render. */
async function openSpeakerModal(page) {
    if (!(await clickVisible(page, 'button[title="Identify speakers"]'))) {
        throw new Error('Identify speakers button not found');
    }
    await page.waitForSelector('.modal-panel:has-text("Identify Speakers")', { state: 'visible' });
    await page.waitForFunction(
        () => document.querySelectorAll('.modal-panel input[type="text"]').length > 0,
        { timeout: 20000 },
    );
    // Video recordings mount a player that is a black rectangle until it is
    // played; collapse it to the audio-only view so the transcript fills the
    // pane instead.
    await clickVisible(page, 'button[title="Hide video (audio-only view)"]');
    // Park the pointer on the modal header. Left where the click put it, it
    // ends up over a transcript row and reveals that row's hover buttons.
    await page.mouse.move(700, 60);
    await settle(page, 800);
}

/** The scrolling left-hand column of speaker rows inside the modal. */
const SPEAKER_COLUMN = '.modal-panel .lg\\:w-1\\/3';

/**
 * Wait for the voice-profile lookup to come back and park the speaker column
 * so the rows carrying suggestion pills are on screen.
 */
async function showVoiceSuggestions(page) {
    await page.waitForFunction(
        () => [...document.querySelectorAll('.modal-panel button')]
            .some((b) => /%$/.test(b.textContent.trim())),
        { timeout: 30000 },
    );
    await page.waitForTimeout(500);
    await page.evaluate((sel) => {
        const col = document.querySelector(sel);
        if (!col) return;
        const pill = [...col.querySelectorAll('button')]
            .find((b) => /%$/.test(b.textContent.trim()));
        if (!pill) return;
        const row = pill.closest('.space-y-2') || pill.parentElement;
        const target = row.offsetTop - col.offsetTop - 12;
        col.scrollTop = Math.max(0, Math.min(target, col.scrollHeight - col.clientHeight));
    }, SPEAKER_COLUMN);
    await page.waitForTimeout(400);
}

/**
 * Run Auto Identify (a real LLM call) and wait until the names have landed
 * in the fields and the progress toasts have gone.
 */
async function runAutoIdentify(page, timeoutMs = 180000) {
    const before = await page.evaluate(() => [...document.querySelectorAll('.modal-panel input[type="text"]')]
        .filter((i) => i.value.trim()).length);
    if (!(await clickVisible(page, '.modal-footer button:has-text("Auto Identify")'))) {
        throw new Error('Auto Identify button not found');
    }
    await page.waitForFunction(
        (n) => {
            const busy = [...document.querySelectorAll('.modal-footer .fa-spin')].length > 0;
            const filled = [...document.querySelectorAll('.modal-panel input[type="text"]')]
                .filter((i) => i.value.trim()).length;
            return !busy && filled > n;
        },
        before,
        { timeout: timeoutMs },
    );
    await waitForToasts(page);
    await page.mouse.move(700, 60);
    // Park the column at the top so the newly filled names read in order.
    await page.evaluate((sel) => {
        const col = document.querySelector(sel);
        if (col) col.scrollTop = 0;
    }, SPEAKER_COLUMN);
    await settle(page, 600);
}

/**
 * Open the tag editor on the account page for a tag by (partial) name and
 * switch to one of its tabs. Nothing is ever saved — the values typed for
 * the screenshots live only in the form.
 */
async function openTagEditor(page, tagName, tab) {
    await go(page, '/account#tags', 1200);
    await page.waitForFunction(
        () => document.querySelectorAll('#tagsGrid .edit-tag-btn').length > 0,
        { timeout: 20000 },
    );
    const found = await page.evaluate((name) => {
        const card = [...document.querySelectorAll('#tagsGrid > div')]
            .find((c) => c.querySelector('h3')?.textContent.trim().endsWith(name));
        const btn = card?.querySelector('.edit-tag-btn');
        if (!btn) return false;
        btn.click();
        return true;
    }, tagName);
    if (!found) throw new Error(`Tag card for ${tagName} not found`);
    await page.waitForFunction(
        () => !document.getElementById('tagModal').classList.contains('hidden'),
        { timeout: 10000 },
    );
    await page.click(`#tagModalTabs [data-tab="${tab}"]`);
    // Off the tab strip, so no control is left in its hover state.
    await page.mouse.move(720, 1050);
    await settle(page, 500);
}

/**
 * Grow a textarea to its content height (the same thing the drag handle in
 * its corner does) so a long prompt is readable instead of clipped mid-line,
 * and drop focus so no field shows a focus ring in the shot.
 */
async function fitAndBlur(page, selector) {
    await page.evaluate((sel) => {
        const el = document.querySelector(sel);
        if (el) {
            el.style.height = 'auto';
            el.style.height = `${el.scrollHeight + 2}px`;
        }
        document.activeElement?.blur();
    }, selector);
    await page.waitForTimeout(300);
}

export default [
    {
        name: 'speaker-id-voice-profiles',
        description: 'Identifying speakers by their voice profile',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await openRecording(page, REC.voiceProfiles);
            await parkSidebar(page, SIDEBAR_ANCHOR);
            await openSpeakerModal(page);
            await showVoiceSuggestions(page);
        },
    },
    {
        name: 'auto-name-recognition',
        description: 'Speaker names recognised from the transcript',
        theme: { dark: true, scheme: 'teal' },
        run: async (page) => {
            await openRecording(page, REC.autoIdentify);
            await parkSidebar(page, SIDEBAR_ANCHOR);
            await openSpeakerModal(page);
            await runAutoIdentify(page);
        },
    },
    {
        name: 'share-modal',
        description: 'Sharing a recording with users or a public link',
        theme: { dark: true, scheme: 'purple' },
        run: async (page) => {
            await openRecording(page, REC.share);
            await parkSidebar(page, SIDEBAR_ANCHOR);
            if (!(await clickVisible(page, 'button[title="Share recording"]'))) {
                throw new Error('Share recording button not found');
            }
            await page.waitForSelector('.modal-panel:has-text("Share Recording")', { state: 'visible' });
            await settle(page, 1200);
        },
    },
    {
        name: 'tag-transcription-options',
        description: 'Per-tag transcription and ASR settings',
        theme: { dark: true, scheme: 'emerald' },
        run: async (page) => {
            await openTagEditor(page, 'Interview', 'tagTabTranscription');
            // Example values only — the form is never submitted.
            await page.selectOption('#tagLanguage', 'en');  // language is a dropdown now (#362 round)
            await page.fill('#tagMinSpeakers', '2');
            await page.fill('#tagMaxSpeakers', '4');
            await page.fill('#tagHotwords', 'Speakr, WhisperX, PyAnnote, diarization');
            await page.fill('#tagInitialPrompt', 'A recorded one-on-one interview between a host and a guest.');
            await fitAndBlur(page, '#tagInitialPrompt');
            await settle(page, 500);
        },
    },
    {
        name: 'tag-summary-template-options',
        description: 'Per-tag summary prompt and title template',
        theme: { dark: true, scheme: 'rose' },
        run: async (page) => {
            await openTagEditor(page, 'Interview', 'tagTabSummary');
            await page.fill(
                '#tagCustomPrompt',
                'Summarise this interview for a reader who did not attend. Open with a short '
                + 'paragraph on who the guest is and why they were interviewed, then list the '
                + 'main themes discussed with the guest’s own claims under each, and close '
                + 'with any commitments, recommendations or follow-up questions raised.',
            );
            // Pick a real naming template so the title-template row shows a value.
            await page.evaluate(() => {
                const sel = document.getElementById('tagNamingTemplate');
                const opt = [...sel.options].find((o) => /Date Prefix/i.test(o.textContent));
                if (opt) {
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                }
            });
            await fitAndBlur(page, '#tagCustomPrompt');
            await settle(page, 500);
        },
    },
    {
        name: 'tag-prompt-recipe-example',
        description: 'A Recipe tag turning cooking narration into a formatted recipe',
        theme: { dark: true, scheme: 'amber' },
        run: async (page) => {
            // The Cooking tag already carries a recipe prompt — nothing is typed
            // and nothing is saved.
            await openTagEditor(page, 'Cooking', 'tagTabSummary');
            await fitAndBlur(page, '#tagCustomPrompt');
            await settle(page, 400);
        },
    },
    {
        name: 'tag-sharing-options',
        description: 'Group tags that auto-share to every member',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await openTagEditor(page, 'Family', 'tagTabSharing');
            await settle(page, 400);
        },
    },
];
