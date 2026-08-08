/**
 * Unit tests for filenameFromContentDisposition in content-disposition.js.
 *
 * The helper prefers the RFC 5987 `filename*` parameter (percent-encoded UTF-8)
 * over the ASCII `filename=` fallback, so downloads fetched as blobs keep
 * non-ASCII titles instead of degrading to the fallback name.
 */

import { describe, it, expect } from 'vitest';
import { filenameFromContentDisposition } from './content-disposition.js';

describe('filenameFromContentDisposition', () => {
    it('returns the fallback when there is no header', () => {
        expect(filenameFromContentDisposition(null, 'transcript.md')).toBe('transcript.md');
        expect(filenameFromContentDisposition(undefined, 'transcript.md')).toBe('transcript.md');
        expect(filenameFromContentDisposition('', 'transcript.md')).toBe('transcript.md');
    });

    it('reads a plain ASCII filename', () => {
        expect(filenameFromContentDisposition(
            'attachment; filename="meeting notes.md"', 'transcript.md',
        )).toBe('meeting notes.md');
    });

    it('prefers filename* over the ASCII fallback', () => {
        const header = 'attachment; filename="transcript-46.md"; '
            + "filename*=UTF-8''%D0%92%D0%BD%D1%83%D1%82%D1%80%D0%B5%D0%BD%D0%BD%D0%B8%D0%B9.md";
        expect(filenameFromContentDisposition(header, 'transcript.md')).toBe('Внутренний.md');
    });

    it('accepts a lowercase charset label', () => {
        const header = "attachment; filename*=utf-8''%E4%BC%9A%E8%AD%B0.txt";
        expect(filenameFromContentDisposition(header, 'transcript.txt')).toBe('会議.txt');
    });

    it('stops filename* at the parameter separator', () => {
        const header = "attachment; filename*=UTF-8''report.md; foo=bar";
        expect(filenameFromContentDisposition(header, 'transcript.md')).toBe('report.md');
    });

    it('falls back to the ASCII name when filename* is malformed', () => {
        const header = 'attachment; filename="transcript-46.md"; filename*=UTF-8\'\'%E0%A4%A';
        expect(filenameFromContentDisposition(header, 'transcript.md')).toBe('transcript-46.md');
    });

    it('reads an unquoted filename parameter', () => {
        expect(filenameFromContentDisposition(
            'attachment; filename=notes.txt', 'transcript.txt',
        )).toBe('notes.txt');
    });

    it('returns the fallback when the header carries no filename', () => {
        expect(filenameFromContentDisposition('attachment', 'transcript.md')).toBe('transcript.md');
    });
});
