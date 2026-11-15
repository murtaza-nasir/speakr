import { describe, expect, it } from 'vitest';

import {
    bestSuggestion, buildSpeakerOptions, formatClock, formatExact, isPlaceholderSpeaker, isTypingTarget,
    longestSegments, nextSelection, nextSpeakerLabel, parseTime, speakerColorClass,
} from './helpers.js';

describe('the label of an added speaker', () => {
    it('follows the highest SPEAKER_NN in use', () => {
        expect(nextSpeakerLabel(['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_02', 'SPEAKER_03'])).toBe('SPEAKER_04');
        expect(nextSpeakerLabel(['SPEAKER_00'])).toBe('SPEAKER_01');
        // A gap is not filled: the label that is missing may have been renamed
        expect(nextSpeakerLabel(['SPEAKER_00', 'SPEAKER_05'])).toBe('SPEAKER_06');
        expect(nextSpeakerLabel(['Dana Lee', 'SPEAKER_01'])).toBe('SPEAKER_02');
        expect(nextSpeakerLabel(['SPEAKER_09'])).toBe('SPEAKER_10');
        expect(nextSpeakerLabel(['SPEAKER_99'])).toBe('SPEAKER_100');
    });

    it('reads the labels in any case and with any number of digits', () => {
        expect(nextSpeakerLabel(['speaker_03', 'Speaker_1'])).toBe('SPEAKER_04');
        expect(nextSpeakerLabel(['SPEAKER_7'])).toBe('SPEAKER_08');
        expect(nextSpeakerLabel(['SPEAKER_007'])).toBe('SPEAKER_08');
    });

    it('is the number of speakers when no label is in use', () => {
        expect(nextSpeakerLabel([])).toBe('SPEAKER_00');
        expect(nextSpeakerLabel(['Alice', 'Bob'])).toBe('SPEAKER_02');
        expect(nextSpeakerLabel(['Alice', 'Alice', '', null, ' Bob '])).toBe('SPEAKER_02');
        // Names that only look like labels are not labels
        expect(nextSpeakerLabel(['Speaker 1', 'SPEAKER_A'])).toBe('SPEAKER_02');
    });

    it('never takes a reserved label', () => {
        expect(nextSpeakerLabel(['SPEAKER_00', 'Dana'], ['SPEAKER_01', 'SPEAKER_02'])).toBe('SPEAKER_03');
        expect(nextSpeakerLabel(['Alice'], ['speaker_01'])).toBe('SPEAKER_02');
        expect(nextSpeakerLabel(['Alice', 'Bob'], ['Carol'])).toBe('SPEAKER_02');
    });
});

describe('times as text', () => {
    it('shows a compact time', () => {
        expect(formatClock(38.9)).toBe('0:38');
        expect(formatClock(725)).toBe('12:05');
        expect(formatClock(3723)).toBe('1:02:03');
        expect(formatClock(0)).toBe('0:00');
        expect(formatClock(null)).toBe('–:––');
    });

    it('shows an exact time', () => {
        expect(formatExact(38.25)).toBe('0:38.250');
        expect(formatExact(3723.001)).toBe('1:02:03.001');
        expect(formatExact(59.9996)).toBe('1:00.000');
        expect(formatExact(undefined)).toBe('');
    });

    it('reads typed times', () => {
        expect(parseTime('38')).toBe(38);
        expect(parseTime('38.5')).toBe(38.5);
        expect(parseTime('38,5')).toBe(38.5);
        expect(parseTime('0:38.25')).toBe(38.25);
        expect(parseTime('1:02:03')).toBe(3723);
        expect(parseTime(' 12:05 ')).toBe(725);
        expect(parseTime('90s')).toBe(90);
        expect(parseTime(formatExact(4521.375))).toBe(4521.375);
    });

    it('rejects what is not a time', () => {
        for (const text of ['', 'abc', '1:75', '-3', '1:2:3:4', '1::2', null]) expect(parseTime(text)).toBeNull();
    });
});

describe('speaker picker options', () => {
    const base = {
        recording: ['SPEAKER_00', 'Dana Lee'],
        saved: [{ id: 1, name: 'Dana Lee' }, { id: 2, name: 'Kim Roe' }, { id: 3, name: 'Alex Poe' }],
        voice: [{ speaker_id: 3, name: 'Alex Poe', similarity: 71 }, { speaker_id: 2, name: 'Kim Roe', similarity: 87 }],
    };

    it('lists the recording first, then voice matches, then the other saved speakers', () => {
        expect(buildSpeakerOptions(base)).toEqual([
            { kind: 'recording', name: 'SPEAKER_00', key: 1 },
            { kind: 'recording', name: 'Dana Lee', key: 2 },
            { kind: 'voice', name: 'Kim Roe', id: 2, similarity: 87 },
            { kind: 'voice', name: 'Alex Poe', id: 3, similarity: 71 },
        ]);
    });

    it('filters by what is typed and offers a new name last', () => {
        expect(buildSpeakerOptions({ ...base, query: 'ro' })).toEqual([
            { kind: 'voice', name: 'Kim Roe', id: 2, similarity: 87 },
            { kind: 'new', name: 'ro' },
        ]);
        expect(buildSpeakerOptions({ ...base, query: 'dana lee' }).map((o) => o.kind)).toEqual(['recording']);
    });

    it('gives number keys to the first nine speakers only', () => {
        const recording = Array.from({ length: 11 }, (_, i) => `S${i}`);
        const keys = buildSpeakerOptions({ recording }).map((o) => o.key);
        expect(keys).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, null, null]);
    });

    it('lists saved speakers without a voice match', () => {
        expect(buildSpeakerOptions({ recording: [], saved: base.saved }).map((o) => `${o.kind}:${o.name}`))
            .toEqual(['saved:Dana Lee', 'saved:Kim Roe', 'saved:Alex Poe']);
        expect(buildSpeakerOptions()).toEqual([]);
    });
});

describe('voice-match suggestions', () => {
    const suggestions = { SPEAKER_00: [{ speaker_id: 2, name: 'Dana Lee', similarity: 87 }, { speaker_id: 3, name: 'Kim', similarity: 60 }], Dana: [{ name: 'X', similarity: 99 }] };

    it('offers the best match for an unnamed speaker', () => {
        expect(bestSuggestion(suggestions, 'SPEAKER_00', ['SPEAKER_00', 'SPEAKER_01'])).toMatchObject({ name: 'Dana Lee', similarity: 87 });
    });

    it('offers nothing for a named speaker or an unknown label', () => {
        expect(bestSuggestion(suggestions, 'Dana', [])).toBeNull();
        expect(bestSuggestion(suggestions, 'SPEAKER_01', [])).toBeNull();
        expect(bestSuggestion(null, 'SPEAKER_00', [])).toBeNull();
    });

    it('offers a match as a merge when the recording already has a speaker of that name', () => {
        expect(bestSuggestion(suggestions, 'SPEAKER_00', ['SPEAKER_00', 'SPEAKER_01'])).toMatchObject({ merge: false, same: [] });
        expect(bestSuggestion(suggestions, 'SPEAKER_00', ['Dana Lee', 'SPEAKER_00'])).toMatchObject({ name: 'Dana Lee', similarity: 87, merge: true, same: ['Dana Lee'] });
        // Whatever the spelling in the recording
        expect(bestSuggestion(suggestions, 'SPEAKER_00', ['dana lee', 'SPEAKER_00', 'DANA LEE'])).toMatchObject({ merge: true, same: ['dana lee', 'DANA LEE'] });
    });

    it('offers one person to two labels, and keeps the second offer after the first was accepted', () => {
        const both = { SPEAKER_00: [{ speaker_id: 4, name: 'Dana Lee', similarity: 87 }], SPEAKER_01: [{ speaker_id: 4, name: 'Dana Lee', similarity: 83 }] };
        expect(bestSuggestion(both, 'SPEAKER_00', ['SPEAKER_00', 'SPEAKER_01']).merge).toBe(false);
        expect(bestSuggestion(both, 'SPEAKER_01', ['SPEAKER_00', 'SPEAKER_01']).merge).toBe(false);
        // SPEAKER_00 was accepted: the recording now has Dana Lee and SPEAKER_01
        expect(bestSuggestion(both, 'SPEAKER_01', ['Dana Lee', 'SPEAKER_01'])).toMatchObject({ name: 'Dana Lee', similarity: 83, merge: true });
    });

    it('does not list a merged speaker twice in the picker', () => {
        const options = buildSpeakerOptions({
            recording: ['Dana Lee', 'SPEAKER_01'], saved: [{ id: 4, name: 'Dana Lee' }],
            voice: [{ speaker_id: 4, name: 'Dana Lee', similarity: 83 }],
        });
        expect(options.map((o) => [o.kind, o.name])).toEqual([['recording', 'Dana Lee'], ['recording', 'SPEAKER_01']]);
    });

    it('knows a diarization label', () => {
        expect(isPlaceholderSpeaker('SPEAKER_07')).toBe(true);
        expect(isPlaceholderSpeaker('speaker_7')).toBe(true);
        expect(isPlaceholderSpeaker('Speaker 1')).toBe(false);
        expect(isPlaceholderSpeaker('')).toBe(false);
    });

    it('picks the longest segments of the speaker, in time order, cut short', () => {
        const segments = [
            { speaker: 'A', start_time: 0, end_time: 2 },
            { speaker: 'B', start_time: 2, end_time: 30 },
            { speaker: 'A', start_time: 30, end_time: 40 },
            { speaker: 'A', start_time: 40, end_time: 43 },
            { speaker: 'A', start_time: 50, end_time: 56 },
            { speaker: 'A', start_time: 60, end_time: 60.1 },
            { speaker: 'A', start_time: null, end_time: 70 },
        ];
        expect(longestSegments(segments, 'A')).toEqual([{ start: 30, end: 34 }, { start: 40, end: 43 }, { start: 50, end: 54 }]);
        expect(longestSegments(segments, 'A', { count: 1, maxSeconds: 20 })).toEqual([{ start: 30, end: 40 }]);
        expect(longestSegments(segments, 'C')).toEqual([]);
    });
});

describe('speaker colours', () => {
    it('keeps a colour once given and takes the next one for a new name', () => {
        const map = { A: 'speaker-color-1', B: 'speaker-color-2' };
        expect(speakerColorClass(map, 'A')).toBe('speaker-color-1');
        expect(speakerColorClass(map, 'C')).toBe('speaker-color-3');
        expect(speakerColorClass(map, 'C')).toBe('speaker-color-3');
        expect(speakerColorClass(map, '')).toBe('speaker-color-1');
        const full = {};
        for (let i = 0; i < 16; i++) speakerColorClass(full, `S${i}`);
        expect(speakerColorClass(full, 'next')).toBe('speaker-color-1');
    });
});

describe('typing targets', () => {
    it('tells text fields from buttons and sliders', () => {
        expect(isTypingTarget({ tagName: 'TEXTAREA' })).toBe(true);
        expect(isTypingTarget({ tagName: 'INPUT', type: 'text' })).toBe(true);
        expect(isTypingTarget({ tagName: 'INPUT' })).toBe(true);
        expect(isTypingTarget({ tagName: 'INPUT', type: 'range' })).toBe(false);
        expect(isTypingTarget({ tagName: 'INPUT', type: 'checkbox' })).toBe(false);
        expect(isTypingTarget({ tagName: 'BUTTON' })).toBe(false);
        expect(isTypingTarget({ tagName: 'DIV', isContentEditable: true })).toBe(true);
        expect(isTypingTarget(null)).toBe(false);
    });
});

describe('selection', () => {
    const ids = [10, 11, 12, 13, 14];
    const list = (r) => [...r.selected].sort();

    it('selects one row on a plain click', () => {
        const r = nextSelection(ids, new Set([10, 11]), 10, 3);
        expect(list(r)).toEqual([13]);
        expect(r.anchor).toBe(13);
    });

    it('selects a range with Shift, from the anchor', () => {
        const r = nextSelection(ids, new Set([11]), 11, 3, { shift: true });
        expect(list(r)).toEqual([11, 12, 13]);
        expect(r.anchor).toBe(11);
        expect(list(nextSelection(ids, new Set([13]), 13, 0, { shift: true }))).toEqual([10, 11, 12, 13]);
    });

    it('toggles a row with Ctrl or Cmd', () => {
        const added = nextSelection(ids, new Set([10]), 10, 2, { toggle: true });
        expect(list(added)).toEqual([10, 12]);
        expect(list(nextSelection(ids, added.selected, added.anchor, 0, { toggle: true }))).toEqual([12]);
    });

    it('adds a range to the selection with Ctrl+Shift', () => {
        const r = nextSelection(ids, new Set([10]), 12, 4, { shift: true, toggle: true });
        expect(list(r)).toEqual([10, 12, 13, 14]);
    });

    it('falls back to a plain click when there is no anchor', () => {
        expect(list(nextSelection(ids, new Set(), null, 2, { shift: true }))).toEqual([12]);
        expect(nextSelection(ids, new Set([10]), 10, 9).selected.has(10)).toBe(true);
    });
});
