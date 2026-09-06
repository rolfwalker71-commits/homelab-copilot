/* Homelab Operations Copilot — Service Worker (PWA)
 * Cache-first for static assets; network-first for HTML/API with offline fallback.
 * Web Push: show notifications for patch findings etc.
 */
const CACHE_VERSION = "hlops-v47";
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const OFFLINE_URL = "/offline";

const PRECACHE = [
  OFFLINE_URL,
  "/static/css/app.css",
  "/static/js/chrome.js",
  "/static/js/mobile.js",
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

self.addEventListener("push", (event) => {
  let data = { title: "HomelabOps", body: "Neue Benachrichtigung", url: "/", tag: "homelab-ops" };
  try {
    if (event.data) {
      const parsed = event.data.json();
      data = Object.assign({}, data, parsed);
    }
  } catch (_) {
    try {
      data.body = event.data ? event.data.text() : data.body;
    } catch (__) {
      /* ignore */
    }
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "HomelabOps", {
      body: data.body || "",
      tag: data.tag || "homelab-ops",
      data: { url: data.url || "/" },
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});

async function networkFirstNavigation(req) {
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

  if (url.pathname.startsWith("/api/")) {
    return;
  }

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

  if (req.mode === "navigate" || (req.headers.get("accept") || "").includes("text/html")) {
    event.respondWith(networkFirstNavigation(req));
  }
});
