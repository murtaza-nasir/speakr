import { describe, it, expect } from 'vitest';
import { keptSeconds, previewSpans, trimFields, wholeEnd } from './upload-trim.js';

describe('wholeEnd', () => {
    it('rounds the duration up to a whole second', () => {
        expect(wholeEnd(6.2)).toBe(7);
        expect(wholeEnd(6)).toBe(6);
    });
    it('is empty when the duration was not read', () => {
        expect(wholeEnd(null)).toBeNull();
        expect(wholeEnd(0)).toBeNull();
    });
});

describe('trimFields', () => {
    it('sends nothing while the defaults cover the whole file', () => {
        expect(trimFields(0, 7, 6.2)).toEqual({});
        expect(trimFields(0, 6, 6)).toEqual({});
        expect(trimFields('', '', 6.2)).toEqual({});
    });
    it('sends only the start or only the end', () => {
        expect(trimFields(2, 7, 6.2)).toEqual({ trim_start: '2' });
        expect(trimFields(0, 4, 6.2)).toEqual({ trim_end: '4' });
    });
    it('sends both when each end of the file is dropped', () => {
        expect(trimFields(2, 5, 6.2)).toEqual({ trim_start: '2', trim_end: '5' });
    });
});

describe('keptSeconds', () => {
    it('is the span between the two times', () => {
        expect(keptSeconds(2, 5)).toBe(3);
        expect(keptSeconds(0, 7)).toBe(7);
    });
});

describe('previewSpans', () => {
    it('plays three seconds at each end', () => {
        expect(previewSpans(2, 20)).toEqual([{ start: 2, end: 5 }, { start: 17, end: 20 }]);
    });
    it('plays a kept span of six seconds or less once', () => {
        expect(previewSpans(2, 8)).toEqual([{ start: 2, end: 8 }]);
        expect(previewSpans(2, 7)).toEqual([{ start: 2, end: 7 }]);
    });
});
