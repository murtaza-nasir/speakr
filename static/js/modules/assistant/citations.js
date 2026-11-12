/**
 * Citations in an Ambrose answer (phase 2b): one numbering for a whole chat.
 *
 * Two kinds of citation reach the browser:
 *   - segment markers [S12 "exact words"] for the recording open in the
 *     panel; the server sends a `source` event for each one before the text
 *     that carries it (src/services/assistant_events.py);
 *   - links to other recordings, [Title @ 12:34](/recordings/73?t=754),
 *     which the model writes for what it found with the library tools (the
 *     Ask page, and the panel when it searched other recordings).
 *
 * Both are shown as numbered markers with one Sources list under the answer.
 * Numbers continue through the chat: an earlier answer's numbers stay, and
 * citing the same segment quote or the same recording again reuses its
 * number. The numbering an answer showed is kept on it as `sources` and sent
 * back with the history, so "tell me more about 3" means what the user saw.
 *
 * Pure string functions, so they run under vitest without a DOM; render.js
 * turns the placeholders into elements.
 */

// Wraps an occurrence number put in place of a citation, so only our
// placeholders become markers (a "[1]" the model writes stays text).
export const CITE_MARK = '⁣';
export const PLACEHOLDER_RE = /⁣\[(\d+)\]⁣/g;

// [S12 "quote"] or [S12], then [text](/recordings/<id>?t=<seconds>)
const CITE_RE = /\[S(\d+)(?:\s+"([^"\]]*)")?\]|\[([^\]\n]+)\]\((\/recordings\/(\d+)(?:\?t=(\d+))?)\)/g;
const STATED_TIME_RE = /@\s*(\d+(?::\d{1,2}){0,2})\s*$/;

export const normQuote = (text) => String(text || '').replace(/\s+/g, ' ').trim().toLowerCase();

/** The identity of a cited thing: a recording, or a quote in a segment. */
export function citationKey(entry) {
    if (!entry) return '';
    if (entry.kind === 'library' || (entry.kind === undefined && entry.recording_id != null)) {
        return 'R' + entry.recording_id;
    }
    const where = entry.segment_index != null ? entry.segment_index : (entry.marker || '');
    return 'S' + where + '|' + normQuote(entry.quote);
}

/** The numbers shown in the messages before `index`: {numbers: key -> n, next}. */
export function priorNumbers(messages, index) {
    const numbers = new Map();
    let max = 0;
    const list = Array.isArray(messages) ? messages : [];
    for (let i = 0; i < Math.min(index, list.length); i++) {
        const sources = list[i] && Array.isArray(list[i].sources) ? list[i].sources : [];
        for (const source of sources) {
            if (!source || !Number.isInteger(source.n)) continue;
            const key = citationKey(source);
            if (!numbers.has(key)) numbers.set(key, source.n);
            if (source.n > max) max = source.n;
        }
    }
    return { numbers, next: max + 1 };
}

function statedSeconds(text) {
    const m = String(text || '').match(STATED_TIME_RE);
    if (!m) return null;
    return m[1].split(':').map(Number).reduce((acc, part) => acc * 60 + part, 0);
}

/**
 * Number the citations of one answer.
 *
 * Returns {text, occurrences, sources}: the markdown with each citation
 * replaced by a placeholder for its occurrence, the occurrences in order
 * ({n, kind, seconds, recording_id, title, verified}), and this answer's
 * sources once each in number order. A segment marker without its `source`
 * event (a plain-text transcript) is left as text.
 */
export function numberAnswer(content, segmentSources, prior) {
    const byMarker = new Map();
    for (const s of Array.isArray(segmentSources) ? segmentSources : []) {
        if (s && typeof s.marker === 'string' && s.marker) byMarker.set(s.marker, s);
    }
    const numbers = new Map(prior && prior.numbers ? prior.numbers : []);
    let next = prior && prior.next ? prior.next : 1;
    const occurrences = [];
    const listed = new Map();

    const numberOf = (entry) => {
        const key = citationKey(entry);
        if (!numbers.has(key)) numbers.set(key, next++);
        entry.n = numbers.get(key);
        const seen = listed.get(key);
        if (!seen) listed.set(key, entry);
        else if (entry.title && (!seen.title || entry.title.length > seen.title.length)) seen.title = entry.title;
        return entry.n;
    };
    const placeholder = (occurrence) => {
        occurrences.push(occurrence);
        return `${CITE_MARK}[${occurrences.length - 1}]${CITE_MARK}`;
    };

    const text = (typeof content === 'string' ? content : '').replace(CITE_RE,
        (match, segIndex, _quote, linkText, href, recId, seek) => {
            if (segIndex !== undefined) {
                const source = byMarker.get(match);
                if (!source) return match;
                const entry = {
                    kind: 'segment', segment_index: source.segment_index ?? null, marker: source.marker,
                    start_time: source.start_time ?? null, end_time: source.end_time ?? null,
                    speaker: source.speaker || null, quote: source.quote || null, verified: !!source.verified,
                };
                const n = numberOf(entry);
                return placeholder({ n, kind: 'segment', seconds: entry.start_time, verified: entry.verified });
            }
            const raw = linkText.trim();
            const stated = raw.match(STATED_TIME_RE);
            const seconds = seek !== undefined ? parseInt(seek, 10) : statedSeconds(raw);
            const title = (stated ? raw.slice(0, stated.index) : raw).trim() || null;
            const recordingId = Number(recId);
            const n = numberOf({ kind: 'library', recording_id: recordingId, title });
            return placeholder({ n, kind: 'library', recording_id: recordingId, seconds, title });
        });

    const sources = [...listed.values()].sort((a, b) => a.n - b.n);
    return { text, occurrences, sources };
}

/** m:ss or h:mm:ss. */
export function formatClock(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h > 0
        ? h + ':' + String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0')
        : m + ':' + String(sec).padStart(2, '0');
}

/** A library source's title, or its id when the model gave none. */
export const sourceTitle = (source) => (source && source.title) || ('#' + (source ? source.recording_id : ''));

/**
 * An answer as plain text for copy and download: citations as [n] and the
 * sources listed under `label`.
 */
export function plainAnswer(content, segmentSources, prior, label) {
    const { text, occurrences, sources } = numberAnswer(content, segmentSources, prior);
    const plain = text.replace(PLACEHOLDER_RE, (_m, k) => `[${occurrences[Number(k)].n}]`);
    if (!sources.length) return plain;
    const lines = sources.map((s) => {
        if (s.kind === 'library') return `${s.n}. ${sourceTitle(s)}`;
        const head = [s.start_time != null ? formatClock(s.start_time) : '', s.speaker || ''].filter(Boolean).join(' ');
        const quote = s.quote ? `“${s.quote}”` : '';
        return `${s.n}. ${[head, quote].filter(Boolean).join(': ')}`;
    });
    return `${plain}\n\n${label}\n${lines.join('\n')}`;
}

/** The sources as the history sends them back: only the fields the server reads. */
export function historySources(sources) {
    if (!Array.isArray(sources) || !sources.length) return undefined;
    return sources.map((s) => (s.kind === 'library'
        ? { n: s.n, recording_id: s.recording_id, title: s.title || '' }
        : { n: s.n, segment_index: s.segment_index, start_time: s.start_time, speaker: s.speaker, quote: s.quote }));
}
