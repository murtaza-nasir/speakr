import { describe, it, expect } from 'vitest';
import { globalBindings, shortcutSettings, HELP_GROUPS } from './shortcut-help.js';
import { neighborId, steppedSpeed } from '../composables/shortcuts.js';

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
        const admin = globalBindings({ openHelp() {}, toggleDarkMode() {}, win: win({ admin: true, inquire: true }), doc });
        expect(keys(admin)).toEqual(['?', 'Escape', 'Escape', 'g r', 'g a', 'g s', 'g d', 'D']);
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
        globalBindings({ openHelp() {}, toggleDarkMode() {}, win: win({ admin: true, inquire: true }), doc })
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
