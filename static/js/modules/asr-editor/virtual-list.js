/**
 * Row positions for a virtual list with rows of different heights.
 *
 * Pure math, no DOM. Every row has a height: the measured one once the row
 * has been rendered, an estimate until then. Offsets are prefix sums kept in
 * one typed array and recomputed only from the first row whose height
 * changed, so a measurement near the viewport costs little even with tens of
 * thousands of rows.
 */

export function createHeightIndex() {
    let count = 0;
    let heights = new Float64Array(0);
    let prefix = new Float64Array(1);   // prefix[i] = top of row i; prefix[count] = total
    let valid = 0;                      // prefix[0..valid] is current

    const ensure = (i) => {
        for (let k = valid; k < i; k++) prefix[k + 1] = prefix[k] + heights[k];
        if (i > valid) valid = i;
    };

    return {
        get count() { return count; },

        /** Start over with `n` rows; heightAt(i) gives each row's height. */
        reset(n, heightAt) {
            count = n;
            if (heights.length < n || heights.length > 2 * n + 64) {
                heights = new Float64Array(n + 32);
                prefix = new Float64Array(n + 33);
            }
            for (let i = 0; i < n; i++) heights[i] = heightAt(i);
            prefix[0] = 0;
            valid = 0;
        },

        /** Set one row's height. Returns the change in pixels (0 when it is the same). */
        set(i, height) {
            if (i < 0 || i >= count) return 0;
            const delta = height - heights[i];
            if (Math.abs(delta) < 0.5) return 0;
            heights[i] = height;
            if (i < valid) valid = i;
            return delta;
        },

        height: (i) => (i >= 0 && i < count ? heights[i] : 0),

        /** Top of row i (i === count gives the total height). */
        offset(i) {
            const k = Math.min(Math.max(i, 0), count);
            ensure(k);
            return prefix[k];
        },

        total() {
            ensure(count);
            return prefix[count];
        },

        /** Index of the row at vertical position y, clamped to the rows. */
        indexAt(y) {
            if (count === 0 || y <= 0) return 0;
            ensure(count);
            if (y >= prefix[count]) return count - 1;
            let lo = 0;
            let hi = count - 1;
            while (lo < hi) {
                const mid = (lo + hi + 1) >> 1;
                if (prefix[mid] <= y) lo = mid; else hi = mid - 1;
            }
            return lo;
        },
    };
}

/** Rows to render for a viewport: { start, end } with end exclusive. */
export function visibleRange(index, scrollTop, viewportHeight, overscan = 6) {
    if (index.count === 0) return { start: 0, end: 0 };
    const first = index.indexAt(scrollTop);
    const last = index.indexAt(scrollTop + Math.max(viewportHeight, 1));
    return { start: Math.max(0, first - overscan), end: Math.min(index.count, last + 1 + overscan) };
}

/**
 * The row at the top of the viewport and how far into it the viewport
 * begins. Taken before heights change and given to scrollTopFor afterwards,
 * it keeps what the user is looking at in place.
 */
export function anchorAt(index, scrollTop) {
    if (index.count === 0) return { row: 0, within: 0 };
    const row = index.indexAt(scrollTop);
    return { row, within: scrollTop - index.offset(row) };
}

export function scrollTopFor(index, anchor) {
    if (index.count === 0) return 0;
    const row = Math.min(anchor.row, index.count - 1);
    return index.offset(row) + Math.min(anchor.within, index.height(row));
}

/** Scroll position that brings row i into view, or null when it already is. */
export function scrollTopToReveal(index, i, scrollTop, viewportHeight, margin = 8) {
    if (i < 0 || i >= index.count) return null;
    const top = index.offset(i);
    const bottom = top + index.height(i);
    if (top - margin < scrollTop) return Math.max(0, top - margin);
    if (bottom + margin > scrollTop + viewportHeight) {
        // A row taller than the viewport shows its top.
        return Math.max(0, Math.min(top - margin, bottom + margin - viewportHeight));
    }
    return null;
}

/** Height estimate of a row from the length of its text. */
export function estimateRowHeight(textLength, charsPerLine, { lineHeight = 24, padding = 20, min = 44 } = {}) {
    const lines = Math.max(1, Math.ceil(textLength / Math.max(8, charsPerLine)));
    return Math.max(min, padding + lines * lineHeight);
}
