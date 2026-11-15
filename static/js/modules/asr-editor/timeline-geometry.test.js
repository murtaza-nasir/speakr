import { describe, expect, it } from 'vitest';

import { createAsrModel } from './model.js';
import {
    CREATE_MIN, MAX_PPS, MIN_LENGTH,
    beginCreate, beginMove, beginResize, buildLanes, changesOf, clampView, createTo, findSnap, hitTest,
    indexInLane, lowerBound, minPps, moveTo, nextLaneOrder, overviewWindow, resizeTo, revealInView, rulerStep,
    upperBound, zoomAt,
} from './timeline-geometry.js';

const seg = (id, speaker, start, end) => ({ id, speaker, sentence: `${speaker} ${start}`, start_time: start, end_time: end });

// Two lanes: A has 0-2, 2-5 (touching) and 8-10; B has 5-7 and 12-13.
const sample = () => [
    seg(1, 'A', 0, 2), seg(2, 'A', 2, 5), seg(3, 'B', 5, 7), seg(4, 'A', 8, 10), seg(5, 'B', 12, 13),
];
const lanesOf = (segments, order = ['A', 'B']) => buildLanes(segments, order);
const envOf = (data, extra = {}) => ({
    lanes: data.lanes, maxLength: data.maxLength, pps: 100, playhead: NaN, duration: 20, quietNear: null, ...extra,
});

describe('lanes', () => {
    it('puts each speaker in a lane, in the given order, sorted as the model is', () => {
        const data = lanesOf(sample());
        expect(data.lanes.map((l) => l.name)).toEqual(['A', 'B']);
        expect(data.lanes[0].segs.map((s) => s.id)).toEqual([1, 2, 4]);
        expect(data.lanes[1].segs.map((s) => s.id)).toEqual([3, 5]);
        expect(data.laneOf.get('B')).toBe(1);
        expect(data.maxLength).toBe(3);
        expect(data.end).toBe(13);
    });

    it('adds lanes for speakers that are not in the order, and leaves out segments without times', () => {
        const segments = [...sample(), seg(6, 'C', 14, 15), { id: 7, speaker: 'A', sentence: '', start_time: null, end_time: 3 }, seg(8, '', 16, 17)];
        const data = buildLanes(segments, ['B']);
        expect(data.lanes.map((l) => [l.name, l.segs.length])).toEqual([['B', 2], ['A', 3], ['C', 1], ['', 1]]);
    });

    it('marks the segments the model reports a problem for', () => {
        const model = createAsrModel([
            { speaker: 'A', sentence: 'a', start_time: 0, end_time: 3 },
            { speaker: 'A', sentence: 'b', start_time: 2, end_time: 4 },
            { speaker: 'B', sentence: 'c', start_time: 5, end_time: 5 },
        ]);
        const data = buildLanes(model.segments, model.speakers(), model.problemAt);
        expect(data.lanes[0].bad).toEqual([0, 1]);
        expect(data.lanes[1].bad).toEqual([1]);
    });

    it('searches a lane by start time', () => {
        const segs = lanesOf(sample()).lanes[0].segs;
        expect([lowerBound(segs, 2), upperBound(segs, 2), lowerBound(segs, 2.1), upperBound(segs, 100), lowerBound(segs, -1)]).toEqual([1, 2, 2, 3, 0]);
        expect(indexInLane(segs, segs[2])).toBe(2);
        expect(indexInLane(segs, seg(9, 'A', 2, 3))).toBe(-1);
        // Equal starts: the segment itself is found
        const twins = [seg(1, 'A', 1, 2), seg(2, 'A', 1, 3), seg(3, 'A', 1, 4)];
        expect(indexInLane(twins, twins[2])).toBe(2);
    });

    it('keeps the lane order stable', () => {
        expect(nextLaneOrder([], ['A', 'B'])).toEqual(['A', 'B']);
        // B now speaks first: the lanes stay where they are
        expect(nextLaneOrder(['A', 'B'], ['B', 'A'])).toEqual(['A', 'B']);
        // A speaker without segments loses its lane, a new one is added at the end
        expect(nextLaneOrder(['A', 'B', 'C'], ['C', 'A'])).toEqual(['A', 'C']);
        expect(nextLaneOrder(['A', 'B'], ['D', 'A', 'B'])).toEqual(['A', 'B', 'D']);
        // One name gone and one new: a rename, in place
        expect(nextLaneOrder(['SPEAKER_00', 'B'], ['B', 'Dana'])).toEqual(['Dana', 'B']);
        expect(nextLaneOrder(['A', 'B'], ['C', 'D'])).toEqual(['C', 'D']);
    });
});

describe('the view', () => {
    it('stays inside the recording', () => {
        expect(minPps(800, 100)).toBe(8);
        expect(clampView({ t0: -5, pps: 1 }, 800, 100)).toEqual({ t0: 0, pps: 8 });
        expect(clampView({ t0: 95, pps: 80 }, 800, 100)).toEqual({ t0: 90, pps: 80 });
        expect(clampView({ t0: 3, pps: 9999 }, 800, 100).pps).toBe(MAX_PPS);
        // A recording shorter than the canvas at the highest zoom
        expect(clampView({ t0: 1, pps: 10 }, 800, 1)).toEqual({ t0: 0, pps: MAX_PPS });
    });

    it('zooms around the pointer', () => {
        const view = { t0: 10, pps: 20 };
        const under = view.t0 + 300 / view.pps;
        zoomAt(view, 2, 300, 800, 1000);
        expect(view.pps).toBe(40);
        expect(view.t0 + 300 / view.pps).toBeCloseTo(under, 9);
        zoomAt(view, 0.001, 300, 800, 1000);
        expect(view).toEqual({ t0: 0, pps: 0.8 });
    });

    it('scrolls a segment into view only when it is out of it', () => {
        const view = { t0: 0, pps: 10 };
        expect(revealInView(view, 20, 30, 800, 1000)).toBe(false);
        expect(revealInView(view, 200, 210, 800, 1000)).toBe(true);
        expect(view.t0).toBe(165);                      // centred
        expect(revealInView(view, 160, 170, 800, 1000)).toBe(true);
        expect(view.t0).toBe(125);
        // Longer than the view and reaching into it: left alone
        expect(revealInView(view, 100, 400, 800, 1000)).toBe(false);
    });

    it('labels the ruler at least 76 px apart', () => {
        expect(rulerStep(400)).toBe(0.2);
        expect(rulerStep(30)).toBe(5);
        expect(rulerStep(1)).toBe(120);
        expect(rulerStep(0.02)).toBe(7200);
        expect(rulerStep(0.0001)).toBe(7200);
    });

    it('places the window of the overview', () => {
        expect(overviewWindow({ t0: 50, pps: 16 }, 800, 100)).toEqual({ x: 400, w: 400 });
        // Never narrower than 8 px, never outside
        const w = overviewWindow({ t0: 35990, pps: 400 }, 800, 36000);
        expect(w.w).toBe(8);
        expect(w.x).toBeLessThanOrEqual(792);
    });
});

describe('hit testing', () => {
    const view = { t0: 0, pps: 100 };
    const data = lanesOf(sample());
    const hit = (x, touch = false) => hitTest(data.lanes[0].segs, x, view, data.maxLength, touch);

    it('tells the edges from the body', () => {
        expect(hit(100)).toMatchObject({ zone: 'body', index: 0 });
        expect(hit(3)).toMatchObject({ zone: 'l', index: 0 });
        expect(hit(497)).toMatchObject({ zone: 'r', index: 1 });
        expect(hit(900).seg.id).toBe(4);
    });

    it('finds nothing on empty lane space, but an edge a few pixels outside a block', () => {
        expect(hit(650)).toBeNull();
        expect(hit(503)).toMatchObject({ zone: 'r', index: 1 });
        expect(hit(797)).toMatchObject({ zone: 'l', index: 2 });
        expect(hit(510)).toBeNull();
        expect(hit(510, true)).toMatchObject({ zone: 'r', index: 1 });      // touch reaches further
    });

    it('gives a very short block a body', () => {
        const segs = [seg(1, 'A', 1, 1)];
        expect(hitTest(segs, 101, view, 0)).toMatchObject({ index: 0 });
        expect(['l', 'r', 'body']).toContain(hitTest(segs, 101.5, view, 0).zone);
    });

    it('looks back far enough for a long block', () => {
        const segs = [seg(1, 'A', 0, 60), seg(2, 'A', 60, 61)];
        expect(hitTest(segs, 550, { t0: 50, pps: 100 }, 60).seg.id).toBe(1);
    });
});

describe('snapping', () => {
    const data = lanesOf(sample());

    it('takes the nearest of the playhead, an edge and a quiet point within 8 px', () => {
        const env = envOf(data, { playhead: 6.05, quietNear: () => 6.5 });
        expect(findSnap(env, 6.03)).toEqual({ t: 6.05, kind: 'playhead' });
        expect(findSnap(env, 6.97)).toEqual({ t: 7, kind: 'edge' });
        expect(findSnap(env, 6.52)).toEqual({ t: 6.5, kind: 'quiet' });
        expect(findSnap(env, 6.2)).toBeNull();
        // Edges of every lane count
        expect(findSnap(env, 7.96)).toEqual({ t: 8, kind: 'edge' });
    });

    it('reaches as far as 8 px are at the zoom', () => {
        expect(findSnap(envOf(data, { pps: 10 }), 7.4)).toEqual({ t: 7, kind: 'edge' });
        expect(findSnap(envOf(data, { pps: 400 }), 7.05)).toBeNull();
    });

    it('skips the segments being dragged', () => {
        const [, second] = data.lanes[0].segs;
        expect(findSnap(envOf(data), 4.98, second)).toEqual({ t: 5, kind: 'edge' });       // B starts at 5
        expect(findSnap(envOf(data), 2.01, second, data.lanes[0].segs[0])).toBeNull();
    });
});

describe('dragging an edge', () => {
    const start = (index, edge) => {
        const data = lanesOf(sample());
        return { data, env: envOf(data), state: beginResize(data.lanes[0].segs, index, edge) };
    };

    it('takes the touching neighbour along', () => {
        const { state, env } = start(0, 'r');
        expect(state.neighbour.id).toBe(2);
        resizeTo(state, 3.2, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 0, end: 3.2, neighbourStart: 3.2, neighbourEnd: 5, linked: true });
        expect(changesOf(state)).toEqual([{ id: 1, start: 0, end: 3.2 }, { id: 2, start: 3.2, end: 5 }]);
    });

    it('leaves the neighbour with Alt, and may then overlap it', () => {
        const { state, env } = start(0, 'r');
        resizeTo(state, 3.2, { alt: true, ctrl: true }, env);
        expect(state).toMatchObject({ end: 3.2, neighbourStart: 2, linked: false });
        expect(changesOf(state)).toEqual([{ id: 1, start: 0, end: 3.2 }]);
    });

    it('keeps both segments at least 0.05 s long', () => {
        const { state, env } = start(0, 'r');
        resizeTo(state, 99, { ctrl: true }, env);
        expect(state.end).toBe(5 - MIN_LENGTH);
        resizeTo(state, -5, { ctrl: true }, env);
        expect(state.end).toBe(MIN_LENGTH);
        const left = start(1, 'l');
        expect(left.state.neighbour.id).toBe(1);
        resizeTo(left.state, -5, { ctrl: true }, left.env);
        expect(left.state).toMatchObject({ start: MIN_LENGTH, neighbourEnd: MIN_LENGTH });
    });

    it('stops at the next block of the lane when no neighbour touches', () => {
        const { state, env } = start(1, 'r');
        expect(state.neighbour).toBeNull();
        resizeTo(state, 9, { ctrl: true }, env);
        expect(state.end).toBe(8);
        resizeTo(state, 9, { alt: true, ctrl: true }, env);
        expect(state.end).toBe(9);
        const first = start(2, 'l');
        resizeTo(first.state, 1, { ctrl: true }, first.env);
        expect(first.state.start).toBe(5);                  // the end of the block before it
    });

    it('snaps, unless Ctrl is held or the limit is nearer', () => {
        const { state, env } = start(1, 'r');
        resizeTo(state, 6.96, {}, env);
        expect(state.end).toBe(7);
        expect(state.snap).toEqual({ t: 7, kind: 'edge' });
        resizeTo(state, 6.96, { ctrl: true }, env);
        expect(state.end).toBe(6.96);
        expect(state.snap).toBeNull();
        // The playhead beyond the next block: the edge stops at the block and shows no snap
        env.playhead = 8.05;
        resizeTo(state, 8.06, {}, env);
        expect(state.end).toBe(8);
    });

    it('reports nothing when the edge is back where it was', () => {
        const { state, env } = start(0, 'r');
        resizeTo(state, 3, { ctrl: true }, env);
        resizeTo(state, 2, { ctrl: true }, env);
        expect(changesOf(state)).toEqual([]);
    });

    it('never ends past the recording', () => {
        const { state, env } = start(2, 'r');
        resizeTo(state, 500, { ctrl: true }, env);
        expect(state.end).toBe(20);
    });
});

describe('dragging a block', () => {
    const start = (lane, index, grabAt) => {
        const data = lanesOf(sample());
        const segs = data.lanes[lane].segs;
        return { data, env: envOf(data), state: beginMove(segs, index, lane, grabAt - segs[index].start_time) };
    };
    const name = (data) => (li) => data.lanes[li].name;

    it('moves in time between its neighbours, keeping its length', () => {
        const { state, env, data } = start(0, 2, 8.5);
        moveTo(state, 6, 0, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 6, end: 8, lane: 0 });
        moveTo(state, 1, 0, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 5, end: 7 });              // stops at the block before it
        moveTo(state, 50, 0, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 18, end: 20 });            // and at the end of the recording
        expect(changesOf(state, name(data))).toEqual([{ id: 4, start: 18, end: 20 }]);
    });

    it('passes its neighbours with Alt', () => {
        const { state, env } = start(0, 2, 8.5);
        moveTo(state, 1, 0, { alt: true, ctrl: true }, env);
        expect(state).toMatchObject({ start: 1, end: 3 });
    });

    it('changes the speaker in another lane, staying between the blocks there', () => {
        const { state, env, data } = start(0, 2, 8.5);
        moveTo(state, 8, 1, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 8, end: 10, lane: 1 });
        expect(changesOf(state, name(data))).toEqual([{ id: 4, start: 8, end: 10, speaker: 'B' }]);
        moveTo(state, 10.5, 1, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 10, end: 12 });            // B's next block starts at 12
        moveTo(state, 11.5, 1, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 13, end: 15 });            // its middle is past that start: after the block
        moveTo(state, 6.5, 1, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 7, end: 9 });              // B's block before ends at 7
    });

    it('only changes the lane while the pointer has not moved sideways', () => {
        const { state, env, data } = start(0, 2, 8.5);
        env.playhead = 8.03;
        moveTo(state, 8.001, 1, { keepTime: true }, env);
        expect(state).toMatchObject({ start: 8, end: 10, lane: 1, snap: null });
        expect(changesOf(state, name(data))).toEqual([{ id: 4, start: 8, end: 10, speaker: 'B' }]);
    });

    it('snaps with either edge', () => {
        const { state, env } = start(0, 2, 8.5);
        moveTo(state, 7.03, 0, {}, env);
        expect(state).toMatchObject({ start: 7, end: 9 });               // its start to B's end at 7
        expect(state.snap).toEqual({ t: 7, kind: 'edge' });
        moveTo(state, 9.97, 0, {}, env);
        expect(state).toMatchObject({ start: 10, end: 12 });             // its end to B's start at 12
        expect(state.snap).toEqual({ t: 12, kind: 'edge' });
    });

    it('reports nothing when it is back where it was', () => {
        const { state, env, data } = start(0, 2, 8.5);
        moveTo(state, 6, 1, { ctrl: true }, env);
        moveTo(state, 8, 0, { ctrl: true }, env);
        expect(changesOf(state, name(data))).toEqual([]);
    });
});

describe('drawing a new segment', () => {
    it('stays between the blocks of its lane', () => {
        const data = lanesOf(sample());
        const env = envOf(data);
        const state = beginCreate(data.lanes[0].segs, 0, 6, env);
        expect(state).toMatchObject({ lo: 5, hi: 8 });
        createTo(state, 7.2, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 6, end: 7.2 });
        createTo(state, 9, { ctrl: true }, env);
        expect(state.end).toBe(8);
        createTo(state, 3, { ctrl: true }, env);
        expect(state).toMatchObject({ start: 5, end: 6 });               // dragged to the left
        createTo(state, 3, { alt: true, ctrl: true }, env);
        expect(state).toMatchObject({ start: 3, end: 6 });
    });

    it('reaches the end of the recording after the last block, and snaps', () => {
        const data = lanesOf(sample());
        const env = envOf(data, { playhead: 15 });
        const state = beginCreate(data.lanes[1].segs, 1, 14, env);
        expect(state).toMatchObject({ lo: 13, hi: 20 });
        createTo(state, 15.04, {}, env);
        expect(state.end).toBe(15);
        expect(state.snap.kind).toBe('playhead');
        createTo(state, 14.05, { ctrl: true }, env);
        expect(state.end - state.start).toBeLessThan(CREATE_MIN);
    });

    it('finds the latest end before it among overlapping blocks', () => {
        // The first block is long and ends after the two that start later
        const data = buildLanes([seg(1, 'A', 0, 9), seg(2, 'A', 1, 2), seg(3, 'A', 3, 4), seg(4, 'A', 15, 16)], ['A']);
        expect(beginCreate(data.lanes[0].segs, 0, 10, envOf(data))).toMatchObject({ lo: 9, hi: 15 });
        // Inside the long block: the last end before the point
        expect(beginCreate(data.lanes[0].segs, 0, 5, envOf(data))).toMatchObject({ lo: 4, hi: 15 });
    });

    it('starts in an empty lane', () => {
        const data = buildLanes([], ['A']);
        const state = beginCreate(data.lanes[0].segs, 0, 2, envOf(data));
        expect(state).toMatchObject({ lo: 0, hi: 20 });
    });
});

describe('a drag, stored in the model', () => {
    it('is one undo step for an edge with its neighbour', () => {
        const model = createAsrModel(sample().map(({ id, ...rest }) => rest));
        const data = buildLanes(model.segments, model.speakers(), model.problemAt);
        const state = beginResize(data.lanes[0].segs, 0, 'r');
        resizeTo(state, 3.5, { ctrl: true }, envOf(data));
        const before = JSON.stringify(model.toJSON());
        expect(model.retime(changesOf(state))).toBe(true);
        expect(model.undoDepth).toBe(1);
        expect(model.toJSON().slice(0, 2).map((s) => [s.start_time, s.end_time])).toEqual([[0, 3.5], [3.5, 5]]);
        model.undo();
        expect(JSON.stringify(model.toJSON())).toBe(before);
    });

    it('is one undo step for a block moved to another lane', () => {
        const model = createAsrModel(sample().map(({ id, ...rest }) => rest));
        const data = buildLanes(model.segments, model.speakers(), model.problemAt);
        const state = beginMove(data.lanes[0].segs, 2, 0, 0.5);
        moveTo(state, 9.5, 1, { ctrl: true }, envOf(data));
        expect(model.retime(changesOf(state, (li) => data.lanes[li].name), { label: 'speaker' })).toBe(true);
        expect(model.undoDepth).toBe(1);
        const moved = model.toJSON().find((s) => s.sentence === 'A 8');
        expect(moved).toMatchObject({ speaker: 'B', start_time: 9.5, end_time: 11.5 });
        expect(model.problemCount).toBe(0);
        model.undo();
        expect(model.toJSON().find((s) => s.sentence === 'A 8')).toMatchObject({ speaker: 'A', start_time: 8, end_time: 10 });
    });
});

describe('a 10-hour recording (20,000 segments)', () => {
    const big = () => {
        const out = [];
        for (let i = 0; i < 20000; i++) out.push({ speaker: `SPEAKER_0${i % 5}`, sentence: `Segment ${i}`, start_time: i * 1.8, end_time: i * 1.8 + 1.7 });
        return out;
    };
    const ms = () => performance.now();

    it('builds the lanes and answers lookups well inside a frame', () => {
        const model = createAsrModel(big());
        let a = ms();
        const data = buildLanes(model.segments, model.speakers(), model.problemAt);
        const build = ms() - a;
        expect(data.lanes.map((l) => l.segs.length)).toEqual([4000, 4000, 4000, 4000, 4000]);
        const env = envOf(data, { duration: 36000, pps: 30 });
        const view = { t0: 0, pps: 30 };
        a = ms();
        let hits = 0;
        for (let i = 0; i < 10000; i++) {
            view.t0 = (i * 3.57) % 35900;
            const lane = data.lanes[i % 5].segs;
            if (hitTest(lane, (i * 7) % 800, view, data.maxLength)) hits++;
            findSnap(env, view.t0 + 3);
            // The blocks in view: what a frame looks up per lane
            if (upperBound(lane, view.t0 + 27) - lowerBound(lane, view.t0 - data.maxLength) > 8) throw new Error('too many blocks in view');
        }
        const each = (ms() - a) / 10000;
        console.log(`[asr timeline] 20,000 segments: lanes built in ${build.toFixed(1)} ms, a hit test with a snap search and a lane lookup ${(each * 1000).toFixed(1)} µs`);
        expect(hits).toBeGreaterThan(1000);
        expect(build).toBeLessThan(100);
        expect(each).toBeLessThan(0.5);
    });
});
