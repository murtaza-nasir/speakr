// The pre-paint scripts of templates/includes/theme_account.html and
// templates/includes/ui_theme.html, run against a fake browser: which
// color scheme and UI theme a page applies, and what it leaves in
// localStorage, for a visitor and for a signed-in account.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf-8');

function scripts(html) {
    return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
}

// Renders theme_account.html as Jinja would: the account block only for a
// signed-in account.
function accountScripts(defaults, account) {
    let html = read('templates/includes/theme_account.html').replace(/\{#-?[\s\S]*?-?#\}/g, '');
    html = html.replace('{{ theme_defaults|tojson }}', JSON.stringify(defaults));
    const block = /\{% if account_theme %\}([\s\S]*?)\{% endif %\}/;
    html = account
        ? html.replace(block, '$1').replace('{{ account_theme|tojson }}', JSON.stringify(account))
            .replace('{{ csrf_token()|tojson }}', '"token"')
        : html.replace(block, '');
    return scripts(html);
}

function uiThemeScripts(themes) {
    const html = read('templates/includes/ui_theme.html').replace(/\{#-?[\s\S]*?-?#\}/g, '');
    return scripts(html.replace('{{ ui_theme_assets|tojson }}', JSON.stringify(themes)));
}

function browser(stored = {}) {
    const values = { ...stored };
    const attrs = {};
    const listeners = {};
    const doc = {
        documentElement: { setAttribute: (k, v) => { attrs[k] = v; } },
        head: { appendChild: () => {} },
        getElementById: () => null,
        createElement: () => ({ setAttribute() {}, getAttribute() { return null; } }),
        querySelectorAll: () => [],
        addEventListener: () => {},
    };
    const win = {
        localStorage: {
            getItem: (k) => (k in values ? values[k] : null),
            setItem: (k, v) => { values[k] = String(v); },
        },
        addEventListener: (name, fn) => { listeners[name] = fn; },
        dispatchEvent: () => {},
        CustomEvent: function CustomEvent() {},
        fetch: () => Promise.resolve(),
    };
    win.window = win;
    win.document = doc;
    return { win, values, attrs };
}

function run(b, sources) {
    const ctx = vm.createContext(b.win);
    for (const src of sources) vm.runInContext(src, ctx);
}

const THEMES = { default: '', cards: '/static/css/themes/cards.css', focus: '/static/css/themes/focus.css' };
const VISITOR = { colorScheme: 'emerald', uiTheme: 'cards' };
const SIGNED_IN = { colorScheme: 'blue', uiTheme: 'default' };

describe('signed-out pages', () => {
    it('show the default for new accounts in a browser with nothing saved, and save nothing', () => {
        const b = browser();
        run(b, [...accountScripts(VISITOR, null), ...uiThemeScripts(THEMES)]);
        expect(b.attrs['data-ui-theme']).toBe('cards');
        expect(b.win.speakrThemeDefaults.colorScheme).toBe('emerald');
        expect(b.values).toEqual({});
        expect(b.win.speakrSaveTheme).toBeUndefined();
    });

    it('keep the theme saved in the browser', () => {
        const b = browser({ uiTheme: 'focus', colorScheme: 'rose' });
        run(b, [...accountScripts(VISITOR, null), ...uiThemeScripts(THEMES)]);
        expect(b.attrs['data-ui-theme']).toBe('focus');
        expect(b.values).toEqual({ uiTheme: 'focus', colorScheme: 'rose' });
    });
});

describe('signed-in pages', () => {
    it('apply the default theme and blue for an account with no saved values', () => {
        // The browser showed the sign-in page with the new-account look, which
        // was never saved; nothing of it remains after sign-in.
        const b = browser();
        run(b, [...accountScripts(VISITOR, null), ...uiThemeScripts(THEMES)]);
        run(b, [...accountScripts(SIGNED_IN, { dark: null, scheme: null, ui_theme: null }), ...uiThemeScripts(THEMES)]);
        expect(b.attrs['data-ui-theme']).toBe('default');
        expect(b.values).toEqual({ colorScheme: 'blue', uiTheme: 'default' });
    });

    it('replace another account\'s look left in the browser', () => {
        const b = browser({ uiTheme: 'cards', colorScheme: 'emerald', darkMode: 'true' });
        run(b, [...accountScripts(SIGNED_IN, { dark: null, scheme: null, ui_theme: null }), ...uiThemeScripts(THEMES)]);
        expect(b.attrs['data-ui-theme']).toBe('default');
        expect(b.values).toEqual({ colorScheme: 'blue', uiTheme: 'default', darkMode: 'true' });
    });

    it('apply the account\'s saved values', () => {
        const b = browser({ uiTheme: 'default', colorScheme: 'blue' });
        run(b, [...accountScripts(SIGNED_IN, { dark: false, scheme: 'emerald', ui_theme: 'cards' }), ...uiThemeScripts(THEMES)]);
        expect(b.attrs['data-ui-theme']).toBe('cards');
        expect(b.values).toEqual({ colorScheme: 'emerald', uiTheme: 'cards', darkMode: 'false' });
        expect(typeof b.win.speakrSaveTheme).toBe('function');
    });
});
