/**
 * Segment citations in the recording chat (Ambrose phase 2a).
 *
 * The model cites the transcript as [S12 "exact words"]. The server sends a
 * `source` event for each marker before the text that carries it
 * (src/services/assistant_events.py): {n, marker, segment_index, start_time,
 * end_time, speaker, quote, verified}. The message keeps the model's own
 * text, so the history sent back is what the model wrote; these helpers
 * show each marker as its number.
 */

// Wraps a number put in place of a marker, so only our placeholders are
// turned into citation buttons (a "[1]" the model writes stays text).
export const CITE_MARK = '⁣';
export const CITE_PLACEHOLDER_RE = /⁣\[(\d+)\]⁣/g;

/** Add or replace a source by its marker text. */
export function upsertSource(sources, source) {
    if (!source || typeof source.marker !== 'string' || !source.marker) return sources;
    const list = Array.isArray(sources) ? sources : [];
    const at = list.findIndex((s) => s.marker === source.marker);
    if (at >= 0) list.splice(at, 1, source);
    else list.push(source);
    return list;
}

/** The sources once each, by number, in number order. */
export function numberedSources(sources) {
    const byN = new Map();
    for (const s of Array.isArray(sources) ? sources : []) {
        if (Number.isInteger(s.n) && !byN.has(s.n)) byN.set(s.n, s);
    }
    return [...byN.values()].sort((a, b) => a.n - b.n);
}

function replaceMarkers(content, sources, render) {
    let text = typeof content === 'string' ? content : '';
    // Longest markers first, so one marker that contains another is whole.
    const list = (Array.isArray(sources) ? sources : [])
        .filter((s) => typeof s.marker === 'string' && s.marker && Number.isInteger(s.n))
        .sort((a, b) => b.marker.length - a.marker.length);
    for (const s of list) text = text.split(s.marker).join(render(s.n));
    return text;
}

/** Markdown source for display: markers become placeholders. */
export function markCitations(content, sources) {
    return replaceMarkers(content, sources, (n) => `${CITE_MARK}[${n}]${CITE_MARK}`);
}

/** m:ss or h:mm:ss. */
export function formatClock(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h > 0
        ? h + ':' + String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0')
        : m + ':' + String(sec).padStart(2, '0');
}

/**
 * Text for copy and download: markers become [n] and the sources follow as
 * a numbered list under `label`.
 */
export function plainCitations(content, sources, label) {
    const text = replaceMarkers(content, sources, (n) => `[${n}]`);
    const list = numberedSources(sources);
    if (!list.length) return text;
    const lines = list.map((s) => {
        const parts = [];
        if (s.start_time !== null && s.start_time !== undefined) parts.push(formatClock(s.start_time));
        if (s.speaker) parts.push(s.speaker);
        const head = parts.join(' ');
        const quote = s.quote ? `“${s.quote}”` : '';
        return `${s.n}. ${[head, quote].filter(Boolean).join(': ')}`;
    });
    return `${text}\n\n${label}\n${lines.join('\n')}`;
}
