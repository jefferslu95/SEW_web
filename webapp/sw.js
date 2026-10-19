/* ------------------------------------------------------------------
   SEW web — Service Worker：把 Pyodide / 轮子等运行依赖缓存到浏览器，
   第二次打开直接命中缓存，不再重复下载。
   （只处理 ./vendor/ 下的请求，其余交给网络）
   ------------------------------------------------------------------ */
const CACHE = 'sew-vendor-v1';

self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const u = new URL(req.url);
  if (u.origin !== self.location.origin) return;
  if (!u.pathname.includes('/vendor/')) return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req);
    if (hit) return hit;
    const resp = await fetch(req);
    if (resp.ok) cache.put(req, resp.clone());
    return resp;
  })());
});

/* 主线程可以主动预热：{type:'prime', urls:[...]} */
self.addEventListener('message', (e) => {
  const m = e.data || {};
  if (m.type !== 'prime') return;
  e.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    for (const url of m.urls || []) {
      try {
        if (await cache.match(url)) continue;
        const r = await fetch(url);
        if (r.ok) await cache.put(url, r);
      } catch (err) { /* 单个失败不影响其它 */ }
    }
    if (e.source) e.source.postMessage({ type: 'primed' });
  })());
});
