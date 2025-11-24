// Speakr Service Worker
// Provides offline support and background capabilities for recording

const CACHE_VERSION = 'speakr-v2';
const CACHE_STATIC = `${CACHE_VERSION}-static`;
const CACHE_DYNAMIC = `${CACHE_VERSION}-dynamic`;

// Files to cache for offline functionality
const STATIC_ASSETS = [
    '/',
    '/static/css/styles.css',
    '/static/js/app.modular.js',
    '/static/manifest.json',
    '/static/offline.html',
    '/static/img/icon-192x192.png',
    '/static/img/icon-512x512.png'
];

// Install event - cache static assets
self.addEventListener('install', (event) => {
    console.log('[Service Worker] Installing...');
    event.waitUntil(
        caches.open(CACHE_STATIC)
            .then((cache) => {
                console.log('[Service Worker] Caching static assets');
                return cache.addAll(STATIC_ASSETS.map(url => new Request(url, { credentials: 'same-origin' })));
            })
            .catch((error) => {
                console.error('[Service Worker] Failed to cache static assets:', error);
            })
    );
    // Force the waiting service worker to become the active service worker
    self.skipWaiting();
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
    console.log('[Service Worker] Activating...');
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName.startsWith('speakr-') &&
                        cacheName !== CACHE_STATIC &&
                        cacheName !== CACHE_DYNAMIC) {
                        console.log('[Service Worker] Deleting old cache:', cacheName);
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
    // Ensure the service worker takes control of all clients immediately
    return self.clients.claim();
});

// Fetch event - serve from cache when offline, otherwise fetch from network
self.addEventListener('fetch', (event) => {
    const { request } = event;
    const url = new URL(request.url);

    // Skip caching for:
    // - API calls
    // - File uploads
    // - WebSocket connections
    // - Chrome extensions
    if (request.method !== 'GET' ||
        url.pathname.startsWith('/api/') ||
        url.pathname.startsWith('/upload') ||
        url.pathname.startsWith('/transcribe') ||
        url.protocol === 'chrome-extension:') {
        return;
    }

    event.respondWith(
        caches.match(request)
            .then((cachedResponse) => {
                if (cachedResponse) {
                    // Return cached version
                    return cachedResponse;
                }

                // Fetch from network and cache dynamically
                return fetch(request).then((response) => {
                    // Only cache successful responses
                    if (!response || response.status !== 200 || response.type === 'error') {
                        return response;
                    }

                    // Clone the response (can only use once)
                    const responseToCache = response.clone();

                    caches.open(CACHE_DYNAMIC).then((cache) => {
                        cache.put(request, responseToCache);
                    });

                    return response;
                }).catch(() => {
                    // If network fails and not cached, show offline page
                    if (request.mode === 'navigate') {
                        return caches.match('/static/offline.html');
                    }
                });
            })
    );
});

// Message event - handle messages from the client
self.addEventListener('message', (event) => {
    console.log('[Service Worker] Received message:', event.data);

    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }

    // Handle recording state updates
    if (event.data && event.data.type === 'RECORDING_STATE') {
        if (event.data.isRecording) {
            console.log('[Service Worker] Recording started, maintaining background state');
        } else {
            console.log('[Service Worker] Recording stopped');
        }
    }
});

// Background sync for failed uploads (future enhancement)
self.addEventListener('sync', (event) => {
    if (event.tag === 'sync-uploads') {
        console.log('[Service Worker] Background sync triggered');
        // Future: Handle failed upload retry
    }
});

// Notification click handler
self.addEventListener('notificationclick', (event) => {
    console.log('[Service Worker] Notification clicked');
    event.notification.close();

    // Focus or open the app
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then((clientList) => {
                // If app is already open, focus it
                for (let i = 0; i < clientList.length; i++) {
                    const client = clientList[i];
                    if (client.url.includes('/') && 'focus' in client) {
                        return client.focus();
                    }
                }
                // Otherwise, open a new window
                if (clients.openWindow) {
                    return clients.openWindow('/');
                }
            })
    );
});

console.log('[Service Worker] Script loaded');
