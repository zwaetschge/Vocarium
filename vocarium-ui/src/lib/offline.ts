/**
 * Offline-Schicht für Hörbücher, Hörspiele und Podcasts.
 *
 * Alles läuft über den Cache-Storage des Service Workers (`vocarium-audio-*`):
 * der Worker beantwortet Audio-Anfragen ohnehin cache-first, hier werden die
 * Einträge nur vollständig und gezielt vorbefüllt. Eine kleine Registry im
 * localStorage merkt sich, was fertig heruntergeladen wurde, damit Listen ein
 * Offline-Abzeichen zeigen können, ohne den Cache zu durchsuchen.
 */

export const AUDIO_CACHE = 'vocarium-audio-v2';
const REGISTRY_KEY = 'vocarium:offline:v1';

export type OfflineKind = 'audiobook' | 'hoerspiel' | 'podcast';

export interface OfflineEntry {
  key: string;            // z. B. audiobook:<bookId>:<voiceId>
  kind: OfflineKind;
  id: string;             // Buch-, Projekt- oder Podcast-ID
  variant?: string;       // Stimme (Hörbuch)
  title: string;
  bytes: number;
  items: number;
  savedAt: string;
}

export interface OfflineProgress { done: number; total: number; bytes: number }

export function offlineSupported(): boolean {
  return typeof window !== 'undefined' && 'caches' in window && window.isSecureContext;
}

export function isAndroidApp(): boolean {
  return typeof window !== 'undefined' && typeof (window as unknown as { VocariumAndroid?: unknown }).VocariumAndroid !== 'undefined';
}

function readRegistry(): OfflineEntry[] {
  try {
    const raw = localStorage.getItem(REGISTRY_KEY);
    const parsed = raw ? (JSON.parse(raw) as OfflineEntry[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeRegistry(entries: OfflineEntry[]) {
  localStorage.setItem(REGISTRY_KEY, JSON.stringify(entries));
  window.dispatchEvent(new CustomEvent('vocarium-offline-change'));
}

export function listOffline(kind?: OfflineKind): OfflineEntry[] {
  const all = readRegistry();
  return kind ? all.filter((e) => e.kind === kind) : all;
}

export function offlineEntry(kind: OfflineKind, id: string, variant?: string): OfflineEntry | undefined {
  return readRegistry().find((e) => e.kind === kind && e.id === id && (variant === undefined || e.variant === variant));
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
  if (bytes >= 1024 * 1024) return `${Math.round(bytes / 1024 / 1024)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

/** Lädt eine URL in den Audio-Cache; gibt die Byte-Zahl zurück (0 = schon da). */
async function cacheUrl(cache: Cache, url: string, signal?: AbortSignal): Promise<number> {
  const existing = await cache.match(url);
  if (existing) return 0;
  const res = await fetch(url, { signal, credentials: 'same-origin' });
  if (!res.ok) throw new Error(`HTTP ${res.status} für ${url}`);
  const blob = await res.blob();
  await cache.put(url, new Response(blob, { headers: res.headers }));
  return blob.size;
}

/** Zusätzlich JSON-Antworten sichern, damit Listen und Reader offline öffnen. */
async function warmJson(cache: Cache, urls: string[]) {
  const api = await caches.open('vocarium-api-v2');
  for (const url of urls) {
    try {
      const res = await fetch(url, { credentials: 'same-origin' });
      if (res.ok) await api.put(url, res);
    } catch { /* optional */ }
  }
  void cache;
}

export async function downloadAudiobook(
  book: { id: string; title: string; chapters: { index: number }[] },
  voiceId: string,
  segmentUrl: (chapter: number, segment: number) => string,
  loadChapter: (chapter: number) => Promise<{ segments: { index: number }[] }>,
  onProgress: (p: OfflineProgress) => void,
  signal?: AbortSignal,
): Promise<OfflineEntry> {
  if (!offlineSupported()) throw new Error('Offline-Speicher nur über HTTPS verfügbar');
  const cache = await caches.open(AUDIO_CACHE);
  const chapters = await Promise.all(book.chapters.map(async (c) => ({ index: c.index, segments: (await loadChapter(c.index)).segments })));
  const total = chapters.reduce((n, c) => n + c.segments.length, 0);
  let done = 0;
  let bytes = 0;
  await warmJson(cache, [
    `/api/audiobooks/${book.id}`,
    `/api/audiobooks/${book.id}?voice=${encodeURIComponent(voiceId)}`,
    `/api/audiobooks/${book.id}/bookmarks`,
    `/api/audiobooks/${book.id}/offline-voices`,
    ...chapters.map((c) => `/api/audiobooks/${book.id}/content?chapter=${c.index}`),
  ]);
  for (const chapter of chapters) {
    for (const seg of chapter.segments) {
      if (signal?.aborted) throw new DOMException('abgebrochen', 'AbortError');
      bytes += await cacheUrl(cache, segmentUrl(chapter.index, seg.index), signal);
      done += 1;
      onProgress({ done, total, bytes });
    }
  }
  const entry: OfflineEntry = {
    key: `audiobook:${book.id}:${voiceId}`, kind: 'audiobook', id: book.id, variant: voiceId,
    title: book.title, bytes, items: total, savedAt: new Date().toISOString(),
  };
  writeRegistry([...readRegistry().filter((e) => e.key !== entry.key), entry]);
  return entry;
}

export async function downloadSingleAudio(
  kind: 'hoerspiel' | 'podcast',
  id: string,
  title: string,
  url: string,
  onProgress: (p: OfflineProgress) => void,
  extraJson: string[] = [],
  signal?: AbortSignal,
): Promise<OfflineEntry> {
  if (!offlineSupported()) throw new Error('Offline-Speicher nur über HTTPS verfügbar');
  const cache = await caches.open(AUDIO_CACHE);
  await warmJson(cache, extraJson);
  const existing = await cache.match(url);
  let bytes = 0;
  if (existing) {
    bytes = Number(existing.headers.get('content-length') || 0);
  } else {
    // Fortschritt beim Streamen zählen: grosse M4A-Dateien brauchen Feedback.
    const res = await fetch(url, { signal, credentials: 'same-origin' });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    const total = Number(res.headers.get('content-length') || 0);
    const reader = res.body.getReader();
    const chunks: Uint8Array[] = [];
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      bytes += value.byteLength;
      onProgress({ done: bytes, total: total || bytes, bytes });
    }
    const blob = new Blob(chunks as BlobPart[], { type: res.headers.get('content-type') || 'audio/mpeg' });
    await cache.put(url, new Response(blob, { headers: { 'Content-Type': blob.type, 'Content-Length': String(blob.size) } }));
  }
  const entry: OfflineEntry = {
    key: `${kind}:${id}`, kind, id, title, bytes, items: 1, savedAt: new Date().toISOString(),
  };
  writeRegistry([...readRegistry().filter((e) => e.key !== entry.key), entry]);
  return entry;
}

export async function removeOffline(entry: OfflineEntry, urls: string[]) {
  if (offlineSupported()) {
    const cache = await caches.open(AUDIO_CACHE);
    await Promise.all(urls.map((u) => cache.delete(u)));
  }
  writeRegistry(readRegistry().filter((e) => e.key !== entry.key));
}

export async function storageEstimate(): Promise<{ usage: number; quota: number } | null> {
  try {
    const est = await navigator.storage?.estimate();
    return est ? { usage: est.usage || 0, quota: est.quota || 0 } : null;
  } catch {
    return null;
  }
}

/* --- Wiedergabe-Metadaten fuer Sperrbildschirm und Android-Dienst --------- */

interface AndroidBridge {
  setPlayback(playing: boolean, title: string, subtitle: string): void;
}

export interface PlaybackMeta {
  title: string;
  subtitle: string;
  artwork?: string;
  onPlay?: () => void;
  onPause?: () => void;
  onNext?: () => void;
  onPrev?: () => void;
  onSeek?: (seconds: number) => void;
}

function bridge(): AndroidBridge | undefined {
  return (window as unknown as { VocariumAndroid?: AndroidBridge }).VocariumAndroid;
}

/** Meldet Titel und Zustand an MediaSession (Browser) und die Android-App. */
export function announcePlayback(playing: boolean, meta: PlaybackMeta) {
  bridge()?.setPlayback(playing, meta.title, meta.subtitle);
  if (!('mediaSession' in navigator)) return;
  const ms = navigator.mediaSession;
  ms.metadata = new MediaMetadata({
    title: meta.title,
    artist: meta.subtitle,
    album: 'Vocarium',
    artwork: meta.artwork ? [{ src: meta.artwork, sizes: '512x512', type: 'image/png' }] : [],
  });
  ms.playbackState = playing ? 'playing' : 'paused';
  const bind = (action: MediaSessionAction, handler?: () => void) => {
    try { ms.setActionHandler(action, handler ? () => handler() : null); } catch { /* nicht unterstuetzt */ }
  };
  bind('play', meta.onPlay);
  bind('pause', meta.onPause);
  bind('nexttrack', meta.onNext);
  bind('previoustrack', meta.onPrev);
  try {
    ms.setActionHandler('seekbackward', meta.onSeek ? (d) => meta.onSeek?.(-(d.seekOffset || 15)) : null);
    ms.setActionHandler('seekforward', meta.onSeek ? (d) => meta.onSeek?.(d.seekOffset || 15) : null);
  } catch { /* optional */ }
  // Die Android-App ruft window.__vocariumMediaAction('pause') aus der
  // Benachrichtigung; hier wird die Aktion an denselben Handler geleitet.
  (window as unknown as { __vocariumMediaAction?: (a: string) => void }).__vocariumMediaAction = (action) => {
    if (action === 'play') meta.onPlay?.();
    else if (action === 'pause') meta.onPause?.();
    else if (action === 'next') meta.onNext?.();
    else if (action === 'prev') meta.onPrev?.();
  };
}

export function clearPlayback() {
  bridge()?.setPlayback(false, '', '');
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'none';
}
