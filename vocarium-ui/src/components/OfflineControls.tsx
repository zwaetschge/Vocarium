import { useCallback, useEffect, useState } from 'react';
import { audiobookSegmentLiveUrl, getAudiobook, getAudiobookContent, getOfflineVoices } from '../api';
import type { AbBook } from '../types';
import type { AbOfflineVoice } from '../api';
import {
  downloadAudiobook, downloadSingleAudio, formatBytes, listOffline, offlineEntry, offlineSupported, removeOffline,
} from '../lib/offline';
import type { OfflineEntry, OfflineKind, OfflineProgress } from '../lib/offline';

/** Reagiert auf Änderungen der Offline-Registry (auch aus anderen Komponenten). */
export function useOfflineRegistry(kind?: OfflineKind): OfflineEntry[] {
  const [entries, setEntries] = useState<OfflineEntry[]>(() => listOffline(kind));
  useEffect(() => {
    const update = () => setEntries(listOffline(kind));
    window.addEventListener('vocarium-offline-change', update);
    window.addEventListener('storage', update);
    return () => {
      window.removeEventListener('vocarium-offline-change', update);
      window.removeEventListener('storage', update);
    };
  }, [kind]);
  return entries;
}

export function OfflineBadge({ kind, id }: { kind: OfflineKind; id: string }) {
  const entries = useOfflineRegistry(kind).filter((e) => e.id === id);
  if (!entries.length) return null;
  return (
    <span className="status-chip offline-badge" title={entries.map((e) => e.variant || e.title).join(', ')}>
      <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M8 2v8M4.5 7 8 10.5 11.5 7M3 13h10" /></svg>
      Offline
    </span>
  );
}

function progressLabel(p: OfflineProgress | null, fallback: string) {
  if (!p) return fallback;
  const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
  return `${pct} % · ${formatBytes(p.bytes)}`;
}

/**
 * Menüeinträge "Offline speichern" für ein Hörbuch: eine Zeile je Stimme, die
 * das Buch komplett vertont hat, plus "entfernen" für vorhandene Kopien.
 */
export function AudiobookOfflineMenu({ book, onError, onDone }: { book: AbBook; onError: (m: string) => void; onDone?: () => void }) {
  const [voices, setVoices] = useState<AbOfflineVoice[] | null>(null);
  const [busy, setBusy] = useState('');
  const [progress, setProgress] = useState<OfflineProgress | null>(null);
  const saved = useOfflineRegistry('audiobook').filter((e) => e.id === book.id);

  useEffect(() => {
    getOfflineVoices(book.id).then((d) => setVoices(d.voices)).catch(() => setVoices([]));
  }, [book.id]);

  const save = useCallback(async (voiceId: string) => {
    setBusy(voiceId);
    setProgress(null);
    try {
      const detail = await getAudiobook(book.id, voiceId);
      await downloadAudiobook(
        { id: book.id, title: book.title, chapters: detail.chapters },
        voiceId,
        (c, s) => audiobookSegmentLiveUrl(book.id, voiceId, c, s),
        (c) => getAudiobookContent(book.id, c),
        setProgress,
      );
      onDone?.();
    } catch (exc) {
      onError(exc instanceof Error ? exc.message : 'Offline-Download fehlgeschlagen');
    } finally {
      setBusy('');
    }
  }, [book, onError, onDone]);

  const remove = useCallback(async (entry: OfflineEntry) => {
    try {
      const detail = await getAudiobook(book.id, entry.variant);
      const urls: string[] = [];
      for (const c of detail.chapters) {
        const content = await getAudiobookContent(book.id, c.index);
        for (const s of content.segments) urls.push(audiobookSegmentLiveUrl(book.id, entry.variant || '', c.index, s.index));
      }
      await removeOffline(entry, urls);
    } catch (exc) {
      onError(exc instanceof Error ? exc.message : 'Entfernen fehlgeschlagen');
    }
  }, [book.id, onError]);

  const complete = (voices || []).filter((v) => v.complete);
  return (
    <>
      <div className="label-eyebrow" style={{ padding: '4px 8px' }}>Offline</div>
      {!offlineSupported() && (
        <span style={{ fontSize: '11.5px', opacity: 0.6, padding: '2px 8px' }}>Nur über HTTPS verfügbar.</span>
      )}
      {voices === null && <span style={{ fontSize: '11.5px', opacity: 0.6, padding: '2px 8px' }}>Stimmen werden geprüft…</span>}
      {voices !== null && !complete.length && (
        <span style={{ fontSize: '11.5px', opacity: 0.6, padding: '2px 8px' }}>Noch keine Stimme hat das ganze Buch vertont.</span>
      )}
      {complete.map((v) => {
        const entry = saved.find((e) => e.variant === v.voice_id);
        return (
          <button
            key={v.voice_id}
            className="btn btn-ghost btn-sm"
            style={{ justifyContent: 'flex-start', gap: '8px' }}
            disabled={busy !== '' || !offlineSupported()}
            onClick={() => (entry ? void remove(entry) : void save(v.voice_id))}
          >
            <span style={{ flex: 1, textAlign: 'left' }}>{v.voice_id}</span>
            <span style={{ fontSize: '10.5px', fontFamily: 'var(--font-mono)', opacity: 0.7 }}>
              {busy === v.voice_id ? progressLabel(progress, '…') : entry ? `entfernen · ${formatBytes(entry.bytes)}` : 'speichern'}
            </span>
          </button>
        );
      })}
    </>
  );
}

/** Ein Knopf für Einzeldateien (Hörspiel-M4A, Podcast-MP3). */
export function SingleOfflineButton({
  kind, id, title, url, extraJson = [], className = 'btn btn-sm btn-ghost',
}: { kind: 'hoerspiel' | 'podcast'; id: string; title: string; url: string; extraJson?: string[]; className?: string }) {
  const entry = useOfflineRegistry(kind).find((e) => e.id === id);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<OfflineProgress | null>(null);
  const [error, setError] = useState('');

  const run = useCallback(async () => {
    setBusy(true);
    setError('');
    setProgress(null);
    try {
      if (entry) await removeOffline(entry, [url]);
      else await downloadSingleAudio(kind, id, title, url, setProgress, extraJson);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Offline-Download fehlgeschlagen');
    } finally {
      setBusy(false);
    }
  }, [entry, kind, id, title, url, extraJson]);

  if (!offlineSupported()) return null;
  return (
    <button type="button" className={className} disabled={busy} onClick={() => void run()} title={error || undefined}>
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M8 2v8M4.5 7 8 10.5 11.5 7M3 13h10" /></svg>
      {busy ? progressLabel(progress, '…') : entry ? `Offline · ${formatBytes(entry.bytes)}` : 'Offline speichern'}
    </button>
  );
}

/** Verwendet offlineEntry direkt, damit Aufrufer ohne Hook prüfen können. */
export const hasOfflineCopy = (kind: OfflineKind, id: string) => Boolean(offlineEntry(kind, id));
