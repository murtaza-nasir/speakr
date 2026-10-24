/**
 * Friendly titles and guidance for a failed upload (upload dialog,
 * incognito upload).
 *
 * The server's `code` decides when there is one (#482): the message text can
 * already be localized, and an unreadable file must not fall through to the
 * generic "try reprocessing the recording", since no recording was created.
 * An incognito transcription error carries the server's error type instead
 * (src/utils/error_formatting.py); its message is a fixed English text.
 * Without either, the message text is matched against the patterns below.
 */

import { errorTextMatches } from './error-patterns.js';

const CODE_KEYS = {
    audio_decode_failed: ['errors.invalidAudioFormat', 'errors.audioDecodeFailedGuidance'],
    empty_file: ['errors.invalidAudioFormat', 'errors.emptyFileGuidance'],
    audio_conversion_timeout: ['errors.processingTimeout', 'errors.splitAudioGuidance'],
    storage_quota_exceeded: ['storageQuota.title', 'storageQuota.guidance'],
};

const TYPE_KEYS = {
    size_limit: ['errors.fileTooLargeTitle', 'errors.enableChunkingGuidance'],
    timeout: ['errors.processingTimeout', 'errors.splitAudioGuidance'],
    auth: ['errors.authenticationError', 'errors.checkApiKeyGuidance'],
    rate_limit: ['errors.rateLimitExceeded', 'errors.waitAndRetryGuidance'],
    connection: ['errors.connectionError', 'errors.checkNetworkGuidance'],
    service_error: ['errors.serviceUnavailable', 'errors.tryAgainLaterGuidance'],
    format: ['errors.invalidAudioFormat', 'errors.convertFormatGuidance'],
    extraction: ['errors.audioExtractionFailed', 'errors.convertStandardGuidance'],
};

export function getFriendlyError(errorMessage, t, code, errorType) {
    const _t = t || ((key) => key);
    const keys = (code && CODE_KEYS[code]) || (errorType && TYPE_KEYS[errorType]);
    if (keys) {
        const [title, guidance] = keys;
        return { title: _t(title), guidance: _t(guidance) };
    }
    if (!errorMessage) return { title: _t('errors.processingError'), message: _t('errors.processingErrorMessage') };
    const lowerText = errorMessage.toLowerCase();
    const patterns = [
        // Reverse-proxy 413 (nginx / NPM default). Must match BEFORE the
        // generic 413 pattern below; the proxy rejects the request body before
        // Speakr sees it, so enabling chunking on the Speakr side will not help.
        { patterns: ['request entity too large', 'request body is too large', '413 request entity too large'], title: _t('errors.uploadBlockedByProxyTitle'), guidance: _t('errors.uploadBlockedByProxyGuidance') },
        { patterns: ['storage quota', 'storage is full'], title: _t('storageQuota.title'), guidance: _t('storageQuota.guidance') },
        { patterns: ['maximum content size limit', 'file too large', 'payload too large', 'exceeded', 'content too large'], title: _t('errors.fileTooLargeTitle'), guidance: _t('errors.enableChunkingGuidance') },
        { patterns: ['timed out', 'timeout', 'deadline exceeded'], title: _t('errors.processingTimeout'), guidance: _t('errors.splitAudioGuidance') },
        { patterns: ['401', 'unauthorized', 'invalid api key', 'authentication failed', 'incorrect api key'], title: _t('errors.authenticationError'), guidance: _t('errors.checkApiKeyGuidance') },
        { patterns: ['rate limit', 'too many requests', '429', 'quota exceeded'], title: _t('errors.rateLimitExceeded'), guidance: _t('errors.waitAndRetryGuidance') },
        { patterns: ['connection refused', 'connection reset', 'could not connect', 'network unreachable'], title: _t('errors.connectionError'), guidance: _t('errors.checkNetworkGuidance') },
        { patterns: ['503', '502', '500', 'service unavailable', 'server error', 'internal server error'], title: _t('errors.serviceUnavailable'), guidance: _t('errors.tryAgainLaterGuidance') },
        // "Invalid data found when processing input" is FFmpeg's wording for
        // a file it cannot open.
        { patterns: ['invalid file format', 'unsupported format', 'could not decode', 'corrupt', 'invalid data found when processing input'], title: _t('errors.invalidAudioFormat'), guidance: _t('errors.convertFormatGuidance') },
        { patterns: ['audio extraction failed', 'ffmpeg failed', 'no audio stream'], title: _t('errors.audioExtractionFailed'), guidance: _t('errors.convertStandardGuidance') },
    ];
    for (const pattern of patterns) {
        for (const p of pattern.patterns) {
            if (errorTextMatches(lowerText, p)) return { title: pattern.title, guidance: pattern.guidance };
        }
    }
    return { title: _t('errors.processingError'), guidance: _t('errors.processingErrorFallbackGuidance') };
}
