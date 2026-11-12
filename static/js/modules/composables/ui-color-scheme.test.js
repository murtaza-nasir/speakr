// The color scheme in the main app: the stored or default scheme is applied
// without being stored, so a value in localStorage is always a choice the
// user made (templates/includes/theme_account.html); a picked scheme is
// stored and saved with the account.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useUI } from './ui.js';

const SCHEMES = ['blue', 'emerald', 'purple', 'rose', 'amber', 'teal'];
const colorSchemes = {
    light: SCHEMES.map(id => ({ id, class: id === 'blue' ? '' : `theme-light-${id}` })),
    dark: SCHEMES.map(id => ({ id, class: id === 'blue' ? '' : `theme-dark-${id}` })),
};

let stored, classes, saved;

function setup(initial = {}, defaults = { colorScheme: 'blue' }) {
    stored = { ...initial };
    classes = new Set();
    saved = [];
    globalThis.Vue = { ref: v => ({ value: v }), computed: f => ({ get value() { return f(); } }), watch: () => {} };
    globalThis.localStorage = {
        getItem: k => (k in stored ? stored[k] : null),
        setItem: (k, v) => { stored[k] = String(v); },
    };
    globalThis.document = { documentElement: { classList: {
        add: (...c) => c.forEach(x => classes.add(x)),
        remove: (...c) => c.forEach(x => classes.delete(x)),
    } } };
    globalThis.window = { localStorage: globalThis.localStorage, speakrThemeDefaults: defaults,
                          speakrSaveTheme: key => saved.push(key) };
    const state = new Proxy({ colorSchemes, isDarkMode: { value: false }, currentColorScheme: { value: 'blue' } }, {
        get: (t, k) => (k in t ? t[k] : (t[k] = { value: null })),
    });
    return useUI(state, { showToast: () => {}, nextTick: () => Promise.resolve(), t: k => k }, { value: null });
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => { delete globalThis.Vue; delete globalThis.localStorage; delete globalThis.document; delete globalThis.window; });

describe('color scheme', () => {
    it('applies blue without storing it when nothing is stored', () => {
        const ui = setup();
        ui.initializeColorScheme();
        expect(classes.size).toBe(0);
        expect(stored).toEqual({});
        expect(saved).toEqual([]);
    });

    it('applies the stored scheme without storing or saving again', () => {
        const ui = setup({ colorScheme: 'rose' });
        ui.initializeColorScheme();
        expect([...classes]).toEqual(['theme-light-rose']);
        expect(stored).toEqual({ colorScheme: 'rose' });
        expect(saved).toEqual([]);
    });

    it('stores and saves a scheme the user picks', () => {
        const ui = setup();
        ui.initializeColorScheme();
        ui.selectColorScheme('teal');
        expect([...classes]).toEqual(['theme-light-teal']);
        expect(stored).toEqual({ colorScheme: 'teal' });
        expect(saved).toEqual(['colorScheme']);
    });

    it('stores blue when the user resets the scheme', () => {
        const ui = setup({ colorScheme: 'amber' });
        ui.initializeColorScheme();
        ui.resetColorScheme();
        expect(classes.size).toBe(0);
        expect(stored).toEqual({ colorScheme: 'blue' });
        expect(saved).toEqual(['colorScheme']);
    });
});
