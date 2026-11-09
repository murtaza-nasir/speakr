/**
 * Unit tests for getContrastTextColor in colors.js.
 *
 * The helper returns whichever of 'black' and 'white' has the higher WCAG
 * contrast ratio against the background (relative luminance with sRGB
 * linearisation), falling back to 'white' for falsy input or when the
 * luminance computation throws. The crossover is at a relative luminance of
 * about 0.179; the better colour always reaches at least 4.58:1 (WCAG AA).
 */

import { describe, it, expect } from 'vitest';
import { getContrastTextColor } from './colors.js';

describe('getContrastTextColor', () => {
    it('defaults to white for falsy input', () => {
        expect(getContrastTextColor(undefined)).toBe('white');
        expect(getContrastTextColor(null)).toBe('white');
        expect(getContrastTextColor('')).toBe('white');
    });

    it('returns black for very light backgrounds', () => {
        expect(getContrastTextColor('#ffffff')).toBe('black');
        expect(getContrastTextColor('#ffff00')).toBe('black'); // yellow
        expect(getContrastTextColor('#cccccc')).toBe('black');
    });

    it('returns black on mid-tone backgrounds where black has more contrast', () => {
        // #808080: L = 0.216, black 5.32:1 against white 3.95:1
        expect(getContrastTextColor('#808080')).toBe('black');
        // Tag colours that gave white text 2.5 to 4.2:1 before
        expect(getContrastTextColor('#10b981')).toBe('black'); // white 2.53:1
        expect(getContrastTextColor('#3b82f6')).toBe('black'); // white 3.67:1
        expect(getContrastTextColor('#8b5cf6')).toBe('black'); // white 4.23:1
    });

    it('returns white for dark backgrounds', () => {
        expect(getContrastTextColor('#000000')).toBe('white');
        expect(getContrastTextColor('#008000')).toBe('white'); // green, white 5.1:1
        expect(getContrastTextColor('#1e3a8a')).toBe('white');
    });

    it('expands 3-digit hex shorthand', () => {
        expect(getContrastTextColor('#fff')).toBe('black');
        expect(getContrastTextColor('#000')).toBe('white');
    });

    it('tolerates a missing leading hash', () => {
        expect(getContrastTextColor('ffffff')).toBe('black');
        expect(getContrastTextColor('000000')).toBe('white');
    });

    it('falls back to white for an unparseable hex string (NaN luminance)', () => {
        expect(getContrastTextColor('notacolor')).toBe('white');
    });

    it('falls back to white when the input throws inside the calculation', () => {
        // A non-string truthy value has no .replace and throws, exercising the catch branch.
        expect(getContrastTextColor(123)).toBe('white');
    });
});
