/**
 * Color utility functions
 */

/**
 * Calculate the WCAG relative luminance of a color
 * (https://www.w3.org/TR/WCAG21/#dfn-relative-luminance)
 * @param {string} hexColor - Hex color code (e.g., "#RRGGBB" or "#RGB")
 * @returns {number} Luminance value between 0 and 1 (NaN for an unparseable string)
 */
function calculateLuminance(hexColor) {
    // Remove # if present
    const hex = hexColor.replace('#', '');

    // Convert 3-digit hex to 6-digit
    const fullHex = hex.length === 3
        ? hex.split('').map(char => char + char).join('')
        : hex;

    // Parse RGB values and linearise each channel (sRGB gamma)
    const channel = (offset) => {
        const c = parseInt(fullHex.substr(offset, 2), 16) / 255;
        return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
}

/**
 * Get the appropriate text color (black or white) for a given background color
 * Picks whichever of black and white has the higher WCAG contrast ratio
 * against the background, so the text reaches 4.5:1 wherever either can.
 * @param {string} bgColor - Background color in hex format
 * @returns {string} Either 'white' or 'black'
 */
export function getContrastTextColor(bgColor) {
    if (!bgColor) {
        return 'white'; // Default to white for undefined colors
    }

    try {
        const luminance = calculateLuminance(bgColor);
        if (Number.isNaN(luminance)) {
            return 'white';
        }
        const contrastWithWhite = 1.05 / (luminance + 0.05);
        const contrastWithBlack = (luminance + 0.05) / 0.05;
        return contrastWithBlack > contrastWithWhite ? 'black' : 'white';
    } catch (e) {
        console.warn('Failed to calculate contrast color for:', bgColor, e);
        return 'white'; // Fallback to white
    }
}
