const scope = new URL(self.registration.scope);
// The Pages build gives the HTML, imports and cache the same revision.
const paths = [
  "./",
  "./style.css?v=__MAP_REVISION__",
  "./app.js?v=__MAP_REVISION__",
  "./core.js?v=__MAP_REVISION__",
  "./calendar.js?v=__MAP_REVISION__",
  "./places.js?v=__MAP_REVISION__",
  "./favorites.js?v=__MAP_REVISION__",
  "./layer-views.js?v=__MAP_REVISION__",
  "./snapshots.js?v=__MAP_REVISION__",
  "./address-watch.js?v=__MAP_REVISION__",
];
const shell = paths.map((path) => new URL(path, scope).href);
const libraries = new Map([
  ["https://unpkg.com/leaflet@1.9.4/dist/leaflet.css", "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="],
  ["https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="],
]);
const prefix = `relocant-map-shell:${scope.href}:`;
const cacheName = prefix + paths.join("|");
const assets = new Set([...shell.slice(1), ...libraries.keys()]);

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(cacheName);
    await cache.addAll(shell);
    await Promise.all([...libraries].map(async ([url, integrity]) => {
      try {
        await cache.add(new Request(url, {
          mode: "cors", integrity, signal: AbortSignal.timeout(5_000),
        }));
      } catch { /* The saved lists also work when the map library is unavailable. */ }
    }));
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith(prefix) && name !== cacheName) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

async function navigate(request) {
  try {
    const response = await fetch(request);
    if (response.status < 500) return response;
    return await caches.match(scope.href, { cacheName }) || response;
  } catch {
    return await caches.match(scope.href, { cacheName }) || Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (request.mode === "navigate" && url.origin === scope.origin
      && (url.pathname === scope.pathname || url.pathname === scope.pathname + "index.html")) {
    event.respondWith(navigate(request));
  } else if (assets.has(url.href)) {
    event.respondWith(caches.match(request, { cacheName }).then((saved) => saved || fetch(request)));
  }
});
