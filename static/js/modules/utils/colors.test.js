/**
 * Unit tests for getContrastTextColor in colors.js.
 *
 * The helper returns whichever of black and white has the higher WCAG
 * contrast ratio against the background, falling back to 'white' for falsy
 * input, an unparseable string, or when the luminance computation throws.
 */

import { describe, it, expect } from 'vitest';
import { getContrastTextColor, folderMarkColor } from './colors.js';

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

    it('returns white for dark backgrounds', () => {
        expect(getContrastTextColor('#000000')).toBe('white');
        expect(getContrastTextColor('#008000')).toBe('white'); // green: white 5.1, black 4.1
        expect(getContrastTextColor('#1d4ed8')).toBe('white'); // blue-700
    });

    it('returns black for medium backgrounds where white falls below black', () => {
        expect(getContrastTextColor('#808080')).toBe('black'); // mid grey: black 5.3, white 3.9
        expect(getContrastTextColor('#3b82f6')).toBe('black'); // blue-500: black 5.7, white 3.7
        expect(getContrastTextColor('#6366f1')).toBe('black'); // indigo-500: black 4.7, white 4.5
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

describe('folderMarkColor', () => {
    it('keeps an ordinary colour', () => {
        expect(folderMarkColor('#1a5fb4')).toBe('#1a5fb4');
        expect(folderMarkColor('#ffbe6f')).toBe('#ffbe6f');
        expect(folderMarkColor('#5e5c64')).toBe('#5e5c64');
    });

    it('wraps a white or near-white colour for light pages', () => {
        expect(folderMarkColor('#ffffff')).toBe('var(--folder-mark-pale, #ffffff)');
        expect(folderMarkColor('#FFF')).toBe('var(--folder-mark-pale, #FFF)');
        expect(folderMarkColor('#f5f5f5')).toBe('var(--folder-mark-pale, #f5f5f5)');
    });

    it('wraps a black or near-black colour for dark pages', () => {
        expect(folderMarkColor('#000000')).toBe('var(--folder-mark-dark, #000000)');
        expect(folderMarkColor('#111111')).toBe('var(--folder-mark-dark, #111111)');
    });

    it('passes anything else through', () => {
        expect(folderMarkColor(undefined)).toBe(undefined);
        expect(folderMarkColor('')).toBe('');
        expect(folderMarkColor('var(--text-3)')).toBe('var(--text-3)');
    });
});
