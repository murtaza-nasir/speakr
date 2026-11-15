import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// app-dialog.js is a classic script that sets window.confirmDialog,
// alertDialog, promptDialog and window.SpeakrDialog (the logic tested here).
// The dialog itself is covered in the browser (tests/e2e/test_app_dialog_ui.py).
const SOURCE = readFileSync(fileURLToPath(new URL('./app-dialog.js', import.meta.url)), 'utf8');

function load(i18n) {
    const window = i18n ? { i18n } : {};
    vm.runInContext(SOURCE, vm.createContext({ window, Promise, Date }));
    return window;
}

const t = (key, fallback) => `<${fallback}>`;

describe('what a dialog shows', () => {
    const { dialogSpec } = load().SpeakrDialog;

    it('takes the message alone, with OK and Cancel', () => {
        expect(dialogSpec('confirm', 'Rename the files?')).toEqual({
            kind: 'confirm', title: '', message: 'Rename the files?', confirmLabel: 'OK', cancelLabel: 'Cancel',
            danger: false, value: '', placeholder: '',
        });
    });

    it('names a destructive action on a danger button', () => {
        const spec = dialogSpec('confirm', { title: 'Unsaved changes', message: 'Discard unsaved changes?', confirmLabel: 'Discard', danger: true });
        expect(spec).toMatchObject({ title: 'Unsaved changes', confirmLabel: 'Discard', cancelLabel: 'Cancel', danger: true });
    });

    it('translates the default labels', () => {
        expect(dialogSpec('confirm', { message: 'x' }, t)).toMatchObject({ confirmLabel: '<OK>', cancelLabel: '<Cancel>' });
        expect(dialogSpec('confirm', { message: 'x', confirmLabel: 'Delete', cancelLabel: 'Keep' }, t)).toMatchObject({ confirmLabel: 'Delete', cancelLabel: 'Keep' });
        const translated = load({ t: (key) => ({ 'common.ok': 'In Ordnung', 'common.cancel': 'Abbrechen' }[key] || key) }).SpeakrDialog;
        expect(translated.dialogSpec('confirm', 'x')).toMatchObject({ confirmLabel: 'In Ordnung', cancelLabel: 'Abbrechen' });
    });

    it('gives a message one button and never the danger style', () => {
        expect(dialogSpec('alert', { message: 'Save failed', danger: true })).toMatchObject({ kind: 'alert', confirmLabel: 'OK', cancelLabel: '', danger: false });
    });

    it('gives an input dialog its starting text', () => {
        expect(dialogSpec('prompt', { message: 'Model id', value: 'whisper-1', placeholder: 'large-v3' }))
            .toMatchObject({ kind: 'prompt', value: 'whisper-1', placeholder: 'large-v3', cancelLabel: 'Cancel' });
        expect(dialogSpec('confirm', { message: 'x', value: 'ignored' }).value).toBe('');
    });

    it('shows nothing odd for a missing message or an unknown kind', () => {
        expect(dialogSpec('confirm', undefined).message).toBe('');
        expect(dialogSpec('whatever', { message: 5 })).toMatchObject({ kind: 'confirm', message: '5' });
    });
});

describe('keys in an open dialog', () => {
    const { keyIntent, trapIndex } = load().SpeakrDialog;

    it('cancels on Esc wherever the focus is', () => {
        for (const on of ['cancel', 'confirm', 'input', 'other']) expect(keyIntent('Escape', on)).toBe('cancel');
    });

    it('confirms on Enter, and leaves Enter on a button to that button', () => {
        expect(keyIntent('Enter', 'input')).toBe('confirm');
        expect(keyIntent('Enter', 'other')).toBe('confirm');
        expect(keyIntent('Enter', 'confirm')).toBeNull();     // the browser presses the focused button: confirm
        expect(keyIntent('Enter', 'cancel')).toBeNull();      // and here: cancel
    });

    it('does not take Enter or Esc from text being composed', () => {
        expect(keyIntent('Enter', 'input', { composing: true })).toBeNull();
        expect(keyIntent('Escape', 'input', { composing: true })).toBeNull();
    });

    it('blocks undo, redo and save for the page under it, and leaves them to its own input', () => {
        for (const key of ['z', 'Z', 'y', 's']) expect(keyIntent(key, 'confirm', { mod: true })).toBe('block');
        expect(keyIntent('z', 'input', { mod: true })).toBeNull();
        expect(keyIntent('c', 'confirm', { mod: true })).toBeNull();
        expect(keyIntent('Enter', 'other', { mod: true })).toBeNull();
    });

    it('leaves other keys alone', () => {
        expect(keyIntent('a', 'input')).toBeNull();
        expect(keyIntent(' ', 'confirm')).toBeNull();
    });

    it('keeps Tab inside the dialog', () => {
        expect(keyIntent('Tab', 'confirm')).toBe('tab');
        expect(trapIndex(2, 0, false)).toBe(1);
        expect(trapIndex(2, 1, false)).toBe(0);
        expect(trapIndex(2, 0, true)).toBe(1);
        expect(trapIndex(3, -1, false)).toBe(0);     // the focus was outside
        expect(trapIndex(3, -1, true)).toBe(2);
        expect(trapIndex(1, 0, false)).toBe(0);
        expect(trapIndex(0, -1, false)).toBe(-1);
    });
});

describe('what a dialog resolves to', () => {
    const { result } = load().SpeakrDialog;

    it('is true or false for a confirmation', () => {
        expect(result('confirm', true)).toBe(true);
        expect(result('confirm', false)).toBe(false);
    });

    it('is the text, or null when cancelled, for an input', () => {
        expect(result('prompt', true, 'large-v3')).toBe('large-v3');
        expect(result('prompt', true, '')).toBe('');
        expect(result('prompt', false, 'typed')).toBeNull();
    });

    it('is nothing for a message', () => {
        expect(result('alert', true)).toBeUndefined();
        expect(result('alert', false)).toBeUndefined();
    });
});

describe('without a page', () => {
    it('answers no and does not throw', async () => {
        const window = load();
        expect(await window.confirmDialog('Delete?')).toBe(false);
        expect(await window.promptDialog('Name?')).toBeNull();
        expect(await window.alertDialog('Done')).toBeUndefined();
        expect(window.SpeakrDialog.isOpen()).toBe(false);
    });
});
