const CACHE = 'butsuzo-v25';

self.addEventListener('install', e => { self.skipWaiting(); });

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
});

self.addEventListener('fetch', e => {
  const url = e.request.url;
  // index.html / news.json / archives.json / scheduled_posts.json は常に network-first
  if (url.endsWith('/') || url.includes('/index.html')
      || url.includes('/data/news.json')
      || url.includes('/data/archives.json')
      || url.includes('/data/scheduled_posts.json')) {
    e.respondWith(
      fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }
  // その他（アイコン・manifest 等）はキャッシュ優先
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
