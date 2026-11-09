/**
 * Offsets of rows with measured heights, for virtual scrolling.
 *
 * Rows that have not been measured count as `estimate` pixels. offsets[i]
 * is the top of row i and offsets[count] the total height.
 */

/** Top offset of every row, plus the total height at the end. */
export function buildOffsets(count, estimate, measured) {
    const offsets = new Float64Array(count + 1);
    for (let i = 0; i < count; i++) {
        const h = measured.get(i);
        offsets[i + 1] = offsets[i] + (h === undefined ? estimate : h);
    }
    return offsets;
}

/** Index of the row that contains vertical position y (clamped to the rows). */
export function indexAtOffset(offsets, y) {
    const count = offsets.length - 1;
    if (count <= 0) return 0;
    if (y <= 0) return 0;
    if (y >= offsets[count]) return count - 1;
    let lo = 0;
    let hi = count - 1;
    while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (offsets[mid] <= y) lo = mid;
        else hi = mid - 1;
    }
    return lo;
}

/**
 * Record new heights. Returns how much the rows that end above `scrollTop`
 * grew in total, so the caller can move the scroll position by that much and
 * keep what the user is looking at in place.
 */
export function applyMeasurements(measured, offsets, estimate, scrollTop, updates) {
    let shiftAbove = 0;
    let changed = false;
    for (const [index, height] of updates) {
        const old = measured.has(index) ? measured.get(index) : estimate;
        if (Math.abs(old - height) < 0.5) continue;
        changed = true;
        if (index + 1 < offsets.length && offsets[index + 1] <= scrollTop) {
            shiftAbove += height - old;
        }
        measured.set(index, height);
    }
    return { changed, shiftAbove };
}
