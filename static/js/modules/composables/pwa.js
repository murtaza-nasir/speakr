/**
 * PWA Features Composable
 * Handles the install prompt, badging and the media session.
 *
 * Push notifications, system notifications and background sync are not part
 * of it. The functions for them were never called by the interface and,
 * while the service worker controlled no page, could not have worked; they
 * were removed when the worker got the scope / (static/sw.js), so that no
 * permission question and no push subscription can come from here. The
 * server sends no push messages.
 */

export function usePWA(state, utils) {
    const {
        deferredInstallPrompt,
        showInstallButton,
        isPWAInstalled,
        appBadgeCount
    } = state;

    const { showToast } = utils;
    const t = (key, params = {}) => (utils.t ? utils.t(key, params) : key);

    // --- Install Prompt ---

    /**
     * Handle beforeinstallprompt event
     * This event is fired when the browser detects the app can be installed
     */
    const handleBeforeInstallPrompt = (e) => {
        console.log('[PWA] beforeinstallprompt event fired');
        // Prevent the mini-infobar from appearing on mobile
        e.preventDefault();
        // Stash the event so it can be triggered later
        deferredInstallPrompt.value = e;
        // Show our custom install button
        showInstallButton.value = true;
    };

    /**
     * Prompt user to install the PWA
     */
    const promptInstall = async () => {
        if (!deferredInstallPrompt.value) {
            console.log('[PWA] No deferred install prompt available');
            return;
        }

        // Show the install prompt
        deferredInstallPrompt.value.prompt();

        // Wait for the user's response
        const { outcome } = await deferredInstallPrompt.value.userChoice;
        console.log(`[PWA] User response to install prompt: ${outcome}`);

        if (outcome === 'accepted') {
            showToast(t('pwa.installing'), 'success');
        }

        // Clear the deferred prompt since it can only be used once
        deferredInstallPrompt.value = null;
        showInstallButton.value = false;
    };

    /**
     * Check if app is already installed
     */
    const checkIfInstalled = () => {
        // Check if running in standalone mode (installed PWA)
        if (window.matchMedia('(display-mode: standalone)').matches ||
            window.navigator.standalone === true) {
            isPWAInstalled.value = true;
            showInstallButton.value = false;
            console.log('[PWA] App is installed and running in standalone mode');
        }
    };

    /**
     * Handle appinstalled event
     */
    const handleAppInstalled = () => {
        console.log('[PWA] App was installed');
        isPWAInstalled.value = true;
        showInstallButton.value = false;
        showToast(t('pwa.installed'), 'success');
    };

    // --- Badging API ---

    /**
     * Set app badge count
     */
    const setAppBadge = async (count) => {
        if (!('setAppBadge' in navigator)) {
            console.log('[PWA] Badging API not supported');
            return;
        }

        try {
            if (count > 0) {
                await navigator.setAppBadge(count);
                appBadgeCount.value = count;
                console.log(`[PWA] App badge set to ${count}`);
            } else {
                await navigator.clearAppBadge();
                appBadgeCount.value = 0;
                console.log('[PWA] App badge cleared');
            }
        } catch (error) {
            console.error('[PWA] Error setting app badge:', error);
        }
    };

    /**
     * Clear app badge
     */
    const clearAppBadge = async () => {
        await setAppBadge(0);
    };

    /**
     * Update badge with unread count
     */
    const updateBadgeCount = async (audioFiles) => {
        if (!audioFiles || !Array.isArray(audioFiles)) return;

        // Count unread recordings (those still in inbox)
        const unreadCount = audioFiles.filter(file => file.in_inbox).length;
        await setAppBadge(unreadCount);
    };

    // --- Media Session API ---

    /**
     * Set up Media Session for audio playback control
     * @param {Object} metadata - Track metadata { title, artist, album, artwork }
     * @param {Object} handlers - Action handlers { play, pause, seekbackward, seekforward, previoustrack, nexttrack }
     */
    const setupMediaSession = (metadata, handlers = {}) => {
        if (!('mediaSession' in navigator)) {
            console.log('[PWA] Media Session API not supported');
            return false;
        }

        try {
            // Set metadata
            if (metadata) {
                navigator.mediaSession.metadata = new MediaMetadata({
                    title: metadata.title || t('upload.untitled'),
                    artist: metadata.artist || 'Speakr',
                    album: metadata.album || t('pwa.mediaAlbum'),
                    artwork: metadata.artwork || [
                        { src: '/static/img/icon-192x192.png', sizes: '192x192', type: 'image/png' },
                        { src: '/static/img/icon-512x512.png', sizes: '512x512', type: 'image/png' }
                    ]
                });
                currentMediaMetadata.value = metadata;
            }

            // Set action handlers
            const actions = ['play', 'pause', 'seekbackward', 'seekforward', 'previoustrack', 'nexttrack', 'stop'];

            actions.forEach(action => {
                if (handlers[action]) {
                    try {
                        navigator.mediaSession.setActionHandler(action, handlers[action]);
                    } catch (error) {
                        console.warn(`[PWA] The ${action} action is not supported`);
                    }
                }
            });

            // Set position state if provided
            if (handlers.setPositionState) {
                try {
                    navigator.mediaSession.setPositionState(handlers.setPositionState);
                } catch (error) {
                    console.warn('[PWA] setPositionState not supported:', error);
                }
            }

            isMediaSessionActive.value = true;
            console.log('[PWA] Media Session configured successfully');
            return true;
        } catch (error) {
            console.error('[PWA] Error setting up Media Session:', error);
            return false;
        }
    };

    /**
     * Update Media Session position state
     * @param {Object} state - { duration, playbackRate, position }
     */
    const updateMediaSessionPosition = (state) => {
        if (!('mediaSession' in navigator) || !isMediaSessionActive.value) return;

        try {
            navigator.mediaSession.setPositionState({
                duration: state.duration || 0,
                playbackRate: state.playbackRate || 1.0,
                position: state.position || 0
            });
        } catch (error) {
            console.warn('[PWA] Error updating position state:', error);
        }
    };

    /**
     * Update Media Session playback state
     * @param {string} state - 'playing' | 'paused' | 'none'
     */
    const updateMediaSessionPlaybackState = (state) => {
        if (!('mediaSession' in navigator) || !isMediaSessionActive.value) return;

        try {
            navigator.mediaSession.playbackState = state;
        } catch (error) {
            console.warn('[PWA] Error updating playback state:', error);
        }
    };

    /**
     * Clear Media Session
     */
    const clearMediaSession = () => {
        if (!('mediaSession' in navigator)) return;

        try {
            navigator.mediaSession.metadata = null;
            const actions = ['play', 'pause', 'seekbackward', 'seekforward', 'previoustrack', 'nexttrack', 'stop'];
            actions.forEach(action => {
                try {
                    navigator.mediaSession.setActionHandler(action, null);
                } catch (e) { /* ignore */ }
            });
            isMediaSessionActive.value = false;
            currentMediaMetadata.value = null;
            console.log('[PWA] Media Session cleared');
        } catch (error) {
            console.error('[PWA] Error clearing Media Session:', error);
        }
    };

    /**
     * Initialize PWA features
     */
    const initPWA = () => {
        // Check if already installed
        checkIfInstalled();

        // Listen for beforeinstallprompt event
        window.addEventListener('beforeinstallprompt', handleBeforeInstallPrompt);

        // Listen for appinstalled event
        window.addEventListener('appinstalled', handleAppInstalled);

        console.log('[PWA] PWA features initialized');
    };

    /**
     * Cleanup PWA event listeners
     */
    const cleanupPWA = () => {
        window.removeEventListener('beforeinstallprompt', handleBeforeInstallPrompt);
        window.removeEventListener('appinstalled', handleAppInstalled);
    };

    return {
        // Install prompt
        promptInstall,
        checkIfInstalled,

        // Badging
        setAppBadge,
        clearAppBadge,
        updateBadgeCount,

        // Media Session
        setupMediaSession,
        updateMediaSessionPosition,
        updateMediaSessionPlaybackState,
        clearMediaSession,

        // Initialization
        initPWA,
        cleanupPWA
    };
}
