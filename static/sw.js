// Speakr service worker.
//
// It is served at /sw.js (src/app.py) and registered with scope / by
// static/js/service-worker-client.js, as /sw.js?v=<app version>.
//
// What it does, and nothing else:
//
//   - Files under /static/ are kept in Cache Storage. A request with this
//     release's ?v= is answered from the cache first; any other static
//     request goes to the network and the cached copy is used only when
//     the network fails.
//   - A navigation (a page) always goes to the network. When the network
//     fails, a fixed "You are offline" page is answered. It is part of this
//     file and holds no user data; it loads the requested page again once
//     the server answers. No page is ever stored: pages embed user
//     data and CSRF tokens.
//   - Everything else is left to the browser, as if no worker existed:
//     /api/, /audio/, /recording/, /share/, /admin, sign-in and sign-out,
//     uploads, every request that is not a GET, every cross-origin request,
//     media and Range requests. Nothing of these reaches Cache Storage, so a
//     later user of the same browser cannot be shown an earlier user's lists,
//     transcripts or summaries, and nothing of an incognito request is kept.
//   - At sign-out every cache is deleted (the SIGN_OUT message of
//     service-worker-client.js, and the POST /logout request itself).
//
// No push, notification, background sync or message feature is implemented:
// the server sends no push messages, and failed uploads are retried by the
// page (composables/upload.js).
//
// routeRequest() and mayStore() are pure and are tested by
// static/js/sw.test.js, which also runs the handlers against a fake Cache
// Storage.

const VERSION = new URLSearchParams(self.location.search).get('v') || '';
const CACHE_PREFIX = 'speakr-static-';
const CACHE_NAME = `${CACHE_PREFIX}${VERSION || 'unversioned'}`;

// Never stored and never answered from a cache, whatever a later rule says.
const PRIVATE_PREFIXES = [
  '/api/', '/audio/', '/recording/', '/recordings/', '/share/', '/admin', '/upload',
  '/speakers', '/auth/', '/login', '/logout', '/register', '/sso/', '/account',
  '/forgot-password', '/reset-password', '/verify-email', '/resend-verification',
  '/change_password', '/inquire', '/chat', '/search', '/status', '/tags', '/folders',
  '/tokens', '/share-target', '/group-management',
];

// Files and streams: a navigation to one of these (a download link) is left
// to the browser as well, without the offline page. /recordings/<id> is a
// page (the app with that recording open) and is not in this list.
const DOWNLOAD_PREFIXES = ['/api/', '/audio/', '/recording/', '/upload', '/speakers/', '/share/audio/'];

// Static files that must always come from the network: the manifest is read
// again by the browser to register share targets and shortcuts, and the
// worker file itself is fetched by the browser's update check.
const STATIC_NETWORK_ONLY = ['/static/manifest.json', '/static/sw.js'];

const startsWithAny = (path, prefixes) => prefixes.some((prefix) => path.startsWith(prefix));

const isPrivatePath = (path) => !path.startsWith('/static/') && startsWithAny(path, PRIVATE_PREFIXES);

/**
 * What the worker does with a request.
 *
 * @param {{method: string, url: string, mode?: string, destination?: string, range?: boolean}} request
 * @param {string} origin - the worker's origin
 * @param {string} version - the worker's release ('' when unknown)
 * @returns {'bypass'|'navigate'|'static-cache-first'|'static-network-first'|'static-network-only'}
 *   bypass: not handled, the browser does what it does without a worker;
 *   navigate: network only, the offline page when the network fails;
 *   static-cache-first: this release's file, from the cache when it is there;
 *   static-network-first: a static file without ?v=, cached copy only offline;
 *   static-network-only: a static file of another release, never stored.
 */
function routeRequest(request, origin, version) {
  if (!request || request.method !== 'GET') return 'bypass';
  let url;
  try {
    url = new URL(request.url);
  } catch (e) {
    return 'bypass';
  }
  if (url.origin !== origin) return 'bypass';
  if (request.range || request.destination === 'audio' || request.destination === 'video'
      || request.destination === 'track') {
    return 'bypass';
  }
  const path = url.pathname;
  if (path.startsWith('/static/')) {
    if (STATIC_NETWORK_ONLY.includes(path)) return 'bypass';
    const v = url.searchParams.get('v');
    if (v === null) return 'static-network-first';
    return version && v === version ? 'static-cache-first' : 'static-network-only';
  }
  if (request.mode === 'navigate') {
    return startsWithAny(path, DOWNLOAD_PREFIXES) ? 'bypass' : 'navigate';
  }
  return 'bypass';
}

/**
 * Whether a response may be put into Cache Storage. Only static routes store
 * anything; this is the second lock on the same door.
 *
 * Browsers remove Set-Cookie from every response a worker can read, so the
 * stored copy never holds a cookie; the header is still checked for the
 * environments that expose it.
 *
 * @param {string} route - the result of routeRequest()
 * @param {string} url - the request URL
 * @param {{status: number, type?: string, cacheControl?: string, vary?: string, setCookie?: boolean}} response
 */
function mayStore(route, url, response) {
  if (route !== 'static-cache-first' && route !== 'static-network-first') return false;
  let path;
  try {
    path = new URL(url).pathname;
  } catch (e) {
    return false;
  }
  if (!path.startsWith('/static/') || isPrivatePath(path)) return false;
  if (!response || response.status !== 200) return false;
  if (response.type && response.type !== 'basic') return false;
  if (response.setCookie) return false;
  if (/\b(no-store|private)\b/i.test(response.cacheControl || '')) return false;
  if ((response.vary || '').trim() === '*') return false;
  return true;
}

// A worker from a release before this one was registered at /static/sw.js,
// with scope /static/, where it controlled no page. The browser's update
// check gives such a registration this file. It then removes itself and
// touches nothing; the page removes it as well (service-worker-client.js).
const isLeftoverScope = (scope) => {
  try {
    return new URL(scope).pathname !== '/';
  } catch (e) {
    return false;
  }
};
const LEFTOVER = isLeftoverScope(self.registration ? self.registration.scope : `${self.location.origin}/`);

const OFFLINE_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex">
<title>Offline - Speakr</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: #f0f2f5; color: #1c1e21; display: flex; justify-content: center; align-items: center;
         min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; text-align: center; }
  main { background: #fff; padding: 40px; border-radius: 8px; max-width: 28rem;
         box-shadow: 0 2px 4px rgba(0,0,0,.1), 0 8px 16px rgba(0,0,0,.1); }
  h1 { font-size: 24px; margin: 0 0 10px; }
  p { font-size: 16px; color: #606770; margin: 0 0 20px; }
  button { font: inherit; padding: 8px 20px; border-radius: 6px; border: 1px solid #8a8d91;
           background: transparent; color: inherit; cursor: pointer; }
  @media (prefers-color-scheme: dark) {
    body { background: #1c1e21; color: #e4e6eb; }
    main { background: #242526; }
    p { color: #b0b3b8; }
  }
</style>
</head>
<body>
<main id="speakr-offline">
  <h1>You are offline</h1>
  <p>Speakr cannot be reached. This page is loaded again when the connection is back.</p>
  <button type="button" onclick="location.reload()">Try again</button>
</main>
<script>
// The page is loaded again once the server answers: asked when the browser
// reports a connection, and every five seconds while the tab is visible (the
// server may have been the part that was away). The question is a HEAD
// request, which the worker leaves to the browser; any answer counts.
(function () {
  var asking = false;
  function ask() {
    if (asking) return;
    asking = true;
    fetch('/static/manifest.json', { method: 'HEAD', cache: 'no-store' })
      .then(function () { location.reload(); })
      .catch(function () { asking = false; });
  }
  window.addEventListener('online', ask);
  setInterval(function () { if (document.visibilityState === 'visible') ask(); }, 5000);
})();
</script>
</body>
</html>`;

const offlineResponse = () => new Response(OFFLINE_HTML, {
  status: 503,
  statusText: 'Offline',
  headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
});

const responseFacts = (response) => ({
  status: response.status,
  type: response.type,
  cacheControl: response.headers.get('Cache-Control') || '',
  vary: response.headers.get('Vary') || '',
  setCookie: response.headers.has('Set-Cookie'),
});

// Static responses carry "Vary: Cookie" when the session was read; the file
// is the same for everyone, so Vary is ignored on lookup.
const MATCH = { ignoreVary: true };

const store = async (route, request, response) => {
  if (!mayStore(route, request.url, responseFacts(response))) return;
  try {
    const cache = await caches.open(CACHE_NAME);
    await cache.put(request, response);
  } catch (e) {
    // A full disk or a private window: the file is simply not kept.
  }
};

const cacheFirst = async (event, route) => {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(event.request, MATCH);
  if (cached) return cached;
  const response = await fetch(event.request);
  event.waitUntil(store(route, event.request, response.clone()));
  return response;
};

const networkFirst = async (event, route) => {
  try {
    const response = await fetch(event.request);
    event.waitUntil(store(route, event.request, response.clone()));
    return response;
  } catch (error) {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(event.request, MATCH);
    if (cached) return cached;
    throw error;
  }
};

const navigate = async (event) => {
  try {
    return await fetch(event.request);
  } catch (error) {
    return offlineResponse();
  }
};

const deleteCaches = async (keep) => {
  const names = await caches.keys();
  await Promise.all(names.filter((name) => name !== keep).map((name) => caches.delete(name)));
};

self.addEventListener('install', () => {
  // Nothing is fetched at install. The new worker takes over at once: static
  // URLs carry the release, so a page of the release before is not answered
  // with this release's files, nor the other way round.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  if (LEFTOVER) {
    event.waitUntil(self.registration.unregister());
    return;
  }
  // Every cache of another release, and the caches of the workers before
  // this one (Speakr-cache-*), are deleted.
  event.waitUntil(deleteCaches(CACHE_NAME).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  if (LEFTOVER) return;
  const request = event.request;

  // Sign-out: the caches are deleted when the request leaves the browser,
  // also when the page that follows never runs its part.
  if (request.method === 'POST') {
    try {
      const url = new URL(request.url);
      if (url.origin === self.location.origin && url.pathname === '/logout') {
        event.waitUntil(deleteCaches(null));
      }
    } catch (e) { /* not a URL the worker handles */ }
    return;
  }

  // A request the browser may answer from its own cache only (developer
  // tools) cannot be fetched by a worker.
  if (request.cache === 'only-if-cached' && request.mode !== 'same-origin') return;

  const route = routeRequest({
    method: request.method,
    url: request.url,
    mode: request.mode,
    destination: request.destination,
    range: request.headers.has('range'),
  }, self.location.origin, VERSION);

  if (route === 'navigate') event.respondWith(navigate(event));
  else if (route === 'static-cache-first') event.respondWith(cacheFirst(event, route));
  else if (route === 'static-network-first') event.respondWith(networkFirst(event, route));
  // bypass and static-network-only: no respondWith, the browser fetches it.
});

self.addEventListener('message', (event) => {
  if (LEFTOVER || !event.data || event.data.type !== 'SIGN_OUT') return;
  // The worker keeps nothing in memory besides the constants above; the
  // caches are all it holds.
  const done = deleteCaches(null).then(() => {
    if (event.ports && event.ports[0]) event.ports[0].postMessage({ type: 'SIGNED_OUT' });
  });
  if (event.waitUntil) event.waitUntil(done);
});
