import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// i18n.js is a classic script that sets window.i18n, so it is run here in a
// fresh context per test with a minimal window, localStorage and fetch.
const SOURCE = readFileSync(fileURLToPath(new URL('./i18n.js', import.meta.url)), 'utf8');

const EN = { nav: { account: 'Account', admin: 'Admin' }, only: { english: 'English only' } };
const DE = { nav: { account: 'Konto' } };
const FR = { nav: { account: 'Compte' } };

function load({ bootstrap, saved = null, files = { en: EN, de: DE, fr: FR } } = {}) {
    const events = [];
    const store = new Map(saved ? [['preferredLanguage', saved]] : []);
    const fetch = vi.fn(async (url) => {
        const code = url.match(/locales\/(.+)\.json$/)[1];
        return files[code]
            ? { ok: true, json: async () => files[code] }
            : { ok: false, json: async () => ({}) };
    });
    const window = {
        dispatchEvent: (e) => events.push({ type: e.type, detail: e.detail }),
    };
    if (bootstrap !== undefined) window.__I18N_BOOTSTRAP = bootstrap;
    const context = vm.createContext({
        window,
        fetch,
        CustomEvent,
        console: { log() {}, warn() {}, error() {} },
        navigator: { language: 'en-US' },
        localStorage: {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
        },
    });
    vm.runInContext(SOURCE, context);
    return { i18n: window.i18n, fetch, events, store };
}

describe('i18n bootstrap', () => {
    it('is ready synchronously with the embedded locale and no fetch', async () => {
        const { i18n, fetch, events } = load({ bootstrap: { locale: 'de', translations: { de: DE, en: EN } } });
        expect(i18n.isReady).toBe(true);
        expect(i18n.getLocale()).toBe('de');
        expect(i18n.t('nav.account')).toBe('Konto');
        expect(i18n.t('only.english')).toBe('English only'); // English fallback
        expect(events.map(e => e.type)).toEqual(['i18nReady']);
        await expect(i18n.ready).resolves.toBeUndefined();
        await i18n.init('de'); // the pages still call init(); nothing is fetched
        expect(fetch).not.toHaveBeenCalled();
        expect(events).toHaveLength(1);
    });

    it('keeps the saved language first, as init() does, and fetches it when not embedded', async () => {
        const { i18n, fetch, events } = load({
            bootstrap: { locale: 'de', translations: { de: DE, en: EN } },
            saved: 'fr',
        });
        expect(i18n.isReady).toBe(false);
        expect(i18n.t('nav.account')).toBe('Account'); // English until French arrives
        await i18n.init('de');
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(fetch.mock.calls[0][0]).toBe('/static/locales/fr.json');
        expect(i18n.isReady).toBe(true);
        expect(i18n.t('nav.account')).toBe('Compte');
        expect(events.map(e => e.type)).toEqual(['i18nReady']);
    });

    it('uses a saved language that is embedded', () => {
        const { i18n } = load({ bootstrap: { locale: 'en', translations: { de: DE, en: EN } }, saved: 'de' });
        expect(i18n.isReady).toBe(true);
        expect(i18n.t('nav.account')).toBe('Konto');
    });

    it('works as before without a bootstrap', async () => {
        const { i18n, fetch, events } = load();
        expect(i18n.isReady).toBe(false);
        await i18n.init('de');
        expect(fetch.mock.calls.map(c => c[0])).toEqual(['/static/locales/de.json', '/static/locales/en.json']);
        expect(i18n.t('nav.account')).toBe('Konto');
        expect(events.map(e => e.type)).toEqual(['i18nReady']);
    });

    it('ignores a malformed bootstrap', async () => {
        for (const bootstrap of [null, 'x', { locale: 'de' }, { locale: 'de', translations: 'x' }]) {
            const { i18n } = load({ bootstrap });
            expect(i18n.isReady).toBe(false);
            await i18n.init('en');
            expect(i18n.t('nav.account')).toBe('Account');
        }
    });

    it('waits for English when only the locale is embedded', async () => {
        const { i18n, fetch } = load({ bootstrap: { locale: 'de', translations: { de: DE } } });
        expect(i18n.isReady).toBe(false);
        await i18n.init('de');
        expect(fetch.mock.calls.map(c => c[0])).toEqual(['/static/locales/en.json']);
        expect(i18n.isReady).toBe(true);
    });

    it('changes language with setLocale as before', async () => {
        const { i18n, events, store } = load({ bootstrap: { locale: 'en', translations: { en: EN } } });
        await i18n.setLocale('fr');
        expect(i18n.t('nav.account')).toBe('Compte');
        expect(store.get('preferredLanguage')).toBe('fr');
        expect(events.map(e => e.type)).toEqual(['i18nReady', 'localeChanged']);
    });
});

describe('interface locale for dates and numbers (#480)', () => {
    const at = new Date(Date.UTC(2026, 9, 17, 14, 30));

    it('returns the selected language as an Intl tag', () => {
        for (const code of ['en', 'de', 'es', 'fr', 'ru', 'zh', 'pt-BR']) {
            const { i18n } = load({ bootstrap: { locale: code, translations: { [code]: {}, en: EN } } });
            expect(i18n.getIntlLocale()).toBe(code);
            expect(() => new Intl.DateTimeFormat(i18n.getIntlLocale())).not.toThrow();
        }
    });

    it('falls back to the browser default for a code Intl rejects', () => {
        const { i18n } = load({ bootstrap: { locale: 'en', translations: { en: EN } }, saved: 'not a tag!' });
        expect(i18n.getLocale()).toBe('not a tag!');
        expect(i18n.getIntlLocale()).toBeUndefined();
        expect(() => i18n.formatDate(at, { timeZone: 'UTC' })).not.toThrow();
        expect(() => i18n.formatNumber(1.5)).not.toThrow();
    });

    it('formats dates and numbers in the interface language, not the browser one', () => {
        const { i18n } = load({ bootstrap: { locale: 'de', translations: { de: DE, en: EN } } });
        const opts = { month: 'short', day: 'numeric', timeZone: 'UTC' };
        expect(i18n.formatDate(at, opts)).toBe('17. Okt.');
        expect(at.toLocaleDateString(i18n.getIntlLocale(), opts)).toBe('17. Okt.');
        expect(at.toLocaleTimeString(i18n.getIntlLocale(), { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' })).toBe('14:30');
        expect(i18n.formatNumber(1234.5)).toBe('1.234,5');
    });

    it('keeps the English formats for English', () => {
        const { i18n } = load({ bootstrap: { locale: 'en', translations: { en: EN } } });
        expect(i18n.formatDate(at, { month: 'short', day: 'numeric', timeZone: 'UTC' })).toBe('Oct 17');
        expect(at.toLocaleTimeString(i18n.getIntlLocale(), { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' })).toMatch(/^2:30\sPM$/); // ICU may use a narrow no-break space
        expect(i18n.formatNumber(1234.5)).toBe('1,234.5');
    });
});

describe('file sizes (#481)', () => {
    // The shipped catalogs, so a missing fileSize.* key fails here too.
    const catalog = (code) => JSON.parse(readFileSync(fileURLToPath(new URL(`../locales/${code}.json`, import.meta.url)), 'utf8'));
    const sizes = (code) => {
        const translations = { en: catalog('en') };
        translations[code] = catalog(code);
        return load({ bootstrap: { locale: code, translations } }).i18n;
    };

    it('uses the decimal separator of the interface language', () => {
        expect(sizes('de').formatFileSize(1464812)).toBe('1,4 MB');
        expect(sizes('en').formatFileSize(1464812)).toBe('1.4 MB');
        for (const code of ['es', 'fr', 'pt-BR']) expect(sizes(code).formatFileSize(1464812)).toBe('1,4 MB');
        expect(sizes('zh').formatFileSize(1464812)).toBe('1.4 MB');
        expect(sizes('ru').formatFileSize(1464812)).toBe('1,4 МБ');
    });

    it('keeps zero, whole units and the unit boundaries', () => {
        const de = sizes('de');
        expect(de.formatFileSize(0)).toBe('0 B');
        expect(de.formatFileSize(1023)).toBe('1023 B');
        expect(de.formatFileSize(1024)).toBe('1 KB');
        expect(de.formatFileSize(1536000)).toBe('1,5 MB');
        expect(de.formatFileSize(1024 ** 3)).toBe('1 GB');
        expect(de.formatFileSize(2.5 * 1024 ** 4)).toBe('2,5 TB');
        expect(de.formatFileSize(1024 ** 5)).toBe('1024 TB');
    });

    it('rounds to one decimal and moves up a unit when the rounding reaches 1024', () => {
        const en = sizes('en');
        expect(en.formatFileSize(1530000)).toBe('1.5 MB'); // 1.459 MB
        expect(en.formatFileSize(9 * 1024 ** 2)).toBe('9 MB');
        expect(en.formatFileSize(1024 ** 2 - 1)).toBe('1 MB'); // 1023.999 KB
        expect(en.formatFileSize(1024 ** 3 - 1024)).toBe('1 GB'); // 1023.999 MB
    });

    it('reads a missing or invalid size as zero', () => {
        const en = sizes('en');
        for (const value of [null, undefined, NaN, 'x', -5]) expect(en.formatFileSize(value)).toBe('0 B');
    });
});

describe('percentages (#480)', () => {
    const pct = (code) => load({ bootstrap: { locale: code, translations: { en: EN } } }).i18n;
    // ICU puts a no-break space (U+00A0) or a narrow one (U+202F) before % in some languages.
    const plain = (s) => s.replace(/[\u00a0\u202f]/g, ' ');

    it('uses the separator and the % spacing of the interface language', () => {
        expect(pct('en').formatPercent(12.5)).toBe('12.5%');
        expect(plain(pct('de').formatPercent(12.5))).toBe('12,5 %');
        expect(plain(pct('fr').formatPercent(12.5))).toBe('12,5 %');
        expect(plain(pct('es').formatPercent(12.5))).toBe('12,5 %');
        expect(plain(pct('ru').formatPercent(12.5))).toBe('12,5 %');
        expect(pct('pt-BR').formatPercent(12.5)).toBe('12,5%');
        expect(pct('zh').formatPercent(12.5)).toBe('12.5%');
    });

    it('takes the value on the 0-100 scale with at most one decimal by default', () => {
        const en = pct('en');
        expect(en.formatPercent(0)).toBe('0%');
        expect(en.formatPercent(100)).toBe('100%');
        expect(en.formatPercent(150)).toBe('150%');
        expect(en.formatPercent(33.333)).toBe('33.3%');
        expect(en.formatPercent(7)).toBe('7%');
        expect(en.formatPercent(12.345, { maximumFractionDigits: 2 })).toBe('12.35%');
    });

    it('signs a change when asked and reads an invalid value as zero', () => {
        const en = pct('en');
        expect(en.formatPercent(12, { signDisplay: 'exceptZero' })).toBe('+12%');
        expect(en.formatPercent(-4, { signDisplay: 'exceptZero' })).toBe('-4%');
        for (const value of [null, undefined, NaN, 'x']) expect(en.formatPercent(value)).toBe('0%');
    });
});
