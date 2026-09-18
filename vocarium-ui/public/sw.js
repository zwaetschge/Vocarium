/* Vocarium Service Worker v2.
 * - Audio (Hörbuch-Segmente, Hörspiel-Artefakte, Podcast-Audio, Cover)
 *   cache-first: einmal gehört oder offline gesichert = spielt ohne Netz.
 * - API-Lesezugriffe der drei Bereiche network-first mit Cache-Fallback, damit
 *   Bibliothek, Reader und Mediathek offline öffnen.
 * - App-Shell (index.html + Assets) wird gecacht; Navigation faellt offline
 *   auf die Shell zurueck statt auf eine Fehlerseite, /offline.html nur wenn
 *   die Shell nie geladen wurde.
 * - Schreibzugriffe gehen immer ans Netz. */

const VERSION = 'v2';
const AUDIO_CACHE = `vocarium-audio-${VERSION}`;
const ASSET_CACHE = `vocarium-assets-${VERSION}`;
const API_CACHE = `vocarium-api-${VERSION}`;
const AUDIO_MAX_ENTRIES = 6000;
const API_MAX_ENTRIES = 600;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(ASSET_CACHE)
      .then((c) => c.addAll(['/offline.html', '/index.html', '/manifest.webmanifest']).catch(() => c.add('/offline.html')))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k.startsWith('vocarium-') && !k.endsWith(VERSION)).map((k) => caches.delete(k)),
    )).then(() => self.clients.claim()),
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'skipWaiting') self.skipWaiting();
});

async function trimCache(name, max) {
  const cache = await caches.open(name);
  const keys = await cache.keys();
  if (keys.length > max) {
    await Promise.all(keys.slice(0, keys.length - max).map((k) => cache.delete(k)));
  }
}

function isAudio(url) {
  const p = url.pathname;
  return /\/api\/audiobooks\/[^/]+\/audio(-live)?\//.test(p)
    || /\/api\/audiobooks\/ambience\/[^/]+\/audio$/.test(p)
    || /\/api\/audiobooks\/[^/]+\/cover$/.test(p)
    || /\/api\/hoerspiele\/projects\/[^/]+\/cover$/.test(p)
    || /\/api\/hoerspiele\/artifacts\/[^/]+\/content$/.test(p)
    || /\/api\/podcasts\/[^/]+\/audio\/(stream|download)$/.test(p);
}

function isCacheableApi(url) {
  const p = url.pathname;
  if (!p.startsWith('/api/')) return false;
  if (/\/(queue\/events|export\/status|pipeline-runs|health|settings\/agents)/.test(p)) return false;
  return p.startsWith('/api/audiobooks')
    || p.startsWith('/api/hoerspiele/projects')
    || p.startsWith('/api/podcasts')
    || p === '/api/voices'
    || p === '/api/auth/me';
}

function isStaticAsset(url) {
  return url.pathname.startsWith('/assets/')
    || /\.(png|svg|woff2?|css|js|webmanifest)$/.test(url.pathname);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (isAudio(url)) {
    event.respondWith((async () => {
      const cache = await caches.open(AUDIO_CACHE);
      // Range-Anfragen (Seeking) aus dem Cache bedienen, sonst holt der
      // Browser Teilstuecke vom Netz und Offline-Seeking bricht.
      const cacheKey = new Request(url.href, { credentials: 'same-origin' });
      const hit = await cache.match(cacheKey);
      if (hit) return rangeResponse(req, hit);
      const res = await fetch(req);
      if (res.ok && res.status === 200) {
        cache.put(cacheKey, res.clone());
        void trimCache(AUDIO_CACHE, AUDIO_MAX_ENTRIES);
      }
      return res;
    })());
    return;
  }

  if (isCacheableApi(url)) {
    event.respondWith((async () => {
      const cache = await caches.open(API_CACHE);
      try {
        const res = await fetch(req);
        if (res.ok) {
          cache.put(req, res.clone());
          void trimCache(API_CACHE, API_MAX_ENTRIES);
        }
        return res;
      } catch (err) {
        const hit = await cache.match(req);
        if (hit) return hit;
        throw err;
      }
    })());
    return;
  }

  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const res = await fetch(req);
        const cache = await caches.open(ASSET_CACHE);
        cache.put('/index.html', res.clone());
        return res;
      } catch {
        return (await caches.match('/index.html')) ?? (await caches.match('/offline.html')) ?? Response.error();
      }
    })());
    return;
  }

  if (isStaticAsset(url)) {
    event.respondWith((async () => {
      const cache = await caches.open(ASSET_CACHE);
      const hit = await cache.match(req);
      const network = fetch(req).then((res) => {
        if (res.ok) cache.put(req, res.clone());
        return res;
      }).catch(() => hit);
      return hit ?? network;
    })());
  }
});

async function rangeResponse(req, full) {
  const range = req.headers.get('range');
  if (!range) return full;
  const blob = await full.blob();
  const m = /bytes=(\d*)-(\d*)/.exec(range);
  if (!m) return full;
  const start = m[1] ? Number(m[1]) : Math.max(0, blob.size - Number(m[2]));
  const end = m[2] && m[1] ? Math.min(Number(m[2]), blob.size - 1) : blob.size - 1;
  const slice = blob.slice(start, end + 1);
  return new Response(slice, {
    status: 206,
    headers: {
      'Content-Type': full.headers.get('Content-Type') || 'audio/mpeg',
      'Content-Range': `bytes ${start}-${end}/${blob.size}`,
      'Content-Length': String(slice.size),
      'Accept-Ranges': 'bytes',
    },
  });
}
