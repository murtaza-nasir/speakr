import { describe, expect, it } from 'vitest';

import { getFriendlyError } from './upload-errors.js';

const t = (key) => key;

describe('getFriendlyError', () => {
    it('names an unreadable or empty file by its code (#482)', () => {
        expect(getFriendlyError('Datei nicht lesbar', t, 'audio_decode_failed')).toEqual({
            title: 'errors.invalidAudioFormat', guidance: 'errors.audioDecodeFailedGuidance',
        });
        expect(getFriendlyError('The uploaded file is empty.', t, 'empty_file')).toEqual({
            title: 'errors.invalidAudioFormat', guidance: 'errors.emptyFileGuidance',
        });
    });

    it('names a full storage by its code when the message is localized', () => {
        expect(getFriendlyError('Diese Datei passt nicht in Ihren Speicher', t, 'storage_quota_exceeded').title)
            .toBe('storageQuota.title');
    });

    it('reads FFmpeg\'s wording for a file it cannot open as an invalid format', () => {
        const ffmpeg = 'Compression of x.mp3 to mp3 failed: ffmpeg version n8.1.3 ... '
            + 'Error opening input files: Invalid data found when processing input';
        expect(getFriendlyError(ffmpeg, t).title).toBe('errors.invalidAudioFormat');
    });

    it('names an incognito transcription error by its server type', () => {
        expect(getFriendlyError('The transcription service rejected the API credentials.', t, undefined, 'auth'))
            .toEqual({ title: 'errors.authenticationError', guidance: 'errors.checkApiKeyGuidance' });
        expect(getFriendlyError('Too many requests', t, undefined, 'unknown').title).toBe('errors.rateLimitExceeded');
    });

    it('falls back to the message text for an unknown code', () => {
        expect(getFriendlyError('Upload timed out', t, 'something_new').title).toBe('errors.processingTimeout');
        expect(getFriendlyError('odd failure', t).title).toBe('errors.processingError');
    });
});
