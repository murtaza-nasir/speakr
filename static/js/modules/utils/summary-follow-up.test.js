import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createSummaryFollowUp } from './summary-follow-up.js';

const ref = (value) => ({ value });

function setup(statuses, { selectedId = 7 } = {}) {
    const recording = { id: 7, status: 'COMPLETED', summary: 'Old', title: 'Meeting' };
    const other = { id: 8, status: 'COMPLETED' };
    const recordings = ref([other, recording]);
    const selectedRecording = ref(selectedId === 7 ? recording : other);
    const toasts = [];
    const requests = [];
    const done = vi.fn();
    const queue = [...statuses];
    const fetchImpl = async (url) => {
        requests.push(url);
        if (url === '/recording/7/status') {
            const status = queue.length > 1 ? queue.shift() : queue[0];
            return status === 'HTTP 500' ? { ok: false } : { ok: true, json: async () => ({ status }) };
        }
        return { ok: true, json: async () => ({ id: 7, status: 'COMPLETED', summary: 'New', title: 'Meeting' }) };
    };
    const followUp = createSummaryFollowUp({
        recordings, selectedRecording, showToast: (message) => toasts.push(message), t: (key) => key, onDone: done, fetchImpl,
    });
    return { followUp, recordings, selectedRecording, toasts, requests, done };
}

describe('after a save that queued a summary regeneration', () => {
    beforeEach(() => { vi.useFakeTimers(); });
    afterEach(() => { vi.useRealTimers(); });

    it('shows the saved recording as summarizing, in the list and as the open recording', () => {
        const { followUp, recordings, selectedRecording, toasts } = setup(['SUMMARIZING']);
        followUp.started(7, { id: 7, status: 'COMPLETED', summary: 'Old', title: 'Renamed' });
        expect(selectedRecording.value).toMatchObject({ status: 'SUMMARIZING', title: 'Renamed' });
        expect(recordings.value[1]).toBe(selectedRecording.value);
        expect(toasts).toEqual(['help.summaryRegenerationStarted']);
    });

    it('changes only the status when no saved recording is given', () => {
        const { followUp, selectedRecording } = setup(['SUMMARIZING']);
        followUp.started(7);
        expect(selectedRecording.value).toEqual({ id: 7, status: 'SUMMARIZING', summary: 'Old', title: 'Meeting' });
    });

    it('leaves another open recording alone', () => {
        const { followUp, recordings, selectedRecording } = setup(['SUMMARIZING'], { selectedId: 8 });
        followUp.started(7);
        expect(selectedRecording.value.id).toBe(8);
        expect(recordings.value[1].status).toBe('SUMMARIZING');
    });

    it('polls until the summary is there, then loads the recording and stops', async () => {
        const { followUp, selectedRecording, toasts, requests, done } = setup(['SUMMARIZING', 'SUMMARIZING', 'COMPLETED']);
        followUp.started(7);
        await vi.advanceTimersByTimeAsync(6100);
        expect(selectedRecording.value).toMatchObject({ status: 'SUMMARIZING', summary: 'Old' });
        await vi.advanceTimersByTimeAsync(3000);
        expect(selectedRecording.value).toMatchObject({ status: 'COMPLETED', summary: 'New' });
        expect(toasts).toEqual(['help.summaryRegenerationStarted', 'help.summaryUpdated']);
        expect(done).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(30000);
        expect(requests.filter((url) => url.endsWith('/status')).length).toBe(3);
    });

    it('says so when the summary failed', async () => {
        const { followUp, toasts, done } = setup(['FAILED']);
        followUp.started(7);
        await vi.advanceTimersByTimeAsync(3100);
        expect(toasts).toContain('help.summaryGenerationFailed');
        expect(done).not.toHaveBeenCalled();
    });

    it('gives up after two minutes, and stops at once when the status cannot be read', async () => {
        const slow = setup(['SUMMARIZING']);
        slow.followUp.started(7);
        await vi.advanceTimersByTimeAsync(3000 * 40 + 100);
        expect(slow.toasts).toContain('help.summaryGenerationTimedOut');
        const count = slow.requests.length;
        await vi.advanceTimersByTimeAsync(9000);
        expect(slow.requests.length).toBe(count);

        const broken = setup(['HTTP 500']);
        broken.followUp.started(7);
        await vi.advanceTimersByTimeAsync(10000);
        expect(broken.requests.length).toBe(1);
    });
});
