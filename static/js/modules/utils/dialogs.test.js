import { afterEach, describe, expect, it, vi } from 'vitest';

import { alertDialog, confirmDialog, promptDialog } from './dialogs.js';

afterEach(() => { delete globalThis.window; });

describe('the dialogs for the modules', () => {
    it('ask the app\'s own dialogs', async () => {
        globalThis.window = {
            confirmDialog: vi.fn().mockResolvedValue(true),
            alertDialog: vi.fn().mockResolvedValue(undefined),
            promptDialog: vi.fn().mockResolvedValue('large-v3'),
            confirm: vi.fn(), alert: vi.fn(), prompt: vi.fn(),
        };
        const options = { message: 'Discard unsaved changes?', confirmLabel: 'Discard', danger: true };
        expect(await confirmDialog(options)).toBe(true);
        expect(window.confirmDialog).toHaveBeenCalledWith(options);
        await alertDialog('Saved');
        expect(window.alertDialog).toHaveBeenCalledWith('Saved');
        expect(await promptDialog({ message: 'Model id' })).toBe('large-v3');
        expect(window.confirm).not.toHaveBeenCalled();
        expect(window.alert).not.toHaveBeenCalled();
        expect(window.prompt).not.toHaveBeenCalled();
    });

    it('ask the browser when the app\'s dialogs are not on the page, so the question is never skipped', async () => {
        globalThis.window = { confirm: vi.fn().mockReturnValue(false), alert: vi.fn(), prompt: vi.fn().mockReturnValue('typed') };
        expect(await confirmDialog({ message: 'Delete?', danger: true })).toBe(false);
        expect(window.confirm).toHaveBeenCalledWith('Delete?');
        await alertDialog({ message: 'Failed' });
        expect(window.alert).toHaveBeenCalledWith('Failed');
        expect(await promptDialog({ message: 'Name?', value: 'a' })).toBe('typed');
        expect(window.prompt).toHaveBeenCalledWith('Name?', 'a');
    });

    it('answer no with neither', async () => {
        globalThis.window = {};
        expect(await confirmDialog('Delete?')).toBe(false);
        expect(await promptDialog('Name?')).toBeNull();
        await expect(alertDialog('x')).resolves.toBeUndefined();
    });
});
