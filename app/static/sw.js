// Minimal service worker: caches the order-entry shell so it opens without a network.
const CACHE = 'oams-v1';
const SHELL = ['/static/css/app.css', '/static/js/order.js', '/static/icon.svg'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))); self.clients.claim(); });
self.addEventListener('fetch', e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== 'GET' || u.origin !== location.origin) return;
  if (u.pathname.startsWith('/static/')) { e.respondWith(caches.match(r).then(h => h || fetch(r))); return; }
  if (u.pathname === '/s/new' || u.pathname === '/s/') {            // network-first, fall back to last copy
    e.respondWith(fetch(r).then(res => { const c = res.clone(); caches.open(CACHE).then(x => x.put(r, c)); return res; }).catch(() => caches.match(r)));
  }
});
