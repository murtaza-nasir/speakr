import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { currentUiTheme, useUiTheme, UI_THEME_EVENT } from './ui-theme.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..');

// A minimal browser: <html> attributes, <head> children, localStorage and
// window events, enough for templates/includes/ui_theme.html.
function fakeBrowser(stored) {
    const listeners = {};
    const docListeners = {};
    const attrs = {};
    const head = [];
    const storage = { ...(stored || {}) };
    const makeElement = (tag) => {
        const el = {
            tagName: tag, attrs: {},
            setAttribute(k, v) { this.attrs[k] = String(v); },
            getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
            parentNode: null,
        };
        Object.defineProperty(el, 'id', { get() { return el.attrs.id || ''; }, set(v) { el.attrs.id = v; } });
        Object.defineProperty(el, 'rel', { get() { return el.attrs.rel || ''; }, set(v) { el.attrs.rel = v; } });
        return el;
    };
    const document = {
        documentElement: {
            setAttribute(k, v) { attrs[k] = String(v); },
            getAttribute(k) { return k in attrs ? attrs[k] : null; },
        },
        head: {
            appendChild(el) { el.parentNode = this; head.push(el); return el; },
            removeChild(el) { head.splice(head.indexOf(el), 1); el.parentNode = null; },
        },
        createElement: makeElement,
        getElementById(id) { return head.find((el) => el.id === id) || null; },
        querySelectorAll() { return []; },
        addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    };
    const win = {
        document,
        localStorage: {
            getItem: (k) => (k in storage ? storage[k] : null),
            setItem: (k, v) => { storage[k] = String(v); },
        },
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        dispatchEvent(event) { (listeners[event.type] || []).forEach((fn) => fn(event)); return true; },
        CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    };
    return { win, attrs, head, storage, listeners };
}

// Runs the inline script of templates/includes/ui_theme.html with the given
// registry (id -> stylesheet URL, as the ui_theme_assets context value).
function runPrePaint(browser, assets) {
    const html = readFileSync(join(ROOT, 'templates', 'includes', 'ui_theme.html'), 'utf8');
    const script = html.slice(html.indexOf('<script>') + 8, html.indexOf('</script>'))
        .replace('{{ ui_theme_assets|tojson }}', JSON.stringify(assets));
    const { win } = browser;
    // eslint-disable-next-line no-new-func
    new Function('window', 'document', 'localStorage', 'CustomEvent', script)(
        win, win.document, win.localStorage, win.CustomEvent);
}

const ref = (value) => ({ value });

describe('pre-paint script (includes/ui_theme.html)', () => {
    const assets = { default: '', classic: '/static/css/themes/classic.css' };

    it('sets the default theme and adds no stylesheet when nothing is stored', () => {
        const b = fakeBrowser();
        runPrePaint(b, assets);
        expect(b.attrs['data-ui-theme']).toBe('default');
        expect(b.head).toHaveLength(0);
    });

    it('applies a stored theme with its stylesheet', () => {
        const b = fakeBrowser({ uiTheme: 'classic' });
        runPrePaint(b, assets);
        expect(b.attrs['data-ui-theme']).toBe('classic');
        expect(b.head).toHaveLength(1);
        expect(b.head[0].getAttribute('href')).toBe('/static/css/themes/classic.css');
        expect(b.head[0].rel).toBe('stylesheet');
    });

    it('falls back to the default for an unknown or unavailable id', () => {
        for (const stored of ['nonsense', 'cards', '__proto__', '']) {
            const b = fakeBrowser({ uiTheme: stored });
            runPrePaint(b, assets);
            expect(b.attrs['data-ui-theme']).toBe('default');
            expect(b.head).toHaveLength(0);
        }
    });

    it('switches the attribute, the stylesheet and the stored value without a reload', () => {
        const b = fakeBrowser();
        runPrePaint(b, assets);
        const seen = [];
        b.win.addEventListener(UI_THEME_EVENT, (e) => seen.push(e.detail.id));
        let saved = 0;
        b.win.speakrSaveTheme = (key) => { if (key === 'uiTheme') saved += 1; };

        expect(b.win.speakrApplyUiTheme('classic')).toBe('classic');
        expect(b.attrs['data-ui-theme']).toBe('classic');
        expect(b.head.map((el) => el.getAttribute('href'))).toEqual(['/static/css/themes/classic.css']);
        expect(b.storage.uiTheme).toBe('classic');

        expect(b.win.speakrApplyUiTheme('default')).toBe('default');
        expect(b.attrs['data-ui-theme']).toBe('default');
        expect(b.head).toHaveLength(0);
        expect(b.storage.uiTheme).toBe('default');

        expect(seen).toEqual(['classic', 'default']);
        expect(saved).toBe(2);
    });

    it('follows a theme chosen in another tab without saving it again', () => {
        const b = fakeBrowser();
        runPrePaint(b, assets);
        let saved = 0;
        b.win.speakrSaveTheme = () => { saved += 1; };
        b.listeners.storage.forEach((fn) => fn({ key: 'uiTheme', newValue: 'classic' }));
        expect(b.attrs['data-ui-theme']).toBe('classic');
        expect(saved).toBe(0);
    });
});

describe('useUiTheme', () => {
    it('starts from the attribute the pre-paint script set', () => {
        const b = fakeBrowser({ uiTheme: 'classic' });
        runPrePaint(b, { default: '', classic: '/x.css' });
        expect(currentUiTheme(b.win.document)).toBe('classic');
        const { uiTheme } = useUiTheme(ref, b.win);
        expect(uiTheme.value).toBe('classic');
    });

    it('is the default without the attribute', () => {
        const b = fakeBrowser();
        const { uiTheme } = useUiTheme(ref, b.win);
        expect(uiTheme.value).toBe('default');
    });

    it('updates the reactive value when the theme is switched', () => {
        const b = fakeBrowser();
        runPrePaint(b, { default: '', classic: '/x.css' });
        const { uiTheme, setUiTheme } = useUiTheme(ref, b.win);
        expect(setUiTheme('classic')).toBe('classic');
        expect(uiTheme.value).toBe('classic');
        setUiTheme('unknown');
        expect(uiTheme.value).toBe('default');
    });
});
