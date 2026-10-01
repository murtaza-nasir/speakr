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
            // Reverse-proxy 413 (nginx default "Request Entity Too Large", Apache
            // "Request body is too large", Nginx Proxy Manager defaults). The
            // request never reached Speakr, so enabling chunking on the Speakr
            // side cannot help; the proxy must allow a larger body. Match this
            // BEFORE the generic 413/file-too-large pattern below.
            patterns: ['request entity too large', 'request body is too large', '413 request entity too large'],
            title: 'Upload Blocked by Reverse Proxy',
            message: 'The reverse proxy in front of Speakr rejected the upload before it reached Speakr.',
            guidance: 'Increase the client body size limit on your reverse proxy. For nginx, set client_max_body_size to a value larger than your audio file. For Nginx Proxy Manager the default is 2000m and is adjustable per host in the Advanced tab. The chunking and compression options in Speakr do not apply here because the upload is rejected before Speakr sees it.',
            icon: 'fa-shield-halved',
            type: 'proxy_limit'
        },
        {
            // Speakr's own MAX_FILE_SIZE_MB rejection (returns JSON with a
            // specific "File too large. Max: N MB" payload) and downstream
            // transcription-service size limits (e.g. OpenAI Whisper 25 MB).
            // Both are addressable from the Speakr side via chunking.
            patterns: ['maximum content size limit', 'file too large', 'payload too large', 'exceeded', 'content too large'],
            title: 'File Too Large',
            message: 'The audio file exceeds the maximum size allowed by the transcription service.',
            guidance: 'Enable audio chunking in your admin settings if it is off, or compress the audio file before uploading. If chunking is already enabled, the file may still exceed the per-chunk limit; lower the chunk size or compress the source.',
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

    // Full literal class names per color. The Tailwind stylesheet is compiled at
    // build time from complete class strings in the source files; names assembled
    // from a color variable are absent from it.
    const colorClasses = {
        amber: { box: 'bg-amber-500/10 border-amber-500/30', badge: 'bg-amber-500/20', icon: 'text-amber-500', title: 'text-amber-600 dark:text-amber-400' },
        orange: { box: 'bg-orange-500/10 border-orange-500/30', badge: 'bg-orange-500/20', icon: 'text-orange-500', title: 'text-orange-600 dark:text-orange-400' },
        red: { box: 'bg-red-500/10 border-red-500/30', badge: 'bg-red-500/20', icon: 'text-red-500', title: 'text-red-600 dark:text-red-400' },
        yellow: { box: 'bg-yellow-500/10 border-yellow-500/30', badge: 'bg-yellow-500/20', icon: 'text-yellow-500', title: 'text-yellow-600 dark:text-yellow-400' },
        blue: { box: 'bg-blue-500/10 border-blue-500/30', badge: 'bg-blue-500/20', icon: 'text-blue-500', title: 'text-blue-600 dark:text-blue-400' },
        purple: { box: 'bg-purple-500/10 border-purple-500/30', badge: 'bg-purple-500/20', icon: 'text-purple-500', title: 'text-purple-600 dark:text-purple-400' },
        pink: { box: 'bg-pink-500/10 border-pink-500/30', badge: 'bg-pink-500/20', icon: 'text-pink-500', title: 'text-pink-600 dark:text-pink-400' },
        indigo: { box: 'bg-indigo-500/10 border-indigo-500/30', badge: 'bg-indigo-500/20', icon: 'text-indigo-500', title: 'text-indigo-600 dark:text-indigo-400' },
        gray: { box: 'bg-gray-500/10 border-gray-500/30', badge: 'bg-gray-500/20', icon: 'text-gray-500', title: 'text-gray-600 dark:text-gray-400' }
    };

    const color = typeColors[error.type] || 'gray';
    const cls = colorClasses[color] || colorClasses.gray;

    let html = `
        <div class="error-display border ${cls.box} rounded-lg p-4">
            <div class="flex items-start gap-3">
                <div class="flex-shrink-0 w-10 h-10 rounded-full ${cls.badge} flex items-center justify-center">
                    <i class="fas ${error.icon} ${cls.icon}"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <h3 class="text-lg font-semibold ${cls.title} mb-1">
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
