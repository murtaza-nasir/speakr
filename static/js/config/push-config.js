/**
 * Push Notification Configuration
 *
 * AUTO-CONFIGURATION:
 * ------------------
 * Push notifications are now auto-configured!
 *
 * On first server startup:
 * 1. VAPID keys are automatically generated (requires pywebpush)
 * 2. Keys are saved to /config/vapid_keys.json (persists across restarts)
 * 3. Public key is served via /api/push/config
 * 4. Client fetches config dynamically
 *
 * No manual configuration needed - just make sure:
 * - pywebpush is installed: pip install pywebpush
 * - /config directory is mounted as Docker volume (for persistence)
 */

// Cached config fetched from server
let cachedConfig = null;

/**
 * Fetch push notification config from server
 */
export async function getPushConfig() {
    if (cachedConfig) {
        return cachedConfig;
    }

    try {
        const response = await fetch('/api/push/config');
        if (!response.ok) {
            console.warn('[Push Config] Failed to fetch config:', response.status);
            return { enabled: false, public_key: null };
        }

        cachedConfig = await response.json();
        console.log('[Push Config] Loaded from server:', cachedConfig.enabled ? 'enabled' : 'disabled');
        return cachedConfig;
    } catch (error) {
        console.error('[Push Config] Error fetching config:', error);
        return { enabled: false, public_key: null };
    }
}

/**
 * Check if push notifications are enabled
 */
export async function isPushEnabled() {
    const config = await getPushConfig();
    return config.enabled && !!config.public_key;
}

/**
 * Get VAPID public key from server
 */
export async function getPublicKey() {
    const config = await getPushConfig();
    return config.public_key;
}

/**
 * Convert a base64 string to Uint8Array (required for push subscription)
 */
export function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding)
        .replace(/-/g, '+')
        .replace(/_/g, '/');

    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);

    for (let i = 0; i < rawData.length; ++i) {
        outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
}
