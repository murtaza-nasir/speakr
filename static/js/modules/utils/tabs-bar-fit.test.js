import { describe, it, expect } from 'vitest';
import { FIT_CLASSES, fitTabsBar, overflows } from './tabs-bar-fit.js';

// A bar whose need for room shrinks by `saves[i]` px at each fit level.
function fakeBar({ width = 400, need = 500, saves = [60, 30, 0] } = {}) {
    const classes = new Set(['tabs-bar']);
    return {
        clientWidth: width,
        classList: {
            add: (...names) => names.forEach((n) => classes.add(n)),
            remove: (...names) => names.forEach((n) => classes.delete(n)),
            contains: (n) => classes.has(n),
        },
        classes,
        need() {
            let n = need;
            FIT_CLASSES.forEach((c, i) => { if (classes.has(c)) n -= saves[i]; });
            return n;
        },
    };
}
const measure = (bar) => bar.need() > bar.clientWidth;

describe('tabs bar fit', () => {
    it('adds no class when the tabs and actions fit', () => {
        const bar = fakeBar({ need: 380 });
        expect(fitTabsBar(bar, measure)).toBe(0);
        expect([...bar.classes]).toEqual(['tabs-bar']);
    });

    it('steps down only as far as needed', () => {
        const bar = fakeBar({ need: 450 });
        expect(fitTabsBar(bar, measure)).toBe(1);
        expect(bar.classes.has('tabs-bar--fit-1')).toBe(true);
        expect(bar.classes.has('tabs-bar--fit-2')).toBe(false);
    });

    it('keeps the earlier levels and stops at the last one', () => {
        const bar = fakeBar({ need: 900 });
        expect(fitTabsBar(bar, measure)).toBe(FIT_CLASSES.length);
        FIT_CLASSES.forEach((c) => expect(bar.classes.has(c)).toBe(true));
    });

    it('starts again from the full bar when there is more room', () => {
        const bar = fakeBar({ need: 900 });
        fitTabsBar(bar, measure);
        bar.clientWidth = 1000;
        expect(fitTabsBar(bar, measure)).toBe(0);
        FIT_CLASSES.forEach((c) => expect(bar.classes.has(c)).toBe(false));
    });

    it('leaves a hidden bar alone', () => {
        const bar = fakeBar({ width: 0 });
        bar.classList.add('tabs-bar--fit-1');
        expect(fitTabsBar(bar, measure)).toBe(-1);
        expect(bar.classes.has('tabs-bar--fit-1')).toBe(true);
    });

    it('measures the tab list at its full width, with the gap and without hidden children', () => {
        const tabs = { scrollWidth: 260, offsetWidth: 202, clientWidth: 200 };
        const actions = { scrollWidth: 150, offsetWidth: 150, clientWidth: 150 };
        const hidden = { scrollWidth: 0, offsetWidth: 0, clientWidth: 0, hidden: true };
        const bar = { clientWidth: 450, children: [tabs, hidden, actions] };
        const view = {
            getComputedStyle: (el) => (el === bar
                ? { paddingLeft: '16px', paddingRight: '16px', columnGap: '16px' }
                : { display: el.hidden ? 'none' : 'flex' }),
        };
        // need 262 + 150 + 16 = 428 > room 418
        expect(overflows(bar, view)).toBe(true);
        bar.clientWidth = 460;
        expect(overflows(bar, view)).toBe(false);
    });
});
