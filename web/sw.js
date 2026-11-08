'use strict';

/* Avenza service worker.
 *
 * The app shell is cached so the PWA installs and opens offline. API and
 * WebSocket traffic is never cached: a stale transfer state or a stale health
 * response would be dangerous. Offline the UI says so and refuses to pretend
 * a payment happened.
 */

const VERSION = 'avenza-v1';
const SHELL = [
  '/',
  '/static/styles.css',
  '/static/app.js',
  '/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never cache live money or health data.
  const noCache = [
    '/health', '/config', '/me', '/transfers', '/audit', '/locks', '/consents',
    '/devices', '/dev/', '/ws', '/prompts/',
  ];
  if (noCache.some((prefix) => url.pathname.startsWith(prefix))) {
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/').then((cached) => cached || Response.error()))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request)
        .then((response) => {
          if (response && response.status === 200 && response.type === 'basic') {
            const copy = response.clone();
            caches.open(VERSION).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached || Response.error());
    })
  );
});
