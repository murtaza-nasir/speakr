/**
 * Unit tests for getContrastTextColor in colors.js.
 *
 * The helper returns 'black' for backgrounds whose simple luminance exceeds
 * 0.65 and 'white' otherwise, falling back to 'white' for falsy input or when
 * the luminance computation throws.
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
        expect(getContrastTextColor('#ffff00')).toBe('black'); // yellow, lum ~0.886
        expect(getContrastTextColor('#cccccc')).toBe('black'); // lum 0.8
    });

    it('returns white for dark and medium backgrounds', () => {
        expect(getContrastTextColor('#000000')).toBe('white');
        expect(getContrastTextColor('#008000')).toBe('white'); // green
        expect(getContrastTextColor('#808080')).toBe('white'); // mid grey, lum ~0.5
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
