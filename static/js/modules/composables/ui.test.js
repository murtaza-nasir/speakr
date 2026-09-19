import { describe, expect, it } from 'vitest';

import { resolveSidebarCollapsed } from './ui.js';

// Below the lg breakpoint the sidebar is an overlay that draws a backdrop over
// the whole app, so "expanded" there means "the app is behind grey". iPad
// portrait is 834px on an 11" and lands on this every time.
describe('resolveSidebarCollapsed', () => {
    it('starts collapsed on a narrow screen when nothing is stored', () => {
        expect(resolveSidebarCollapsed(null, true)).toBe(true);
    });

    it('starts collapsed on a narrow screen even when a wide screen stored expanded', () => {
        expect(resolveSidebarCollapsed('false', true)).toBe(true);
    });

    it('keeps a narrow screen collapsed when that is what was stored', () => {
        expect(resolveSidebarCollapsed('true', true)).toBe(true);
    });

    it('restores the stored preference on a wide screen', () => {
        expect(resolveSidebarCollapsed('true', false)).toBe(true);
        expect(resolveSidebarCollapsed('false', false)).toBe(false);
    });

    it('defaults to expanded on a wide screen with nothing stored', () => {
        expect(resolveSidebarCollapsed(null, false)).toBe(false);
        expect(resolveSidebarCollapsed(undefined, false)).toBe(false);
    });
});
