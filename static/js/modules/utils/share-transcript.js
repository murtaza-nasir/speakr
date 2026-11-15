/**
 * The transcript of the public share page (static/js/share-page.js).
 *
 * Pure logic, no DOM and no Vue. The server sends a transcript stored as
 * segments in a short form (src/api/shares.py, public_transcript):
 * { speakers: [name], segments: [[speaker, start, end, text]] }, with
 * `speaker` an index into `speakers` or -1. Any other transcript comes as
 * { text }. From that, shareTranscript builds what the transcript views of
 * the recording page work with (processedTranscription), so the share page
 * can use the same window (composables/transcript-view.js).
 */

import { segmentRuns } from './transcript-window.js';

// The speaker colors of the recording page: one per speaker in order of
// first appearance, repeating after this many.
export const SPEAKER_COLOR_COUNT = 16;

const speakerColor = (index) => `speaker-color-${(index % SPEAKER_COLOR_COUNT) + 1}`;
const isMe = (name) => typeof name === 'string' && name.toLowerCase().includes('me');

/** The rows of the bubble view: consecutive segments of one speaker. */
export function bubbleRows(segments) {
    const rows = [];
    let last;
    (segments || []).forEach((segment) => {
        if (!rows.length || segment.speakerId !== last) {
            rows.push({ speaker: segment.speaker, color: segment.color, isMe: isMe(segment.speaker), bubbles: [] });
            last = segment.speakerId;
        }
        rows[rows.length - 1].bubbles.push({
            sentence: segment.sentence, startTime: segment.startTime, endTime: segment.endTime, color: segment.color,
        });
    });
    return rows;
}

/**
 * What the transcript views render, from the server's short form.
 * simpleSegmentRuns and bubbleRows are built when a view first asks for
 * them: a page opens in one view and may never show the other.
 */
export function shareTranscript(data) {
    const rows = data && Array.isArray(data.segments) ? data.segments : null;
    if (!rows) {
        const content = data && typeof data.text === 'string' ? data.text : '';
        return { isJson: false, hasDialogue: false, content, speakers: [], simpleSegments: [], simpleSegmentRuns: [], bubbleRows: [] };
    }
    const names = Array.isArray(data.speakers) ? data.speakers : [];
    const hasDialogue = rows.some((row) => row[0] >= 0);
    let last;
    const simpleSegments = rows.map((row) => {
        const speaker = row[0] >= 0 ? names[row[0]] : undefined;
        const segment = { sentence: row[3] || '', startTime: row[1] ?? undefined, endTime: row[2] ?? undefined };
        if (hasDialogue) {
            segment.speakerId = speaker;
            segment.speaker = speaker;
            segment.color = row[0] >= 0 ? speakerColor(row[0]) : 'speaker-color-1';
            segment.showSpeaker = speaker !== last;
            last = speaker;
        }
        return segment;
    });
    const transcript = {
        isJson: true,
        hasDialogue,
        content: hasDialogue ? '' : simpleSegments.map((segment) => segment.sentence).join('\n'),
        speakers: hasDialogue ? names.map((name, index) => ({ name, color: speakerColor(index) })) : [],
        simpleSegments,
    };
    const lazy = (key, build) => Object.defineProperty(transcript, key, {
        configurable: true,
        enumerable: true,
        get() {
            const value = hasDialogue ? build(simpleSegments) : [];
            Object.defineProperty(transcript, key, { value, enumerable: true });
            return value;
        },
    });
    lazy('simpleSegmentRuns', segmentRuns);
    lazy('bubbleRows', bubbleRows);
    return transcript;
}

/** The transcript as text for the clipboard: one line per segment, "[Speaker]: text" with speakers. */
export function transcriptText(transcript) {
    if (!transcript) return '';
    if (!transcript.isJson) return transcript.content || '';
    return transcript.simpleSegments
        .map((segment) => (segment.speaker ? `[${segment.speaker}]: ${segment.sentence}` : segment.sentence))
        .join('\n');
}
