/**
 * Color utility functions
 */

/**
 * Calculate the relative luminance of a color
 * Based on WCAG contrast ratio formula
 * @param {string} hexColor - Hex color code (e.g., "#RRGGBB" or "#RGB")
 * @returns {number} Luminance value between 0 and 1
 */
function calculateLuminance(hexColor) {
    // Remove # if present
    const hex = hexColor.replace('#', '');

    // Convert 3-digit hex to 6-digit
    const fullHex = hex.length === 3
        ? hex.split('').map(char => char + char).join('')
        : hex;

    // Parse RGB values
    const r = parseInt(fullHex.substr(0, 2), 16) / 255;
    const g = parseInt(fullHex.substr(2, 2), 16) / 255;
    const b = parseInt(fullHex.substr(4, 2), 16) / 255;

    // WCAG 2.x relative luminance: linearise each sRGB channel first
    const lin = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/**
 * Get the appropriate text color (black or white) for a given background color.
 * Picks whichever of the two has the higher WCAG contrast ratio against the
 * background. Contrast with white is 1.05 / (L + 0.05), with black it is
 * (L + 0.05) / 0.05, so the two are equal at L of about 0.179 and the better
 * of them is never below 4.58:1, which meets WCAG AA for normal text.
 * @param {string} bgColor - Background color in hex format
 * @returns {string} Either 'white' or 'black'
 */
export function getContrastTextColor(bgColor) {
    if (!bgColor) {
        return 'white'; // Default to white for undefined colors
    }

    try {
        const luminance = calculateLuminance(bgColor);
        const contrastWithWhite = 1.05 / (luminance + 0.05);
        const contrastWithBlack = (luminance + 0.05) / 0.05;
        // A NaN luminance (unparseable input) makes the comparison false: white
        return contrastWithBlack > contrastWithWhite ? 'black' : 'white';
    } catch (e) {
        console.warn('Failed to calculate contrast color for:', bgColor, e);
        return 'white'; // Fallback to white
    }
}
