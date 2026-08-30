// Shared helpers for reproducible Speakr documentation screenshots.

export const BASE_URL = process.env.SPEAKR_URL || 'https://spdev.murtaza.cc';

/** Exact 4:3 desktop viewport used for every documentation screenshot. */
export const VIEWPORT = { width: 1440, height: 1080 };
/** Phone viewport for mobile shots (portrait, 3x for crispness). */
export const MOBILE_VIEWPORT = { width: 390, height: 844 };

/**
 * Create a browser context pre-configured for a deterministic shot.
 * theme: { dark: true, scheme: 'blue'|'emerald'|'purple'|'rose'|'amber'|'teal' }
 */
export async function makeContext(browser, { dark = true, scheme = 'blue', mobile = false, storageState } = {}) {
    const context = await browser.newContext({
        viewport: mobile ? MOBILE_VIEWPORT : VIEWPORT,
        deviceScaleFactor: mobile ? 3 : 1,
        locale: 'en-US',
        timezoneId: 'America/Chicago',
        colorScheme: dark ? 'dark' : 'light',
        reducedMotion: 'reduce',
        storageState,
        ...(mobile ? { isMobile: true, hasTouch: true } : {}),
    });
    // Theme is decided BEFORE the app boots so there is no flash and no
    // dependency on UI interaction order.
    await context.addInitScript(([d, s]) => {
        try {
            localStorage.setItem('darkMode', d ? 'true' : 'false');
            if (s && s !== 'blue') localStorage.setItem('colorScheme', s);
            else localStorage.removeItem('colorScheme');
        } catch (_) {}
    }, [dark, scheme]);
    return context;
}

/** Kill animations/transitions and hide carets so pixels are stable. */
export async function freezePage(page) {
    await page.addStyleTag({
        content: `
            *, *::before, *::after {
                transition: none !important;
                animation-duration: 0.001s !important;
                animation-iteration-count: 1 !important;
                caret-color: transparent !important;
            }
        `,
    });
}

/** Wait for the SPA to be genuinely settled (loader gone, fonts in, net idle). */
export async function settle(page, extraMs = 800) {
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.waitForFunction(() => {
        const l = document.getElementById('loading-overlay') || document.querySelector('.loading-overlay');
        return !l || l.offsetParent === null;
    }, { timeout: 15000 }).catch(() => {});
    await page.evaluate(() => document.fonts && document.fonts.ready).catch(() => {});
    await page.waitForTimeout(extraMs);
}

/** Log in and return a storageState the runner reuses for every shot. */
export async function login(browser) {
    const email = process.env.SPEAKR_EMAIL;
    const password = process.env.SPEAKR_PASSWORD;
    if (!email || !password) {
        throw new Error('Set SPEAKR_EMAIL and SPEAKR_PASSWORD in the environment');
    }
    const context = await browser.newContext({ viewport: VIEWPORT });
    const page = await context.newPage();
    await page.goto(`${BASE_URL}/login`, { waitUntil: 'networkidle' });
    await page.locator('input[name="email"]:visible').first().fill(email);
    await page.locator('input[name="password"]:visible').first().fill(password);
    const form = page.locator('form:has(input[name="password"]:visible)').first();
    await Promise.all([
        page.waitForURL((u) => !String(u).includes('/login'), { timeout: 20000 }),
        form.locator('button[type="submit"], input[type="submit"]').first().click(),
    ]);
    const state = await context.storageState();
    await context.close();
    return state;
}

/** Navigate to a path and settle. */
export async function go(page, path, extraMs) {
    await page.goto(`${BASE_URL}${path}`, { waitUntil: 'domcontentloaded' });
    await settle(page, extraMs);
    await freezePage(page);
}

/**
 * Open a recording by (partial) title via the sidebar list. Text locators
 * match hidden template duplicates, so filter by on-screen position.
 */
export async function openRecordingByTitle(page, title) {
    await loadSidebarUntil(page, title);
    const cands = page.locator(`h4:has-text(${JSON.stringify(title)})`);
    const n = await cands.count();
    for (let i = 0; i < n; i++) {
        const box = await cands.nth(i).boundingBox();
        if (box && box.y >= 0 && box.x >= 0) {
            await cands.nth(i).click();
            await settle(page, 1000);
            return;
        }
    }
    throw new Error(`Recording titled ${title} not visible in the sidebar`);
}

/**
 * The sidebar list is infinite-scroll (25 rows per page). Scroll its
 * container until a recording title is present in the DOM (or growth stops).
 */
export async function loadSidebarUntil(page, title) {
    await page.evaluate(async (wanted) => {
        const list = document.querySelector('.flex-1.overflow-y-auto.pb-3')
            || document.querySelector('aside .overflow-y-auto');
        if (!list) return;
        let lastHeight = -1;
        for (let i = 0; i < 30; i++) {
            const found = [...list.querySelectorAll('h4')].some((h) => h.textContent.includes(wanted));
            if (found) return;
            if (list.scrollHeight === lastHeight) return;
            lastHeight = list.scrollHeight;
            list.scrollTop = list.scrollHeight;
            await new Promise((r) => setTimeout(r, 600));
        }
    }, title);
    await settle(page, 400);
}

/**
 * Scroll the sidebar so the recording with `title` is the FIRST visible row.
 * Use this to frame a curated, presentable region of the list (rows with
 * tags, folders and participants) instead of whatever happens to be newest.
 */
export async function anchorSidebar(page, title) {
    await loadSidebarUntil(page, title);
    await page.evaluate((wanted) => {
        const h = [...document.querySelectorAll('h4')].find((el) => el.textContent.includes(wanted) && el.closest('.overflow-y-auto'));
        if (!h) return;
        const row = h.closest('[class*="border-b"], .recording-item') || h.parentElement;
        const list = h.closest('.overflow-y-auto');
        list.scrollTop = row.offsetTop - list.offsetTop + list.scrollTop - 4;
    }, title);
    await page.waitForTimeout(400);
}

/** Click the first VISIBLE element matching a locator (skip hidden dupes). */
export async function clickVisible(page, selector) {
    const cands = page.locator(selector);
    const n = await cands.count();
    for (let i = 0; i < n; i++) {
        const box = await cands.nth(i).boundingBox();
        if (box) {
            await cands.nth(i).click();
            return true;
        }
    }
    return false;
}

/**
 * Blur every visible email address on the page (privacy: documentation
 * screenshots must not leak real addresses/domains). Applies a CSS blur to
 * the deepest elements whose own text contains an email; idempotent.
 */
export async function blurEmails(page) {
    await page.evaluate(() => {
        const emailRe = /[\w.+-]+@[\w-]+\.[\w.]+/;
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        const seen = new Set();
        let n;
        while ((n = walker.nextNode())) {
            if (!emailRe.test(n.nodeValue)) continue;
            const el = n.parentElement;
            if (!el || seen.has(el)) continue;
            seen.add(el);
            el.style.filter = 'blur(5px)';
        }
        // inputs whose VALUE is an email (account forms)
        document.querySelectorAll('input').forEach((inp) => {
            if (emailRe.test(inp.value)) inp.style.filter = 'blur(5px)';
        });
    });
    await page.waitForTimeout(200);
}
