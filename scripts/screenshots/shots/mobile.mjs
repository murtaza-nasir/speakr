// Mobile (phone) documentation shots.
//
// Every shot in this file renders at the 390x844 @3x phone viewport, where
// the app has a genuinely different shape from the desktop layout: it opens
// on the recordings list (a full-height drawer), opening a recording takes
// you to a detail screen with bottom tab navigation, and most per-recording
// actions live behind the chevron in the mobile detail header.
//
// Two rules that apply to nearly everything here:
//   * The desktop templates stay in the DOM on mobile, so a text locator
//     matches hidden duplicates — always resolve elements through the
//     viewport-filtering helpers below, never with a bare page.click().
//   * Escape does not close the app's popovers/sheets; backdrop taps do.

import { go, settle, loadSidebarUntil } from '../helpers.mjs';

const MOBILE_WIDTH = 390;

/** The scrolling container of the recordings list (the mobile drawer). */
const LIST_SCROLLER = '.flex-1.overflow-y-auto.pb-3';

/** A sample file that exists in the repo; queued but never uploaded. */
const SAMPLE_FILE = '/home/murtaza/whispertranscribe/temp/german_sample.mp3';

/**
 * True when the element is actually laid out inside the phone viewport.
 * The hidden desktop duplicates either have no box at all or sit outside
 * the 390px column, so this is what separates them.
 */
async function inViewport(locator) {
    const box = await locator.boundingBox().catch(() => null);
    if (!box) return false;
    return box.width > 0 && box.height > 0 && box.x >= 0 && box.x < MOBILE_WIDTH && box.y >= 0;
}

/** Tap the first element matching `selector` that is really on the phone screen. */
async function tap(page, selector, { required = true } = {}) {
    const cands = page.locator(selector);
    const n = await cands.count();
    for (let i = 0; i < n; i++) {
        const el = cands.nth(i);
        if (await inViewport(el)) {
            await el.click();
            return true;
        }
    }
    if (required) throw new Error(`No on-screen element matched ${selector}`);
    return false;
}

/**
 * Park the recordings list so the row titled `title` is the first one under
 * the search bar. Measured against the scroller's own box rather than
 * offsetTop, because the rows' offsetParent is not the scroller.
 */
async function anchorList(page, title) {
    await loadSidebarUntil(page, title);
    const ok = await page.evaluate(([sel, wanted]) => {
        const list = document.querySelector(sel);
        if (!list) return false;
        const h = [...list.querySelectorAll('h4')].find((e) => e.textContent.includes(wanted));
        if (!h) return false;
        const row = h.closest('li') || h.parentElement.parentElement;
        list.scrollTop += row.getBoundingClientRect().top - list.getBoundingClientRect().top - 1;
        return true;
    }, [LIST_SCROLLER, title]);
    if (!ok) throw new Error(`Could not anchor the mobile list on ${title}`);
    await settle(page, 500);
}

/** Open a recording from the mobile list. */
async function openRecording(page, title) {
    await anchorList(page, title);
    const cands = page.locator(`h4:has-text(${JSON.stringify(title)})`);
    const n = await cands.count();
    for (let i = 0; i < n; i++) {
        const el = cands.nth(i);
        if (await inViewport(el)) {
            await el.click();
            await settle(page, 1500);
            return;
        }
    }
    throw new Error(`Recording titled ${title} not tappable in the mobile list`);
}

/** Icons the bottom tab bar uses, keyed by the tab they switch to. */
const TAB_ICONS = {
    summary: 'fa-file-alt',
    transcript: 'fa-align-left',
    chat: 'fa-comments',
    notes: 'fa-sticky-note',
};

/** Switch the detail screen's bottom tab bar to a tab. */
async function selectTab(page, tab) {
    await tap(page, `[data-mobile-bottom-nav] button:has(i.${TAB_ICONS[tab]})`);
    await settle(page, 800);
}

/** Expand the collapsible metadata panel in the mobile detail header. */
async function expandMetadata(page) {
    await tap(page, 'button:has(i.fa-chevron-down)');
    await settle(page, 600);
}

/** Re-open the recordings drawer over the detail screen. */
async function openListDrawer(page) {
    await tap(page, 'header button:has(i.fa-bars)');
    await settle(page, 700);
}

/** True while the list drawer is open (it covers the header on a phone). */
function drawerIsOpen(page) {
    return page.evaluate(() => {
        const aside = document.querySelector('aside.sidebar');
        return !!aside && !aside.classList.contains('collapsed');
    });
}

/**
 * Open the "New Recording" sheet and wait for the bottom sheet to be on
 * screen. The + button is taken from whichever bar is on top: the drawer
 * covers the app header while the list is showing, so the header's own +
 * is present but unclickable there.
 */
async function openUploadSheet(page) {
    await tap(page, await drawerIsOpen(page)
        ? 'aside button:has(i.fa-plus)'
        : 'header button:has(i.fa-plus)');
    await page.waitForSelector('.modal-panel--bottom-sheet', { state: 'visible' });
    await settle(page, 800);
}

/**
 * Dismiss the processing-queue pill. Queuing a file makes it appear, and on
 * a phone it parks itself over the sheet's sticky Upload footer.
 */
async function dismissQueuePill(page) {
    await tap(page, '.progress-popup__close', { required: false });
    await settle(page, 400);
}

/**
 * Replace getUserMedia with a synthesised oscillator stream so a real
 * microphone recording can run headlessly. Must be installed before the
 * record button is tapped.
 */
async function installFakeMicrophone(page) {
    await page.addInitScript(() => {
        const make = () => {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            const dest = ctx.createMediaStreamDestination();
            osc.type = 'sine';
            osc.frequency.value = 220;
            gain.gain.value = 0.25;
            osc.connect(gain).connect(dest);
            osc.start();
            return dest.stream;
        };
        const md = navigator.mediaDevices || {};
        md.getUserMedia = async () => make();
        md.enumerateDevices = async () => ([
            { deviceId: 'default', kind: 'audioinput', label: 'Built-in Microphone', groupId: 'g' },
        ]);
        try {
            Object.defineProperty(navigator, 'mediaDevices', { value: md, configurable: true });
        } catch (_) { /* already writable */ }
    });
}

/**
 * Throw away everything the in-progress recording put on the server, while
 * leaving the live recorder on screen for the shot.
 *
 * The harness screenshots the page as `run` returns, so the recorder has to
 * still be running in the final frame — which rules out driving the app's
 * own Stop + Discard buttons, since Discard navigates straight back to the
 * upload sheet. Closing the context kills the client half of the recording
 * (MediaRecorder, blobs, IndexedDB session) by itself, but this instance
 * also streams chunks to the server (ENABLE_SERVER_RECORDING_CHUNKS) and
 * those would sit on disk until the expiry sweep. So:
 *   1. issue exactly the DELETE that discardRecording() issues, against the
 *      session id the client stashed in localStorage — nothing was ever
 *      finalized, so no recording is created; then
 *   2. swallow any further chunk POST, which would otherwise 404 against
 *      the deleted session and put a sync-failure banner in the frame.
 */
async function discardServerRecordingSession(page) {
    const result = await page.evaluate(async () => {
        const realFetch = window.fetch.bind(window);
        let outcome = 'no server session';
        try {
            const stashed = JSON.parse(localStorage.getItem('speakr.serverRecordingSession') || 'null');
            if (stashed && stashed.session_id) {
                const csrf = document.querySelector('meta[name="csrf-token"]');
                const resp = await realFetch(`/upload/session/${encodeURIComponent(stashed.session_id)}`, {
                    method: 'DELETE',
                    credentials: 'same-origin',
                    headers: { 'X-CSRFToken': csrf ? csrf.getAttribute('content') : '' },
                });
                outcome = resp.status === 204 || resp.status === 404
                    ? 'aborted' : `abort failed: HTTP ${resp.status}`;
                localStorage.removeItem('speakr.serverRecordingSession');
            }
        } catch (e) {
            outcome = `abort failed: ${e.message}`;
        }
        window.fetch = (input, init) => {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.includes('/upload/session/')) {
                return Promise.resolve(new Response('{"ok":true}', {
                    status: 200, headers: { 'Content-Type': 'application/json' },
                }));
            }
            return realFetch(input, init);
        };
        return outcome;
    });
    if (result.startsWith('abort failed')) throw new Error(`Recording session left behind (${result})`);
    return result;
}

export default [
    {
        name: 'mobile-main',
        description: 'The main view on mobile',
        theme: { dark: true, scheme: 'blue' },
        mobile: true,
        run: async (page) => {
            await go(page, '/');
            // Open a recording and leave the list drawer CLOSED: the main
            // mobile shot is the detail screen, not the drawer.
            await openRecording(page, 'Attempting a Michelin Star Dish Challenge');
        },
    },
    {
        name: 'mobile-summary',
        description: 'Summary tab with bottom navigation',
        theme: { dark: true, scheme: 'emerald' },
        mobile: true,
        run: async (page) => {
            await go(page, '/');
            await openRecording(page, 'Attempting a Michelin Star Dish Challenge');
            await selectTab(page, 'summary');
        },
    },
    {
        name: 'mobile-transcript',
        description: 'Transcript in bubble view',
        theme: { dark: true, scheme: 'purple' },
        mobile: true,
        run: async (page) => {
            await go(page, '/');
            await openRecording(page, 'Job Cuts, Internet Regulations, and Apple Subscriptions');
            await selectTab(page, 'transcript');
            // The view-mode toggle lives in the header's contextual toolbar,
            // which only carries it while the transcript tab is active.
            await tap(page, 'button[title*="Bubble"]');
            await settle(page, 900);
        },
    },
    {
        name: 'mobile-speaker-id',
        description: 'Identifying speakers on mobile',
        theme: { dark: true, scheme: 'teal' },
        mobile: true,
        run: async (page) => {
            await go(page, '/');
            // A Supreme Court argument: some voices already carry real
            // names while the rest are still raw diarization labels, which
            // is exactly the state this modal exists to resolve.
            await openRecording(page, 'SEC Disgorgement Limits and Investor Harm');
            // The Identify Speakers action only exists in the expanded
            // metadata panel's action toolbar.
            await expandMetadata(page);
            await tap(page, 'button:has(i.fa-user-tag)');
            await page.waitForSelector('.modal-panel', { state: 'visible' });
            await settle(page, 1200);
            // Type one name to show the form in use. Nothing is saved: the
            // shot ends here and the context is thrown away.
            const blank = page.locator('.modal-panel input[placeholder^="Enter name for SPEAKER"]:visible').first();
            if (await blank.count()) {
                await blank.fill('Justice Breyer');
                await settle(page, 400);
            }
        },
    },
    {
        name: 'mobile-upload',
        description: 'The upload sheet on mobile',
        theme: { dark: true, scheme: 'rose' },
        mobile: true,
        run: async (page) => {
            await go(page, '/');
            // Open a recording first so the sheet rises over real content
            // instead of the empty-state screen.
            await openRecording(page, 'Attempting a Michelin Star Dish Challenge');
            await openUploadSheet(page);
            await page.setInputFiles('input[type=file]', SAMPLE_FILE);
            // Wait for the queue row to show the probed size/duration.
            await page.waitForFunction(
                () => /\(\d[\d.]* MB, /.test(document.body.innerText),
                { timeout: 20000 },
            ).catch(() => {});
            await dismissQueuePill(page);
            await settle(page, 800);
        },
    },
    {
        name: 'mobile-recording',
        description: 'Recording with notes on mobile',
        theme: { dark: true, scheme: 'amber' },
        mobile: true,
        run: async (page) => {
            await installFakeMicrophone(page);
            await go(page, '/');
            await openUploadSheet(page);
            await tap(page, '.modal-panel--bottom-sheet button:has(i.fa-microphone)');
            await settle(page, 700);
            // The instance shows a consent disclaimer between the tap and
            // the recorder; accept it if it is configured.
            await tap(page, '.modal-footer button:has-text("Start Recording")', { required: false });
            await page.waitForSelector('.recording-notes-editor .CodeMirror', { state: 'visible', timeout: 25000 });
            await settle(page, 1500);

            // The notes field is an EasyMDE editor, so the underlying
            // <textarea> is hidden — type into CodeMirror instead. Its
            // markdown list continuation supplies the second "- ".
            await page.locator('.recording-notes-editor .CodeMirror').first().click();
            await page.keyboard.type('## Kickoff');
            await page.keyboard.press('Enter');
            await page.keyboard.type('- ship the mobile docs shots');
            await page.keyboard.press('Enter');
            await page.keyboard.type('follow up on **speaker names**');
            // Let the clock run on (and any wake-lock toast expire) so the
            // frame shows a recording under way rather than one just begun.
            await settle(page, 6000);
            await discardServerRecordingSession(page);
        },
    },
];
