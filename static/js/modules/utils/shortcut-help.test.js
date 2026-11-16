import { describe, it, expect } from 'vitest';
import { fieldValue, globalBindings, hasUnsavedInput, noteFields, shortcutSettings, HELP_GROUPS } from './shortcut-help.js';
import { neighborId, spaceIsFree, steppedSpeed } from '../composables/shortcuts.js';

const doc = { querySelector: () => null, querySelectorAll: () => [] };
const win = (speakrShortcuts) => ({ speakrShortcuts, location: { href: '' } });

describe('what the page knows about the account', () => {
    it('is on unless the account turned shortcuts off', () => {
        expect(shortcutSettings({})).toEqual({ enabled: true, admin: false, inquire: false });
        expect(shortcutSettings(win({ enabled: false, admin: true, inquire: true }))).toEqual({ enabled: false, admin: true, inquire: true });
        expect(shortcutSettings(win({ enabled: true })).enabled).toBe(true);
    });
});

describe('the keys of every page', () => {
    const keys = (list) => list.flatMap((binding) => binding.keys);

    it('offer only the pages the account can open', () => {
        const plain = globalBindings({ openHelp() {}, win: win({}), doc });
        expect(keys(plain)).toEqual(['?', 'Escape', 'Escape', 'g r', 'g s']);
        const admin = globalBindings({ openHelp() {}, win: win({ admin: true, inquire: true }), doc });
        expect(keys(admin)).toEqual(['?', 'Escape', 'Escape', 'g r', 'g a', 'g s', 'g d']);
    });

    it('open the page of a navigation key', () => {
        const page = win({ admin: true, inquire: true });
        const list = globalBindings({ openHelp() {}, win: page, doc });
        const run = (key) => list.find((binding) => binding.keys.includes(key)).run();
        run('g a');
        expect(page.location.href).toBe('/inquire');
        run('g s');
        expect(page.location.href).toBe('/account');
        run('g d');
        expect(page.location.href).toBe('/admin');
        run('g r');
        expect(page.location.href).toBe('/');
    });

    it('go to the recordings the way the page says', () => {
        let called = 0;
        const list = globalBindings({ openHelp() {}, home: () => { called += 1; }, win: win({}), doc });
        list.find((binding) => binding.keys.includes('g r')).run();
        expect(called).toBe(1);
    });

    it('belong to a group of the help', () => {
        globalBindings({ openHelp() {}, win: win({ admin: true, inquire: true }), doc })
            .forEach((binding) => expect(HELP_GROUPS).toContain(binding.group));
    });

    it('close a dialog only where it has a Close button', () => {
        const list = globalBindings({ openHelp() {}, win: win({}), doc });
        const escapes = list.filter((binding) => binding.keys.includes('Escape'));
        expect(escapes.map((binding) => binding.when())).toEqual([false, false]);
    });
});

describe('moving in the recording list', () => {
    const ids = [7, 5, 3];

    it('starts at the first row and stops at the ends', () => {
        expect(neighborId(ids, null, 1)).toBe(7);
        expect(neighborId(ids, null, -1)).toBe(7);
        expect(neighborId(ids, 7, 1)).toBe(5);
        expect(neighborId(ids, 3, 1)).toBe(3);
        expect(neighborId(ids, 7, -1)).toBe(7);
        expect(neighborId(ids, 99, 1)).toBe(7);
        expect(neighborId([], 7, 1)).toBeNull();
    });
});

describe('playback speed', () => {
    const speeds = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0];

    it('steps through the speeds of the speed menu and stops at the ends', () => {
        expect(steppedSpeed(speeds, 1.0, 1)).toBe(1.25);
        expect(steppedSpeed(speeds, 1.0, -1)).toBe(0.75);
        expect(steppedSpeed(speeds, 2.0, 1)).toBe(2.0);
        expect(steppedSpeed(speeds, 0.5, -1)).toBe(0.5);
        // A saved speed that is not in the menu: from the nearest one
        expect(steppedSpeed(speeds, 1.1, 1)).toBe(1.25);
    });
});

describe('Space for play and pause', () => {
    const body = { tagName: 'BODY' };
    const page = (activeElement) => ({ body, activeElement, documentElement: { tagName: 'HTML' } });

    it('is free while the page itself, or nothing, has the focus', () => {
        expect(spaceIsFree(page(body), { target: body })).toBe(true);
        expect(spaceIsFree(page(null), { target: body })).toBe(true);
        expect(spaceIsFree(page(body))).toBe(true);
    });

    it('is not free on a control, in a field or in a list that it would scroll', () => {
        const button = { tagName: 'BUTTON' };
        const list = { tagName: 'DIV', tabIndex: 0 };
        expect(spaceIsFree(page(button), { target: button })).toBe(false);
        expect(spaceIsFree(page({ tagName: 'TEXTAREA' }), { target: body })).toBe(false);
        expect(spaceIsFree(page(list), { target: list })).toBe(false);
        // The event comes from somewhere else than the page
        expect(spaceIsFree(page(body), { target: list })).toBe(false);
    });
});

describe('unsaved input in a dialog', () => {
    const field = (tagName, props = {}) => ({ tagName, isConnected: true, ...props });
    function dialog(fields, { vue = true, dirty = null, dirtyInside = null } = {}) {
        const flagged = dirtyInside == null ? null : { getAttribute: () => dirtyInside };
        return {
            fields,
            closest: (selector) => (selector === '[data-v-app]' && vue ? {} : null),
            matches: (selector) => selector === '[data-shortcut-dirty]' && dirty != null,
            getAttribute: (name) => (name === 'data-shortcut-dirty' ? dirty : null),
            querySelector: (selector) => (selector === '[data-shortcut-dirty]' ? flagged : null),
            querySelectorAll() { return this.fields; },
        };
    }

    it('reads what a field holds', () => {
        expect(fieldValue(field('INPUT', { type: 'text', value: 'Ana' }))).toBe('Ana');
        expect(fieldValue(field('INPUT', { type: 'checkbox', checked: true, value: 'on' }))).toBe('on');
        expect(fieldValue(field('INPUT', { type: 'checkbox', checked: false, value: 'on' }))).toBe('off');
        expect(fieldValue(field('INPUT', { type: 'hidden', value: 'token' }))).toBe('');
        expect(fieldValue(field('TEXTAREA', { value: 'notes' }))).toBe('notes');
        expect(fieldValue(field('SELECT', { value: 'fr' }))).toBe('fr');
        expect(fieldValue(field('SELECT', { multiple: true, options: [{ selected: true, value: 'a' }, { selected: false, value: 'b' }, { selected: true, value: 'c' }] }))).toBe('a\nc');
        expect(fieldValue(field('DIV', { textContent: 'edited' }))).toBe('edited');
    });

    it('has none while every field holds what it held when it was noted', () => {
        const name = field('INPUT', { type: 'text', value: 'Ana' });
        const keep = field('INPUT', { type: 'checkbox', checked: false });
        const box = dialog([name, keep]);
        expect(hasUnsavedInput(box)).toBe(false); // nothing noted yet: nothing was touched
        noteFields(box);
        expect(hasUnsavedInput(box)).toBe(false);
        name.value = 'Ana B';
        expect(hasUnsavedInput(box)).toBe(true);
        name.value = 'Ana';
        expect(hasUnsavedInput(box)).toBe(false);
        keep.checked = true;
        expect(hasUnsavedInput(box)).toBe(true);
    });

    it('keeps the first value of a field that is noted again, and notes a field that came later', () => {
        const name = field('INPUT', { type: 'text', value: '' });
        const box = dialog([name]);
        noteFields(box);
        name.value = 'typed';
        noteFields(box);
        expect(hasUnsavedInput(box)).toBe(true);
        name.value = '';
        const later = field('TEXTAREA', { value: 'loaded' });
        box.fields = [name, later];
        noteFields(box);
        expect(hasUnsavedInput(box)).toBe(false);
        later.value = 'loaded and edited';
        expect(hasUnsavedInput(box)).toBe(true);
    });

    it('ignores a field that left the dialog', () => {
        const row = field('INPUT', { type: 'text', value: 'x' });
        const box = dialog([row]);
        noteFields(box);
        row.value = 'y';
        row.isConnected = false;
        expect(hasUnsavedInput(box)).toBe(false);
    });

    it('takes the word of a dialog that says so itself', () => {
        expect(hasUnsavedInput(dialog([], { dirty: 'true' }))).toBe(true);
        expect(hasUnsavedInput(dialog([], { dirty: '' }))).toBe(true);
        expect(hasUnsavedInput(dialog([], { dirty: 'false' }))).toBe(false);
        expect(hasUnsavedInput(dialog([], { dirtyInside: 'true' }))).toBe(true);
        expect(hasUnsavedInput(dialog([]))).toBe(false);
    });

    it('never judges a dialog that stays in the page between openings', () => {
        const box = dialog([field('INPUT', { type: 'text', value: '' })], { vue: false });
        noteFields(box);
        expect(hasUnsavedInput(box)).toBe(true);
        expect(hasUnsavedInput(null)).toBe(true);
    });
});
