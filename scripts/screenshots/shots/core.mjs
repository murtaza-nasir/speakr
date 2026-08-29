// Core documentation shots: the main view, per-recording stats, and the
// sidebar filter panel. See docs/screenshots.md for the captions these serve.

import { go, settle, openRecordingByTitle, clickVisible } from '../helpers.mjs';

/** The scrolling container of the sidebar recordings list. */
const SIDEBAR_SCROLLER = '.flex-1.overflow-y-auto.pb-3';

/**
 * The sidebar list is paginated by infinite scroll, so a recording further
 * down the library is not in the DOM until the list has been scrolled.
 * Scroll to the bottom repeatedly until the title shows up.
 */
async function loadSidebarUntil(page, title, maxPages = 20) {
    let lastHeight = -1;
    for (let i = 0; i < maxPages; i++) {
        const found = await page.evaluate(
            (t) => [...document.querySelectorAll('h4')].some(
                (h) => h.textContent.includes(t) && h.getBoundingClientRect().height > 0,
            ),
            title,
        );
        if (found) return;
        const height = await page.evaluate((sel) => {
            const s = document.querySelector(sel);
            if (!s) return -1;
            s.scrollTop = s.scrollHeight;
            return s.scrollHeight;
        }, SIDEBAR_SCROLLER);
        if (height === lastHeight || height < 0) break;   // whole library loaded
        lastHeight = height;
        await page.waitForTimeout(900);
    }
    throw new Error(`Recording titled ${title} never appeared in the sidebar list`);
}

/**
 * Open a recording, loading more of the list first if needed, then park the
 * sidebar so the selected row sits in the middle of the list (stable framing
 * regardless of how far down the library the recording lives).
 */
async function openRecording(page, title) {
    await loadSidebarUntil(page, title);
    await openRecordingByTitle(page, title);
    await page.evaluate((t) => {
        const h = [...document.querySelectorAll('h4')].find(
            (x) => x.textContent.includes(t) && x.getBoundingClientRect().height > 0,
        );
        if (h) h.scrollIntoView({ block: 'center', behavior: 'instant' });
    }, title);
    await settle(page, 500);
}

/** Open the collapsed filter panel above the recordings list. */
async function openFilterPanel(page) {
    await clickVisible(page, 'button:has-text("Search recordings...")');
    await page.waitForSelector('text=Filter by tag', { state: 'visible' });
    await settle(page, 400);
}

/**
 * Click a tag chip inside the filter panel. Recording rows carry same-named
 * tag chips of their own, so scope the click to the panel's "Filter by tag"
 * card via a temporary marker attribute.
 */
async function applyTagFilter(page, tag) {
    const marked = await page.evaluate((t) => {
        const label = [...document.querySelectorAll('label')].find(
            (e) => e.textContent.trim() === 'Filter by tag',
        );
        if (!label) return false;
        const card = label.closest('div').parentElement;
        const chip = [...card.querySelectorAll('button')].find(
            (b) => b.textContent.trim() === t,
        );
        if (!chip) return false;
        chip.setAttribute('data-shot-target', 'tag-filter');
        return true;
    }, tag);
    if (!marked) throw new Error(`Tag filter chip for ${tag} not found in the filter panel`);
    await page.click('[data-shot-target="tag-filter"]');
    await settle(page, 900);
    await page.evaluate((sel) => {
        const el = document.querySelector('[data-shot-target="tag-filter"]');
        if (el) el.removeAttribute('data-shot-target');
        // Show the filtered results from the top rather than wherever the
        // unfiltered list happened to be parked.
        const s = document.querySelector(sel);
        if (s) s.scrollTop = 0;
    }, SIDEBAR_SCROLLER);
    await settle(page, 400);
}

export default [
    {
        name: 'main-view',
        description: 'The main view: recordings list, transcript, and summary',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await go(page, '/');
            await openRecording(page, 'Railroad Tax on Stock Compensation Remuneration');
        },
    },
    {
        name: 'main-view-stats',
        description: 'Per-recording stats and speaker breakdown',
        theme: { dark: true, scheme: 'teal' },
        run: async (page) => {
            await go(page, '/');
            await openRecording(page, 'SEC/Data Science Team Updates and Announcements');
            await clickVisible(page, '.tab:has-text("Stats")');
            await settle(page, 600);
            // The per-speaker table needs the full width to show every column,
            // so collapse the sidebar for this one shot.
            await clickVisible(page, 'button[title="Hide sidebar"]');
            await settle(page, 600);
        },
    },
    {
        name: 'sidebar-filtering',
        description: 'Advanced filtering in the sidebar',
        theme: { dark: true, scheme: 'amber' },
        run: async (page) => {
            await go(page, '/');
            await openRecording(page, 'SEC/Data Science Team Updates and Announcements');
            await openFilterPanel(page);
            await applyTagFilter(page, 'AI');
        },
    },
];
