// Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
// Licensed under the GNU AGPL v3.0 or later; see the LICENSE file.

/* Offline shell for the PWA + Web Push handling.

   Three kinds of response age differently, so each gets its own policy:

     shell (HTML/CSS/JS/icons)  network-first — an upgrade must arrive at once,
                                the cache is only the offline fallback
     dossiers (a written report cache-first, refreshed behind — the files never
       and its archived sources) change once written, so a dossier read on the
                                sofa stays readable with no network at all
     the job list (/jobs)       network only, falling back to the last list
                                seen: stale status is worse than an error while
                                online, but offline the alternative is a blank
                                page. The fallback is marked so the UI can say
                                what it is.

   Everything else — job status, .md downloads, the bundle zip — goes straight
   to the network. */

const SHELL_CACHE = "footnote-shell-v2";
const DOSSIER_CACHE = "footnote-dossier-v1";
const DOSSIER_MAX = 60;          // entries; archived source pages are the bulk
const SHELL = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/manifest.json",
  "/favicon.svg",
  "/static/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  const keep = [SHELL_CACHE, DOSSIER_CACHE];
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !keep.includes(k))
                                      .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const isShell = (url) =>
  SHELL.includes(url.pathname) || url.pathname.startsWith("/static/");

// The rendered report and the archived source pages (including the JSON index
// the source panel reads) — but not `?raw=1` downloads, `report.md`, or the
// bundle zip, which are files to save rather than pages to read.
const isDossier = (url) =>
  /^\/jobs\/[^/]+\/(report|sources)(\/|$)/.test(url.pathname) &&
  !url.searchParams.has("raw");

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  if (isShell(url)) {
    event.respondWith(networkFirst(event.request, SHELL_CACHE));
  } else if (isDossier(url)) {
    event.respondWith(cacheThenRefresh(event));
  } else if (url.pathname === "/jobs") {
    event.respondWith(networkFirst(event.request, SHELL_CACHE, true));
  }
});

async function networkFirst(request, cacheName, markFallback = false) {
  try {
    const res = await fetch(request);
    // Only successful responses: an unauthorized page must never be cached
    // as though it were the app.
    if (res.ok) (await caches.open(cacheName)).put(request, res.clone());
    else if (res.status === 401) await forgetEverything();
    return res;
  } catch (err) {
    const cached = await (await caches.open(cacheName)).match(request);
    if (!cached) throw err;
    return markFallback ? marked(cached) : cached;
  }
}

async function cacheThenRefresh(event) {
  const cache = await caches.open(DOSSIER_CACHE);
  const cached = await cache.match(event.request);
  const fresh = fetch(event.request).then(async (res) => {
    if (res.ok) {
      await cache.put(event.request, res.clone());
      await trim(cache, DOSSIER_MAX);
    } else if (res.status === 401) {
      await forgetEverything();
    }
    return res;
  });
  if (!cached) return fresh;
  event.waitUntil(fresh.catch(() => {}));   // a server-side re-render, next time
  return cached;
}

// The token was changed or revoked: drop what was read under the old one.
// Only reachable while online — an offline device keeps whatever it cached
// until its site data is cleared, which is a property of browser storage, not
// something a server can revoke.
async function forgetEverything() {
  await Promise.all([caches.delete(SHELL_CACHE), caches.delete(DOSSIER_CACHE)]);
}

// Oldest fetch first — cache.keys() is insertion-ordered.
async function trim(cache, max) {
  const keys = await cache.keys();
  for (const key of keys.slice(0, keys.length - max)) await cache.delete(key);
}

// Response headers are immutable, so saying "this came from the cache" means
// rebuilding the response around them.
async function marked(res) {
  const headers = new Headers(res.headers);
  headers.set("X-Footnote-Cached", "1");
  return new Response(await res.blob(), {
    status: res.status, statusText: res.statusText, headers,
  });
}

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) {}
  event.waitUntil(
    self.registration.showNotification(data.title || "Footnote", {
      body: data.body || "",
      icon: data.icon || "/static/icon-192.png",
      badge: data.badge || "/static/icon-192.png",
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const win of wins) {
        if (new URL(win.url).origin === self.location.origin) {
          win.navigate(url);
          return win.focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});
