// hap service worker — caches the app shell for offline open.
// Live data (/api/*, including the SSE stream) and non-GET requests are never
// cached; they always go straight to the network.
const CACHE = "hap-v5";
const SHELL = [
  "/",
  "/static/app.js",
  "/static/styles.css",
  "/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/offline.html",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Never intercept API calls / SSE / non-GET — straight to the network.
  if (req.method !== "GET" || url.pathname.startsWith("/api/")) return;

  // Page navigations: network-first, fall back to cached shell, then offline page.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/").then((r) => r || caches.match("/offline.html")))
    );
    return;
  }

  // Static assets: cache-first, then network (and cache the result).
  event.respondWith(
    caches.match(req).then((cached) =>
      cached ||
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => caches.match("/offline.html"))
    )
  );
});
