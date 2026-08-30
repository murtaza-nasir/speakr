// Account settings and admin dashboard shots.
//
// Two classic server-rendered pages rather than the SPA: /account (tabbed,
// each tab a `#content-*` panel with its own `.tab-scroll` scroller) and
// /admin (a small Vue app whose tabs are driven by `activeTab`).

import { go, settle, clickVisible, openRecordingByTitle, blurEmails } from '../helpers.mjs';

/** A recording carrying tags, used for the localized interface shots. */
const TAGGED_RECORDING = 'SEC/Data Science Team Updates and Announcements';

/** Switch the /account page to a tab by its nav id ("prompts", "tokens", ...). */
async function accountTab(page, name) {
    await page.click(`#tab-${name}`);
    await settle(page, 900);
}

/**
 * Scroll the active tab's `.tab-scroll` container so the first VISIBLE element
 * matching `selector` (optionally also containing `text`) sits near the top.
 * Both pages render every tab's markup, so hidden duplicates must be skipped.
 */
async function scrollTabTo(page, selector, margin = 24, text = null) {
    const found = await page.evaluate(([sel, m, txt]) => {
        const target = [...document.querySelectorAll(sel)].find(
            (el) => el.getBoundingClientRect().height > 0 && (!txt || el.textContent.includes(txt)),
        );
        if (!target) return false;
        const scroller = target.closest('.tab-scroll');
        if (!scroller) return false;
        const offset = target.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
        scroller.scrollTop = Math.min(Math.max(offset - m, 0), scroller.scrollHeight - scroller.clientHeight);
        return true;
    }, [selector, margin, text]);
    if (!found) throw new Error(`Nothing visible to scroll to for ${selector}${text ? ` / ${text}` : ''}`);
    await page.waitForTimeout(400);
}

/** Switch the /admin Vue dashboard to one of its tabs by label text. */
async function adminTab(page, label) {
    const ok = await clickVisible(page, `nav.tabs button:has-text(${JSON.stringify(label)})`);
    if (!ok) throw new Error(`Admin tab ${label} not found`);
    await settle(page, 1500);
}

/**
 * Make sure a personal API token with this name exists, creating it through
 * the real dialog on the first run only (so re-runs don't pile up tokens).
 */
async function ensureApiToken(page, name) {
    const exists = await page.evaluate(async (n) => {
        const res = await fetch('/api/tokens');
        const body = await res.json();
        return (body.tokens || []).some((t) => t.name === n && !t.revoked);
    }, name);
    if (exists) return;
    await page.click('#createTokenBtn');
    await page.waitForSelector('#createTokenModal:not(.hidden)');
    await page.fill('#tokenNameInput', name);
    await page.selectOption('#tokenExpirationSelect', '365');
    await page.click('#submitCreateTokenBtn');
    await page.waitForSelector('#tokenSecretModal:not(.hidden)', { timeout: 15000 });
    await page.click('#closeTokenSecretModal');
    await settle(page, 600);
}

/**
 * Render the interface in another language WITHOUT touching the account's
 * stored `ui_language`. Both the SPA bootstrap (templates/index.html) and
 * i18n.init() read `localStorage.preferredLanguage` in preference to the
 * server-rendered value, and the browser context is thrown away after the
 * shot, so nothing about the account changes.
 */
async function useLanguage(page, locale) {
    await page.context().addInitScript((l) => {
        try { localStorage.setItem('preferredLanguage', l); } catch (_) {}
    }, locale);
}

/** Fail loudly if the locale file did not actually take effect. */
async function assertLocale(page, locale) {
    const active = await page.evaluate(() => (window.i18n && window.i18n.currentLocale) || null);
    if (active !== locale) throw new Error(`Interface locale is ${active}, expected ${locale}`);
}

export default [
    {
        name: 'custom-summary-prompt',
        description: 'Customising the summary prompt',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await go(page, '/account');
            await accountTab(page, 'prompts');
            // Frame the whole "Summary Generation Prompt" card: the user's own
            // prompt, and the admin default it overrides.
            await scrollTabTo(page, '[data-i18n="customPrompts.summaryGeneration"]', 32);
        },
    },
    {
        name: 'settings-templates',
        description: 'Export templates for transcripts and titles',
        theme: { dark: true, scheme: 'emerald' },
        run: async (page) => {
            await go(page, '/account');
            await accountTab(page, 'templates');
            // Open one template so the editor shows a real body and the
            // variable reference instead of the "select a template" placeholder.
            if (!(await clickVisible(page, '#templatesList h4:has-text("Meeting Minutes")'))) {
                throw new Error('Meeting Minutes template row not found');
            }
            await settle(page, 600);
        },
    },
    {
        name: 'settings-api-tokens',
        description: 'Personal access tokens for the REST API',
        theme: { dark: true, scheme: 'teal' },
        run: async (page) => {
            await go(page, '/account');
            await accountTab(page, 'tokens');
            // The reveal dialog dims the whole page behind it, so the shot
            // shows the token list itself; the sample token is created (once)
            // through that dialog so the list has a plausible entry.
            await ensureApiToken(page, 'n8n automation');
        },
    },
    {
        name: 'settings-webhooks',
        description: 'Outbound webhooks on recording events',
        theme: { dark: true, scheme: 'purple' },
        run: async (page) => {
            await go(page, '/account');
            await accountTab(page, 'webhooks');
            // Expand the delivery log of the first endpoint so the shot shows
            // the event history, not just the endpoint list.
            await page.click('#webhook-list > div:first-child [data-action="deliveries"]');
            await page.waitForFunction(
                () => !/Loading/.test(document.querySelector('#webhook-list [data-deliveries-list]').innerText),
                { timeout: 15000 },
            );
            await settle(page, 600);
        },
    },
    {
        name: 'admin-user-management',
        description: 'Managing users',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await go(page, '/admin', 1500);
            await blurEmails(page);
        },
    },
    {
        name: 'admin-group-management',
        description: 'Managing groups',
        theme: { dark: true, scheme: 'rose' },
        run: async (page) => {
            await go(page, '/admin', 1500);
            await adminTab(page, 'Groups');
            await blurEmails(page);
        },
    },
    {
        name: 'admin-system-settings',
        description: 'System settings',
        theme: { dark: true, scheme: 'amber' },
        run: async (page) => {
            await go(page, '/admin', 1500);
            await adminTab(page, 'System Settings');
        },
    },
    {
        name: 'admin-default-prompt',
        description: 'The default summarization prompt',
        theme: { dark: true, scheme: 'emerald' },
        run: async (page) => {
            await go(page, '/admin', 1500);
            await adminTab(page, 'Default Prompts');
        },
    },
    {
        name: 'admin-token-budgets',
        description: 'Per-user monthly token budgets',
        theme: { dark: true, scheme: 'teal' },
        run: async (page) => {
            await go(page, '/admin', 1500);
            await adminTab(page, 'System Statistics');
            // The budgets live below the instance-wide counters; the negative
            // margin parks the section heading just above the frame so the
            // whole per-user budget panel fits.
            await scrollTabTo(page, 'h4', -60, 'Token Usage Statistics');
            await blurEmails(page);
        },
    },
    {
        name: 'admin-vector-store',
        description: 'Vector store status for semantic search',
        theme: { dark: true, scheme: 'purple' },
        run: async (page) => {
            await go(page, '/admin', 1500);
            await adminTab(page, 'Vector Store');
        },
    },
    {
        name: 'ui-german',
        description: 'The full interface in German',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await useLanguage(page, 'de');
            await go(page, '/');
            await assertLocale(page, 'de');
            await openRecordingByTitle(page, TAGGED_RECORDING);
        },
    },
    {
        name: 'ui-chinese',
        description: 'The full interface in Chinese',
        theme: { dark: true, scheme: 'purple' },
        run: async (page) => {
            await useLanguage(page, 'zh');
            await go(page, '/');
            await assertLocale(page, 'zh');
            await openRecordingByTitle(page, TAGGED_RECORDING);
        },
    },
];
