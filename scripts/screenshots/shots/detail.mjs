// Recording-detail shots: video dock, the audio player + summary, transcript
// auto-follow, the transcript editor, the floating/docked chat, per-user token
// usage, and the two summary shots (next steps + regeneration dialog).
//
// See docs/screenshots.md for the captions these serve. Recordings are chosen
// so the sidebar always frames a tag/folder-rich region of the library rather
// than the untagged scratch recordings at the top of the list.

import { go, settle, openRecordingByTitle, anchorSidebar, clickVisible } from '../helpers.mjs';

/* ------------------------------------------------------------------ *
 * Small building blocks
 * ------------------------------------------------------------------ */

/**
 * Park the sidebar so `title` is its first visible row. anchorSidebar() loads
 * enough of the infinite-scroll list and gets close; the rect-based pass after
 * it lands the row exactly on the top edge of the list viewport.
 */
async function frameSidebar(page, title) {
    await anchorSidebar(page, title);
    await page.evaluate((wanted) => {
        const h = [...document.querySelectorAll('h4')]
            .find((el) => el.textContent.includes(wanted) && el.closest('.overflow-y-auto'));
        if (!h) return;
        const list = h.closest('.overflow-y-auto');
        const row = h.closest('[class*="border-b"]') || h.parentElement;
        list.scrollTop += row.getBoundingClientRect().top - list.getBoundingClientRect().top;
    }, title);
    await page.waitForTimeout(400);
}

/** Open a recording and park the sidebar so `anchor` is its first row. */
async function openWithSidebar(page, title, anchor) {
    await openRecordingByTitle(page, title);
    await frameSidebar(page, anchor || title);
    await settle(page, 400);
}

/** Wait until the main <audio>/<video> element has metadata. */
async function waitForMedia(page) {
    await page.waitForFunction(() => {
        const m = document.getElementById('mainPlayerMedia');
        return m && m.readyState >= 1 && Number.isFinite(m.duration);
    }, { timeout: 30000 });
}

/**
 * Put the player at a fixed position: play briefly (a real user gesture, so
 * the browser allows it), then pause and seek to an exact second. Pausing
 * keeps the shot deterministic — a still-playing clock would differ between
 * runs — while the filled progress bar still reads as a session in progress.
 */
async function playToTime(page, seconds) {
    await waitForMedia(page);
    await clickVisible(page, 'button[title="Play"], button[title="Pause"]');
    await page.waitForTimeout(700);
    await page.evaluate(async (t) => {
        const m = document.getElementById('mainPlayerMedia');
        m.pause();
        m.currentTime = t;
    }, seconds);
    await page.waitForFunction((t) => {
        const m = document.getElementById('mainPlayerMedia');
        return m && !m.seeking && Math.abs(m.currentTime - t) < 1.5;
    }, seconds, { timeout: 30000 });
    await settle(page, 800);
}

/** Scroll the summary pane so the given heading is at the top of the rail. */
async function scrollSummaryToHeading(page, heading) {
    const ok = await page.evaluate((text) => {
        const pane = document.querySelector('.prose-flush');
        if (!pane) return false;
        const h = [...pane.querySelectorAll('h1, h2, h3, h4, strong')]
            .find((e) => e.textContent.trim().toLowerCase().startsWith(text.toLowerCase()));
        if (!h) return false;
        let s = h.parentElement;
        while (s && s.scrollHeight <= s.clientHeight + 4) s = s.parentElement;
        if (!s) return true;   // the whole summary already fits
        const offset = h.getBoundingClientRect().top - s.getBoundingClientRect().top + s.scrollTop;
        s.scrollTop = Math.min(Math.max(offset - 12, 0), s.scrollHeight - s.clientHeight);
        return true;
    }, heading);
    if (!ok) throw new Error(`Summary heading "${heading}" not found`);
    await page.waitForTimeout(400);
}

/** Open the floating chat panel from its collapsed FAB. */
async function openChat(page) {
    if (!(await clickVisible(page, '.floating-chat-fab'))) {
        throw new Error('Chat FAB not found');
    }
    await page.waitForSelector('.floating-chat-panel', { state: 'visible' });
    await settle(page, 400);
}

/** Drag the floating panel's SE corner handle until it is w x h. */
async function resizeChatPanel(page, w, h) {
    const handle = page.locator('.floating-chat-resize-handle').first();
    const grip = await handle.boundingBox();
    const panel = await page.locator('.floating-chat-panel').first().boundingBox();
    if (!grip || !panel) throw new Error('Chat panel resize handle not visible');
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
    await page.mouse.down();
    await page.mouse.move(
        grip.x + grip.width / 2 + (w - panel.width),
        grip.y + grip.height / 2 + (h - panel.height),
        { steps: 12 },
    );
    await page.mouse.up();
    await page.waitForTimeout(300);
}

/**
 * Scroll the chat log so the last question sits at the top of the panel — the
 * exchange should read question-then-answer rather than opening mid-answer.
 */
async function frameChat(page) {
    await page.evaluate(() => {
        const log = document.querySelector('.floating-chat-messages');
        if (!log) return;
        const asked = [...log.querySelectorAll('.user-message')].pop();
        if (!asked) return;
        log.scrollTop = Math.max(
            0,
            Math.min(
                asked.getBoundingClientRect().top - log.getBoundingClientRect().top + log.scrollTop - 12,
                log.scrollHeight - log.clientHeight,
            ),
        );
    });
    await page.waitForTimeout(300);
}

/** Drag the floating chat panel by its header so its top-left lands at x,y. */
async function moveChatPanel(page, x, y) {
    const header = page.locator('.floating-chat-header').first();
    const box = await header.boundingBox();
    if (!box) throw new Error('Chat panel header not visible');
    const panel = await page.locator('.floating-chat-panel').first().boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(
        box.x + box.width / 2 + (x - panel.x),
        box.y + box.height / 2 + (y - panel.y),
        { steps: 12 },
    );
    await page.mouse.up();
    await page.waitForTimeout(300);
}

/**
 * Ask the per-recording chat a question and wait for the streamed answer to
 * finish: the "thinking" row gone, no spinners, and the answer text stable.
 * Chat history is client-side only, so nothing is left behind on the server.
 */
async function askChat(page, question, timeoutMs = 150000) {
    const box = page.locator('.floating-chat-input textarea').first();
    await box.click();
    await box.fill(question);
    await page.keyboard.press('Enter');

    const deadline = Date.now() + timeoutMs;
    let previous = -1;
    let stable = 0;
    while (Date.now() < deadline) {
        const state = await page.evaluate(() => {
            const visible = (el) => !!el.offsetParent;
            const msgs = [...document.querySelectorAll('.floating-chat-messages .ai-message')].filter(visible);
            const last = msgs[msgs.length - 1];
            return {
                busy: [...document.querySelectorAll('.floating-chat-messages .spinner')].filter(visible).length > 0,
                answers: msgs.length,
                length: last ? last.innerText.length : 0,
            };
        });
        if (!state.busy && state.answers > 0 && state.length > 0) {
            stable = state.length === previous ? stable + 1 : 0;
            previous = state.length;
            if (stable >= 2) {
                await page.waitForTimeout(600);
                return;
            }
        } else {
            stable = 0;
            previous = state.length;
        }
        await page.waitForTimeout(1200);
    }
    throw new Error('Chat answer did not finish in time');
}

/**
 * The header usage meter only renders for accounts that have a monthly token
 * budget. If the account already has one, use it untouched; otherwise set one
 * for the duration of the shot and put the account back exactly as it was.
 * The meter is loaded once at boot, so it stays on screen for the screenshot
 * even after the budget has been removed again.
 */
async function withTokenBudget(page, fallbackBudget, body) {
    const email = (process.env.SPEAKR_EMAIL || '').toLowerCase();
    const account = () => page.evaluate(async (who) => {
        const users = await (await fetch('/admin/users')).json();
        const me = users.find((u) => (u.email || '').toLowerCase() === who);
        if (!me) throw new Error(`account ${who} not found in /admin/users`);
        return { id: me.id, budget: me.monthly_token_budget };
    }, email);
    const setBudget = (id, value) => page.evaluate(async ([userId, v]) => {
        const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
        const res = await fetch(`/admin/users/${userId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            body: JSON.stringify({ monthly_token_budget: v }),
        });
        if (!res.ok) throw new Error(`token budget update failed: ${res.status}`);
    }, [id, value]);

    const before = await account();
    if (before.budget) {
        await body();                        // real budget already configured
        return;
    }
    await setBudget(before.id, fallbackBudget);
    try {
        await body();
    } finally {
        await setBudget(before.id, null);    // restore; the meter is already drawn
    }
}

/* ------------------------------------------------------------------ *
 * Recordings used, and where to park the sidebar for each
 * ------------------------------------------------------------------ */

const R = {
    // Chromium ships without the proprietary H.264/AAC decoders, so the video
    // shot has to use one of the library's WebM recordings.
    video: 'SEC/Data Science Team Updates and Announcements',
    player: 'Attempting a Michelin Star Dish Challenge',
    follow: 'Job Cuts, Internet Regulations, and Apple Subscriptions',
    edit: 'Fortnightly Team Meeting at ABC Manufacturing',
    chat: 'Job Cuts, Internet Regulations, and Apple Subscriptions',
    tokens: 'Road Trip Reflections and Future Research Plans',
    nextSteps: 'Fortnightly Team Meeting at ABC Manufacturing',
    reprocess: 'SEC Disgorgement Limits and Investor Harm',
};

const ANCHOR = {
    learning: 'German Listening Practice: Meeting New People',
    interviews: 'Art, Jiu-Jitsu, and Creative Processes',
    work: 'Collaborative Approaches in Team Management',
    sec: 'SEC/Data Science Team Updates and Announcements',
};

export default [
    {
        name: 'main-view-video',
        description: 'Video playback synced to the transcript',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            // Park the video dock at the top of the transcript column, where
            // it sits directly above the transcript it is synced to.
            await page.addInitScript(() => {
                try {
                    localStorage.setItem('videoDockPosition', 'left-top');
                    localStorage.setItem('videoDockHeight', '380');
                } catch (_) {}
            });
            await go(page, '/');
            await openWithSidebar(page, R.video, R.video);
            if (!(await clickVisible(page, 'button[title="Show video"]'))) {
                throw new Error('Show video button not found (not a video recording?)');
            }
            await settle(page, 600);
            await playToTime(page, 620);
            // The docked video is a muted follower kept in step by
            // syncDockVideo(); wait until it actually has a frame decoded.
            await page.waitForFunction(() => {
                const v = document.getElementById('dockVideoElement');
                return v && v.readyState >= 2;
            }, { timeout: 30000 });
            await settle(page, 800);
        },
    },
    {
        name: 'main-view-player',
        description: 'Audio player with the generated summary',
        theme: { dark: true, scheme: 'emerald' },
        run: async (page) => {
            await go(page, '/');
            await openWithSidebar(page, R.player, ANCHOR.learning);
            await playToTime(page, 615);
        },
    },
    {
        name: 'transcript-auto-follow',
        description: 'The transcript auto-scrolls to follow playback',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await go(page, '/');
            await openWithSidebar(page, R.follow, ANCHOR.learning);
            if (!(await clickVisible(page, 'button[title*="Enable auto-scroll"]'))) {
                throw new Error('Auto-scroll toggle not found');
            }
            await settle(page, 400);
            await playToTime(page, 11558);
            // The follow handler scrolls the segment for the current time into
            // view; wait for it to actually be highlighted and on screen.
            await page.waitForSelector('.active-playing-segment', { state: 'visible', timeout: 20000 });
            await settle(page, 800);
        },
    },
    {
        name: 'edit-transcript-modal',
        description: 'Editing the transcript',
        theme: { dark: true, scheme: 'teal' },
        run: async (page) => {
            await go(page, '/');
            await openWithSidebar(page, R.edit, ANCHOR.interviews);
            if (!(await clickVisible(page, 'button[title="Edit transcript"]'))) {
                throw new Error('Edit transcript button not found');
            }
            await page.waitForSelector('.modal-overlay', { state: 'visible' });
            await settle(page, 1000);
            // Nudge past the intro so the segment table shows several of the
            // named speakers rather than a run of the same one, landing on a
            // row boundary so the first row is not sliced in half.
            await page.evaluate(() => {
                const list = document.querySelector('[class*="flex-grow"].overflow-y-auto');
                const row = list && list.querySelector('tbody tr');
                if (!list || !row) return;
                const h = row.getBoundingClientRect().height || 42;
                list.scrollTop = Math.round(240 / h) * h;
            });
            await settle(page, 500);
        },
    },
    {
        name: 'main-view-chat-notes',
        description: 'Floating chat and notes over the transcript',
        theme: { dark: true, scheme: 'purple' },
        run: async (page) => {
            await go(page, '/');
            await openWithSidebar(page, R.chat, ANCHOR.learning);
            await clickVisible(page, '.tab:has-text("Notes")');
            await settle(page, 400);
            await openChat(page);
            await resizeChatPanel(page, 600, 540);
            await moveChatPanel(page, 348, 460);
            await askChat(page, 'In two or three sentences, what did the hosts say about Apple charging $240 a year for device insurance?');
            await moveChatPanel(page, 348, 460);
            await frameChat(page);
        },
    },
    {
        name: 'chat-docked',
        description: 'The chat panel docked into the layout',
        theme: { dark: true, scheme: 'rose' },
        run: async (page) => {
            await go(page, '/');
            await openWithSidebar(page, R.chat, ANCHOR.learning);
            await openChat(page);
            await askChat(page, 'Give me three short bullets on the topics in this episode that are not about technology.');
            if (!(await clickVisible(page, 'button[title="Dock right"]'))) {
                throw new Error('Dock right button not found');
            }
            await settle(page, 800);
            await frameChat(page);
        },
    },
    {
        name: 'main-view-token-usage',
        description: 'LLM token usage shown per recording',
        theme: { dark: true, scheme: 'amber' },
        run: async (page) => {
            await go(page, '/');
            await withTokenBudget(page, 1000000, async () => {
                await go(page, '/');
                await openWithSidebar(page, R.tokens, ANCHOR.work);
                await page.waitForSelector('.fa-coins', { state: 'visible', timeout: 10000 });
            });
            await settle(page, 400);
        },
    },
    {
        name: 'summary-next-steps',
        description: 'A summary with action items and next steps',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            // Give the summary rail the wider half of the split (the column
            // divider is user-draggable and the split is remembered), so the
            // action-item table reads as a table instead of three word-wrapped
            // slivers.
            await page.addInitScript(() => {
                try {
                    localStorage.setItem('transcriptColumnWidth', '45');
                    localStorage.setItem('summaryColumnWidth', '55');
                } catch (_) {}
            });
            await go(page, '/');
            await openWithSidebar(page, R.nextSteps, ANCHOR.interviews);
            await scrollSummaryToHeading(page, 'Next Steps');
        },
    },
    {
        name: 'summary-reprocess',
        description: 'Regenerating a summary with a new prompt',
        theme: { dark: true, scheme: 'emerald' },
        run: async (page) => {
            await go(page, '/');
            await openWithSidebar(page, R.reprocess, ANCHOR.sec);
            // Opens the confirmation dialog only — reprocessing is never
            // started, so no LLM call is made and no data changes.
            if (!(await clickVisible(page, 'button[title="Reprocess summary"]'))) {
                throw new Error('Reprocess summary button not found');
            }
            await page.waitForSelector('.modal-overlay', { state: 'visible' });
            await settle(page, 800);
        },
    },
];
