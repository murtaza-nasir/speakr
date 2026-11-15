import { describe, expect, it } from 'vitest';
import { SPEAKER_COLOR_COUNT, bubbleRows, shareTranscript, transcriptText } from './share-transcript.js';
import { bubbleUnits, runUnits, segmentUnits } from './transcript-window.js';

const data = {
    speakers: ['Ana', 'Ben'],
    segments: [
        [0, 0, 2.5, 'Good morning.'],
        [0, 2.5, 4, 'Shall we start?'],
        [1, 4, 6, 'Yes.'],
        [0, 6, 9, 'The budget first.'],
    ],
};

describe('shareTranscript', () => {
    it('builds the segments of a transcript with speakers', () => {
        const transcript = shareTranscript(data);
        expect(transcript.isJson).toBe(true);
        expect(transcript.hasDialogue).toBe(true);
        expect(transcript.speakers).toEqual([{ name: 'Ana', color: 'speaker-color-1' }, { name: 'Ben', color: 'speaker-color-2' }]);
        expect(transcript.simpleSegments.map((s) => [s.speaker, s.startTime, s.endTime, s.sentence, s.color, s.showSpeaker])).toEqual([
            ['Ana', 0, 2.5, 'Good morning.', 'speaker-color-1', true],
            ['Ana', 2.5, 4, 'Shall we start?', 'speaker-color-1', false],
            ['Ben', 4, 6, 'Yes.', 'speaker-color-2', true],
            ['Ana', 6, 9, 'The budget first.', 'speaker-color-1', true],
        ]);
    });

    it('gives the views of the recording page their units', () => {
        const transcript = shareTranscript(data);
        const runs = runUnits(transcript.simpleSegmentRuns);
        expect(runs.map((run) => [run.speaker, run.first, run.count])).toEqual([['Ana', 0, 2], ['Ben', 2, 1], ['Ana', 3, 1]]);
        expect(runs[0].segments[1].originalIndex).toBe(1);
        const rows = bubbleUnits(transcript.bubbleRows);
        expect(rows.map((row) => row.bubbles.map((bubble) => bubble.index))).toEqual([[0, 1], [2], [3]]);
        expect(rows[1].bubbles[0]).toMatchObject({ sentence: 'Yes.', startTime: 4, color: 'speaker-color-2' });
    });

    it('builds the runs and the bubble rows once, when they are asked for', () => {
        const transcript = shareTranscript(data);
        expect(Object.getOwnPropertyDescriptor(transcript, 'bubbleRows').get).toBeTypeOf('function');
        const rows = transcript.bubbleRows;
        expect(transcript.bubbleRows).toBe(rows);
        expect(transcript.simpleSegmentRuns).toBe(transcript.simpleSegmentRuns);
    });

    it('repeats the colors after the sixteenth speaker, as the recording page does', () => {
        const speakers = Array.from({ length: SPEAKER_COLOR_COUNT + 1 }, (_, i) => `S${i}`);
        const transcript = shareTranscript({ speakers, segments: speakers.map((_, i) => [i, i, i + 1, 'x']) });
        expect(transcript.speakers[SPEAKER_COLOR_COUNT - 1].color).toBe('speaker-color-16');
        expect(transcript.speakers[SPEAKER_COLOR_COUNT].color).toBe('speaker-color-1');
    });

    it('treats a transcript without speakers as plain segments', () => {
        const transcript = shareTranscript({ speakers: [], segments: [[-1, 0, 2, 'One.'], [-1, 2, null, 'Two.']] });
        expect(transcript.hasDialogue).toBe(false);
        expect(transcript.content).toBe('One.\nTwo.');
        expect(transcript.simpleSegments).toEqual([
            { sentence: 'One.', startTime: 0, endTime: 2 },
            { sentence: 'Two.', startTime: 2, endTime: undefined },
        ]);
        expect(transcript.simpleSegmentRuns).toEqual([]);
        expect(segmentUnits(transcript.simpleSegments)).toHaveLength(2);
    });

    it('keeps a segment without a speaker among segments with speakers', () => {
        const transcript = shareTranscript({ speakers: ['Ana'], segments: [[0, 0, 1, 'Hi.'], [-1, 1, 2, '(noise)'], [0, 2, 3, 'Right.']] });
        expect(transcript.simpleSegments.map((s) => [s.speaker, s.showSpeaker])).toEqual([['Ana', true], [undefined, true], ['Ana', true]]);
        expect(transcript.simpleSegmentRuns).toHaveLength(3);
    });

    it('shows any other transcript as text', () => {
        expect(shareTranscript({ text: 'Just text.\nTwo lines.' })).toMatchObject({ isJson: false, hasDialogue: false, content: 'Just text.\nTwo lines.', simpleSegments: [] });
        expect(shareTranscript(null)).toMatchObject({ isJson: false, content: '' });
        expect(shareTranscript({})).toMatchObject({ isJson: false, content: '' });
    });
});

describe('bubbleRows', () => {
    it('marks the rows of a speaker called "me"', () => {
        const transcript = shareTranscript({ speakers: ['Me', 'Ben'], segments: [[0, 0, 1, 'Hi.'], [1, 1, 2, 'Hello.']] });
        expect(bubbleRows(transcript.simpleSegments).map((row) => row.isMe)).toEqual([true, false]);
    });
});

describe('transcriptText', () => {
    it('writes one line per segment, with the speaker when there is one', () => {
        expect(transcriptText(shareTranscript(data))).toBe('[Ana]: Good morning.\n[Ana]: Shall we start?\n[Ben]: Yes.\n[Ana]: The budget first.');
        expect(transcriptText(shareTranscript({ speakers: [], segments: [[-1, 0, 1, 'One.'], [-1, 1, 2, 'Two.']] }))).toBe('One.\nTwo.');
    });

    it('is the text itself for a transcript that is not stored as segments', () => {
        expect(transcriptText(shareTranscript({ text: 'Just text.' }))).toBe('Just text.');
        expect(transcriptText(null)).toBe('');
    });
});
