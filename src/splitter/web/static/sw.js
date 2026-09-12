/* Splitter service worker: makes the app installable and keeps the shell
   (pages + static assets) available while the server is briefly away. Live
   data is never cached: /api, /ws and /healthz always go to the network. The
   cache name carries the build stamp, so a new build drops old assets. */
const VERSION = new URL(self.location.href).searchParams.get("v") || "dev";
const CACHE = "splitter-" + VERSION;
const SHELL = ["/", "/races", "/tracks", "/settings", "/static/app.css", "/static/fmt.js",
  "/static/live.js", "/static/picker.js", "/static/icon.svg", "/static/icon-256.png", "/static/icon-512.png"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL).catch(function () {}); }).then(function () { return self.skipWaiting(); }));
});
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});
self.addEventListener("fetch", function (e) {
  var req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/") || url.pathname === "/healthz" || url.pathname.startsWith("/protocol")) return;
  if (url.pathname.startsWith("/static/")) {
    // Static: cache first, refresh in the background.
    e.respondWith(caches.match(req).then(function (hit) {
      var fetching = fetch(req).then(function (res) { if (res.ok) caches.open(CACHE).then(function (c) { c.put(req, res.clone()); }); return res; }).catch(function () { return hit; });
      return hit || fetching;
    }));
    return;
  }
  // Pages: network first, cached copy if the server is unreachable.
  e.respondWith(fetch(req).then(function (res) {
    if (res.ok && req.mode === "navigate") caches.open(CACHE).then(function (c) { c.put(req, res.clone()); });
    return res;
  }).catch(function () { return caches.match(req).then(function (hit) { return hit || caches.match("/"); }); }));
});
