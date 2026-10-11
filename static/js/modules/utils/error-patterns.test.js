import { describe, expect, it } from 'vitest';

import { errorTextMatches } from './error-patterns.js';

describe('errorTextMatches', () => {
    it('matches a status code written as a code', () => {
        for (const text of ['asr request failed with status 401', 'http 401 unauthorized',
            'error code: 429 - too many', 'failed (503)', 'server returned 502.', 'status=500, retrying']) {
            expect(errorTextMatches(text, text.match(/\d{3}/)[0]), text).toBe(true);
        }
    });

    it('does not match a code inside an id or a longer number', () => {
        // The WhisperX traceback that was labelled "Authentication Error".
        expect(errorTextMatches('actor_id=8f2c401ab93e7d10 cuda failed with error out of memory', '401')).toBe(false);
        expect(errorTextMatches('request 3a-401f failed', '401')).toBe(false);
        expect(errorTextMatches('max_tokens=1500 exceeded', '500')).toBe(false);
        expect(errorTextMatches('version 2.4.1.401', '401')).toBe(false);
    });

    it('matches words as substrings, as before', () => {
        expect(errorTextMatches('the service is unauthorized here', 'unauthorized')).toBe(true);
        expect(errorTextMatches('request timed out after 600s', 'timed out')).toBe(true);
    });
});
