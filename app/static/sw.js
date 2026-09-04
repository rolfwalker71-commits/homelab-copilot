/* Homelab Operations Copilot — Service Worker (PWA)
 * Cache-first for static assets; network-first for HTML/API with offline fallback.
 */
const CACHE_VERSION = "hlops-v13";
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const OFFLINE_URL = "/offline";

const PRECACHE = [
  OFFLINE_URL,
  "/static/css/app.css",
  "/static/js/chrome.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => !k.startsWith(CACHE_VERSION)).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

async function networkFirstNavigation(req) {
  // Brief retry: short backend restarts often fail the first navigation fetch.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await fetch(req);
      if (res && res.ok) {
        const clone = res.clone();
        caches.open(STATIC_CACHE).then((c) => c.put(req, clone));
      }
      return res;
    } catch (_) {
      if (attempt === 0) {
        await new Promise((r) => setTimeout(r, 350));
        continue;
      }
    }
  }
  return (await caches.match(req)) || (await caches.match(OFFLINE_URL));
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // API: network only (no stale topology)
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // Static assets: cache-first
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const fetched = fetch(req).then((res) => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(STATIC_CACHE).then((c) => c.put(req, clone));
          }
          return res;
        });
        return cached || fetched;
      })
    );
    return;
  }

  // HTML navigations: network-first (+ brief retry), offline fallback
  if (req.mode === "navigate" || (req.headers.get("accept") || "").includes("text/html")) {
    event.respondWith(networkFirstNavigation(req));
  }
});
