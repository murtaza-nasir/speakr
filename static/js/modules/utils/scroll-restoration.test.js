import { describe, expect, it } from 'vitest';
import { restoreScrollTop } from './scroll-restoration.js';

describe('saved transcript location', () => {
    it.each(['wheel', 'click'])('keeps the idle location and resumes anchoring on %s', type => {
        const boundary = new EventTarget();
        const container = {style:{overflowAnchor:'auto'},scrollTop:0,closest:()=>boundary};
        restoreScrollTop(container, 7600);
        expect(container.scrollTop).toBe(7600);
        expect(container.style.overflowAnchor).toBe('none');
        boundary.dispatchEvent(new Event(type));
        expect(container.style.overflowAnchor).toBe('auto');
    });

    it('replaces an older restore without losing the original inherited style', () => {
        const boundary = new EventTarget();
        const container = {style:{overflowAnchor:''},scrollTop:0,closest:()=>boundary};
        restoreScrollTop(container, 100);
        restoreScrollTop(container, 7600);
        expect(container.scrollTop).toBe(7600);
        expect(container.style.overflowAnchor).toBe('none');
        boundary.dispatchEvent(new Event('pointerdown'));
        expect(container.style.overflowAnchor).toBe('');
    });
});
