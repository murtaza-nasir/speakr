const CACHE_NAME = 'Speakr-cache-v4';
const ASSETS_TO_CACHE = [
  '/',
  '/static/offline.html',
  '/static/manifest.json',
  '/static/css/styles.css',
  '/static/js/app.modular.js',
  '/static/js/i18n.js',
  '/static/js/csrf-refresh.js',
  '/static/img/icon-192x192.png',
  '/static/img/icon-512x512.png',
  '/static/img/favicon.ico',
  // Local vendor assets (no external CDN dependencies)
  '/static/vendor/js/tailwind.min.js',
  '/static/vendor/js/vue.global.js',
  '/static/vendor/js/marked.min.js',
  '/static/vendor/js/easymde.min.js',
  '/static/vendor/css/fontawesome.min.css',
  '/static/vendor/css/easymde.min.css'
];

// Function to update shortcuts (structure from your example)
// The actual `lists` data would need to be sent from your client-side app.js
const updateShortcuts = async (lists) => {
  if (!self.registration || !('shortcuts' in self.registration)) {
    console.log('Shortcuts API not supported or registration not available.');
    return;
  }

  try {
    let shortcuts = [
      {
        name: "New Recording",
        short_name: "New",
        description: "Upload or record new audio",
        url: "/#upload", // Or your direct upload page route
        icons: [{ src: "/static/img/icon-192x192.png", sizes: "192x192" }]
      },
      {
        name: "View Gallery",
        short_name: "Gallery",
        description: "Access your recordings gallery",
        url: "/#gallery", // Or your direct gallery page route
        icons: [{ src: "/static/img/icon-192x192.png", sizes: "192x192" }]
      }
    ];

    // Example: If you had dynamic lists to add as shortcuts
    if (Array.isArray(lists) && lists.length > 0) {
      const dynamicShortcuts = lists.slice(0, 2).map(list => { // Max 2 dynamic, total 4
        if (list && list.id && list.title) {
          return {
            name: list.title,
            short_name: list.title.length > 10 ? list.title.substring(0, 9) + '…' : list.title,
            description: `View ${list.title}`,
            url: `/list/${list.id}`, // Example dynamic URL
            icons: [{ src: "/static/img/icon-192x192.png", sizes: "192x192" }]
          };
        }
        return null;
      }).filter(Boolean);
      shortcuts = [...shortcuts, ...dynamicShortcuts];
    }

    await self.registration.shortcuts.set(shortcuts);
    console.log('PWA shortcuts updated successfully:', shortcuts);
  } catch (error) {
    console.error('Error updating PWA shortcuts:', error);
  }
};


// Cache first strategy: Respond from cache if available, otherwise fetch from network and cache.
const cacheFirst = async (request) => {
  const responseFromCache = await caches.match(request);
  if (responseFromCache) {
    return responseFromCache;
  }
  try {
    const responseFromNetwork = await fetch(request);
    // Check if the response is valid before caching
    if (responseFromNetwork && responseFromNetwork.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, responseFromNetwork.clone());
    }
    return responseFromNetwork;
  } catch (error) {
    console.error('CacheFirst: Network request failed for:', request.url, error);
    // For assets, returning a generic error or specific offline asset might be better than network error.
    // However, if it's a critical asset not found, this indicates an issue.
    return new Response('Network error trying to fetch asset.', {
      status: 408,
      headers: { 'Content-Type': 'text/plain' },
    });
  }
};

// Stale-while-revalidate strategy: Respond from cache immediately if available,
// then update the cache with a fresh response from the network.
const staleWhileRevalidate = async (request) => {
  const cache = await caches.open(CACHE_NAME);
  const cachedResponsePromise = cache.match(request);
  const networkResponsePromise = fetch(request).then(networkResponse => {
    if (networkResponse && networkResponse.ok) {
      cache.put(request, networkResponse.clone());
    }
    return networkResponse;
  }).catch(error => {
    console.error('StaleWhileRevalidate: Network request failed for:', request.url, error);
    // If network fails, we still might have a cached response.
    // If not, this error will propagate.
    return new Response('API request failed and no cache available.', {
        status: 503, // Service Unavailable
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ error: 'Service temporarily unavailable. Please try again later.' })
    });
  });

  return (await cachedResponsePromise) || networkResponsePromise;
};

// Network first strategy: Try to fetch from network first.
// If network fails, fall back to cache. If cache also fails, serve offline page for navigation.
const networkFirst = async (request) => {
  try {
    const networkResponse = await fetch(request);
    if (networkResponse && networkResponse.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, networkResponse.clone());
    }
    return networkResponse;
  } catch (error) {
    console.warn('NetworkFirst: Network request failed for:', request.url, error);
    const cachedResponse = await caches.match(request);
    if (cachedResponse) {
      return cachedResponse;
    }
    // For navigation requests, fall back to the offline page if both network and cache fail.
    if (request.mode === 'navigate') {
      const offlinePage = await caches.match('/static/offline.html');
      if (offlinePage) return offlinePage;
    }
    // For other types of requests, or if offline page isn't cached, re-throw or return error.
    return new Response('Network error and no cache available.', {
      status: 408,
      headers: { 'Content-Type': 'text/plain' },
    });
  }
};

self.addEventListener('install', (event) => {
  self.skipWaiting(); // Activate new service worker immediately
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('Service Worker: Caching app shell');
      return cache.addAll(ASSETS_TO_CACHE.map(url => new Request(url, { cache: 'reload' }))) // Force reload from network for app shell
        .catch(error => {
          console.error('Failed to cache app shell during install:', error);
          // You might want to log which specific asset failed
          ASSETS_TO_CACHE.forEach(url => {
            cache.add(new Request(url, { cache: 'reload' })).catch(err => console.warn(`Failed to cache: ${url}`, err));
          });
        });
    })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => {
            console.log('Service Worker: Deleting old cache', name);
            return caches.delete(name);
          })
      );
    }).then(() => {
      console.log('Service Worker: Activated and old caches cleared.');
      return self.clients.claim(); // Take control of all open clients
    })
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Skip non-GET requests from caching strategies (they should pass through)
  if (request.method !== 'GET') {
    // event.respondWith(fetch(request)); // Let non-GET requests pass through to the network
    return; // Or simply return to let the browser handle it
  }

  // Serve API calls from /api/ with stale-while-revalidate
  // (excluding auth-related endpoints)
  if (url.pathname.startsWith('/api/')) {
    if (url.pathname.includes('/login') || url.pathname.includes('/logout') || url.pathname.includes('/auth')) {
      // For auth, always go to network, don't cache
      event.respondWith(fetch(request));
      return;
    }
    event.respondWith(staleWhileRevalidate(request));
    return;
  }
  
  // Serve /audio/<id> requests with cache-first, then network.
  // These are media files and can be large, so cache-first is good.
  if (url.pathname.startsWith('/audio/')) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // Handle navigation requests (HTML pages) with network-first, then cache, then offline page.
  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request));
    return;
  }

  // For static assets listed in ASSETS_TO_CACHE, use cache-first.
  // This ensures that if an asset path is directly requested, it's served from cache if possible.
  // We need to match against the origin + pathname for ASSETS_TO_CACHE.
  const requestPath = url.origin === self.origin ? url.pathname : request.url;
  if (ASSETS_TO_CACHE.includes(requestPath)) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // Default strategy for other GET requests: try cache, then network.
  // This is a good general fallback for other static assets not explicitly listed
  // or for assets from other origins if not handled by ASSETS_TO_CACHE.
  event.respondWith(
    caches.match(request).then((cachedResponse) => {
      if (cachedResponse) {
        return cachedResponse;
      }
      return fetch(request).then(networkResponse => {
        // Optionally cache other successful GET responses here if desired
        // if (networkResponse && networkResponse.ok) {
        //   const cache = await caches.open(CACHE_NAME);
        //   cache.put(request, networkResponse.clone());
        // }
        return networkResponse;
      }).catch(() => {
        // If network fails for a non-navigation, non-API, non-explicitly-cached asset
        // there isn't much we can do other than return an error or nothing.
        // For simplicity, let the browser handle the error.
      });
    })
  );
});

// Listen for messages from the client (e.g., to update shortcuts)
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'UPDATE_SHORTCUTS') {
    console.log('Service Worker: Received UPDATE_SHORTCUTS message:', event.data.lists);
    // updateShortcuts(event.data.lists); // Call if you implement dynamic shortcuts based on client data
  }
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// Background sync for failed uploads
self.addEventListener('sync', (event) => {
  console.log('[Service Worker] Background sync triggered:', event.tag);

  if (event.tag === 'sync-uploads') {
    event.waitUntil(syncFailedUploads());
  }
});

// Sync failed uploads from IndexedDB
async function syncFailedUploads() {
  console.log('[Service Worker] Syncing failed uploads');

  try {
    // Notify that sync is checking for failed uploads
    await self.registration.showNotification('Speakr Upload Sync', {
      body: 'Checking for failed uploads to retry...',
      icon: '/static/img/icon-192x192.png',
      badge: '/static/img/icon-192x192.png',
      tag: 'upload-sync',
      requireInteraction: false
    });

    // Future: Retrieve and retry failed uploads from IndexedDB
    // This would require storing failed uploads in IndexedDB from the main app
    // const db = await openIndexedDB();
    // const failedUploads = await getFailedUploads(db);
    // for (const upload of failedUploads) {
    //     await retryUpload(upload);
    // }

    return Promise.resolve();
  } catch (error) {
    console.error('[Service Worker] Failed to sync uploads:', error);
    return Promise.reject(error);
  }
}

// Push notification handler
self.addEventListener('push', (event) => {
  console.log('[Service Worker] Push notification received');

  const options = {
    icon: '/static/img/icon-192x192.png',
    badge: '/static/img/icon-192x192.png',
    vibrate: [200, 100, 200],
    data: {
      dateOfArrival: Date.now(),
      primaryKey: 1
    }
  };

  if (event.data) {
    const data = event.data.json();
    event.waitUntil(
      self.registration.showNotification(data.title || 'Speakr Notification', {
        body: data.body || 'You have a new notification',
        ...options,
        data: data
      })
    );
  } else {
    event.waitUntil(
      self.registration.showNotification('Speakr Notification', {
        body: 'You have a new notification',
        ...options
      })
    );
  }
});

// Notification click handler
self.addEventListener('notificationclick', (event) => {
  console.log('[Service Worker] Notification clicked:', event.notification.tag);
  event.notification.close();

  // Handle different notification types
  const urlToOpen = event.notification.data?.url || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((clientList) => {
        // Check if there's already a window open
        for (const client of clientList) {
          if (client.url === urlToOpen && 'focus' in client) {
            return client.focus();
          }
        }
        // If no window is open, open a new one
        if (clients.openWindow) {
          return clients.openWindow(urlToOpen);
        }
      })
  );
});
