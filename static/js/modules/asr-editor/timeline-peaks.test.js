import { describe, expect, it } from 'vitest';

import { levelFor, loadPeaks, parsePeaks, quietNear } from './timeline-peaks.js';

// A peaks file as src/services/waveform_peaks.py writes it.
function encode(levels, duration = 10) {
    const described = [];
    let offset = 0;
    for (const level of levels) {
        described.push({ rate: level.rate, length: level.pairs.length / 2, offset });
        offset += level.pairs.length;
    }
    const header = new TextEncoder().encode(JSON.stringify({ version: 1, duration, levels: described }));
    const bytes = new Uint8Array(8 + header.length + offset);
    bytes.set([0x53, 0x50, 0x4b, 0x31], 0);
    new DataView(bytes.buffer).setUint32(4, header.length, true);
    bytes.set(header, 8);
    let at = 8 + header.length;
    for (const level of levels) {
        bytes.set(new Uint8Array(Int8Array.from(level.pairs).buffer), at);
        at += level.pairs.length;
    }
    return bytes.buffer;
}

// Peaks at `rate` buckets per second: loud except in the given [from, to) spans.
function peaksWith(rate, seconds, quiet) {
    const pairs = [];
    for (let i = 0; i < rate * seconds; i++) {
        const t = i / rate;
        const silent = quiet.some(([from, to]) => t >= from - 1e-9 && t < to - 1e-9);
        pairs.push(silent ? -2 : -90, silent ? 3 : 100);
    }
    return parsePeaks(encode([{ rate, pairs }], seconds));
}

describe('reading a peaks file', () => {
    it('reads the levels, finest first', () => {
        const peaks = parsePeaks(encode([
            { rate: 25, pairs: [-8, 9] },
            { rate: 100, pairs: [-1, 2, -3, 4, -127, 127, 0, 0] },
        ], 0.04));
        expect(peaks.duration).toBe(0.04);
        expect(peaks.levels.map((l) => [l.rate, l.length])).toEqual([[100, 4], [25, 1]]);
        expect(Array.from(peaks.levels[0].data)).toEqual([-1, 2, -3, 4, -127, 127, 0, 0]);
        expect(Array.from(peaks.levels[1].data)).toEqual([-8, 9]);
    });

    it('does not copy the data', () => {
        const buffer = encode([{ rate: 100, pairs: [-1, 2] }]);
        expect(parsePeaks(buffer).levels[0].data.buffer).toBe(buffer);
    });

    it('refuses anything else', () => {
        expect(parsePeaks(null)).toBeNull();
        expect(parsePeaks(new ArrayBuffer(4))).toBeNull();
        expect(parsePeaks(new TextEncoder().encode('{"status":"pending"}').buffer)).toBeNull();
        const cut = encode([{ rate: 100, pairs: [-1, 2, -3, 4] }]).slice(0, -2);
        expect(parsePeaks(cut)).toBeNull();                            // shorter than its header says
        const noLevels = encode([]);
        expect(parsePeaks(noLevels)).toBeNull();
        const bad = new Uint8Array(encode([{ rate: 100, pairs: [-1, 2] }]));
        bad[0] = 0x58;
        expect(parsePeaks(bad.buffer)).toBeNull();
    });
});

describe('the level to draw from', () => {
    const levels = [{ rate: 100 }, { rate: 25 }, { rate: 6.25 }, { rate: 1.5625 }];

    it('is the coarsest one with a bucket per pixel', () => {
        expect(levelFor(levels, 400)).toBe(0);        // zoomed in past the data: the finest
        expect(levelFor(levels, 100)).toBe(0);
        expect(levelFor(levels, 30)).toBe(0);
        expect(levelFor(levels, 25)).toBe(1);
        expect(levelFor(levels, 7)).toBe(1);
        expect(levelFor(levels, 2)).toBe(2);
        expect(levelFor(levels, 0.02)).toBe(3);
        expect(levelFor([{ rate: 40 }], 0.02)).toBe(0);
    });

    it('never draws more than four buckets per pixel between levels', () => {
        for (const pps of [90, 26, 24, 7, 6, 1.6]) {
            const level = levels[levelFor(levels, pps)];
            expect(level.rate / pps).toBeGreaterThanOrEqual(1);
            expect(level.rate / pps).toBeLessThanOrEqual(4);
        }
    });
});

describe('quiet points', () => {
    it('finds the middle of a pause near the time', () => {
        const peaks = peaksWith(100, 10, [[2, 2.2], [5, 5.6]]);
        expect(quietNear(peaks, 2.05, 0.2)).toBeCloseTo(2.1, 2);
        expect(quietNear(peaks, 1.95, 0.2)).toBeCloseTo(2.1, 2);
        // A pause longer than the reach is followed to its ends
        expect(quietNear(peaks, 5.25, 0.1)).toBeCloseTo(5.3, 2);
        expect(quietNear(peaks, 5.05, 0.1)).toBeNaN();                 // its middle is out of reach
    });

    it('takes the nearest of two pauses', () => {
        const peaks = peaksWith(100, 10, [[2, 2.1], [2.4, 2.5]]);
        expect(quietNear(peaks, 2.2, 0.3)).toBeCloseTo(2.05, 2);
        expect(quietNear(peaks, 2.35, 0.3)).toBeCloseTo(2.45, 2);
    });

    it('ignores a pause shorter than 40 ms and finds none in speech', () => {
        const peaks = peaksWith(100, 10, [[3, 3.02]]);
        expect(quietNear(peaks, 3.01, 0.2)).toBeNaN();
        expect(quietNear(peaks, 7, 0.5)).toBeNaN();
    });

    it('works at the ends of the recording and without peaks', () => {
        const peaks = peaksWith(100, 4, [[0, 0.3], [3.8, 4]]);
        expect(quietNear(peaks, 0.1, 0.2)).toBeCloseTo(0.15, 2);
        expect(quietNear(peaks, 3.95, 0.2)).toBeCloseTo(3.9, 2);
        expect(quietNear(peaks, 99, 0.2)).toBeNaN();
        expect(quietNear(null, 1, 0.2)).toBeNaN();
        expect(quietNear(peaks, 1, 0)).toBeNaN();
    });

    it('looks at a bounded part of a long recording', () => {
        const peaks = peaksWith(40, 3600, [[1800, 1800.5]]);
        const started = performance.now();
        for (let i = 0; i < 1000; i++) quietNear(peaks, 1800.2, 400);
        expect(performance.now() - started).toBeLessThan(500);
        expect(quietNear(peaks, 1800.2, 400)).toBeCloseTo(1800.25, 1);
    });
});

describe('loading', () => {
    const file = encode([{ rate: 100, pairs: [-5, 5, -6, 6] }], 0.02);
    const answer = (status, body) => ({ status, arrayBuffer: async () => body });

    it('asks again while the server computes the peaks', async () => {
        const answers = [answer(202), answer(202), answer(200, file)];
        const urls = [];
        const waits = [];
        let pending = 0;
        const peaks = await loadPeaks(7, {
            fetchFn: async (url) => { urls.push(url); return answers.shift(); },
            sleep: async (ms) => { waits.push(ms); },
            onPending: () => { pending++; },
        });
        expect(urls).toEqual(['/audio/7/peaks', '/audio/7/peaks', '/audio/7/peaks']);
        expect(waits).toEqual([1000, 1500]);
        expect(pending).toBe(1);
        expect(peaks.levels[0].length).toBe(2);
    });

    it('gives up quietly when there are none', async () => {
        const none = async (status) => loadPeaks(7, { fetchFn: async () => answer(status), sleep: async () => {} });
        expect(await none(204)).toBeNull();
        expect(await none(404)).toBeNull();
        expect(await none(500)).toBeNull();
        expect(await loadPeaks(7, { fetchFn: async () => { throw new Error('offline'); } })).toBeNull();
        expect(await loadPeaks(7, { fetchFn: async () => answer(200, new ArrayBuffer(3)) })).toBeNull();
    });

    it('stops when it is aborted or has waited too long', async () => {
        let calls = 0;
        const signal = { aborted: false };
        const aborted = await loadPeaks(7, {
            signal,
            fetchFn: async () => { calls++; return answer(202); },
            sleep: async () => { signal.aborted = true; },
        });
        expect(aborted).toBeNull();
        expect(calls).toBe(1);
        calls = 0;
        const late = await loadPeaks(7, { fetchFn: async () => { calls++; return answer(202); }, sleep: async () => {}, maxWaitMs: -1 });
        expect(late).toBeNull();
        expect(calls).toBe(1);
    });
});
