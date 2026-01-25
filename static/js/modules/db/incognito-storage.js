/**
 * Incognito Mode storage utilities
 * Uses sessionStorage for temporary storage that auto-clears when tab closes
 */

const INCOGNITO_KEY = 'speakr_incognito_recording';

/**
 * Save incognito recording data to sessionStorage
 * @param {Object} data - Recording data including transcription, summary, title
 */
export function saveIncognitoRecording(data) {
    try {
        sessionStorage.setItem(INCOGNITO_KEY, JSON.stringify(data));
        console.log('[Incognito] Recording saved to sessionStorage');
    } catch (e) {
        console.error('[Incognito] Failed to save recording:', e);
    }
}

/**
 * Get incognito recording data from sessionStorage
 * @returns {Object|null} Recording data or null if not found
 */
export function getIncognitoRecording() {
    try {
        const data = sessionStorage.getItem(INCOGNITO_KEY);
        return data ? JSON.parse(data) : null;
    } catch (e) {
        console.error('[Incognito] Failed to retrieve recording:', e);
        return null;
    }
}

/**
 * Clear incognito recording from sessionStorage
 */
export function clearIncognitoRecording() {
    try {
        sessionStorage.removeItem(INCOGNITO_KEY);
        console.log('[Incognito] Recording cleared from sessionStorage');
    } catch (e) {
        console.error('[Incognito] Failed to clear recording:', e);
    }
}

/**
 * Check if an incognito recording exists
 * @returns {boolean}
 */
export function hasIncognitoRecording() {
    try {
        return sessionStorage.getItem(INCOGNITO_KEY) !== null;
    } catch (e) {
        return false;
    }
}

/**
 * Update specific fields of the incognito recording
 * @param {Object} updates - Fields to update
 */
export function updateIncognitoRecording(updates) {
    try {
        const existing = getIncognitoRecording();
        if (existing) {
            const updated = { ...existing, ...updates };
            saveIncognitoRecording(updated);
            return updated;
        }
        return null;
    } catch (e) {
        console.error('[Incognito] Failed to update recording:', e);
        return null;
    }
}
