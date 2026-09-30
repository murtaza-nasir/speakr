import { describe, it, expect } from 'vitest';
import { applyListClick } from './selection-range.js';

const ids = [1, 2, 3, 4, 5, 6];
const click = (over) => applyListClick({
    selected: new Set(), orderedIds: ids, anchorId: null, openId: null,
    id: 1, ctrl: false, shift: false, selectionMode: false, ...over,
});
const sorted = (set) => [...set].sort((a, b) => a - b);

describe('applyListClick', () => {
    it('leaves a plain click outside selection mode to open the recording', () => {
        expect(click({ id: 3 })).toBeNull();
    });

    it('Ctrl-click starts a selection holding the open recording and the clicked one', () => {
        const r = click({ id: 4, ctrl: true, openId: 2 });
        expect(r.enterMode).toBe(true);
        expect(sorted(r.selected)).toEqual([2, 4]);
        expect(r.anchorId).toBe(4);
    });

    it('Ctrl-click with nothing open selects just the clicked recording', () => {
        const r = click({ id: 4, ctrl: true });
        expect(sorted(r.selected)).toEqual([4]);
    });

    it('Ctrl-clicking the open recording starts a selection holding it', () => {
        const r = click({ id: 2, ctrl: true, openId: 2 });
        expect(sorted(r.selected)).toEqual([2]);
    });

    it('Shift-click from the open recording selects the span between them', () => {
        const r = click({ id: 5, shift: true, openId: 2 });
        expect(sorted(r.selected)).toEqual([2, 3, 4, 5]);
        expect(r.anchorId).toBe(2);
    });

    it('two Shift-clicks with nothing open select the span between them', () => {
        const first = click({ id: 2, shift: true });
        expect(sorted(first.selected)).toEqual([2]);
        const second = applyListClick({ selected: first.selected, orderedIds: ids, anchorId: first.anchorId,
            openId: null, id: 5, ctrl: false, shift: true, selectionMode: true });
        expect(sorted(second.selected)).toEqual([2, 3, 4, 5]);
    });

    it('a span upwards works and keeps earlier selections', () => {
        const r = applyListClick({ selected: new Set([6]), orderedIds: ids, anchorId: 4, openId: null,
            id: 1, ctrl: false, shift: true, selectionMode: true });
        expect(sorted(r.selected)).toEqual([1, 2, 3, 4, 6]);
        expect(r.anchorId).toBe(4);
    });

    it('Ctrl-click in selection mode toggles one recording and moves the anchor', () => {
        const r = applyListClick({ selected: new Set([1, 3]), orderedIds: ids, anchorId: 1, openId: null,
            id: 3, ctrl: true, shift: false, selectionMode: true });
        expect(sorted(r.selected)).toEqual([1]);
        expect(r.anchorId).toBe(3);
        expect(r.enterMode).toBe(false);
    });

    it('a plain click in selection mode toggles, as before', () => {
        const r = applyListClick({ selected: new Set([2]), orderedIds: ids, anchorId: 2, openId: null,
            id: 5, ctrl: false, shift: false, selectionMode: true });
        expect(sorted(r.selected)).toEqual([2, 5]);
    });

    it('an anchor no longer in the list falls back to selecting the clicked recording', () => {
        const r = applyListClick({ selected: new Set(), orderedIds: ids, anchorId: 99, openId: null,
            id: 3, ctrl: false, shift: true, selectionMode: true });
        expect(sorted(r.selected)).toEqual([3]);
        expect(r.anchorId).toBe(3);
    });
});
