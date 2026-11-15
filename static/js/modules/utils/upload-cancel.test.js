import { describe, it, expect } from 'vitest';
import { canCancelUpload, uploadCancelPlan } from './upload-cancel.js';

describe('uploadCancelPlan', () => {
    it('removes a file that has not been sent', () => {
        expect(uploadCancelPlan({ status: 'queued' })).toEqual({ action: 'remove', reason: null });
        expect(uploadCancelPlan({ status: 'ready' })).toEqual({ action: 'remove', reason: null });
    });
    it('aborts an upload in flight, also while the server is processing it', () => {
        expect(uploadCancelPlan({ status: 'uploading', progress: 40 }).action).toBe('abort');
        expect(uploadCancelPlan({ status: 'uploading', serverProcessing: true }).action).toBe('abort');
        expect(uploadCancelPlan({ status: 'uploading', join: { group: 'g1' } }).action).toBe('abort');
    });
    it('keeps the upload of a recording whose only copy is in the browser', () => {
        expect(uploadCancelPlan({ status: 'uploading', fromInProgressRecording: true }))
            .toEqual({ action: 'none', reason: 'only_copy' });
    });
    it('points a finished upload to its job', () => {
        expect(uploadCancelPlan({ status: 'pending', recordingId: 9 })).toEqual({ action: 'none', reason: 'on_server' });
    });
    it('has nothing to cancel for the rest', () => {
        expect(uploadCancelPlan(undefined)).toEqual({ action: 'none', reason: 'missing' });
        expect(uploadCancelPlan({ status: 'completed' }).reason).toBe('finished');
        expect(uploadCancelPlan({ status: 'failed' }).reason).toBe('finished');
    });
});

describe('canCancelUpload', () => {
    it('is true where the plan acts', () => {
        expect(canCancelUpload({ status: 'ready' })).toBe(true);
        expect(canCancelUpload({ status: 'uploading' })).toBe(true);
        expect(canCancelUpload({ status: 'pending' })).toBe(false);
        expect(canCancelUpload({ status: 'uploading', fromInProgressRecording: true })).toBe(false);
    });
});
