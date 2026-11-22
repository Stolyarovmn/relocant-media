const scope = new URL(self.registration.scope);
// CI checks the versions against the HTML entry and its module imports.
const paths = [
  "./",
  "./style.css?v=15",
  "./app.js?v=22",
  "./core.js?v=17",
  "./calendar.js?v=5",
  "./places.js?v=18",
  "./favorites.js?v=1",
  "./layer-views.js?v=2",
  "./snapshots.js?v=1",
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
