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

    // Calculate relative luminance using simplified formula
    // (More accurate would use gamma correction, but this is sufficient)
    return 0.299 * r + 0.587 * g + 0.114 * b;
}

/**
 * Get the appropriate text color (black or white) for a given background color
 * Ensures readable contrast based on background luminance
 * @param {string} bgColor - Background color in hex format
 * @returns {string} Either 'white' or 'black'
 */
export function getContrastTextColor(bgColor) {
    if (!bgColor) {
        return 'white'; // Default to white for undefined colors
    }

    try {
        const luminance = calculateLuminance(bgColor);
        // Threshold of 0.65: only very light backgrounds get black text
        // This ensures medium/dark colors like greens, blues still get white text
        return luminance > 0.65 ? 'black' : 'white';
    } catch (e) {
        console.warn('Failed to calculate contrast color for:', bgColor, e);
        return 'white'; // Fallback to white
    }
}
