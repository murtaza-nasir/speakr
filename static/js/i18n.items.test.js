import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// i18n.js is a classic script that sets window.i18n (see i18n.test.js).
const SOURCE = readFileSync(fileURLToPath(new URL('./i18n.js', import.meta.url)), 'utf8');

function load(translations, locale) {
    const window = { dispatchEvent() {} };
    window.__I18N_BOOTSTRAP = { locale, translations };
    const context = vm.createContext({
        window, CustomEvent, fetch: async () => ({ ok: false, json: async () => ({}) }),
        console: { log() {}, warn() {}, error() {} },
        navigator: { language: 'en-US' },
        localStorage: { getItem: () => null, setItem() {} },
    });
    vm.runInContext(SOURCE, context);
    return window.i18n;
}

const EN = { whatsNew: { releases: { '0_10_20': { 1: 'First', 2: 'Second' }, '0_10_21': { 1: 'Only English' } } } };
const DE = { whatsNew: { releases: { '0_10_20': { 1: 'Erstens', 2: 'Zweitens' } } } };

describe('i18n.items', () => {
    it('returns the strings of an object key in order', () => {
        expect(load({ en: EN, de: DE }, 'de').items('whatsNew.releases.0_10_20')).toEqual(['Erstens', 'Zweitens']);
    });

    it('falls back to English when the language has no entry', () => {
        expect(load({ en: EN, de: DE }, 'de').items('whatsNew.releases.0_10_21')).toEqual(['Only English']);
    });

    it('returns an empty list for a missing key', () => {
        expect(load({ en: EN, de: DE }, 'de').items('whatsNew.releases.9_9_9')).toEqual([]);
    });

    it('accepts an array and skips anything that is not a string', () => {
        const i18n = load({ en: { list: ['a', 3, 'b', { x: 1 }] } }, 'en');
        expect(i18n.items('list')).toEqual(['a', 'b']);
    });
});
