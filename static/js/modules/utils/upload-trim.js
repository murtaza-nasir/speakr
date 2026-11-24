/**
 * The range a person names on New recording before the file is uploaded.
 * Whole seconds. The server applies the same fields during conversion.
 */

/** The end shown when the control is turned on: the duration rounded up. */
export function wholeEnd(durationSeconds) {
    const duration = Number(durationSeconds);
    if (!Number.isFinite(duration) || duration <= 0) return null;
    return Math.ceil(duration - 1e-9);
}

function wholeSeconds(value) {
    if (typeof value === 'number' && Number.isInteger(value)) return value;
    if (typeof value === 'string' && /^-?\d+$/.test(value.trim())) return Number(value.trim());
    return null;
}

/**
 * Fields to send with the upload. Defaults, an empty field, and a range that
 * still covers the whole file send nothing. Only a start or only an end sends
 * that one field.
 */
export function trimFields(start, end, durationSeconds) {
    const duration = Number(durationSeconds);
    if (!Number.isFinite(duration) || duration <= 0) return {};
    const startSeconds = wholeSeconds(start);
    const endSeconds = wholeSeconds(end);
    const fields = {};
    if (startSeconds !== null && startSeconds > 0) fields.trim_start = String(startSeconds);
    if (endSeconds !== null && endSeconds < duration) fields.trim_end = String(endSeconds);
    return fields;
}

/** How many seconds the named range keeps, for the line under the file. */
export function keptSeconds(start, end) {
    const startSeconds = wholeSeconds(start);
    const endSeconds = wholeSeconds(end);
    if (startSeconds === null || endSeconds === null) return null;
    return Math.max(0, endSeconds - startSeconds);
}

/**
 * What Preview plays from the file already on disk: three seconds after the
 * start and three seconds before the end, or the whole kept span once when
 * that span is six seconds or shorter.
 */
export function previewSpans(start, end) {
    const startSeconds = wholeSeconds(start);
    const endSeconds = wholeSeconds(end);
    if (startSeconds === null || endSeconds === null || endSeconds <= startSeconds) return [];
    if (endSeconds - startSeconds <= 6) return [{ start: startSeconds, end: endSeconds }];
    return [
        { start: startSeconds, end: startSeconds + 3 },
        { start: endSeconds - 3, end: endSeconds },
    ];
}
