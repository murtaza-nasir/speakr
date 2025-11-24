/**
 * PWA state management
 */

export function createPWAState(ref) {
    // --- Install Prompt ---
    const deferredInstallPrompt = ref(null);
    const showInstallButton = ref(false);
    const isPWAInstalled = ref(false);

    // --- Notifications ---
    const notificationPermission = ref('default');
    const pushSubscription = ref(null);

    // --- Badging ---
    const appBadgeCount = ref(0);

    // --- Media Session ---
    const currentMediaMetadata = ref(null);
    const isMediaSessionActive = ref(false);

    return {
        // Install prompt
        deferredInstallPrompt,
        showInstallButton,
        isPWAInstalled,

        // Notifications
        notificationPermission,
        pushSubscription,

        // Badging
        appBadgeCount,

        // Media session
        currentMediaMetadata,
        isMediaSessionActive
    };
}
