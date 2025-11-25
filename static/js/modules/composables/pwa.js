/**
 * PWA Features Composable
 * Handles install prompt, push notifications, badging, and other PWA APIs
 */

import { isPushEnabled, getPublicKey, urlBase64ToUint8Array } from '../../config/push-config.js';

export function usePWA(state, utils) {
    const {
        deferredInstallPrompt,
        showInstallButton,
        isPWAInstalled,
        notificationPermission,
        pushSubscription,
        appBadgeCount
    } = state;

    const { showToast } = utils;

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
            showToast('Installing Speakr...', 'success');
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
        showToast('Speakr installed successfully!', 'success');
    };

    // --- Push Notifications ---

    /**
     * Request notification permission
     */
    const requestNotificationPermission = async () => {
        if (!('Notification' in window)) {
            console.warn('[PWA] This browser does not support notifications');
            return false;
        }

        try {
            const permission = await Notification.requestPermission();
            notificationPermission.value = permission;
            console.log(`[PWA] Notification permission: ${permission}`);

            if (permission === 'granted') {
                showToast('Notifications enabled', 'success');
                return true;
            } else if (permission === 'denied') {
                showToast('Notification permission denied', 'error');
                return false;
            }
        } catch (error) {
            console.error('[PWA] Error requesting notification permission:', error);
            return false;
        }
    };

    /**
     * Subscribe to push notifications
     */
    const subscribeToPushNotifications = async () => {
        if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
            console.warn('[PWA] Push notifications not supported');
            showToast('Push notifications not supported in this browser', 'warning');
            return null;
        }

        // Check if push is enabled on server
        const enabled = await isPushEnabled();
        if (!enabled) {
            console.warn('[PWA] Push notifications not configured on server');
            showToast('Push notifications not available. Install pywebpush on server.', 'warning');
            return null;
        }

        // Get public key from server
        const publicKey = await getPublicKey();
        if (!publicKey) {
            console.error('[PWA] Failed to get VAPID public key from server');
            showToast('Failed to configure push notifications', 'error');
            return null;
        }

        try {
            const registration = await navigator.serviceWorker.ready;

            // Check if already subscribed
            let subscription = await registration.pushManager.getSubscription();

            if (!subscription) {
                // Subscribe to push notifications
                console.log('[PWA] Creating new push subscription...');

                const applicationServerKey = urlBase64ToUint8Array(publicKey);

                subscription = await registration.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: applicationServerKey
                });

                // Send subscription to server
                const success = await sendSubscriptionToServer(subscription);

                if (success) {
                    pushSubscription.value = subscription;
                    showToast('Push notifications enabled', 'success');
                    console.log('[PWA] Push subscription successful:', subscription);
                } else {
                    console.warn('[PWA] Failed to save subscription on server');
                    showToast('Failed to enable push notifications', 'error');
                    return null;
                }
            } else {
                pushSubscription.value = subscription;
                console.log('[PWA] Already subscribed to push notifications');
            }

            return subscription;
        } catch (error) {
            console.error('[PWA] Failed to subscribe to push notifications:', error);

            if (error.name === 'NotAllowedError') {
                showToast('Push notification permission denied', 'error');
            } else {
                showToast('Failed to enable push notifications', 'error');
            }

            return null;
        }
    };

    /**
     * Send subscription to server for storage
     */
    const sendSubscriptionToServer = async (subscription) => {
        try {
            const response = await fetch('/api/push/subscribe', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(subscription),
                credentials: 'same-origin'
            });

            if (!response.ok) {
                console.error('[PWA] Server rejected push subscription:', response.status);
                return false;
            }

            const data = await response.json();
            console.log('[PWA] Subscription saved on server:', data);
            return true;
        } catch (error) {
            console.error('[PWA] Failed to send subscription to server:', error);
            return false;
        }
    };

    /**
     * Unsubscribe from push notifications
     */
    const unsubscribeFromPushNotifications = async () => {
        if (!pushSubscription.value) {
            console.log('[PWA] No active push subscription to unsubscribe');
            return true;
        }

        try {
            // Unsubscribe on client
            await pushSubscription.value.unsubscribe();

            // Remove from server
            await fetch('/api/push/unsubscribe', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(pushSubscription.value),
                credentials: 'same-origin'
            });

            pushSubscription.value = null;
            showToast('Push notifications disabled', 'info');
            console.log('[PWA] Unsubscribed from push notifications');
            return true;
        } catch (error) {
            console.error('[PWA] Failed to unsubscribe from push notifications:', error);
            showToast('Failed to disable push notifications', 'error');
            return false;
        }
    };

    /**
     * Show a local notification
     */
    const showNotification = async (title, options = {}) => {
        if (!('Notification' in window)) {
            console.warn('[PWA] Notifications not supported');
            return;
        }

        if (Notification.permission !== 'granted') {
            const granted = await requestNotificationPermission();
            if (!granted) return;
        }

        try {
            const registration = await navigator.serviceWorker.ready;

            const defaultOptions = {
                icon: '/static/img/icon-192x192.png',
                badge: '/static/img/icon-192x192.png',
                vibrate: [200, 100, 200],
                tag: 'speakr-notification',
                renotify: true,
                ...options
            };

            await registration.showNotification(title, defaultOptions);
        } catch (error) {
            console.error('[PWA] Error showing notification:', error);
        }
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
                    title: metadata.title || 'Untitled Recording',
                    artist: metadata.artist || 'Speakr',
                    album: metadata.album || 'Recordings',
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

    // --- Background Sync ---

    /**
     * Register background sync for upload retry
     */
    const registerBackgroundSync = async (tag = 'sync-uploads') => {
        if (!('serviceWorker' in navigator) || !('sync' in ServiceWorkerRegistration.prototype)) {
            console.log('[PWA] Background sync not supported');
            return false;
        }

        try {
            const registration = await navigator.serviceWorker.ready;
            await registration.sync.register(tag);
            console.log(`[PWA] Background sync registered: ${tag}`);
            return true;
        } catch (error) {
            console.error('[PWA] Failed to register background sync:', error);
            return false;
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

        // Check notification permission status
        if ('Notification' in window) {
            notificationPermission.value = Notification.permission;
        }

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

        // Notifications
        requestNotificationPermission,
        subscribeToPushNotifications,
        unsubscribeFromPushNotifications,
        showNotification,

        // Badging
        setAppBadge,
        clearAppBadge,
        updateBadgeCount,

        // Media Session
        setupMediaSession,
        updateMediaSessionPosition,
        updateMediaSessionPlaybackState,
        clearMediaSession,

        // Background sync
        registerBackgroundSync,

        // Initialization
        initPWA,
        cleanupPWA
    };
}
