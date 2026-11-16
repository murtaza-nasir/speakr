import { describe, it, expect, vi } from 'vitest';
import {
    SEQUENCE_MS, createShortcuts, isAltGraphCharacter, isControlTarget, isTypingTarget, keyCaps, keyToken,
} from './keyboard-shortcuts.js';

// A keydown event as the registry reads it. The tests run without a DOM.
const el = (tagName, extra = {}) => ({ tagName, getAttribute: (name) => (extra.attrs || {})[name] ?? null, ...extra });
const BODY = el('BODY');
function press(key, options = {}) {
    const event = {
        key, target: BODY, shiftKey: false, ctrlKey: false, metaKey: false, altKey: false, isComposing: false,
        repeat: false, defaultPrevented: false, ...options,
    };
    event.preventDefault = () => { event.defaultPrevented = true; };
    return event;
}

function setup(options = {}) {
    const calls = [];
    const state = { enabled: true, phone: false, scopes: ['global', 'list'], blocker: null, now: 1000, ...options };
    const registry = createShortcuts({
        enabled: () => state.enabled, phone: () => state.phone, scopes: () => state.scopes,
        blocker: () => state.blocker, now: () => state.now,
    });
    const run = (name) => () => calls.push(name);
    registry.add([
        { keys: ['?'], scope: 'global', group: 'general', label: 'help', run: run('help') },
        { keys: ['j'], scope: 'list', group: 'list', label: 'move', repeat: true, run: run('next') },
        { keys: ['k'], scope: 'list', group: 'list', label: 'move', repeat: true, run: run('previous') },
        { keys: ['s'], scope: 'list', group: 'list', label: 'star', run: run('star') },
        { keys: ['C'], scope: 'global', group: 'general', label: 'copy tab', run: run('copy tab') },
        { keys: ['#'], scope: 'list', group: 'list', label: 'delete', run: run('delete') },
        { keys: ['Enter', 'o'], scope: 'list', group: 'list', label: 'open', run: run('open') },
        { keys: ['g r'], scope: 'global', group: 'navigation', label: 'recordings', run: run('go recordings') },
        { keys: ['g s'], scope: 'global', group: 'navigation', label: 'settings', run: run('go settings') },
        { keys: ['p'], scope: 'recording', group: 'player', label: 'play', run: run('play') },
        { keys: ['Escape'], scope: 'modal', group: 'general', label: 'close', run: run('close dialog') },
        { keys: ['Ctrl+F'], scope: 'recording', group: 'recording', label: 'find' },
    ]);
    return { registry, calls, state };
}

describe('the name of a key', () => {
    it('follows Shift for a letter and ignores Caps Lock', () => {
        expect(keyToken({ key: 'd', shiftKey: false })).toBe('d');
        expect(keyToken({ key: 'D', shiftKey: true })).toBe('D');
        // Caps Lock on: the browser reports the other case
        expect(keyToken({ key: 'D', shiftKey: false })).toBe('d');
        expect(keyToken({ key: 'd', shiftKey: true })).toBe('D');
    });

    it('keeps a character as typed and names the other keys', () => {
        expect(keyToken({ key: '?', shiftKey: true })).toBe('?');
        expect(keyToken({ key: '/' })).toBe('/');
        expect(keyToken({ key: ' ' })).toBe('Space');
        expect(keyToken({ key: 'Esc' })).toBe('Escape');
        expect(keyToken({ key: 'Enter' })).toBe('Enter');
    });

    it('is null for a modifier pressed alone', () => {
        ['Shift', 'Control', 'Alt', 'Meta', 'CapsLock', 'Dead'].forEach((key) => expect(keyToken({ key })).toBeNull());
        expect(keyToken({})).toBeNull();
    });

    it('is shown as key caps in the help', () => {
        expect(keyCaps('j')).toEqual({ caps: ['j'], sequence: false });
        expect(keyCaps('D')).toEqual({ caps: ['Shift', 'D'], sequence: false });
        expect(keyCaps('g r')).toEqual({ caps: ['g', 'r'], sequence: true });
        expect(keyCaps('Escape').caps).toEqual(['Esc']);
        expect(keyCaps('Ctrl+F').caps).toEqual(['Ctrl', 'F']);
        expect(keyCaps('ArrowLeft').caps).toEqual(['←']);
        expect(keyCaps('?').caps).toEqual(['?']);
    });
});

describe('where the focus is', () => {
    it('knows a field that takes typing', () => {
        ['INPUT', 'TEXTAREA', 'SELECT', 'input'].forEach((tag) => expect(isTypingTarget(el(tag))).toBe(true));
        expect(isTypingTarget(el('DIV', { isContentEditable: true }))).toBe(true);
        expect(isTypingTarget(el('DIV'))).toBe(false);
        expect(isTypingTarget(null)).toBe(false);
    });

    it('knows a link, a button and a control with a role', () => {
        ['A', 'BUTTON', 'SUMMARY'].forEach((tag) => expect(isControlTarget(el(tag))).toBe(true));
        expect(isControlTarget(el('DIV', { attrs: { role: 'slider' } }))).toBe(true);
        expect(isControlTarget(el('DIV', { attrs: { role: 'region' } }))).toBe(false);
        expect(isControlTarget(el('DIV'))).toBe(false);
    });
});

describe('a single key', () => {
    it('runs the binding and keeps the key from the browser', () => {
        const { registry, calls } = setup();
        const event = press('j');
        expect(registry.handle(event).label).toBe('move');
        expect(calls).toEqual(['next']);
        expect(event.defaultPrevented).toBe(true);
    });

    it('leaves a key without a binding alone', () => {
        const { registry, calls } = setup();
        const event = press('z');
        expect(registry.handle(event)).toBeNull();
        expect(event.defaultPrevented).toBe(false);
        expect(calls).toEqual([]);
    });

    it('tells Shift from no Shift, whatever Caps Lock says', () => {
        const { registry, calls } = setup();
        registry.handle(press('C', { shiftKey: true }));
        registry.handle(press('C', { shiftKey: false })); // Caps Lock: a plain c, which has no binding
        registry.handle(press('S', { shiftKey: false })); // Caps Lock: a plain s
        expect(calls).toEqual(['copy tab', 'star']);
    });

    it('does nothing while Ctrl, Cmd or Alt is held', () => {
        const { registry, calls } = setup();
        ['ctrlKey', 'metaKey', 'altKey'].forEach((mod) => {
            const event = press('j', { [mod]: true });
            expect(registry.handle(event)).toBeNull();
            expect(event.defaultPrevented).toBe(false);
        });
        expect(calls).toEqual([]);
    });

    it('accepts a character that the layout types with AltGr, and no letter that way', () => {
        // Windows reports AltGr as Ctrl+Alt
        const altGr = { ctrlKey: true, altKey: true, getModifierState: (name) => name === 'AltGraph' };
        const { registry, calls } = setup();
        const hash = press('#', altGr);
        expect(registry.handle(hash).label).toBe('delete');
        expect(hash.defaultPrevented).toBe(true);
        // AltGr alone, as other systems report it
        registry.handle(press('#', { getModifierState: (name) => name === 'AltGraph' }));
        expect(calls).toEqual(['delete', 'delete']);
        // A letter with AltGr, Ctrl+Alt without AltGr, and Cmd are still not keys of the app
        [press('s', altGr), press('j', altGr), press('#', { ctrlKey: true, altKey: true }),
            press('#', { ctrlKey: true, altKey: true, getModifierState: () => false }),
            press('#', { ...altGr, metaKey: true }), press('#', { ctrlKey: true })].forEach((event) => {
            expect(registry.handle(event)).toBeNull();
            expect(event.defaultPrevented).toBe(false);
        });
        expect(calls).toEqual(['delete', 'delete']);

        expect(isAltGraphCharacter(press('#', altGr))).toBe(true);
        expect(isAltGraphCharacter(press('@', altGr))).toBe(true);
        expect(isAltGraphCharacter(press('\u0105', altGr))).toBe(false); // a letter typed with AltGr
        expect(isAltGraphCharacter(press(' ', altGr))).toBe(false);
        expect(isAltGraphCharacter(press('Enter', altGr))).toBe(false);
        expect(isAltGraphCharacter(press('#'))).toBe(false);
        expect(isAltGraphCharacter(null)).toBe(false);
    });

    it('keeps the key from the browser only when it is handled', () => {
        const registry = createShortcuts({ scopes: () => ['recording'] });
        const calls = [];
        let free = true;
        registry.add([{ keys: ['Space'], scope: 'recording', group: 'player', label: 'play', when: () => free, run: () => calls.push('play') }]);
        const handled = press(' ');
        registry.handle(handled);
        expect(handled.defaultPrevented).toBe(true);
        free = false;
        const left = press(' ');
        expect(registry.handle(left)).toBeNull();
        expect(left.defaultPrevented).toBe(false);
        // On a focused button Space is the button's, whatever the binding says
        free = true;
        const onButton = press(' ', { target: el('BUTTON') });
        expect(registry.handle(onButton)).toBeNull();
        expect(onButton.defaultPrevented).toBe(false);
        // In a field it types a space
        const typing = press(' ', { target: el('TEXTAREA') });
        expect(registry.handle(typing)).toBeNull();
        expect(typing.defaultPrevented).toBe(false);
        expect(calls).toEqual(['play']);
    });

    it('does nothing while an input method composes text', () => {
        const { registry, calls } = setup();
        expect(registry.handle(press('j', { isComposing: true }))).toBeNull();
        expect(registry.handle(press('Process', { keyCode: 229 }))).toBeNull();
        expect(registry.handle(press('j', { keyCode: 229 }))).toBeNull();
        expect(calls).toEqual([]);
    });

    it('does nothing for a key another handler already used', () => {
        const { registry, calls } = setup();
        expect(registry.handle(press('j', { defaultPrevented: true }))).toBeNull();
        expect(calls).toEqual([]);
    });

    it('repeats only where the binding says so', () => {
        const { registry, calls } = setup();
        registry.handle(press('j', { repeat: true }));
        registry.handle(press('s', { repeat: true }));
        expect(calls).toEqual(['next']);
    });

    it('runs the first binding of a key whose condition holds', () => {
        const registry = createShortcuts({ scopes: () => ['global'] });
        const calls = [];
        let dialog = false;
        let panel = true;
        registry.add([
            { keys: ['Escape'], scope: 'global', group: 'general', label: 'close', when: () => dialog, run: () => calls.push('dialog') },
            { keys: ['Escape'], scope: 'global', group: 'general', label: 'close', when: () => panel, run: () => { calls.push('panel'); panel = false; } },
        ]);
        dialog = true;
        registry.handle(press('Escape'));
        dialog = false;
        registry.handle(press('Escape'));
        const third = press('Escape');
        expect(registry.handle(third)).toBeNull();
        expect(third.defaultPrevented).toBe(false);
        expect(calls).toEqual(['dialog', 'panel']);
    });

    it('gives the event to the condition', () => {
        const registry = createShortcuts({ scopes: () => ['global'] });
        const when = vi.fn(() => true);
        registry.add([{ keys: ['x'], scope: 'global', group: 'general', label: 'x', when, run: () => {} }]);
        const event = press('x');
        registry.handle(event);
        expect(when).toHaveBeenCalledWith(event);
    });
});

describe('typing', () => {
    it('never runs a letter in an input, a textarea, a select or an editable element', () => {
        const { registry, calls } = setup();
        [el('INPUT'), el('TEXTAREA'), el('SELECT'), el('DIV', { isContentEditable: true })].forEach((target) => {
            ['j', 's', '?', 'g'].forEach((key) => {
                const event = press(key, { target });
                expect(registry.handle(event)).toBeNull();
                expect(event.defaultPrevented).toBe(false);
            });
        });
        expect(calls).toEqual([]);
    });

    it('does not start or finish a sequence in a field', () => {
        const { registry, calls } = setup();
        registry.handle(press('g', { target: el('INPUT') }));
        registry.handle(press('r'));
        registry.handle(press('g'));
        const second = press('r', { target: el('TEXTAREA') });
        registry.handle(second);
        expect(second.defaultPrevented).toBe(false);
        expect(calls).toEqual([]);
    });

    it('runs a binding marked for fields there too', () => {
        const registry = createShortcuts({ scopes: () => ['global'] });
        const calls = [];
        registry.add([
            { keys: ['Escape'], scope: 'global', group: 'general', label: 'close', inInput: true, run: () => calls.push('panel') },
            { keys: ['x'], scope: 'global', group: 'general', label: 'x', run: () => calls.push('x') },
        ]);
        registry.handle(press('Escape', { target: el('TEXTAREA') }));
        registry.handle(press('x', { target: el('TEXTAREA') }));
        expect(calls).toEqual(['panel']);
    });
});

describe('a focused link or button', () => {
    it('keeps Enter, Space, Tab, the arrows, Delete and Backspace', () => {
        const { registry, calls } = setup();
        registry.add([
            { keys: ['Space'], scope: 'global', group: 'general', label: 'space', run: () => calls.push('space') },
            { keys: ['ArrowDown'], scope: 'global', group: 'general', label: 'down', run: () => calls.push('down') },
            { keys: ['Delete', 'Backspace', 'Tab'], scope: 'global', group: 'general', label: 'keys', run: () => calls.push('key') },
        ]);
        [el('BUTTON'), el('A'), el('SUMMARY'), el('DIV', { attrs: { role: 'slider' } })].forEach((target) => {
            ['Enter', ' ', 'Tab', 'ArrowDown', 'Delete', 'Backspace'].forEach((key) => {
                const event = press(key, { target });
                expect(registry.handle(event)).toBeNull();
                expect(event.defaultPrevented).toBe(false);
            });
        });
        expect(calls).toEqual([]);
        // The same keys are shortcuts when no control has the focus
        registry.handle(press('Enter'));
        registry.handle(press(' '));
        expect(calls).toEqual(['open', 'space']);
    });

    it('leaves letters as shortcuts', () => {
        const { registry, calls } = setup();
        registry.handle(press('j', { target: el('BUTTON') }));
        registry.handle(press('o', { target: el('A') }));
        registry.handle(press('?', { target: el('BUTTON'), shiftKey: true }));
        expect(calls).toEqual(['next', 'open', 'help']);
    });
});

describe('a sequence', () => {
    it('runs on g, then the second key', () => {
        const { registry, calls } = setup();
        const first = press('g');
        expect(registry.handle(first)).toBe('pending');
        expect(first.defaultPrevented).toBe(true);
        expect(registry.pending()).toBe('g');
        const second = press('s');
        expect(registry.handle(second).label).toBe('settings');
        expect(second.defaultPrevented).toBe(true);
        expect(registry.pending()).toBeNull();
        // The key alone still has its own meaning
        registry.handle(press('s'));
        expect(calls).toEqual(['go settings', 'star']);
    });

    it('waits through Shift pressed alone', () => {
        const { registry, calls } = setup();
        registry.handle(press('g'));
        registry.handle(press('Shift', { shiftKey: true }));
        registry.handle(press('r'));
        expect(calls).toEqual(['go recordings']);
    });

    it('ends after the wait', () => {
        const { registry, calls, state } = setup();
        registry.handle(press('g'));
        state.now += SEQUENCE_MS + 1;
        registry.handle(press('s'));
        expect(calls).toEqual(['star']);
    });

    it('uses up a second key that means nothing', () => {
        const { registry, calls } = setup();
        registry.handle(press('g'));
        const second = press('j');
        expect(registry.handle(second)).toBeNull();
        expect(second.defaultPrevented).toBe(true);
        registry.handle(press('g'));
        registry.handle(press('Escape'));
        registry.handle(press('r'));
        expect(calls).toEqual([]);
    });

    it('is dropped by a modifier and by the window losing the focus', () => {
        const { registry, calls } = setup();
        registry.handle(press('g'));
        registry.handle(press('r', { ctrlKey: true }));
        registry.handle(press('r'));
        expect(calls).toEqual([]);

        const listeners = {};
        const view = { addEventListener: (name, fn) => { listeners[name] = fn; }, removeEventListener: () => {} };
        const doc = { defaultView: view, addEventListener: (name, fn) => { listeners[`doc:${name}`] = fn; }, removeEventListener: () => {} };
        registry.install(doc);
        listeners['doc:keydown'](press('g'));
        expect(registry.pending()).toBe('g');
        listeners.blur();
        expect(registry.pending()).toBeNull();
    });

    it('does not start where no sequence applies', () => {
        const { registry, state } = setup();
        state.scopes = ['list'];
        const event = press('g');
        expect(registry.handle(event)).toBeNull();
        expect(event.defaultPrevented).toBe(false);
    });
});

describe('scopes', () => {
    it('runs a key only where it applies', () => {
        const { registry, calls, state } = setup();
        registry.handle(press('p'));
        state.scopes = ['global', 'recording'];
        registry.handle(press('p'));
        registry.handle(press('j'));
        expect(calls).toEqual(['play']);
    });

    it('accepts a binding with several scopes', () => {
        const registry = createShortcuts({ scopes: () => ['recording'] });
        const calls = [];
        registry.add([{ keys: ['s'], scope: ['list', 'recording'], group: 'list', label: 'star', run: () => calls.push('star') }]);
        registry.handle(press('s'));
        expect(calls).toEqual(['star']);
    });

    it('gives a dialog over the page the keys to itself', () => {
        const { registry, calls, state } = setup();
        state.blocker = 'modal';
        ['j', '?', 's'].forEach((key) => registry.handle(press(key)));
        expect(registry.handle(press('g'))).toBeNull();
        registry.handle(press('Escape'));
        expect(calls).toEqual(['close dialog']);
        // A dialog with keys of its own (the segment editor) gets none from here
        state.blocker = 'editor';
        const event = press('Escape');
        expect(registry.handle(event)).toBeNull();
        expect(event.defaultPrevented).toBe(false);
        expect(calls).toEqual(['close dialog']);
    });

    it('never runs a reference row', () => {
        const { registry, calls, state } = setup();
        state.scopes = ['global', 'recording'];
        expect(registry.handle(press('f', { ctrlKey: true }))).toBeNull();
        expect(calls).toEqual([]);
    });
});

describe('the setting and the phone layout', () => {
    it('handles nothing while shortcuts are turned off', () => {
        const { registry, calls, state } = setup();
        state.enabled = false;
        ['j', '?', 'g', 's'].forEach((key) => {
            const event = press(key);
            expect(registry.handle(event)).toBeNull();
            expect(event.defaultPrevented).toBe(false);
        });
        state.enabled = true;
        registry.handle(press('j'));
        expect(calls).toEqual(['next']);
    });

    it('handles nothing in the phone layout', () => {
        const { registry, calls, state } = setup();
        state.phone = true;
        ['j', '?', 'g', 'Enter'].forEach((key) => {
            const event = press(key);
            expect(registry.handle(event)).toBeNull();
            expect(event.defaultPrevented).toBe(false);
        });
        expect(calls).toEqual([]);
    });

    it('forgets a started sequence when turned off', () => {
        const { registry, calls, state } = setup();
        registry.handle(press('g'));
        state.enabled = false;
        registry.handle(press('r'));
        state.enabled = true;
        registry.handle(press('r'));
        expect(calls).toEqual([]);
    });
});

describe('the help', () => {
    it('is generated from the table, grouped, with one row per action', () => {
        const { registry } = setup();
        const help = registry.help(['general', 'navigation', 'list', 'recording', 'player']);
        expect(help.map((entry) => entry.group)).toEqual(['general', 'navigation', 'list', 'recording', 'player']);
        const list = help.find((entry) => entry.group === 'list');
        expect(list.rows.map((row) => row.label)).toEqual(['move', 'star', 'delete', 'open']);
        // j and k share a row, Enter and o are two keys of one binding
        expect(list.rows[0].keys.map((key) => key.caps.join('+'))).toEqual(['j', 'k']);
        expect(list.rows[3].keys.map((key) => key.caps.join('+'))).toEqual(['Enter', 'o']);
        expect(help.find((entry) => entry.group === 'navigation').rows[0].keys[0]).toEqual({ caps: ['g', 'r'], sequence: true });
        // A reference row is listed
        expect(help.find((entry) => entry.group === 'recording').rows[0].keys[0].caps).toEqual(['Ctrl', 'F']);
    });

    it('lists every key that can run, and no other', () => {
        const { registry } = setup();
        const listed = new Set(registry.help().flatMap((entry) => entry.rows.flatMap((row) => row.keys.map((key) => key.caps.join(' ')))));
        registry.bindings().forEach((binding) => binding.keys.forEach((key) => {
            expect(listed.has(keyCaps(key).caps.join(' '))).toBe(true);
        }));
        const count = registry.help().reduce((sum, entry) => sum + entry.rows.reduce((n, row) => n + row.keys.length, 0), 0);
        expect(count).toBe(registry.bindings().reduce((sum, binding) => sum + binding.keys.length, 0));
    });

    it('leaves out a binding marked so, an empty group and a group not asked for', () => {
        const registry = createShortcuts();
        registry.add([
            { keys: ['Escape'], scope: 'global', group: 'general', label: 'close', run: () => {} },
            { keys: ['Escape'], scope: 'global', group: 'general', label: 'close', help: false, run: () => {} },
            { keys: ['x'], scope: 'global', group: 'hidden', label: 'x', help: false, run: () => {} },
            { keys: ['y'], scope: 'global', group: 'other', label: 'y', params: { name: 'Ambrose' }, run: () => {} },
        ]);
        expect(registry.help()).toEqual([
            { group: 'general', rows: [{ label: 'close', params: undefined, keys: [{ caps: ['Esc'], sequence: false }] }] },
            { group: 'other', rows: [{ label: 'y', params: { name: 'Ambrose' }, keys: [{ caps: ['y'], sequence: false }] }] },
        ]);
        expect(registry.help(['general']).map((entry) => entry.group)).toEqual(['general']);
    });

    it('ignores a binding without keys', () => {
        const registry = createShortcuts();
        registry.add([{ scope: 'global', label: 'x' }, null, { keys: [], scope: 'global' }]);
        expect(registry.bindings()).toEqual([]);
    });
});

describe('the listener', () => {
    it('is one keydown listener on the document, installed once', () => {
        const { registry, calls } = setup();
        const added = [];
        const doc = { addEventListener: (name, fn) => added.push([name, fn]), removeEventListener: vi.fn() };
        registry.install(doc);
        registry.install(doc);
        expect(added.map(([name]) => name)).toEqual(['keydown']);
        added[0][1](press('j'));
        expect(calls).toEqual(['next']);
        registry.uninstall();
        expect(doc.removeEventListener).toHaveBeenCalledWith('keydown', added[0][1]);
    });
});
