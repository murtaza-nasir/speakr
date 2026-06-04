/**
 * Tests for the local-download safety-net helper (issue #297).
 *
 * triggerLocalDownload is the last-resort fallback when both the upload and
 * IndexedDB persistence fail. It builds a blob URL, attaches an anchor,
 * synthesises a click, and revokes the URL afterwards. We mock the DOM
 * and URL APIs to verify the call shape.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { triggerLocalDownload } from './failed-uploads.js';

describe('triggerLocalDownload', () => {
    let originalDocument;
    let originalURL;
    let createObjectURL;
    let revokeObjectURL;
    let appendChild;
    let removeChild;
    let click;
    let anchor;

    beforeEach(() => {
        createObjectURL = vi.fn(() => 'blob:test-url');
        revokeObjectURL = vi.fn();
        click = vi.fn();
        appendChild = vi.fn();
        removeChild = vi.fn();
        anchor = {
            click,
            style: {},
            set href(val) { this._href = val; },
            get href() { return this._href; },
            set download(val) { this._download = val; },
            get download() { return this._download; },
        };
        originalURL = global.URL;
        originalDocument = global.document;
        global.URL = { createObjectURL, revokeObjectURL };
        global.document = {
            createElement: vi.fn(() => anchor),
            body: { appendChild, removeChild },
        };
    });

    afterEach(() => {
        global.URL = originalURL;
        global.document = originalDocument;
    });

    it('returns false for falsy file', () => {
        expect(triggerLocalDownload(null, 'a.webm')).toBe(false);
        expect(triggerLocalDownload(undefined, 'a.webm')).toBe(false);
    });

    it('returns false for zero-size blob', () => {
        expect(triggerLocalDownload({ size: 0 }, 'a.webm')).toBe(false);
        expect(createObjectURL).not.toHaveBeenCalled();
    });

    it('synthesises a click on an anchor for a non-empty blob', () => {
        const fakeFile = { name: 'recording.webm', size: 1024, type: 'audio/webm' };
        const result = triggerLocalDownload(fakeFile, 'recording.webm');
        expect(result).toBe(true);
        expect(createObjectURL).toHaveBeenCalledWith(fakeFile);
        expect(global.document.createElement).toHaveBeenCalledWith('a');
        expect(anchor.href).toBe('blob:test-url');
        expect(anchor.download).toBe('recording.webm');
        expect(click).toHaveBeenCalledOnce();
        expect(appendChild).toHaveBeenCalledWith(anchor);
        expect(removeChild).toHaveBeenCalledWith(anchor);
    });

    it('falls back to a generated name when none is supplied', () => {
        const fakeFile = { size: 100 };
        triggerLocalDownload(fakeFile);
        expect(anchor.download).toMatch(/^speakr-recording-\d+\.webm$/);
    });

    it('returns false when DOM access throws', () => {
        global.URL.createObjectURL = vi.fn(() => { throw new Error('boom'); });
        const result = triggerLocalDownload({ size: 1 }, 'x.webm');
        expect(result).toBe(false);
    });
});
