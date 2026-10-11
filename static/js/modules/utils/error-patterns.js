/**
 * Pattern matching for the friendly-error tables (upload dialog, stored
 * errors, unformatted errors).
 *
 * A three-digit status code matches only as a separate token, such as
 * "status 401", "(429)" or "HTTP 503:", never inside an id or a longer number.
 * A WhisperX out-of-memory traceback was shown as "Authentication Error"
 * because "401" occurred in a Ray actor id, and "1500" counted as a 500.
 */
const STATUS_CODE = /^\d{3}$/;

export function errorTextMatches(lowerText, pattern) {
    if (STATUS_CODE.test(pattern)) {
        return new RegExp(`(?<![\\w.-])${pattern}(?![\\w-]|\\.\\w)`).test(lowerText);
    }
    return lowerText.includes(pattern);
}
