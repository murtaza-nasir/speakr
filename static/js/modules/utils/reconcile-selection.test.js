import { describe, it, expect, vi } from 'vitest';
import { keepSelectionAfterReconcile } from './reconcile-selection.js';

const ok = () => Promise.resolve({ status: 200 });
const gone = () => Promise.resolve({ status: 404 });

describe('keepSelectionAfterReconcile (#702)', () => {
    it('keeps a recording that is still in the reloaded list', async () => {
        const fetchRecording = vi.fn(gone);
        expect(await keepSelectionAfterReconcile({ id: 3 }, [{ id: 3 }], fetchRecording)).toBe(true);
        expect(fetchRecording).not.toHaveBeenCalled();
    });

    it('keeps a recording that exists but is not on page 1', async () => {
        const fetchRecording = vi.fn(ok);
        expect(await keepSelectionAfterReconcile({ id: 7 }, [{ id: 1 }, { id: 2 }], fetchRecording)).toBe(true);
        expect(fetchRecording).toHaveBeenCalledWith(7);
    });

    it('drops a recording that was deleted', async () => {
        expect(await keepSelectionAfterReconcile({ id: 7 }, [{ id: 1 }], gone)).toBe(false);
    });

    it('drops a recording that is no longer shared', async () => {
        expect(await keepSelectionAfterReconcile({ id: 7 }, [], () => Promise.resolve({ status: 403 }))).toBe(false);
    });

    it('keeps the recording when the check fails on the network', async () => {
        expect(await keepSelectionAfterReconcile({ id: 7 }, [], () => Promise.reject(new Error('offline')))).toBe(true);
    });

    it('keeps an incognito recording without a request', async () => {
        const fetchRecording = vi.fn(gone);
        expect(await keepSelectionAfterReconcile({ id: 'incognito', incognito: true }, [], fetchRecording)).toBe(true);
        expect(fetchRecording).not.toHaveBeenCalled();
    });

    it('has nothing to do without an open recording', async () => {
        expect(await keepSelectionAfterReconcile(null, [], gone)).toBe(true);
    });
});
