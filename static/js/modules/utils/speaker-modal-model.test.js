import { describe, it, expect } from 'vitest';
import {
    parseModalSegments, computeSpeakerStats, orderSpeakers, splitMinorSpeakers,
    buildNameOptions, findSavedSpelling, playingSegmentPosition, turnStarts,
    formatClock, mergeSpeakerSegments,
} from './speaker-modal-model.js';

const seg = (speaker, start, end, sentence = 'x') => ({ speaker, start_time: start, end_time: end, sentence });

// A large-room recording: two real speakers and three diarization fragments.
const ROOM = JSON.stringify([
    seg('SPEAKER_00', 0, 40),
    seg('SPEAKER_03', 40, 42),
    seg('SPEAKER_01', 42, 100),
    seg('SPEAKER_01', 100, 130),
    seg('SPEAKER_04', 130, 131),
    seg('SPEAKER_00', 131, 200),
    seg('SPEAKER_02', 200, 205),
]);

describe('parseModalSegments', () => {
    it('keeps the stored index and normalizes times', () => {
        const { isJson, segments } = parseModalSegments(JSON.stringify([
            { speaker: 'A', sentence: 'hi', startTime: 0, endTime: 1.5 },
            { speaker: 'B', sentence: 'yo', start_time: 1.5 },
        ]));
        expect(isJson).toBe(true);
        expect(segments[0]).toEqual({ index: 0, speakerId: 'A', sentence: 'hi', start: 0, end: 1.5 });
        expect(segments[1].end).toBeNull();
    });

    it('returns plain text for non-JSON transcripts', () => {
        expect(parseModalSegments('[A]: hello')).toEqual({ isJson: false, segments: [], plainText: '[A]: hello' });
    });
});

describe('computeSpeakerStats', () => {
    const { segments } = parseModalSegments(ROOM);
    const stats = computeSpeakerStats(segments);

    it('totals seconds, turns and share per speaker', () => {
        expect(stats.hasTimes).toBe(true);
        expect(stats.bySpeaker.SPEAKER_00).toMatchObject({ seconds: 109, turns: 2, segments: 2, firstIndex: 0, longestIndex: 5 });
        expect(stats.bySpeaker.SPEAKER_01).toMatchObject({ seconds: 88, turns: 1, segments: 2 });
        expect(stats.bySpeaker.SPEAKER_00.share).toBe(53);
    });

    it('reports no times when any segment lacks them', () => {
        const s = computeSpeakerStats(parseModalSegments(JSON.stringify([seg('A', 0, 1), { speaker: 'B', sentence: 'x' }])).segments);
        expect(s.hasTimes).toBe(false);
        expect(s.bySpeaker.A.seconds).toBe(0);
    });
});

describe('orderSpeakers', () => {
    const { segments } = parseModalSegments(ROOM);
    const stats = computeSpeakerStats(segments);
    const ids = Object.keys(stats.bySpeaker);

    it('sorts by speaking time, most first', () => {
        expect(orderSpeakers(ids, stats, 'duration')).toEqual(['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_02', 'SPEAKER_03', 'SPEAKER_04']);
    });

    it('sorts by first appearance', () => {
        expect(orderSpeakers(ids, stats, 'appearance')).toEqual(['SPEAKER_00', 'SPEAKER_03', 'SPEAKER_01', 'SPEAKER_04', 'SPEAKER_02']);
    });

    it('sorts by typed name, unnamed last', () => {
        const names = { SPEAKER_01: 'Ana', SPEAKER_00: 'Zoe' };
        expect(orderSpeakers(ids, stats, 'name', id => names[id]).slice(0, 3)).toEqual(['SPEAKER_01', 'SPEAKER_00', 'SPEAKER_02']);
    });

    it('falls back to appearance for duration without times', () => {
        const noTimes = { hasTimes: false, bySpeaker: { B: { firstIndex: 0 }, A: { firstIndex: 1 } } };
        expect(orderSpeakers(['A', 'B'], noTimes, 'duration')).toEqual(['B', 'A']);
    });
});

describe('splitMinorSpeakers', () => {
    const { segments } = parseModalSegments(ROOM);
    const stats = computeSpeakerStats(segments);
    const ordered = orderSpeakers(Object.keys(stats.bySpeaker), stats, 'duration');

    it('moves speakers under the threshold into the minor group', () => {
        expect(splitMinorSpeakers(ordered, stats, 30)).toEqual({
            main: ['SPEAKER_00', 'SPEAKER_01'],
            minor: ['SPEAKER_02', 'SPEAKER_03', 'SPEAKER_04'],
        });
    });

    it('keeps named speakers in the main list', () => {
        const { minor } = splitMinorSpeakers(ordered, stats, 30, id => id === 'SPEAKER_03');
        expect(minor).toEqual(['SPEAKER_02', 'SPEAKER_04']);
    });

    it('does not split when fewer than two main speakers would remain', () => {
        const { main, minor } = splitMinorSpeakers(ordered, stats, 100);
        expect(minor).toEqual([]);
        expect(main).toEqual(ordered);
    });

    it('keeps speakers added by hand, who have no segments yet', () => {
        const { main } = splitMinorSpeakers([...ordered, 'SPEAKER_09'], stats, 30);
        expect(main).toContain('SPEAKER_09');
    });
});

describe('buildNameOptions', () => {
    const voice = [{ name: 'Jane Doe', similarity: 82 }];
    const saved = [{ name: 'jane doe', use_count: 9 }, { name: 'Bob', use_count: 5 }, { name: 'Ana', use_count: 2 }];

    it('puts voice matches first and drops case-insensitive duplicates', () => {
        expect(buildNameOptions({ voice, saved }).map(o => [o.name, o.source])).toEqual([
            ['Jane Doe', 'voice'], ['Bob', 'saved'], ['Ana', 'saved'],
        ]);
    });

    it('filters by the typed text', () => {
        expect(buildNameOptions({ voice, saved, query: 'an' }).map(o => o.name)).toEqual(['Jane Doe', 'Ana']);
    });

    it('sinks names already given to another speaker', () => {
        const opts = buildNameOptions({ saved, takenNames: ['BOB'] });
        expect(opts.map(o => o.name)).toEqual(['jane doe', 'Ana', 'Bob']);
        expect(opts[2].taken).toBe(true);
    });
});

describe('findSavedSpelling', () => {
    it('returns the saved spelling for a case or spacing variant', () => {
        expect(findSavedSpelling('john  smith', ['John Smith', 'Ana'])).toBe('John Smith');
    });
    it('returns null for an exact match or no match', () => {
        expect(findSavedSpelling('John Smith', ['John Smith'])).toBeNull();
        expect(findSavedSpelling('Joan', ['John Smith'])).toBeNull();
    });
});

describe('playingSegmentPosition', () => {
    const { segments } = parseModalSegments(ROOM);
    it('finds the segment containing the time', () => {
        expect(playingSegmentPosition(segments, 0)).toBe(0);
        expect(playingSegmentPosition(segments, 41)).toBe(1);
        expect(playingSegmentPosition(segments, 150)).toBe(5);
    });
    it('returns -1 past the end or without times', () => {
        expect(playingSegmentPosition(segments, 999)).toBe(-1);
        expect(playingSegmentPosition(segments, NaN)).toBe(-1);
    });
});

describe('turnStarts', () => {
    it('lists where each run of the speaker begins', () => {
        const { segments } = parseModalSegments(ROOM);
        expect(turnStarts(segments, 'SPEAKER_00')).toEqual([0, 5]);
        expect(turnStarts(segments, 'SPEAKER_01')).toEqual([2]);
    });
});

describe('formatClock', () => {
    it('formats minutes and hours', () => {
        expect(formatClock(0)).toBe('0:00');
        expect(formatClock(75.9)).toBe('1:15');
        expect(formatClock(3725)).toBe('1:02:05');
        expect(formatClock(null)).toBe('0:00');
    });
});

describe('mergeSpeakerSegments', () => {
    it('reassigns the source speakers without touching the input', () => {
        const input = JSON.parse(ROOM);
        const { data, moved } = mergeSpeakerSegments(input, ['SPEAKER_03', 'SPEAKER_04', 'SPEAKER_00'], 'SPEAKER_00');
        expect(moved).toBe(2);
        expect(data[1].speaker).toBe('SPEAKER_00');
        expect(data[4].speaker).toBe('SPEAKER_00');
        expect(input[1].speaker).toBe('SPEAKER_03');
    });
});
