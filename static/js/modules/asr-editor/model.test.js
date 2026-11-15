import { describe, expect, it } from 'vitest';

import { createAsrModel, PROBLEM, segmentToJSON, splitTime } from './model.js';

const seg = (speaker, start, end, sentence = `${speaker} ${start}`) => ({ speaker, sentence, start_time: start, end_time: end });

const sample = () => [
    seg('A', 0, 2, 'Good morning everyone'),
    seg('B', 2, 5, 'Morning. Shall we start'),
    seg('A', 5, 9, 'Yes, the first item is the budget'),
    seg('B', 9, 12, 'I have the numbers here'),
];

const texts = (m) => m.segments.map((s) => s.sentence);
const starts = (m) => m.segments.map((s) => s.start_time);
const state = (m) => JSON.stringify(m.toJSON());

function isSorted(m) {
    let last = -Infinity;
    for (const s of m.segments) {
        if (!Number.isFinite(s.start_time)) continue;
        if (s.start_time < last) return false;
        last = s.start_time;
    }
    return true;
}

// Problems as a freshly loaded model reports them: the reference for the
// incremental recheck.
function problemsFromScratch(m) {
    const fresh = createAsrModel(m.toJSON());
    return fresh.segments.map((_, i) => fresh.problemAt(i));
}
const problemsNow = (m) => m.segments.map((_, i) => m.problemAt(i));

// An operation, its undo and its redo: the state returns exactly.
function roundTrip(m, operation) {
    const before = state(m);
    const result = operation();
    const after = state(m);
    expect(after).not.toBe(before);
    expect(m.undo()).not.toBeNull();
    expect(state(m)).toBe(before);
    expect(problemsNow(m)).toEqual(problemsFromScratch(m));
    expect(m.redo()).not.toBeNull();
    expect(state(m)).toBe(after);
    expect(problemsNow(m)).toEqual(problemsFromScratch(m));
    expect(isSorted(m)).toBe(true);
    return result;
}

describe('loading', () => {
    it('keeps the stored fields and gives each segment an id', () => {
        const m = createAsrModel([{ ...seg('A', 0, 1), speaker_id: 7, words: [1], confidence: 0.9 }]);
        expect(m.length).toBe(1);
        expect(m.get(0).id).toBe(1);
        expect(m.toJSON()).toEqual([{ speaker: 'A', sentence: 'A 0', start_time: 0, end_time: 1, speaker_id: 7, words: [1], confidence: 0.9 }]);
        expect(m.dirty).toBe(false);
        expect(m.canUndo).toBe(false);
    });

    it('sorts by start time and keeps the order of equal starts', () => {
        const m = createAsrModel([seg('A', 5, 6, 'c'), seg('A', 1, 2, 'a'), seg('B', 1, 2, 'b'), seg('A', 9, 10, 'd')]);
        expect(texts(m)).toEqual(['a', 'b', 'c', 'd']);
    });

    it('leaves a segment without a start behind the segment it followed', () => {
        const m = createAsrModel([seg('A', 4, 5, 'x'), { speaker: 'A', sentence: 'no time' }, seg('A', 1, 2, 'first')]);
        expect(texts(m)).toEqual(['first', 'x', 'no time']);
        expect(m.problemAt(2) & PROBLEM.DURATION).toBeTruthy();
    });

    it('accepts an empty or missing transcript', () => {
        expect(createAsrModel([]).length).toBe(0);
        expect(createAsrModel(null).length).toBe(0);
        expect(createAsrModel([]).nextProblem()).toBe(-1);
    });
});

describe('text', () => {
    it('sets the text, and undo restores it', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.setText(1, 'Morning. Let us start', { burst: false }));
        expect(m.get(1).sentence).toBe('Morning. Let us start');
        expect(m.setText(1, 'Morning. Let us start')).toBe(false);
        expect(m.setText(99, 'x')).toBe(false);
    });

    it('groups a burst of typing into one undo step', () => {
        let clock = 0;
        const m = createAsrModel(sample(), { now: () => clock });
        for (const text of ['G', 'Go', 'Goo', 'Good']) {
            clock += 100;
            m.setText(0, text);
        }
        expect(m.undoDepth).toBe(1);
        m.undo();
        expect(m.get(0).sentence).toBe('Good morning everyone');
        m.redo();
        expect(m.get(0).sentence).toBe('Good');
    });

    it('starts a new step after a pause, another segment, or endBurst', () => {
        let clock = 0;
        const m = createAsrModel(sample(), { now: () => clock });
        m.setText(0, 'a');
        clock += 5000;
        m.setText(0, 'ab');
        expect(m.undoDepth).toBe(2);
        m.setText(1, 'x');
        m.setText(0, 'abc');
        expect(m.undoDepth).toBe(4);
        m.endBurst();
        m.setText(0, 'abcd');
        expect(m.undoDepth).toBe(5);
    });

    it('does not join typing to a step that was saved', () => {
        let clock = 0;
        const m = createAsrModel(sample(), { now: () => clock });
        m.setText(0, 'a');
        m.markSaved();
        m.setText(0, 'ab');
        expect(m.undoDepth).toBe(2);
        expect(m.dirty).toBe(true);
    });
});

describe('times', () => {
    it('sets a start and an end', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.setStart(1, 2.5));
        roundTrip(m, () => m.setEnd(1, 4.25));
        expect([m.get(1).start_time, m.get(1).end_time]).toEqual([2.5, 4.25]);
    });

    it('rounds to milliseconds and does not go below zero', () => {
        const m = createAsrModel(sample());
        m.setStart(0, -3);
        expect(m.get(0).start_time).toBe(0);
        m.setEnd(0, 1.23456);
        expect(m.get(0).end_time).toBe(1.235);
    });

    it('moves the touching neighbour when asked', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.moveEdge(1, 'start', 1.5, { linked: true }));
        expect(m.get(0).end_time).toBe(1.5);
        roundTrip(m, () => m.moveEdge(1, 'end', 5.5, { linked: true }));
        expect(m.get(2).start_time).toBe(5.5);
        expect(m.undoDepth).toBe(2);
    });

    it('leaves the neighbour alone when not asked, or when it does not touch', () => {
        const m = createAsrModel([seg('A', 0, 1), seg('B', 3, 4)]);
        m.moveEdge(1, 'start', 2, { linked: true });
        expect(m.get(0).end_time).toBe(1);
        const n = createAsrModel(sample());
        n.moveEdge(1, 'start', 1.5);
        expect(n.get(0).end_time).toBe(2);
    });

    it('moves a segment to where its new start belongs', () => {
        const m = createAsrModel(sample());
        const index = roundTrip(m, () => m.setStart(0, 9.5));
        expect(index).toBe(3);
        expect(starts(m)).toEqual([2, 5, 9, 9.5]);
        const back = roundTrip(m, () => m.setStart(3, 1));
        expect(back).toBe(0);
        expect(starts(m)).toEqual([1, 2, 5, 9]);
    });

    it('moves a whole segment, keeping its length', () => {
        const m = createAsrModel(sample());
        const index = roundTrip(m, () => m.move(1, 20));
        expect(index).toBe(3);
        expect([m.get(3).start_time, m.get(3).end_time]).toEqual([20, 23]);
    });

    it('sets both times in one step', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.setTimes(2, 5.5, 8));
        expect(m.undoDepth).toBe(1);
    });

    it('groups repeated nudges of one edge', () => {
        let clock = 0;
        const m = createAsrModel(sample(), { now: () => clock });
        m.moveEdge(1, 'start', 2.1, { burst: true });
        clock += 200;
        m.moveEdge(1, 'start', 2.2, { burst: true });
        expect(m.undoDepth).toBe(1);
        m.undo();
        expect(m.get(1).start_time).toBe(2);
    });

    it('rejects a time that is not a number', () => {
        const m = createAsrModel(sample());
        expect(m.setStart(0, NaN)).toBe(-1);
        expect(m.moveEdge(0, 'middle', 1)).toBe(-1);
        expect(m.canUndo).toBe(false);
    });
});

describe('split', () => {
    it('cuts the text at the cursor and the time by the share of characters', () => {
        const m = createAsrModel([seg('A', 10, 20, 'aaaa bbbbbb')]);
        const index = roundTrip(m, () => m.split(0, 5));
        expect(index).toBe(1);
        expect(texts(m)).toEqual(['aaaa', 'bbbbbb']);
        // 5 of 11 characters of 10 seconds
        expect(m.get(0).end_time).toBeCloseTo(10 + (10 * 5) / 11, 3);
        expect(m.get(1).start_time).toBe(m.get(0).end_time);
        expect(m.get(1).end_time).toBe(20);
        expect(m.get(1).speaker).toBe('A');
    });

    it('cuts at the playhead when it lies inside the segment', () => {
        const m = createAsrModel([seg('A', 10, 20, 'aaaa bbbbbb')]);
        m.split(0, 5, { time: 17.25 });
        expect(m.get(0).end_time).toBe(17.25);
        const n = createAsrModel([seg('A', 10, 20, 'aaaa bbbbbb')]);
        n.split(0, 5, { time: 42 });
        expect(n.get(0).end_time).toBeCloseTo(14.545, 3);
    });

    it('uses word timings when they are supplied', () => {
        const s = { sentence: 'one two three', start_time: 0, end_time: 9 };
        const words = [{ word: 'one', start: 0, end: 1 }, { word: 'two', start: 4, end: 5 }, { word: 'three', start: 7, end: 8 }];
        expect(splitTime(s, 4, { words })).toBe(4);
        expect(splitTime(s, 5, { words })).toBe(7);
        expect(splitTime(s, 4, { words, time: 2.5 })).toBe(2.5);
    });

    it('keeps time on both sides when the cut is at either end', () => {
        const s = { sentence: 'abcdefghij', start_time: 0, end_time: 10 };
        expect(splitTime(s, 0)).toBe(0.5);
        expect(splitTime(s, 10)).toBe(9.5);
        expect(splitTime({ sentence: '', start_time: 2, end_time: 4 }, 0)).toBe(3);
    });

    it('copies the speaker link and the other fields, without word lists', () => {
        const m = createAsrModel([{ ...seg('Dana', 0, 4, 'one two'), speaker_id: 3, words: ['w'], lang: 'en' }]);
        m.split(0, 3);
        expect(m.toJSON()[1]).toEqual({ speaker: 'Dana', sentence: 'two', start_time: m.get(0).end_time, end_time: 4, speaker_id: 3, lang: 'en' });
    });

    it('places the second half after a segment of another speaker that starts before the cut', () => {
        const m = createAsrModel([seg('A', 0, 10, 'aaaaa bbbbb'), seg('B', 2, 3, 'yes')]);
        const index = roundTrip(m, () => m.split(0, 5));
        expect(index).toBe(2);
        expect(texts(m)).toEqual(['aaaaa', 'yes', 'bbbbb']);
    });

    it('returns -1 for a segment that does not exist', () => {
        expect(createAsrModel(sample()).split(9, 1)).toBe(-1);
    });
});

describe('merge', () => {
    it('merges with the previous segment and keeps its speaker', () => {
        const m = createAsrModel(sample());
        const result = roundTrip(m, () => m.mergeWithPrevious(1));
        expect(result).toEqual({ index: 0, caret: 'Good morning everyone '.length });
        expect(m.get(0)).toMatchObject({ speaker: 'A', sentence: 'Good morning everyone Morning. Shall we start', start_time: 0, end_time: 5 });
        expect(m.length).toBe(3);
    });

    it('merges with the next segment', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.mergeWithNext(2));
        expect(m.get(2)).toMatchObject({ speaker: 'A', end_time: 12 });
    });

    it('merges several segments in one step', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.merge([2, 0, 1]));
        expect(m.length).toBe(2);
        expect(m.get(0).end_time).toBe(9);
        expect(m.undoDepth).toBe(1);
    });

    it('is the inverse of a split', () => {
        const m = createAsrModel([seg('A', 10, 20, 'aaaa bbbbbb')]);
        m.split(0, 5);
        m.merge([0, 1]);
        expect(m.toJSON()).toEqual([seg('A', 10, 20, 'aaaa bbbbbb')]);
    });

    it('skips empty texts and needs two segments', () => {
        const m = createAsrModel([seg('A', 0, 1, 'one'), seg('A', 1, 2, '  '), seg('A', 2, 3, 'two')]);
        m.merge([0, 1, 2]);
        expect(m.get(0).sentence).toBe('one two');
        expect(m.merge([0])).toBeNull();
        expect(m.mergeWithPrevious(0)).toBeNull();
        expect(m.mergeWithNext(0)).toBeNull();
    });
});

describe('retime (what a drag on the timeline commits)', () => {
    it('moves an edge and the neighbour that followed it in one step', () => {
        const m = createAsrModel(sample());
        const [a, b] = [m.get(0).id, m.get(2).id];
        const ok = roundTrip(m, () => m.retime([{ id: a, start: 0, end: 3.5 }, { id: b, start: 3.5, end: 9 }]));
        expect(ok).toBe(true);
        expect(m.undoDepth).toBe(1);
        expect([m.get(0).end_time, m.get(2).start_time]).toEqual([3.5, 3.5]);
        m.undo();
        expect([m.get(0).end_time, m.get(2).start_time]).toEqual([2, 5]);
        expect(m.dirty).toBe(false);
    });

    it('moves a segment in time and to another speaker, keeping the order sorted', () => {
        const m = createAsrModel(sample());
        const id = m.get(0).id;
        const before = state(m);
        expect(m.retime([{ id, start: 9.5, end: 11.5, speaker: 'B' }], { label: 'speaker' })).toBe(true);
        expect(isSorted(m)).toBe(true);
        expect(m.indexOfId(id)).toBe(3);
        expect(m.get(3)).toMatchObject({ speaker: 'B', start_time: 9.5, end_time: 11.5, sentence: 'Good morning everyone' });
        // B 9-12 and B 9.5-11.5 overlap now
        expect(problemsNow(m)).toEqual(problemsFromScratch(m));
        expect(m.problemAt(3) & PROBLEM.OVERLAP).toBeTruthy();
        expect(m.undoDepth).toBe(1);
        m.undo();
        expect(state(m)).toBe(before);
        expect(m.problemCount).toBe(0);
        m.redo();
        expect(m.get(3).speaker).toBe('B');
    });

    it('keeps or sets the link to a saved speaker', () => {
        const m = createAsrModel([{ ...seg('Dana', 0, 2), speaker_id: 7 }, seg('SPEAKER_01', 2, 4)]);
        const [a, b] = [m.get(0).id, m.get(1).id];
        m.retime([{ id: a, start: 0.5, end: 2 }]);
        expect(m.toJSON()[0].speaker_id).toBe(7);                // times only: the link stays
        m.retime([{ id: b, start: 2, end: 4, speaker: 'Dana', speakerId: 7 }]);
        expect(m.toJSON()[1]).toMatchObject({ speaker: 'Dana', speaker_id: 7 });
        m.retime([{ id: a, start: 0.5, end: 2, speaker: 'SPEAKER_01' }]);
        expect(m.toJSON()[0].speaker_id).toBeUndefined();
        m.undo();
        expect(m.toJSON()[0].speaker_id).toBe(7);
    });

    it('rounds to milliseconds, never goes below zero, and reports the change once', () => {
        const m = createAsrModel(sample());
        const events = [];
        m.onChange((event) => events.push(event));
        const id = m.get(1).id;
        m.retime([{ id, start: -1, end: 4.00049 }]);
        expect(m.get(m.indexOfId(id))).toMatchObject({ start_time: 0, end_time: 4 });
        expect(events).toHaveLength(1);
        expect(events[0]).toMatchObject({ type: 'time', source: 'do' });
        expect(events[0].ids).toContain(id);
    });

    it('does nothing for unknown ids, missing times and unchanged values', () => {
        const m = createAsrModel(sample());
        const id = m.get(0).id;
        expect(m.retime([{ id: 999, start: 1, end: 2 }])).toBe(false);
        expect(m.retime([{ id, start: NaN, end: 2 }])).toBe(false);
        expect(m.retime([{ id, start: 0, end: 2, speaker: 'A' }])).toBe(false);
        expect(m.retime([])).toBe(false);
        expect(m.canUndo).toBe(false);
    });

    it('is replayed from a draft', () => {
        const m = createAsrModel(sample());
        m.retime([{ id: m.get(0).id, start: 5.5, end: 7, speaker: 'B' }]);
        const fresh = createAsrModel(sample());
        fresh.importLog(JSON.parse(JSON.stringify(m.exportLog())));
        expect(state(fresh)).toBe(state(m));
    });
});

describe('add and remove', () => {
    it('adds a segment at its place', () => {
        const m = createAsrModel(sample());
        const index = roundTrip(m, () => m.add(6, 7, 'C'));
        expect(index).toBe(3);
        expect(m.get(3)).toMatchObject({ speaker: 'C', sentence: '', start_time: 6, end_time: 7 });
        expect(roundTrip(m, () => m.add(100, 101, 'A'))).toBe(5);
        expect(roundTrip(m, () => m.add(0, 1, 'A'))).toBe(1);
    });

    it('removes segments, and undo puts them back in place', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.remove([3, 1]));
        expect(texts(m)).toEqual(['Good morning everyone', 'Yes, the first item is the budget']);
        expect(m.remove([])).toBe(false);
        expect(m.remove([42])).toBe(false);
    });

    it('gives new segments ids that are not reused', () => {
        const m = createAsrModel(sample());
        const a = m.get(m.add(1, 2, 'A')).id;
        m.undo();
        const b = m.get(m.add(1, 2, 'A')).id;
        expect(b).not.toBe(a);
        expect(new Set(m.segments.map((s) => s.id)).size).toBe(m.length);
    });
});

describe('speakers', () => {
    it('sets the speaker of several segments', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.setSpeaker([0, 1, 3], 'Dana'));
        expect(m.segments.map((s) => s.speaker)).toEqual(['Dana', 'Dana', 'A', 'Dana']);
        expect(m.speakers()).toEqual(['Dana', 'A']);
        expect(m.firstIdOfSpeaker('A')).toBe(m.get(2).id);
        expect(m.firstIdOfSpeaker('Dana')).toBe(m.get(0).id);
        expect(m.firstIdOfSpeaker('nobody')).toBeNull();
        expect(m.setSpeaker([0], 'Dana')).toBe(false);
        expect(m.setSpeaker([0], '  ')).toBe(false);
    });

    it('drops the link to a saved speaker when the name changes, and undo restores it', () => {
        const m = createAsrModel([{ ...seg('Dana', 0, 1), speaker_id: 3 }]);
        roundTrip(m, () => m.setSpeaker([0], 'Lee'));
        expect(m.toJSON()[0].speaker_id).toBeUndefined();
        m.undo();
        expect(m.toJSON()[0].speaker_id).toBe(3);
        m.setSpeaker([0], 'Kim', { speakerId: 9 });
        expect(m.toJSON()[0]).toMatchObject({ speaker: 'Kim', speaker_id: 9 });
    });

    it('renames a speaker in the whole recording as one step', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.renameSpeaker('A', 'Dana Lee'));
        expect(m.segments.map((s) => s.speaker)).toEqual(['Dana Lee', 'B', 'Dana Lee', 'B']);
        expect(m.undoDepth).toBe(1);
        expect(m.renameSpeaker('nobody', 'x')).toBe(false);
    });

    it('undoes a rename onto an existing name without mixing the two up', () => {
        const m = createAsrModel(sample());
        m.renameSpeaker('A', 'B');
        expect(m.speakers()).toEqual(['B']);
        m.undo();
        expect(m.segments.map((s) => s.speaker)).toEqual(['A', 'B', 'A', 'B']);
    });

    it('reports accepted voice matches until they are saved or undone', () => {
        const m = createAsrModel(sample());
        m.renameSpeaker('A', 'Dana Lee', { speakerId: 4, match: { label: 'A', name: 'Dana Lee', speakerId: 4 } });
        expect(m.acceptedMatches()).toEqual([{ label: 'A', name: 'Dana Lee', speakerId: 4, ids: [1, 3] }]);
        m.undo();
        expect(m.acceptedMatches()).toEqual([]);
        m.redo();
        m.markSaved();
        expect(m.acceptedMatches()).toEqual([]);
    });

    it('takes the names and links the server stored, outside the undo log', () => {
        const m = createAsrModel(sample());
        m.renameSpeaker('A', 'dana');
        const stored = m.toJSON().map((s) => (s.speaker === 'dana' ? { ...s, speaker: 'Dana', speaker_id: 5 } : s));
        expect(m.adoptSaved(stored)).toBe(true);
        expect(m.toJSON()[0]).toMatchObject({ speaker: 'Dana', speaker_id: 5 });
        expect(m.undoDepth).toBe(1);
        expect(m.adoptSaved(stored.slice(1))).toBe(false);
    });
});

describe('replace text', () => {
    it('changes many segments in one undo step', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.replaceText([{ index: 0, text: 'x' }, { index: 3, text: 'y' }, { index: 1, text: m.get(1).sentence }]));
        expect(m.undoDepth).toBe(1);
        expect(texts(m)).toEqual(['x', 'Morning. Shall we start', 'Yes, the first item is the budget', 'y']);
        expect(m.replaceText([{ index: 0, text: 'x' }])).toBe(false);
    });

    it('changes one segment', () => {
        const m = createAsrModel(sample());
        roundTrip(m, () => m.replaceText([{ index: 2, text: 'z' }]));
    });
});

describe('undo and redo', () => {
    it('walks back and forward through a sequence', () => {
        const m = createAsrModel(sample());
        const states = [state(m)];
        m.setText(0, 'one', { burst: false }); states.push(state(m));
        m.split(2, 8); states.push(state(m));
        m.setSpeaker([1, 2], 'C'); states.push(state(m));
        m.remove([0]); states.push(state(m));
        m.merge([0, 1]); states.push(state(m));
        m.setStart(1, 30); states.push(state(m));
        m.add(4, 5, 'D'); states.push(state(m));
        for (let k = states.length - 2; k >= 0; k--) {
            m.undo();
            expect(state(m)).toBe(states[k]);
        }
        expect(m.undo()).toBeNull();
        for (let k = 1; k < states.length; k++) {
            m.redo();
            expect(state(m)).toBe(states[k]);
        }
        expect(m.redo()).toBeNull();
    });

    it('drops the redo steps after a new edit', () => {
        const m = createAsrModel(sample());
        m.setText(0, 'one', { burst: false });
        m.setText(0, 'two', { burst: false });
        m.undo();
        expect(m.canRedo).toBe(true);
        m.setText(1, 'three', { burst: false });
        expect(m.canRedo).toBe(false);
        expect(m.undoDepth).toBe(2);
    });

    it('reports each change to listeners', () => {
        const m = createAsrModel(sample());
        const events = [];
        const stop = m.onChange((e) => events.push(`${e.source}:${e.type}:${e.structural}`));
        m.setText(0, 'x');
        m.split(1, 3);
        m.undo();
        m.redo();
        stop();
        m.setText(0, 'y', { burst: false });
        expect(events).toEqual(['do:text:false', 'do:split:true', 'undo:split:true', 'redo:split:true']);
    });

    it('survives a long random sequence: sorted, problems current, undo exact', () => {
        let seed = 12345;
        const rnd = (n) => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
        const raw = [];
        for (let i = 0; i < 60; i++) raw.push(seg(['A', 'B', 'C'][rnd(3)], i * 2 + rnd(2), i * 2 + 1 + rnd(4), `text number ${i}`));
        const m = createAsrModel(raw, { now: () => 0 });
        const history = [state(m)];
        for (let step = 0; step < 400; step++) {
            const n = m.length;
            const i = n ? rnd(n) : 0;
            const before = m.undoDepth;
            switch (rnd(12)) {
                case 0: m.setText(i, `edit ${step}`, { burst: false }); break;
                case 1: m.setStart(i, rnd(140) + rnd(10) / 10); break;
                case 2: m.setEnd(i, rnd(140) + rnd(10) / 10); break;
                case 3: m.moveEdge(i, rnd(2) ? 'start' : 'end', rnd(140), { linked: true }); break;
                case 4: m.move(i, rnd(140)); break;
                case 5: if (n) m.split(i, rnd(m.get(i).sentence.length + 1), rnd(2) ? { time: rnd(140) } : {}); break;
                case 6: m.merge([i, i + 1, i + 2].slice(0, 2 + rnd(2))); break;
                case 7: m.add(rnd(140), rnd(140), ['A', 'B', 'C', 'D'][rnd(4)]); break;
                case 8: if (n > 20) m.remove([i, rnd(n)]); break;
                case 9: m.setSpeaker([i, rnd(n || 1)], ['A', 'B', 'C', 'D'][rnd(4)]); break;
                case 10: m.renameSpeaker(['A', 'B', 'C', 'D'][rnd(4)], ['A', 'B', 'C', 'E'][rnd(4)]); break;
                default: m.replaceText([{ index: i, text: `r${step}` }, { index: rnd(n || 1), text: `q${step}` }]);
            }
            expect(isSorted(m)).toBe(true);
            expect(problemsNow(m)).toEqual(problemsFromScratch(m));
            if (m.undoDepth > before) history.push(state(m));
            if (rnd(7) === 0 && m.canUndo) {
                m.undo();
                history.pop();
                expect(state(m)).toBe(history[history.length - 1]);
                expect(problemsNow(m)).toEqual(problemsFromScratch(m));
            }
        }
        while (m.canUndo) {
            m.undo();
            history.pop();
            expect(state(m)).toBe(history[history.length - 1]);
        }
        expect(history.length).toBe(1);
        expect(problemsNow(m)).toEqual(problemsFromScratch(m));
    });
});

describe('problems', () => {
    it('finds an overlap within one speaker', () => {
        const m = createAsrModel([seg('A', 0, 3), seg('B', 1, 2), seg('A', 2.5, 4)]);
        expect(problemsNow(m)).toEqual([0, 0, PROBLEM.OVERLAP]);
        expect(m.problemCount).toBe(1);
    });

    it('does not count different speakers talking at once', () => {
        const m = createAsrModel([seg('A', 0, 3), seg('B', 1, 4)]);
        expect(m.problemCount).toBe(0);
    });

    it('finds an end that is not after the start, and a missing time', () => {
        const m = createAsrModel([seg('A', 2, 2), seg('B', 3, 1), { speaker: 'C', sentence: 'x', start_time: 5 }]);
        expect(problemsNow(m)).toEqual([PROBLEM.DURATION, PROBLEM.DURATION, PROBLEM.DURATION]);
    });

    it('updates the neighbours of an edit only, and stays right', () => {
        const m = createAsrModel(sample());
        expect(m.problemCount).toBe(0);
        m.setEnd(0, 6);                       // A now runs into the next A segment
        expect(problemsNow(m)).toEqual([0, 0, PROBLEM.OVERLAP, 0]);
        m.setSpeaker([2], 'C');               // no longer the same speaker
        expect(m.problemCount).toBe(0);
        m.undo();
        expect(problemsNow(m)).toEqual([0, 0, PROBLEM.OVERLAP, 0]);
        m.remove([0]);
        expect(m.problemCount).toBe(0);
        m.undo();
        expect(problemsNow(m)).toEqual([0, 0, PROBLEM.OVERLAP, 0]);
        m.setEnd(3, 9);
        expect(problemsNow(m)).toEqual([0, 0, PROBLEM.OVERLAP, PROBLEM.DURATION]);
    });

    it('steps to the next and the previous problem, wrapping', () => {
        const m = createAsrModel([seg('A', 0, 0), seg('B', 1, 2), seg('A', 3, 3), seg('B', 4, 5)]);
        expect(m.nextProblem(-1)).toBe(0);
        expect(m.nextProblem(0)).toBe(2);
        expect(m.nextProblem(2)).toBe(0);
        expect(m.nextProblem(0, -1)).toBe(2);
        m.setEnd(0, 1);
        m.setEnd(2, 4);
        expect(m.nextProblem(0)).toBe(-1);
    });
});

describe('dirty tracking', () => {
    it('follows the undo position against the last save', () => {
        const m = createAsrModel(sample());
        m.setText(0, 'a', { burst: false });
        expect(m.dirty).toBe(true);
        m.markSaved();
        expect(m.dirty).toBe(false);
        m.undo();
        expect(m.dirty).toBe(true);
        m.redo();
        expect(m.dirty).toBe(false);
    });

    it('stays dirty when the saved state can no longer be reached', () => {
        const m = createAsrModel(sample());
        m.setText(0, 'a', { burst: false });
        m.markSaved();
        m.undo();
        m.setText(1, 'b', { burst: false });
        m.undo();
        expect(m.dirty).toBe(true);
        expect(m.changes().changed.map((s) => s.id)).toEqual([1]);
    });

    it('yields the changed segments and the removed ids', () => {
        const m = createAsrModel(sample());
        expect(m.changes()).toEqual({ dirty: false, changed: [], removedIds: [] });
        m.setText(2, 'changed', { burst: false });
        m.remove([0]);
        const second = m.split(0, 4);
        const { dirty, changed, removedIds } = m.changes();
        expect(dirty).toBe(true);
        expect(removedIds).toEqual([1]);
        expect(changed.map((s) => s.id)).toEqual([2, m.get(second).id, 3]);
        m.markSaved();
        expect(m.changes().changed).toEqual([]);
        m.undo();
        expect(m.changes().changed.map((s) => s.id)).toEqual([2]);
    });
});

describe('the operation log as a draft', () => {
    it('replays on a fresh model to the same state', () => {
        const m = createAsrModel(sample(), { now: () => 0 });
        m.setText(0, 'Hello', { burst: false });
        m.split(2, 10);
        m.renameSpeaker('B', 'Dana', { match: { label: 'B', name: 'Dana' } });
        m.remove([1]);
        m.setStart(0, 40);
        m.add(3, 4, 'C');
        m.setText(1, 'undone', { burst: false });
        m.undo();
        const stored = JSON.parse(JSON.stringify(m.exportLog()));
        expect(stored.length).toBe(6);

        const fresh = createAsrModel(sample());
        fresh.importLog(stored);
        expect(state(fresh)).toBe(state(m));
        expect(fresh.dirty).toBe(true);
        expect(fresh.acceptedMatches()).toEqual([{ label: 'B', name: 'Dana', ids: [2, 4] }]);
        expect(problemsNow(fresh)).toEqual(problemsFromScratch(fresh));
        while (fresh.canUndo) fresh.undo();
        expect(state(fresh)).toBe(JSON.stringify(sample()));
        // New segments after a restore do not reuse an id of the draft
        fresh.redo(); fresh.redo(); fresh.redo(); fresh.redo(); fresh.redo(); fresh.redo();
        fresh.add(50, 51, 'Z');
        expect(new Set(fresh.segments.map((s) => s.id)).size).toBe(fresh.length);
    });

    it('holds only the edits made since the last save', () => {
        const m = createAsrModel(sample());
        m.split(0, 4);
        m.markSaved();
        expect(m.exportLog()).toEqual([]);
        m.setText(2, 'after the save', { burst: false });
        m.add(1, 1.5, 'C');
        const stored = JSON.parse(JSON.stringify(m.exportLog()));
        expect(stored.map((e) => e.label)).toEqual(['text', 'add']);

        // The saved transcript, loaded again: other ids, same positions.
        m.undo(); m.undo();
        const saved = m.toJSON();
        m.redo(); m.redo();
        const fresh = createAsrModel(saved);
        fresh.importLog(stored);
        expect(state(fresh)).toBe(state(m));
        expect(new Set(fresh.segments.map((s) => s.id)).size).toBe(fresh.length);
        expect(fresh.changes().changed.map((s) => s.sentence)).toEqual(['', 'after the save']);
    });

    it('is not available once the saved state was undone past', () => {
        const m = createAsrModel(sample());
        m.setText(0, 'a', { burst: false });
        m.markSaved();
        m.undo();
        expect(m.exportLog()).toBeNull();
        m.redo();
        expect(m.exportLog()).toEqual([]);
        m.undo();
        m.setText(1, 'b', { burst: false });
        expect(m.exportLog()).toBeNull();
    });

    it('refuses a log that does not fit, leaving the model unchanged', () => {
        const m = createAsrModel(sample());
        const before = state(m);
        expect(() => m.importLog([
            { label: 'text', steps: [{ k: 'set', i: 0, f: 'sentence', a: 'Good morning everyone', b: 'y' }], ids: [1] },
            { label: 'text', steps: [{ k: 'set', i: 99, f: 'sentence', a: 'x', b: 'y' }], ids: [1] },
        ])).toThrow();
        expect(state(m)).toBe(before);
        expect(m.canUndo).toBe(false);
        expect(() => m.importLog('nope')).toThrow();
        expect(() => m.importLog([{ label: 'x', steps: [{ k: 'set', i: 0, f: 'id', a: 1, b: 2 }] }])).toThrow();
        expect(() => m.importLog([{ label: 'x', steps: [null] }])).toThrow();
        expect(state(m)).toBe(before);
    });
});

describe('lookups', () => {
    it('finds the segment at a time', () => {
        const m = createAsrModel([seg('A', 0, 10), seg('B', 2, 3), seg('A', 12, 14)]);
        expect(m.indexAtTime(1)).toBe(0);
        expect(m.indexAtTime(2.5)).toBe(1);
        expect(m.indexAtTime(5)).toBe(0);
        expect(m.indexAtTime(11)).toBe(1);
        expect(m.indexAtTime(13)).toBe(2);
        expect(m.indexAtTime(-1)).toBe(-1);
    });

    it('finds a segment by id after the order changed', () => {
        const m = createAsrModel(sample());
        const id = m.get(0).id;
        m.setStart(0, 50);
        expect(m.indexOfId(id)).toBe(3);
        expect(m.indexOfId(999)).toBe(-1);
    });

    it('serializes a segment in the stored form', () => {
        expect(segmentToJSON({ id: 1, speaker: 'A', sid: undefined, sentence: 's', start_time: 1, end_time: 2, x: null }))
            .toEqual({ speaker: 'A', sentence: 's', start_time: 1, end_time: 2 });
    });
});

describe('a 10-hour recording (20,000 segments)', () => {
    const big = () => {
        const raw = [];
        for (let i = 0; i < 20000; i++) {
            raw.push(seg(`SPEAKER_0${i % 4}`, i * 1.8, i * 1.8 + 1.7, `This is sentence number ${i} of a long meeting, with a few more words in it.`));
        }
        return raw;
    };
    const ms = () => Number(process.hrtime.bigint()) / 1e6;

    it('loads quickly', () => {
        const raw = big();
        const t0 = ms();
        const m = createAsrModel(raw);
        const took = ms() - t0;
        console.log(`[asr model] load 20,000 segments: ${took.toFixed(1)} ms`);
        expect(m.length).toBe(20000);
        expect(took).toBeLessThan(1000);
    });

    it('runs 1,000 mixed operations well inside a frame each', () => {
        const m = createAsrModel(big(), { now: () => 0 });
        let seed = 99;
        const rnd = (n) => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
        const kinds = {};
        let worst = 0;
        const t0 = ms();
        for (let step = 0; step < 1000; step++) {
            const i = rnd(m.length);
            const kind = ['text', 'start', 'end', 'split', 'merge', 'add', 'remove', 'speaker', 'undo', 'redo', 'lookup'][rnd(11)];
            const a = ms();
            switch (kind) {
                case 'text': m.setText(i, `edited ${step}`, { burst: false }); break;
                case 'start': m.setStart(i, m.get(i).start_time + 0.1); break;
                case 'end': m.moveEdge(i, 'end', m.get(i).end_time + 0.05, { linked: true }); break;
                case 'split': m.split(i, 20); break;
                case 'merge': m.mergeWithPrevious(i); break;
                case 'add': m.add(i * 1.8 + 0.5, i * 1.8 + 0.9, 'SPEAKER_09'); break;
                case 'remove': m.remove([i]); break;
                case 'speaker': m.setSpeaker([i, (i + 1) % m.length], 'Dana Lee'); break;
                case 'undo': m.undo(); break;
                case 'redo': m.redo(); break;
                default: m.indexOfId(m.get(i).id); m.nextProblem(i); m.speakers();
            }
            const took = ms() - a;
            worst = Math.max(worst, took);
            kinds[kind] = Math.max(kinds[kind] || 0, took);
        }
        const total = ms() - t0;
        const average = total / 1000;
        console.log(`[asr model] 1,000 mixed operations on 20,000 segments: ${total.toFixed(1)} ms total, ${average.toFixed(3)} ms average, ${worst.toFixed(2)} ms worst`);
        expect(total).toBeLessThan(5000);
        expect(average).toBeLessThan(4);
        let last = -Infinity;
        for (const s of m.segments) { expect(s.start_time).toBeGreaterThanOrEqual(last); last = s.start_time; }
    });

    it('renames a speaker and replaces text everywhere in one step each', () => {
        const m = createAsrModel(big());
        let a = ms();
        m.renameSpeaker('SPEAKER_00', 'Dana Lee');
        const rename = ms() - a;
        a = ms();
        m.replaceText(m.segments.map((s, index) => ({ index, text: s.sentence.replace('meeting', 'call') })));
        const replace = ms() - a;
        a = ms();
        m.undo();
        m.undo();
        const undo = ms() - a;
        a = ms();
        const json = JSON.stringify(m.toJSON());
        const serialize = ms() - a;
        console.log(`[asr model] 20,000 segments: rename speaker ${rename.toFixed(1)} ms, replace all ${replace.toFixed(1)} ms, undo both ${undo.toFixed(1)} ms, serialize ${serialize.toFixed(1)} ms (${(json.length / 1e6).toFixed(1)} MB)`);
        expect(m.undoDepth).toBe(0);
        expect(rename).toBeLessThan(500);
        expect(replace).toBeLessThan(500);
        expect(m.get(0).speaker).toBe('SPEAKER_00');
    });
});
