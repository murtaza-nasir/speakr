import { describe, it, expect } from 'vitest';
import { keptSeconds, previewSpans, trimFields, trimProblem, trimProblemKey, wholeEnd } from './upload-trim.js';

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
    it('stops at the file when an end is past it', () => {
        expect(keptSeconds(0, 301220, 300)).toBe(300);
        expect(keptSeconds(60, 301220, 300)).toBe(240);
        expect(keptSeconds(0, 120, 300)).toBe(120);
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

describe('trimProblem', () => {
    it('accepts a range the server can apply', () => {
        expect(trimProblem(2, 5, 6.2)).toBeNull();
        expect(trimProblem(0, 7, 6.2)).toBeNull();
        expect(trimProblem('', '', 6.2)).toBeNull();
        expect(trimProblem(5, '', 6.2)).toBeNull();
        expect(trimProblem(5, 6, 6.2)).toBeNull();
    });
    it('refuses a start that is not before the end, or not inside the file', () => {
        expect(trimProblem(4, 2, 6.2)).toBe('invalid');
        expect(trimProblem(3, 3, 6.2)).toBe('invalid');
        expect(trimProblem(-1, 4, 6.2)).toBe('invalid');
        expect(trimProblem(7, '', 6.2)).toBe('invalid');
        expect(trimProblem(1.5, 4, 6.2)).toBe('invalid');
        expect(trimProblem(0, 0, 6.2)).toBe('invalid');
    });
    it('refuses less than a second kept, as the server does', () => {
        expect(trimProblem(6, '', 6.5)).toBe('short');
        expect(trimProblem(6, 9, 6.5)).toBe('short');
    });
    it('names the message of the server', () => {
        expect(trimProblemKey('short')).toBe('server.apiError.trim_too_short');
        expect(trimProblemKey('invalid')).toBe('server.apiError.trim_invalid_range');
        expect(trimProblemKey(null)).toBe('');
    });
});
