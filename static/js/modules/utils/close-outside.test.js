import { describe, it, expect } from 'vitest';
import { closeOpenMenus, installCloseOutside } from './close-outside.js';

// A minimal document: open <details data-close-outside> menus and listeners.
function fakeDocument() {
    const listeners = {};
    const menu = {
        open: true,
        children: new Set(['inside']),
        contains(node) { return node === this || this.children.has(node); },
        removeAttribute(name) { if (name === 'open') this.open = false; },
    };
    return {
        menu,
        querySelectorAll(selector) {
            expect(selector).toBe('details[data-close-outside][open]');
            return menu.open ? [menu] : [];
        },
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        fire(type, event) { (listeners[type] || []).forEach((fn) => fn(event)); },
    };
}

describe('close-outside menus', () => {
    it('closes an open menu on a click outside it', () => {
        const doc = fakeDocument();
        closeOpenMenus(doc, 'outside');
        expect(doc.menu.open).toBe(false);
    });

    it('keeps the menu open for a click inside it', () => {
        const doc = fakeDocument();
        closeOpenMenus(doc, 'inside');
        expect(doc.menu.open).toBe(true);
    });

    it('closes on Escape and on an outside click once installed, and installs once', () => {
        const doc = fakeDocument();
        installCloseOutside(doc);
        installCloseOutside(doc);
        doc.fire('keydown', { key: 'Enter' });
        expect(doc.menu.open).toBe(true);
        doc.fire('keydown', { key: 'Escape' });
        expect(doc.menu.open).toBe(false);
        doc.menu.open = true;
        doc.fire('click', { target: 'outside' });
        expect(doc.menu.open).toBe(false);
    });
});
