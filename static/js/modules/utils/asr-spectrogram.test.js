import { describe, it, expect } from 'vitest';
import { validSegmentRange, spectrogramWindow, plotTime, markerPercent } from './asr-spectrogram.js';
import { splitAsrSegment } from './asr-segment-split.js';
import * as projection from './asr-spectrogram.js';

describe('prepared strip viewport projection', () => {
    it('maps a bounded native scrollbar across the entire long representation without losing scale', () => {
        const range = {start: 10, end: 7210};
        const geometry = projection.stripGeometry(range, .25, 320);
        expect(geometry.width).toBeLessThanOrEqual(1000000);
        const tail = projection.scrollWindow(range, .25, geometry.maxScroll, 320);
        expect(tail).toEqual({start:7209.75,end:7210});
        expect(projection.scrollPosition(range, tail, 320)).toBe(geometry.maxScroll);
        expect(plotTime(tail,160,320)).toBe(7209.88);
    });
    it('preserves absolute center and selected scale when width changes', () => {
        const range = {start:10,end:130}; const window = {start:75.125,end:75.375};
        const offset = projection.scrollPosition(range, window, 390);
        expect(projection.scrollWindow(range,.25,offset,390)).toEqual(window);
    });
    it('projects the tiny proportional tail tile using actual temporal width', () => {
        const range={start:10,end:130.005},window={start:129.755,end:130.005};
        const tile={index:120,start:130,end:130.005,width:21};
        const result=projection.tileProjection(tile,range,window,1024);
        expect(result.width).toBeCloseTo(20.48);
        expect(result.left-projection.scrollPosition(range,window,1024)).toBeCloseTo(1003.52);
    });
});

describe('spectral time selection', () => {
    it('maps the plot edges and center to absolute time, independent of resized width', () => {
        const window = {start: 74.2, end: 82.1};
        expect(plotTime(window, 0, 400)).toBe(74.2);
        expect(plotTime(window, 400, 400)).toBe(82.1);
        expect(plotTime(window, 200, 400)).toBe(78.15);
        expect(plotTime(window, 100, 200)).toBe(78.15);
        expect(plotTime(window, 0, 0)).toBeNull();
    });
    it('retains offscreen time without lying about a visible marker', () => {
        expect(markerPercent({start: 20, end: 40}, 25)).toBe(25);
        expect(markerPercent({start: 20, end: 40}, 10)).toBeNull();
        expect(markerPercent({start: 20, end: 40}, null)).toBeNull();
    });
    it('caps long windows, pans to the final part and supplies short-range context', () => {
        expect(spectrogramWindow(10, 150)).toEqual({start:10,end:70});
        expect(spectrogramWindow(10, 150, 60, 140)).toEqual({start:90,end:150});
        expect(spectrogramWindow(0, .02).start).toBe(0);
        expect(spectrogramWindow(0, .02).end).toBe(.25);
    });
    it.each([NaN, Infinity, -1, null])('rejects invalid ranges %s', start_time => {
        expect(validSegmentRange({start_time,end_time:8})).toBe(false);
    });
    it('uses explicit T instead of the unequal text ratio, without changing outer bounds', () => {
        const segment = {speaker:'A',sentence:'Hi. A much longer second phrase.',start_time:10,end_time:20};
        const result = splitAsrSegment(segment,4,4,17.234);
        expect(result.parts[0].end_time).toBe(17.23);
        expect(result.parts[1].start_time).toBe(17.23);
        expect(result.parts[0].start_time).toBe(10);
        expect(result.parts[1].end_time).toBe(20);
        expect(splitAsrSegment(segment,4,4).parts[0].end_time).not.toBe(17.23);
    });
    it.each([NaN, Infinity, null, 10, 20, 21])('never silently falls back for invalid explicit T %s', time => {
        expect(splitAsrSegment({sentence:'Hi. Long phrase.',start_time:10,end_time:20},4,4,time)).toEqual({error:'time'});
    });
});
