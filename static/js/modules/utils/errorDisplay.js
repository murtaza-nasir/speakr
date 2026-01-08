/**
 * Error Display Utility
 *
 * Parses and displays user-friendly error messages from the backend.
 * Handles both JSON-formatted errors (ERROR_JSON:...) and plain text errors.
 */

/**
 * Parse a stored error message from the backend.
 * @param {string} text - The stored transcription/error text
 * @returns {Object|null} - Parsed error object or null if not an error
 */
export function parseStoredError(text) {
    if (!text) return null;

    // Check for JSON-formatted error
    if (text.startsWith('ERROR_JSON:')) {
        try {
            const jsonStr = text.substring(11); // Remove 'ERROR_JSON:' prefix
            const data = JSON.parse(jsonStr);
            return {
                title: data.t || 'Error',
                message: data.m || 'An error occurred',
                guidance: data.g || '',
                icon: data.i || 'fa-exclamation-circle',
                type: data.y || 'unknown',
                isKnown: data.k || false,
                technical: data.d || '',
                isFormattedError: true
            };
        } catch (e) {
            console.error('Failed to parse error JSON:', e);
        }
    }

    // Check for legacy error format (starts with common error prefixes)
    const errorPrefixes = [
        'Transcription failed:',
        'Processing failed:',
        'ASR processing failed:',
        'Audio extraction failed:',
        'Error:'
    ];

    for (const prefix of errorPrefixes) {
        if (text.startsWith(prefix)) {
            // Parse the error using pattern matching
            return parseUnformattedError(text);
        }
    }

    return null;
}

/**
 * Parse an unformatted error message and try to make it user-friendly.
 * @param {string} text - The raw error text
 * @returns {Object} - Parsed error object
 */
function parseUnformattedError(text) {
    const lowerText = text.toLowerCase();

    // Known error patterns
    const patterns = [
        {
            patterns: ['maximum content size limit', 'file too large', '413', 'payload too large', 'exceeded'],
            title: 'File Too Large',
            message: 'The audio file exceeds the maximum size allowed by the transcription service.',
            guidance: 'Try enabling audio chunking in your settings, or compress the audio file before uploading.',
            icon: 'fa-file-audio',
            type: 'size_limit'
        },
        {
            patterns: ['timed out', 'timeout', 'deadline exceeded'],
            title: 'Processing Timeout',
            message: 'The transcription took too long to complete.',
            guidance: 'This can happen with very long recordings. Try splitting the audio into smaller parts.',
            icon: 'fa-clock',
            type: 'timeout'
        },
        {
            patterns: ['401', 'unauthorized', 'invalid api key', 'authentication failed', 'incorrect api key'],
            title: 'Authentication Error',
            message: 'The transcription service rejected the API credentials.',
            guidance: 'Please check that the API key is correct and has not expired.',
            icon: 'fa-key',
            type: 'auth'
        },
        {
            patterns: ['rate limit', 'too many requests', '429', 'quota exceeded'],
            title: 'Rate Limit Exceeded',
            message: 'Too many requests were sent to the transcription service.',
            guidance: 'Please wait a few minutes and try reprocessing.',
            icon: 'fa-hourglass-half',
            type: 'rate_limit'
        },
        {
            patterns: ['connection refused', 'connection reset', 'could not connect', 'network unreachable'],
            title: 'Connection Error',
            message: 'Could not connect to the transcription service.',
            guidance: 'Check your internet connection and ensure the service is available.',
            icon: 'fa-wifi',
            type: 'connection'
        },
        {
            patterns: ['503', '502', '500', 'service unavailable', 'server error', 'internal server error'],
            title: 'Service Unavailable',
            message: 'The transcription service is temporarily unavailable.',
            guidance: 'This is usually temporary. Please try again in a few minutes.',
            icon: 'fa-server',
            type: 'service_error'
        },
        {
            patterns: ['invalid file format', 'unsupported format', 'could not decode', 'corrupt', 'not valid audio'],
            title: 'Invalid Audio Format',
            message: 'The audio file format is not supported or the file may be corrupted.',
            guidance: 'Try converting the audio to MP3 or WAV format before uploading.',
            icon: 'fa-file-audio',
            type: 'format'
        },
        {
            patterns: ['audio extraction failed', 'ffmpeg failed', 'no audio stream'],
            title: 'Audio Extraction Failed',
            message: 'Could not extract audio from the uploaded file.',
            guidance: 'Try converting the file to a standard audio format (MP3, WAV) before uploading.',
            icon: 'fa-file-video',
            type: 'extraction'
        }
    ];

    // Check patterns
    for (const pattern of patterns) {
        for (const p of pattern.patterns) {
            if (lowerText.includes(p)) {
                return {
                    title: pattern.title,
                    message: pattern.message,
                    guidance: pattern.guidance,
                    icon: pattern.icon,
                    type: pattern.type,
                    isKnown: true,
                    technical: text,
                    isFormattedError: true
                };
            }
        }
    }

    // Unknown error - clean it up as best we can
    let cleanMessage = text;
    for (const prefix of ['Transcription failed:', 'Processing failed:', 'Error:', 'ASR processing failed:']) {
        if (cleanMessage.startsWith(prefix)) {
            cleanMessage = cleanMessage.substring(prefix.length).trim();
        }
    }

    // Truncate if too long
    if (cleanMessage.length > 200) {
        cleanMessage = cleanMessage.substring(0, 200) + '...';
    }

    return {
        title: 'Processing Error',
        message: cleanMessage,
        guidance: 'If this error persists, try reprocessing the recording.',
        icon: 'fa-exclamation-circle',
        type: 'unknown',
        isKnown: false,
        technical: text,
        isFormattedError: true
    };
}

/**
 * Check if a transcription text is actually an error message.
 * @param {string} text - The transcription text
 * @returns {boolean}
 */
export function isErrorMessage(text) {
    if (!text) return false;

    if (text.startsWith('ERROR_JSON:')) return true;

    const errorPrefixes = [
        'Transcription failed:',
        'Processing failed:',
        'ASR processing failed:',
        'Audio extraction failed:'
    ];

    return errorPrefixes.some(prefix => text.startsWith(prefix));
}

/**
 * Generate HTML for displaying an error nicely.
 * @param {Object} error - Parsed error object from parseStoredError
 * @param {boolean} showTechnical - Whether to show technical details
 * @returns {string} - HTML string
 */
export function generateErrorHTML(error, showTechnical = false) {
    if (!error) return '';

    const typeColors = {
        size_limit: 'amber',
        timeout: 'orange',
        auth: 'red',
        rate_limit: 'yellow',
        connection: 'blue',
        service_error: 'purple',
        format: 'pink',
        extraction: 'indigo',
        billing: 'red',
        model: 'gray',
        unknown: 'gray'
    };

    const color = typeColors[error.type] || 'gray';

    let html = `
        <div class="error-display bg-${color}-500/10 border border-${color}-500/30 rounded-lg p-4">
            <div class="flex items-start gap-3">
                <div class="flex-shrink-0 w-10 h-10 rounded-full bg-${color}-500/20 flex items-center justify-center">
                    <i class="fas ${error.icon} text-${color}-500"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <h3 class="text-lg font-semibold text-${color}-600 dark:text-${color}-400 mb-1">
                        ${escapeHtml(error.title)}
                    </h3>
                    <p class="text-[var(--text-primary)] mb-2">
                        ${escapeHtml(error.message)}
                    </p>
                    ${error.guidance ? `
                        <div class="flex items-start gap-2 text-sm text-[var(--text-secondary)] bg-[var(--bg-tertiary)]/50 rounded p-2">
                            <i class="fas fa-lightbulb text-yellow-500 mt-0.5"></i>
                            <span>${escapeHtml(error.guidance)}</span>
                        </div>
                    ` : ''}
                </div>
            </div>
            ${showTechnical && error.technical ? `
                <details class="mt-3 text-xs">
                    <summary class="cursor-pointer text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                        Technical details
                    </summary>
                    <pre class="mt-2 p-2 bg-[var(--bg-tertiary)] rounded overflow-x-auto text-[var(--text-muted)]">${escapeHtml(error.technical)}</pre>
                </details>
            ` : ''}
        </div>
    `;

    return html;
}

/**
 * Escape HTML special characters.
 * @param {string} text
 * @returns {string}
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Export for use in Vue components
export default {
    parseStoredError,
    isErrorMessage,
    generateErrorHTML
};
