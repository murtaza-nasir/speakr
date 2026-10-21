// Older browsers can still edit transcripts; splitting needs safe grapheme boundaries.
const graphemes = typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function'
    ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : null;

export const supportsAsrSegmentSplit = () => graphemes !== null;

/** Split a draft without changing its source or pretending text implies exact alignment. */
export function splitAsrSegment(segment, start, end, explicitTime = undefined) {
    if (!supportsAsrSegmentSplit()) return { error: 'unsupported' };
    const text = segment?.sentence;
    if (typeof text !== 'string' || !Number.isInteger(start) || start !== end ||
        start <= 0 || start >= text.length ||
        !Array.from(graphemes.segment(text)).some(part => part.index === start)) {
        return { error: 'cursor' };
    }
    const left = text.slice(0, start).trimEnd();
    const right = text.slice(start).trimStart();
    if (!left.trim() || !right.trim()) return { error: 'cursor' };

    const s = segment.start_time;
    const e = segment.end_time;
    const epsilon = Number.EPSILON * Math.max(1, Math.abs(s), Math.abs(e)) * 4;
    if (!Number.isFinite(s) || !Number.isFinite(e) || s < 0 || e - s + epsilon < 0.02) {
        return { error: 'time' };
    }
    const ratio = Array.from(left).length / (Array.from(left).length + Array.from(right).length);
    const rounded = Math.round((s + (e - s) * ratio) * 100) / 100;
    let boundary = Math.max(s + 0.01, Math.min(e - 0.01, rounded));
    if (explicitTime !== undefined) {
        if (!Number.isFinite(explicitTime)) return { error: 'time' };
        boundary = Math.round(explicitTime * 100) / 100;
        if (boundary < s + 0.01 - epsilon || boundary > e - 0.01 + epsilon) return { error: 'time' };
    }
    if (!(s < boundary && boundary < e)) return { error: 'time' };
    // Legacy word alignment belongs to the original sentence, not either new sentence.
    const { words, ...metadata } = segment;
    return { parts: [
        { ...metadata, sentence: left, end_time: boundary },
        { ...metadata, sentence: right, start_time: boundary }
    ] };
}
