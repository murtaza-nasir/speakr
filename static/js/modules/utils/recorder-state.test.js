import { describe, it, expect } from 'vitest';
import { recorderInUse } from './recorder-state.js';

const ref = (value) => ({ value });

describe('recorderInUse', () => {
    it('is true while recording, wherever the user is', () => {
        expect(recorderInUse(ref(true), ref('detail'))).toBe(true);
    });
    it('is true on the recording screen even when paused or not yet started', () => {
        expect(recorderInUse(ref(false), ref('recording'))).toBe(true);
    });
    it('is false otherwise, or when the refs are missing', () => {
        expect(recorderInUse(ref(false), ref('detail'))).toBe(false);
        expect(recorderInUse(ref(false), ref(null))).toBe(false);
        expect(recorderInUse(undefined, undefined)).toBe(false);
    });
});
