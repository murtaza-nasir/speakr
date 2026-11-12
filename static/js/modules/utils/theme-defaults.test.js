import { describe, it, expect } from 'vitest';
import { savedColorScheme } from './theme-defaults.js';

function storage(values = {}) {
    return { getItem: (k) => (k in values ? values[k] : null) };
}

describe('savedColorScheme', () => {
    it('uses the scheme saved in this browser', () => {
        const win = { localStorage: storage({ colorScheme: 'rose' }), speakrThemeDefaults: { colorScheme: 'emerald' } };
        expect(savedColorScheme(win)).toBe('rose');
    });

    it('uses the page default when nothing is saved', () => {
        const win = { localStorage: storage(), speakrThemeDefaults: { colorScheme: 'emerald' } };
        expect(savedColorScheme(win)).toBe('emerald');
    });

    it('is blue without a saved scheme or a default', () => {
        expect(savedColorScheme({ localStorage: storage() })).toBe('blue');
    });

    it('is the default when storage is blocked', () => {
        const win = {
            localStorage: { getItem: () => { throw new Error('blocked'); } },
            speakrThemeDefaults: { colorScheme: 'teal' },
        };
        expect(savedColorScheme(win)).toBe('teal');
    });
});
