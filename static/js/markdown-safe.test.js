/**
 * window.sanitizeHtml and window.renderMarkdownSafe (#695): the HTML given to
 * v-html goes through DOMPurify, and is escaped text when DOMPurify is absent.
 */
import { afterEach, beforeAll, describe, expect, it } from 'vitest';

beforeAll(async () => {
    globalThis.window = globalThis.window || {};
    await import('./markdown-safe.js');
});

afterEach(() => {
    delete window.DOMPurify;
    delete window.marked;
});

const payload = '<details open ontoggle="window.__xss=1"></details><p>hello</p>';

describe('sanitizeHtml', () => {
    it('passes server HTML through DOMPurify', () => {
        const seen = [];
        window.DOMPurify = { sanitize: (html) => { seen.push(html); return '<p>hello</p>'; } };
        expect(window.sanitizeHtml(payload)).toBe('<p>hello</p>');
        expect(seen).toEqual([payload]);
    });

    it('emits escaped text when DOMPurify is unavailable', () => {
        const out = window.sanitizeHtml(payload);
        expect(out).not.toContain('<details');
        expect(out).toBe('&lt;details open ontoggle=&quot;window.__xss=1&quot;&gt;&lt;/details&gt;&lt;p&gt;hello&lt;/p&gt;');
    });

    it('returns an empty string for no value', () => {
        expect(window.sanitizeHtml(null)).toBe('');
        expect(window.sanitizeHtml(undefined)).toBe('');
        expect(window.sanitizeHtml('')).toBe('');
    });
});

describe('renderMarkdownSafe', () => {
    it('sanitizes the parsed markdown', () => {
        const seen = [];
        window.marked = { parse: (text) => `<p>${text}</p>` };
        window.DOMPurify = { sanitize: (html) => { seen.push(html); return '<p>clean</p>'; } };
        expect(window.renderMarkdownSafe('<img src=x onerror="a()">')).toBe('<p>clean</p>');
        expect(seen).toEqual(['<p><img src=x onerror="a()"></p>']);
    });

    it('fails closed without DOMPurify', () => {
        window.marked = { parse: (text) => `<p>${text}</p>` };
        expect(window.renderMarkdownSafe('<b>x</b>')).toBe('&lt;p&gt;&lt;b&gt;x&lt;/b&gt;&lt;/p&gt;');
    });
});
