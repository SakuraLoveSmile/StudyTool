/*
 * Online-only installation worker. No Cache Storage, precache, persisted state,
 * or fallback HTML: login, private routes, API responses and bundles always go
 * to the network. Updates can activate immediately without retaining old data.
 */
self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  if (new URL(event.request.url).origin !== self.location.origin) return;
  event.respondWith(fetch(event.request, { cache: 'no-store' }));
});
