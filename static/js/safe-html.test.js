/**
 * safeHtml (#692): a tagged template that escapes every value and inserts
 * only its own results as markup.
 */
import { beforeAll, describe, expect, it } from 'vitest';

let safeHtml;
beforeAll(async () => {
    globalThis.window = globalThis.window || {};
    await import('./safe-html.js');
    safeHtml = window.safeHtml;
});

const payload = '<img src=x onerror="window.__xss=1">';

describe('safeHtml', () => {
    it('escapes a value in text', () => {
        expect(String(safeHtml`<p>${payload}</p>`)).toBe('<p>&lt;img src=x onerror=&quot;window.__xss=1&quot;&gt;</p>');
    });

    it('escapes both quote characters in attributes', () => {
        const id = `1" onmouseover="a()' x='`;
        expect(String(safeHtml`<b data-id="${id}" title='${id}'></b>`))
            .toBe('<b data-id="1&quot; onmouseover=&quot;a()&#39; x=&#39;" title=\'1&quot; onmouseover=&quot;a()&#39; x=&#39;\'></b>');
    });

    it('escapes an ampersand once', () => {
        expect(String(safeHtml`${'a &amp; b'}`)).toBe('a &amp;amp; b');
    });

    it('inserts its own results as markup, so pieces compose', () => {
        const badge = safeHtml`<span>${'<b>'}</span>`;
        expect(String(safeHtml`<div>${badge}</div>`)).toBe('<div><span>&lt;b&gt;</span></div>');
    });

    it('inserts an array item by item, with no separator, escaping plain strings', () => {
        const rows = ['<i>', 'b'].map((x) => safeHtml`<li>${x}</li>`);
        expect(String(safeHtml`<ul>${rows}</ul>`)).toBe('<ul><li>&lt;i&gt;</li><li>b</li></ul>');
        expect(String(safeHtml`${['<i>', safeHtml`<i></i>`]}`)).toBe('&lt;i&gt;<i></i>');
    });

    it('inserts nothing for null, undefined and false, and text for numbers and true', () => {
        expect(String(safeHtml`[${null}${undefined}${false}${0}${true}]`)).toBe('[0true]');
    });

    it('treats a string that looks like a result as text', () => {
        const forged = { html: '<script></script>', toString() { return '<script></script>'; } };
        expect(String(safeHtml`${forged}`)).toBe('&lt;script&gt;&lt;/script&gt;');
        expect(safeHtml.isSafe(forged)).toBe(false);
        expect(safeHtml.isSafe(safeHtml`x`)).toBe(true);
    });

    it('is assignable to innerHTML through its string form', () => {
        expect(`${safeHtml`<a>${'&'}</a>`}`).toBe('<a>&amp;</a>');
    });

    it('exposes the escape it uses', () => {
        expect(safeHtml.escape(`<&"'>`)).toBe('&lt;&amp;&quot;&#39;&gt;');
        expect(safeHtml.escape(null)).toBe('');
    });
});
