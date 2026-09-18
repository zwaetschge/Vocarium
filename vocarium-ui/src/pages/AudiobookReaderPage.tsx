import {ModeMenuButton} from '../components/ModeNavigation';
import { adoptAudio, releaseAudio, setSpeed as setPlayerSpeed, setVolume as setPlayerVolume } from '../state/player';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  addAudiobookBookmark,
  audiobookExportDownloadUrl,
  audiobookExportStatus,
  audiobookSegmentLiveUrl,
  deleteAudiobookBookmark,
  generateAudiobook,
  getAudiobook,
  getAudiobookContent,
  getVoices,
  listAudiobookBookmarks,
  enqueueAbGeneration,
  patchAudiobook,
  saveAudiobookProgress,
  startAudiobookExport,
  startListeningSession,
  updateListeningSession,
} from '../api';
import { announcePlayback, downloadAudiobook } from '../lib/offline';
import { deleteAmbience, getOfflineVoices, getReaderPrefs, listAmbience, putReaderPrefs, searchAudiobook, uploadAmbience } from '../api';
import type { AbAmbience, AbBookmark, AbOfflineVoice, AbSearchHit } from '../api';
import type { AbSegment, Voice } from '../types';
import { useAmbience } from '../hooks/useAmbience';
import { attachAnalyser } from '../lib/audioReactive';
import { AudioReactiveBars, AudioReactiveGlow } from '../components/AudioReactive';
import { VoiceOptions } from '../components/VoiceOptions';
import { engineVoices as listEngineVoices } from '../voiceUtils';

const SPEEDS = [0.8, 1, 1.15, 1.3, 1.5, 1.75, 2];
const SLEEP_OPTIONS = [
  { label: 'Aus', value: 0 },
  { label: '15 min', value: 15 },
  { label: '30 min', value: 30 },
  { label: '45 min', value: 45 },
  { label: '60 min', value: 60 },
  { label: 'Kapitelende', value: -1 },
];

interface ReaderPrefs {
  fontSize: number;
  lineHeight: number;
  fontFamily: 'sans' | 'serif' | 'mono';
  theme: 'app' | 'sepia' | 'paper' | 'night';
  bionic: boolean;
}
const DEFAULT_PREFS: ReaderPrefs = { fontSize: 16, lineHeight: 1.85, fontFamily: 'sans', theme: 'app', bionic: false };
const FONT_MAP: Record<ReaderPrefs['fontFamily'], string> = {
  sans: 'inherit',
  serif: 'Georgia, "Times New Roman", serif',
  mono: 'var(--font-mono, "JetBrains Mono", monospace)',
};
const THEMES: Record<ReaderPrefs['theme'], { bg: string; fg: string; hl: string; label: string }> = {
  app:   { bg: 'transparent', fg: 'inherit', hl: 'rgba(123,97,255,0.22)', label: 'Studio' },
  sepia: { bg: '#f4ead8', fg: '#3d3020', hl: 'rgba(197,159,95,0.35)', label: 'Sepia' },
  paper: { bg: '#f7f7f5', fg: '#1d1d1f', hl: 'rgba(24,86,255,0.18)', label: 'Papier' },
  night: { bg: '#000000', fg: 'rgba(255,255,255,0.82)', hl: 'rgba(123,97,255,0.30)', label: 'Nacht' },
};

function loadPrefs(): ReaderPrefs {
  try {
    const raw = JSON.parse(localStorage.getItem('ab-reader-prefs') || '{}');
    if (raw.serif === true && !raw.fontFamily) raw.fontFamily = 'serif';
    delete raw.serif;
    return { ...DEFAULT_PREFS, ...raw };
  } catch { return DEFAULT_PREFS; }
}

/** Bionic reading: erste ~40 % jedes Wortes fett. */
function Bionic({ text }: { text: string }) {
  return (
    <>
      {text.split(/(\s+)/).map((part, i) =>
        /\s/.test(part) || !part ? part : (
          <span key={i}>
            <b>{part.slice(0, Math.max(1, Math.ceil(part.length * 0.4)))}</b>
            {part.slice(Math.max(1, Math.ceil(part.length * 0.4)))}
          </span>
        ),
      )}
    </>
  );
}

/** Aktives Segment: Wort-Highlighting über zeichenproportionale Heuristik auf
 *  audio.currentTime — Cantos Ansatz, es gibt keine echten Wort-Timings. */
function ActiveSegment({ text, wordIndex, bionic }: { text: string; wordIndex: number; bionic: boolean }) {
  const words = useMemo(() => text.split(/\s+/), [text]);
  return (
    <>
      {words.map((w, i) => (
        <span
          key={i}
          style={i === wordIndex ? { background: 'rgba(255,255,255,0.28)', borderRadius: '3px' } : undefined}
        >
          {bionic ? <Bionic text={w} /> : w}{' '}
        </span>
      ))}
    </>
  );
}

export default function AudiobookReaderPage({ id, visible = true }: { id: string; visible?: boolean }) {
  const [book, setBook] = useState<Awaited<ReturnType<typeof getAudiobook>> | null>(null);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voiceId, setVoiceId] = useState('');
  const [chapter, setChapter] = useState(0);
  const [segments, setSegments] = useState<AbSegment[]>([]);
  const [contentError, setContentError] = useState('');
  const [contentAttempt, setContentAttempt] = useState(0);
  const [contentLoading, setContentLoading] = useState(true);
  const [bookmarks, setBookmarks] = useState<AbBookmark[]>([]);
  const [drawer, setDrawer] = useState<'none' | 'chapters' | 'bookmarks' | 'prefs' | 'offline'>('none');
  const [search, setSearch] = useState('');
  const [playing, setPlaying] = useState(false);
  const [activeSegment, setActiveSegment] = useState<number | null>(null);
  const [activeWord, setActiveWord] = useState(-1);
  const [speed, setSpeed] = useState(1);
  const [volume, setVolume] = useState(1);
  const [sleep, setSleep] = useState(0);
  const [sleepUntil, setSleepUntil] = useState<number | null>(null);
  const [showShortcuts, setShowShortcuts] = useState(false);
  const [exporting, setExporting] = useState<'' | 'm4b' | 'mp3'>('');
  const [muted, setMuted] = useState(false);
  const [sleepToast, setSleepToast] = useState(false);
  const [readyVoices, setReadyVoices] = useState<AbOfflineVoice[]>([]);
  const [offlineBusy, setOfflineBusy] = useState('');
  const [offlineProgress, setOfflineProgress] = useState(0);
  const [ambienceSounds, setAmbienceSounds] = useState<AbAmbience[]>([]);
  const ambience = useAmbience();
  const ambienceFileRef = useRef<HTMLInputElement>(null);
  const [prefs, setPrefs] = useState<ReaderPrefs>(loadPrefs);
  const [error, setError] = useState('');

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const playTokenRef = useRef(0);
  const playbackAbortRef = useRef<AbortController | null>(null);
  // Hörsitzung: einmal pro Reader-Besuch angelegt, Fortschritt periodisch gemeldet.
  const sessionRef = useRef<{ id: string; ms: number; segments: number } | null>(null);
  const rafRef = useRef(0);
  const textRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef({ playing: false, activeSegment: null as number | null, chapter: 0, sleep: 0, sleepUntil: null as number | null });
  stateRef.current = { playing, activeSegment, chapter, sleep, sleepUntil };

  const theme = THEMES[prefs.theme];
  const engineVoices = useMemo(() => listEngineVoices(voices), [voices]);
  const currentChapter = book?.chapters.find((c) => c.index === chapter);
  const generating = book?.generation?.status === 'running';

  // Prefs: localStorage für sofortiges Rendern, Server als geräteübergreifende
  // Wahrheit (500 ms debounced PUT).
  const prefsSyncRef = useRef(0);
  const setPref = useCallback(<K extends keyof ReaderPrefs>(key: K, value: ReaderPrefs[K]) => {
    setPrefs((p) => {
      const next = { ...p, [key]: value };
      localStorage.setItem('ab-reader-prefs', JSON.stringify(next));
      window.clearTimeout(prefsSyncRef.current);
      prefsSyncRef.current = window.setTimeout(() => {
        void putReaderPrefs(next as unknown as Record<string, unknown>).catch(() => undefined);
      }, 500);
      return next;
    });
  }, []);

  useEffect(() => {
    getReaderPrefs().then((remote) => {
      if (remote && typeof remote.fontSize === 'number') {
        setPrefs((p) => {
          const merged = { ...p, ...remote } as ReaderPrefs;
          localStorage.setItem('ab-reader-prefs', JSON.stringify(merged));
          return merged;
        });
      }
    }).catch(() => undefined);
  }, []);

  const refresh = useCallback(async (voice?: string) => {
    try {
      const detail = await getAudiobook(id, voice);
      setBook(detail);
      return detail;
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Buch konnte nicht geladen werden');
      return null;
    }
  }, [id]);

  useEffect(() => {
    getVoices().then(setVoices).catch(() => undefined);
    listAmbience().then((d) => setAmbienceSounds(d.sounds)).catch(() => undefined);
    listAudiobookBookmarks(id).then((d) => setBookmarks(d.bookmarks)).catch(() => undefined);
    void refresh().then((detail) => {
      if (!detail) return;
      if (detail.voice_id) setVoiceId(detail.voice_id);
      if (detail.progress) {
        setChapter(detail.progress.chapterIndex);
        setActiveSegment(detail.progress.segmentIndex);
      }
    });
  }, [refresh, id]);

  useEffect(() => { if (voiceId) void refresh(voiceId); }, [voiceId, refresh]);
  // Welche Stimmen haben dieses Buch (teilweise) im Cache — für ✓ im
  // Stimmen-Dropdown und den Offline-Dialog.
  const refreshReadyVoices = useCallback(() => {
    getOfflineVoices(id).then((d) => setReadyVoices(d.voices)).catch(() => undefined);
  }, [id]);
  useEffect(() => { refreshReadyVoices(); }, [refreshReadyVoices, generating]);
  useEffect(() => {
    let current = true;
    setContentLoading(true);
    setContentError('');
    getAudiobookContent(id, chapter).then((d) => {
      if (current) setSegments(d.segments);
    }).catch((exc) => {
      if (current) { setSegments([]); setContentError(exc instanceof Error ? exc.message : 'Kapitel konnte nicht geladen werden'); }
    }).finally(() => { if (current) setContentLoading(false); });
    return () => { current = false; };
  }, [id, chapter, contentAttempt]);
  useEffect(() => {
    if (!generating) return;
    const t = window.setInterval(() => void refresh(voiceId || undefined), 3000);
    return () => window.clearInterval(t);
  }, [generating, refresh, voiceId]);

  const mediaRef = useRef<{ next: () => void; prev: () => void }>({ next: () => undefined, prev: () => undefined });

  const stop = useCallback(() => {
    playTokenRef.current += 1;
    playbackAbortRef.current?.abort();
    playbackAbortRef.current = null;
    cancelAnimationFrame(rafRef.current);
    releaseAudio(audioRef.current);
    audioRef.current?.pause();
    audioRef.current = null;
    setPlaying(false);
    setActiveWord(-1);
  }, []);
  useEffect(() => stop, [stop]);
  useEffect(() => { if (audioRef.current) { audioRef.current.playbackRate = speed; setPlayerSpeed(speed); } }, [speed]);
  useEffect(() => { if (audioRef.current) { audioRef.current.volume = muted ? 0 : volume; setPlayerVolume(muted ? 0 : volume); } }, [volume, muted]);

  useEffect(() => {
    if (activeSegment === null) return;
    textRef.current?.querySelector(`[data-seg="${activeSegment}"]`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [activeSegment]);

  // Hörzeit zählen, solange abgespielt wird; alle 15 s an die API melden.
  useEffect(() => {
    if (!playing) return;
    const tick = window.setInterval(() => {
      const s = sessionRef.current;
      if (!s) return;
      s.ms += 5000;
      if (s.ms % 15000 === 0) void updateListeningSession(s.id, s.ms, s.segments).catch(() => undefined);
    }, 5000);
    return () => {
      window.clearInterval(tick);
      const s = sessionRef.current;
      if (s) void updateListeningSession(s.id, s.ms, s.segments).catch(() => undefined);
    };
  }, [playing]);

  // Wort-Index aus currentTime — zeichenproportional über das Segment verteilt.
  const trackWords = useCallback((audio: HTMLAudioElement, text: string, token: number) => {
    const words = text.split(/\s+/);
    const weights = words.map((w) => w.length + 1);
    const total = weights.reduce((a, b) => a + b, 0);
    const step = () => {
      if (playTokenRef.current !== token || audioRef.current !== audio) return;
      if (audio.duration > 0) {
        const frac = Math.min(1, audio.currentTime / audio.duration);
        let acc = 0; let idx = words.length - 1;
        for (let i = 0; i < weights.length; i++) {
          acc += weights[i] / total;
          if (frac <= acc) { idx = i; break; }
        }
        setActiveWord((prev) => (prev === idx ? prev : idx));
      }
      rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);
  }, []);

  const playFrom = useCallback(async (startChapter: number, startSegment: number) => {
    if (!voiceId || !book) return;
    stop();
    const token = ++playTokenRef.current;
    const controller = new AbortController();
    playbackAbortRef.current = controller;
    setError('');
    setPlaying(true);
    if (stateRef.current.sleep > 0) setSleepUntil(Date.now() + stateRef.current.sleep * 60_000);
    if (!sessionRef.current) {
      try { sessionRef.current = { id: (await startListeningSession(id)).id, ms: 0, segments: 0 }; }
      catch { /* Statistik optional */ }
    }

    let ch = startChapter;
    let detail = book;
    while (playTokenRef.current === token) {
      const chInfo = detail.chapters.find((c) => c.index === ch);
      if (!chInfo) break; // Buchende — fehlende Segmente streamen live
      if (ch !== stateRef.current.chapter) setChapter(ch);
      let content: Awaited<ReturnType<typeof getAudiobookContent>>;
      try { content = await getAudiobookContent(id, ch); }
      catch { if (playTokenRef.current === token) { stop(); setError('Kapitel konnte nicht geladen werden. Bitte erneut abspielen.'); } return; }
      if (playTokenRef.current !== token) return;
      const list = content.segments.filter((s) => ch !== startChapter || s.index >= startSegment);

      for (const seg of list) {
        if (playTokenRef.current !== token) return;
        if (stateRef.current.sleepUntil && Date.now() >= stateRef.current.sleepUntil) {
          setSleepUntil(null); setSleep(0); stop();
          setSleepToast(true); window.setTimeout(() => setSleepToast(false), 4000);
          return;
        }
        setActiveSegment(seg.index);
        void saveAudiobookProgress(id, ch, seg.index).catch(() => setError('Hörposition konnte nicht gespeichert werden.'));
        if (sessionRef.current) sessionRef.current.segments += 1;
        const audio = new Audio(audiobookSegmentLiveUrl(id, voiceId, ch, seg.index));
        audio.playbackRate = speed;
        audio.volume = muted ? 0 : volume;
        // Sekundengenaues Fortsetzen: gespeicherte Position gilt nur für das
        // exakt passende Segment und wird nach Gebrauch verworfen.
        try {
          const raw = localStorage.getItem(`vocarium:pos:${id}`);
          if (raw) {
            const pos = JSON.parse(raw) as { chapterIndex: number; segmentIndex: number; currentTime: number };
            if (pos.chapterIndex === ch && pos.segmentIndex === seg.index && pos.currentTime > 0.5) {
              audio.addEventListener('loadedmetadata', () => {
                if (pos.currentTime < audio.duration - 0.5) audio.currentTime = pos.currentTime;
              }, { once: true });
            }
            localStorage.removeItem(`vocarium:pos:${id}`);
          }
        } catch { /* Position optional */ }
        // Position ~alle 3 s sichern
        let lastSave = 0;
        audio.addEventListener('timeupdate', () => {
          const now = Date.now();
          if (now - lastSave < 3000) return;
          lastSave = now;
          localStorage.setItem(`vocarium:pos:${id}`, JSON.stringify({
            chapterIndex: ch, segmentIndex: seg.index, currentTime: audio.currentTime,
          }));
        });
        audioRef.current = audio;
        adoptAudio(audio, { key:`book:${id}:${voiceId}`, title:detail.title, subtitle:chInfo.title, href:`/audiobooks/${id}`, onStop:stop, next:()=>mediaRef.current.next(), prev:()=>mediaRef.current.prev() });
        audio.addEventListener('play', () => setPlaying(true));
        audio.addEventListener('pause', () => setPlaying(false));
        audio.addEventListener('ratechange', () => setSpeed(audio.playbackRate));
        audio.addEventListener('volumechange', () => { if (audio.volume > 0) { setVolume(audio.volume); setMuted(false); } });
        // Sperrbildschirm, Kopfhoerertasten und die Android-Benachrichtigung
        // bekommen Titel und Zustand; ihre Aktionen laufen auf dieses Element.
        const chapterTitle = (book?.chapters?.[ch] as { title?: string } | undefined)?.title;
        const meta = () => ({
          title: book?.title || 'Vocarium',
          subtitle: `Kapitel ${ch + 1}${chapterTitle ? ` · ${chapterTitle}` : ''}`,
          onPlay: () => { void audio.play(); },
          onPause: () => audio.pause(),
          onNext: () => mediaRef.current.next(),
          onPrev: () => mediaRef.current.prev(),
          onSeek: (delta: number) => { audio.currentTime = Math.max(0, audio.currentTime + delta); },
        });
        audio.addEventListener('play', () => announcePlayback(true, meta()));
        audio.addEventListener('pause', () => announcePlayback(false, meta()));
        attachAnalyser(audio);
        // Die nächsten zwei Segmente vorwärmen: der Live-Endpoint generiert
        // sie dabei bereits, während das aktuelle noch spielt.
        for (const ahead of [1, 2]) {
          const nxt = list[list.indexOf(seg) + ahead];
          if (nxt) void fetch(audiobookSegmentLiveUrl(id, voiceId, ch, nxt.index)).catch(() => undefined);
        }
        if ('mediaSession' in navigator) {
          navigator.mediaSession.metadata = new MediaMetadata({
            title: detail.title, artist: voiceId, album: chInfo.title,
          });
        }
        trackWords(audio, seg.text, token);
        try {
          await new Promise<void>((resolve, reject) => {
            const cleanup = () => { controller.signal.removeEventListener('abort', aborted); audio.removeEventListener('ended', ended); audio.removeEventListener('error', failed); };
            const aborted = () => { cleanup(); reject(new Error('Playback stopped')); };
            const failed = () => { cleanup(); reject(new Error('Audio fehlt')); };
            const ended = () => {
              cleanup();
              if (ch === detail.chapters[detail.chapters.length - 1]?.index && seg.index === list[list.length - 1]?.index) {
                void saveAudiobookProgress(id, ch, seg.index, true).catch(() => setError('Hörfortschritt konnte nicht gespeichert werden.'));
              }
              resolve();
            };
            controller.signal.addEventListener('abort', aborted, {once:true});
            audio.addEventListener('ended', ended, {once:true});
            audio.addEventListener('error', failed, {once:true});
            if (controller.signal.aborted) aborted(); else void audio.play().catch(failed);
          });
        } catch {
          if (playTokenRef.current === token) { stop(); setError('Audio konnte nicht abgespielt werden. Prüfe Verbindung und Stimme und versuche es erneut.'); }
          return;
        }
      }
      if (stateRef.current.sleep === -1) {
        setSleep(0);
        setSleepToast(true); window.setTimeout(() => setSleepToast(false), 4000);
        break; // Kapitelende-Timer
      }
      ch += 1;
      startSegment = 0;
      const refreshed = await refresh(voiceId);
      if (refreshed) detail = refreshed;
    }
    if (playTokenRef.current === token) { setPlaying(false); setActiveSegment(null); setActiveWord(-1); }
  }, [voiceId, book, id, speed, volume, muted, stop, refresh, trackWords]);

  const togglePlay = useCallback(() => {
    const audio = audioRef.current;
    if (audio && stateRef.current.playing) { audio.pause(); setPlaying(false); return; }
    if (audio) { void audio.play().then(() => setPlaying(true)).catch(() => { setPlaying(false); setError('Wiedergabe fehlgeschlagen. Bitte erneut versuchen.'); }); return; }
    const start = stateRef.current.activeSegment ?? (book?.progress?.chapterIndex === stateRef.current.chapter ? book.progress.segmentIndex : undefined) ?? segments[0]?.index ?? 0;
    void playFrom(stateRef.current.chapter, start);
  }, [book, segments, playFrom]);

  const skip = useCallback((delta: number) => {
    const current = stateRef.current.activeSegment ?? segments[0]?.index ?? 0;
    const ids = segments.map((s) => s.index);
    const target = ids[Math.max(0, Math.min(ids.length - 1, ids.indexOf(current) + delta))];
    if (target !== undefined) void playFrom(stateRef.current.chapter, target);
  }, [segments, playFrom]);

  useEffect(() => {
    mediaRef.current = { next: () => skip(1), prev: () => skip(-1) };
  }, [skip]);

  const goChapter = useCallback((delta: number) => {
    const total = book?.total_chapters ?? 0;
    const target = Math.max(0, Math.min(total - 1, stateRef.current.chapter + delta));
    if (target === stateRef.current.chapter) return;
    stop();
    setChapter(target);
    setActiveSegment(null);
  }, [book, stop]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (!visible) return;
      if (e.key === 'Escape') { setShowShortcuts(false); setDrawer('none'); return; }
      if ((e.target as HTMLElement)?.closest('input, textarea, select, button, a, summary, [contenteditable="true"]')) return;
      if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
      else if (e.code === 'ArrowRight' && e.shiftKey) goChapter(1);
      else if (e.code === 'ArrowLeft' && e.shiftKey) goChapter(-1);
      else if (e.code === 'ArrowRight') skip(1);
      else if (e.code === 'ArrowLeft') skip(-1);
      else if (e.key === '?') setShowShortcuts((v) => !v);
      else if (e.key === 'Escape') { setShowShortcuts(false); setDrawer('none'); }
      else if (e.key === 'b') void addBookmark();
      else if (e.key === 'm') setMuted((v) => !v);
      else if (e.key === '+') setSpeed((s) => SPEEDS[Math.min(SPEEDS.length - 1, SPEEDS.indexOf(s) + 1)]);
      else if (e.key === '-') setSpeed((s) => SPEEDS[Math.max(0, SPEEDS.indexOf(s) - 1)]);
      else if (e.key === '1') setSpeed(1);
      else if (e.key === '2') setSpeed(1.5);
      else if (e.key === '3') setSpeed(2);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });

  const addBookmark = useCallback(async () => {
    const si = stateRef.current.activeSegment ?? segments[0]?.index ?? 0;
    // Textauszug (erste ~90 Zeichen) als Notiz — Cantos Lesezeichen-Vorschau.
    const excerpt = (segments.find((s) => s.index === si)?.text ?? '').slice(0, 90);
    await addAudiobookBookmark(id, stateRef.current.chapter, si, excerpt);
    setBookmarks((await listAudiobookBookmarks(id)).bookmarks);
  }, [id, segments]);

  const searchHits = useMemo(() => {
    if (search.trim().length < 2) return [];
    const q = search.trim().toLowerCase();
    return segments.filter((s) => s.text.toLowerCase().includes(q)).slice(0, 20);
  }, [search, segments]);

  // Buchweite semantische Suche (Embeddings, 400 ms debounced)
  const [bookHits, setBookHits] = useState<AbSearchHit[]>([]);
  useEffect(() => {
    const q = search.trim();
    if (q.length < 3) { setBookHits([]); return; }
    const timer = window.setTimeout(() => {
      searchAudiobook(id, q)
        .then((d) => setBookHits(d.results))
        .catch(() => setBookHits([]));
    }, 400);
    return () => window.clearTimeout(timer);
  }, [search, id]);

  if (!book) return <div role={error ? 'alert' : 'status'} className="studio-section"><p>{error || 'Buch wird geladen…'}</p>{error && <button className="btn btn-secondary" onClick={()=>void refresh().then(detail=>{ if(detail) {setError('');setVoiceId(detail.voice_id || '');setChapter(detail.progress?.chapterIndex || 0);setActiveSegment(detail.progress?.segmentIndex ?? null);} })}>Buch erneut laden</button>}</div>;

  const status = !voiceId
    ? { text: 'Stimme wählen, um dieses Buch zu vertonen', tone: 'rgba(232,149,88,0.9)' }
    : generating
      ? { text: `Wird umgewandelt — ${book.generation?.done}/${book.generation?.total}`, tone: 'rgba(232,149,88,0.9)' }
      : currentChapter?.complete
        ? { text: 'Hörbereit', tone: 'rgba(7,202,107,0.9)' }
        : { text: `Streaming — Segmente entstehen beim Hören (${currentChapter?.cachedSegments ?? 0}/${currentChapter?.totalSegments ?? 0} im Cache)`, tone: 'rgba(123,177,255,0.9)' };

  const drawerBtn = (key: typeof drawer, label: string, accessibleLabel = label) => (
    <button
      className={drawer === key ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
      aria-label={accessibleLabel}
      aria-expanded={drawer === key}
      aria-controls="reader-panel"
      onClick={() => setDrawer(drawer === key ? 'none' : key)}
    >{label}</button>
  );

  return (
    <div className="reader-layout" style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      {error && <div role="alert" className="hs-error">{error} <button className="btn btn-ghost btn-sm" onClick={()=>setError('')}>Schließen</button></div>}
      <div className="glass reader-topbar">
        <div className="reader-topbar-row reader-heading">
        <Link to="/audiobooks" className="btn btn-ghost btn-sm" aria-label="Zurück">←</Link>
        <div className="reader-title" style={{ flex: 1, minWidth: 0 }}>
          <strong style={{ display: 'block', fontSize: '15px', fontFamily: 'var(--font-display)' }}>{book.title}</strong>
          <span style={{ fontSize: '11.5px', opacity: 0.6 }}>
            Kapitel {chapter + 1}/{book.total_chapters} · {currentChapter?.title}
          </span>
        </div>
        <div className="reader-tools"><ModeMenuButton /><Link to="/settings/allgemein" className="btn btn-ghost btn-sm" aria-label="Einstellungen öffnen">⚙</Link>
        {drawerBtn('chapters', 'Kapitel')}
        {drawerBtn('bookmarks', `⚑ ${bookmarks.length}`, `Lesezeichen (${bookmarks.length})`)}
        {drawerBtn('offline', '⬇', 'Offline-Kapitel')}
        {drawerBtn('prefs', 'Aa', 'Leseeinstellungen')}
        </div>
        </div>
        <details className="reader-production">
          <summary>Stimme &amp; Vertonung <span style={{ color: status.tone }}>{generating ? 'In Arbeit' : currentChapter?.complete ? 'Hörbereit' : 'Optionen'}</span></summary>
          <div className="reader-production-content">
        <select
          className="input-field" value={voiceId} style={{ width: '170px', padding: '8px 32px 8px 12px' }} aria-label="Stimme"
          onChange={async (e) => { setVoiceId(e.target.value); await patchAudiobook(id, { voice_id: e.target.value || null }); }}
        >
          <option value="">— Stimme —</option>
          <VoiceOptions
            voices={engineVoices}
            label={(v) => {
              const ready = readyVoices.find((r) => r.voice_id === v.id);
              const mark = ready?.complete ? ' ✓' : ready ? ` · ${ready.cachedSegments}/${ready.totalSegments}` : '';
              return `${v.name}${mark}`;
            }}
          />
        </select>

      <div aria-live="polite" className="reader-topbar-row" style={{
        fontSize: '12px', color: status.tone,
      }}>
        <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: status.tone }} />
        {status.text}
        {sleepUntil && <span style={{ opacity: 0.8 }}>· Sleep {Math.max(0, Math.ceil((sleepUntil - Date.now()) / 60000))} min</span>}
        {sleep === -1 && <span style={{ opacity: 0.8 }}>· Sleep: Kapitelende</span>}
        {voiceId && !generating && !currentChapter?.complete && (
          <button className="btn btn-secondary btn-sm" onClick={async () => {
            await generateAudiobook(id, { voice_id: voiceId, chapter });
            await refresh(voiceId);
          }}>Kapitel generieren</button>
        )}
        {voiceId && !generating && book.chapters.some((c) => !c.complete) && (
          <>
            <button className="btn btn-ghost btn-sm" title="Jetzt in die Warteschlange"
              onClick={async () => {
                try { await enqueueAbGeneration(id, voiceId); } catch (exc) { setError(String(exc)); }
                await refresh(voiceId);
              }}>Ganzes Buch</button>
            <button className="btn btn-ghost btn-sm" title="Erst außerhalb der Geschäftszeiten (ab 18 Uhr / Wochenende)"
              onClick={async () => {
                try { await enqueueAbGeneration(id, voiceId, { priority: 8 }); } catch (exc) { setError(String(exc)); }
              }}>☾ Nachts</button>
          </>
        )}
        {voiceId && book.chapters.length > 0 && book.chapters.every((c) => c.complete) && (['m4b', 'mp3'] as const).map((fmt) => (
          <button
            key={fmt} className="btn btn-ghost btn-sm" disabled={exporting !== ''}
            onClick={async () => {
              setExporting(fmt);
              try {
                const pre = await audiobookExportStatus(id, voiceId, fmt);
                if (!pre.ready) {
                  await startAudiobookExport(id, voiceId, fmt);
                  for (let i = 0; i < 600; i++) {
                    await new Promise((r) => setTimeout(r, 2000));
                    const s = await audiobookExportStatus(id, voiceId, fmt);
                    if (s.job?.status === 'failed') throw new Error(s.job.error || 'Export fehlgeschlagen');
                    if (s.ready && s.job?.status !== 'running') break;
                  }
                }
                window.location.href = audiobookExportDownloadUrl(id, voiceId, fmt);
              } catch (exc) {
                setError(exc instanceof Error ? exc.message : 'Export fehlgeschlagen');
              } finally {
                setExporting('');
              }
            }}
          >{exporting === fmt ? '…' : `↓ ${fmt.toUpperCase()}`}</button>
        ))}
      </div>
          </div>
        </details>
      </div>

      <div className="reader-body" data-panel-open={drawer !== 'none'}>
        {drawer !== 'none' && (
          <section id="reader-panel" className="reader-panel" aria-label={{ chapters: 'Kapitel', bookmarks: 'Lesezeichen', offline: 'Offline-Kapitel', prefs: 'Leseeinstellungen' }[drawer]}>
            <div className="reader-panel-heading">
              <strong>{{ chapters: 'Kapitel', bookmarks: 'Lesezeichen', offline: 'Offline-Kapitel', prefs: 'Leseeinstellungen' }[drawer]}</strong>
              <button className="btn btn-ghost btn-sm" aria-label="Panel schließen" onClick={() => setDrawer('none')}>Schließen</button>
            </div>
        {drawer === 'chapters' && (
          <div className="card reader-panel-content" style={{ overflowY: 'auto', padding: '10px', flexShrink: 0 }}>
            <input
              className="input-field" placeholder="Suchen…" value={search}
              onChange={(e) => setSearch(e.target.value)} style={{ marginBottom: '8px' }}
            />
            {search.trim().length >= 2 ? (
              <>
                <div className="label-eyebrow" style={{ padding: '4px 6px' }}>Im Kapitel</div>
                {searchHits.map((s) => (
                  <button key={s.index} className="btn btn-ghost btn-sm" style={{ display: 'block', width: '100%', textAlign: 'left', fontSize: '11.5px' }}
                    onClick={() => { setDrawer('none'); void playFrom(chapter, s.index); }}>
                    …{s.text.slice(0, 60)}…
                  </button>
                ))}
                {!searchHits.length && <p style={{ fontSize: '12px', opacity: 0.6, padding: '4px 6px' }}>Keine Treffer.</p>}
                {bookHits.length > 0 && (
                  <>
                    <div className="label-eyebrow" style={{ padding: '10px 6px 4px' }}>Im ganzen Buch</div>
                    {bookHits.map((h) => (
                      <button key={`${h.chapterIndex}-${h.index}`} className="btn btn-ghost btn-sm"
                        style={{ display: 'block', width: '100%', textAlign: 'left', fontSize: '11.5px', whiteSpace: 'normal' }}
                        onClick={() => { setDrawer('none'); setChapter(h.chapterIndex); void playFrom(h.chapterIndex, h.index); }}>
                        <span style={{ fontFamily: 'var(--font-mono)', fontSize: '10px', opacity: 0.55 }}>Kap. {h.chapterIndex + 1} · </span>
                        …{h.text.slice(0, 70)}…
                      </button>
                    ))}
                  </>
                )}
              </>
            ) : book.chapters.map((c) => (
              <button
                key={c.index}
                onClick={() => { stop(); setChapter(c.index); setActiveSegment(null); setDrawer('none'); }}
                style={{
                  display: 'flex', width: '100%', alignItems: 'center', gap: '8px',
                  padding: '8px 10px', borderRadius: '8px', border: 'none', cursor: 'pointer',
                  background: c.index === chapter ? 'rgba(123,97,255,0.16)' : 'transparent',
                  color: 'inherit', textAlign: 'left', fontSize: '12.5px',
                }}
              >
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.title}</span>
                <span style={{ fontSize: '10px', fontFamily: 'var(--font-mono)', opacity: 0.6 }}>
                  {c.complete ? '✓' : `${c.cachedSegments}/${c.totalSegments}`}
                </span>
              </button>
            ))}
          </div>
        )}

        {drawer === 'bookmarks' && (
          <div className="card reader-panel-content" style={{ overflowY: 'auto', padding: '10px', flexShrink: 0 }}>
            <button className="btn btn-secondary btn-sm" style={{ width: '100%', marginBottom: '8px' }} onClick={() => void addBookmark()}>
              ⚑ Aktuelle Stelle merken (b)
            </button>
            {bookmarks.map((bm) => (
              <div key={bm.id} style={{ display: 'flex', alignItems: 'flex-start', gap: '4px' }}>
                <button className="btn btn-ghost btn-sm" style={{ flex: 1, textAlign: 'left', fontSize: '11.5px', whiteSpace: 'normal' }}
                  onClick={() => { setChapter(bm.chapterIndex); setDrawer('none'); void playFrom(bm.chapterIndex, bm.segmentIndex); }}>
                  <span style={{ display: 'block', fontFamily: 'var(--font-mono)', fontSize: '10px', opacity: 0.6 }}>
                    Kap. {bm.chapterIndex + 1} · Seg. {bm.segmentIndex + 1}
                  </span>
                  {bm.note && (
                    <span style={{
                      display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
                      overflow: 'hidden', opacity: 0.85,
                    }}>„{bm.note}…"</span>
                  )}
                </button>
                <button className="btn btn-ghost btn-sm" aria-label="Lesezeichen löschen" onClick={async () => {
                  await deleteAudiobookBookmark(id, bm.id);
                  setBookmarks((await listAudiobookBookmarks(id)).bookmarks);
                }}>✕</button>
              </div>
            ))}
            {!bookmarks.length && <p style={{ fontSize: '12px', opacity: 0.6 }}>Noch keine Lesezeichen.</p>}
          </div>
        )}

        {drawer === 'offline' && (
          <div className="card reader-panel-content" style={{ overflowY: 'auto', padding: '14px', flexShrink: 0, display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <div className="label-eyebrow">Offline verfügbar machen</div>
            <p style={{ fontSize: '11.5px', opacity: 0.65, margin: 0, lineHeight: 1.5 }}>
              Lädt alle Segmente einer fertigen Stimme in den Browser-Cache — danach spielt das Buch auch ohne Verbindung.
            </p>
            {!('caches' in window) && (
              <p style={{ fontSize: '11.5px', color: '#f87171', margin: 0 }}>
                Nur über die HTTPS-Adresse verfügbar.
              </p>
            )}
            {readyVoices.filter((v) => v.complete).map((v) => (
              <div key={v.voice_id} className="card-subtle" style={{ padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px' }}>
                  <span style={{ flex: 1 }}>{v.voice_id}</span>
                  <span style={{ fontSize: '10px', fontFamily: 'var(--font-mono)', opacity: 0.6 }}>
                    {v.cachedSegments}/{v.totalSegments}
                  </span>
                </div>
                <button
                  className="btn btn-secondary btn-sm" disabled={offlineBusy !== '' || !('caches' in window)}
                  onClick={async () => {
                    setOfflineBusy(v.voice_id);
                    setOfflineProgress(0);
                    try {
                      await downloadAudiobook(
                        { id, title: book.title, chapters: book.chapters },
                        v.voice_id,
                        (c, seg) => audiobookSegmentLiveUrl(id, v.voice_id, c, seg),
                        (c) => getAudiobookContent(id, c),
                        (p) => setOfflineProgress(Math.round((p.done / Math.max(1, p.total)) * 100)),
                      );
                    } catch (exc) {
                      setError(exc instanceof Error ? exc.message : 'Offline-Download fehlgeschlagen');
                    } finally {
                      setOfflineBusy('');
                    }
                  }}
                >
                  {offlineBusy === v.voice_id ? `${offlineProgress} %` : 'Lokal sichern'}
                </button>
              </div>
            ))}
            {!readyVoices.some((v) => v.complete) && (
              <p style={{ fontSize: '12px', opacity: 0.6, margin: 0 }}>
                Noch keine Stimme hat das ganze Buch vertont.
              </p>
            )}
          </div>
        )}

        {drawer === 'prefs' && (
          <div className="card reader-panel-content" style={{ overflowY: 'auto', padding: '14px', flexShrink: 0, display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div>
              <label className="label-eyebrow">Schriftgröße ({prefs.fontSize}px)</label>
              <input type="range" min={13} max={24} value={prefs.fontSize} style={{ width: '100%' }}
                onChange={(e) => setPref('fontSize', Number(e.target.value))} />
            </div>
            <div>
              <label className="label-eyebrow">Zeilenhöhe ({prefs.lineHeight})</label>
              <input type="range" min={1.4} max={2.4} step={0.05} value={prefs.lineHeight} style={{ width: '100%' }}
                onChange={(e) => setPref('lineHeight', Number(e.target.value))} />
            </div>
            <div style={{ display: 'flex', gap: '6px' }}>
              {(['sans', 'serif', 'mono'] as const).map((f) => (
                <button key={f} style={{ flex: 1 }}
                  className={prefs.fontFamily === f ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
                  onClick={() => setPref('fontFamily', f)}>
                  {f === 'sans' ? 'Sans' : f === 'serif' ? 'Serif' : 'Mono'}
                </button>
              ))}
            </div>
            <div>
              <label className="label-eyebrow">Thema</label>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px' }}>
                {(Object.keys(THEMES) as ReaderPrefs['theme'][]).map((t) => (
                  <button key={t} className={prefs.theme === t ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'} onClick={() => setPref('theme', t)}>
                    {THEMES[t].label}
                  </button>
                ))}
              </div>
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px' }}>
              <input type="checkbox" checked={prefs.bionic} onChange={(e) => setPref('bionic', e.target.checked)} />
              Bionic Reading
            </label>
            <div>
              <label className="label-eyebrow">Atmosphäre</label>
              <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
                <select className="input-field" value={ambience.soundId} style={{ flex: 1 }}
                  onChange={(e) => ambience.setSoundId(e.target.value)}>
                  <option value="">Aus</option>
                  {ambienceSounds.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
                <button className="btn btn-ghost btn-sm" title="Klang hochladen" onClick={() => ambienceFileRef.current?.click()}>+</button>
                {ambience.soundId && (
                  <button className="btn btn-ghost btn-sm" title="Klang löschen" onClick={async () => {
                    await deleteAmbience(ambience.soundId);
                    ambience.setSoundId('');
                    setAmbienceSounds((await listAmbience()).sounds);
                  }}>✕</button>
                )}
              </div>
              <input ref={ambienceFileRef} type="file" accept=".mp3,.ogg,.wav,.m4a,.flac" hidden
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (f) {
                    const created = await uploadAmbience(f);
                    setAmbienceSounds((await listAmbience()).sounds);
                    ambience.setSoundId(created.id);
                  }
                  e.target.value = '';
                }} />
              {ambience.soundId && (
                <input type="range" min={0} max={1} step={0.05} value={ambience.volume}
                  aria-label="Atmosphäre-Lautstärke" style={{ width: '100%', marginTop: '6px' }}
                  onChange={(e) => ambience.setVolume(Number(e.target.value))} />
              )}
            </div>
            <div>
              <label className="label-eyebrow">Sleep-Timer</label>
              <select className="input-field" value={sleep} onChange={(e) => {
                const v = Number(e.target.value);
                setSleep(v);
                setSleepUntil(v > 0 ? Date.now() + v * 60_000 : null);
              }}>
                {SLEEP_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>
          </div>
        )}

          </section>
        )}
        <div ref={textRef} className="reader-scroll" style={{ flex: 1, minHeight: 0, background: theme.bg, borderRadius: '14px', transition: 'background 250ms' }}>
          <div style={{
            maxWidth: '680px', margin: '0 auto', padding: '20px 18px 40px',
            color: theme.fg,
            fontFamily: FONT_MAP[prefs.fontFamily],
          }}>
            {contentLoading && <p role="status">Kapitel wird geladen…</p>}
            {contentError && <div role="alert"><p>{contentError}</p><button className="btn btn-secondary" onClick={() => setContentAttempt(v => v + 1)}>Erneut laden</button></div>}
            {!contentLoading && segments.map((s) => (
              <span
                key={s.index}
                data-seg={s.index}
                onClick={() => voiceId && void playFrom(chapter, s.index)}
                style={s.heading ? {
                  // Überschriften: Display-Typografie statt Fließtext
                  display: 'block',
                  margin: `${s.paragraphBreak ? 34 : 10}px 0 14px`,
                  fontSize: `${Math.round(prefs.fontSize * 1.45)}px`,
                  lineHeight: 1.3,
                  fontWeight: 700,
                  fontFamily: 'var(--font-display)',
                  letterSpacing: '0.01em',
                  cursor: voiceId ? 'pointer' : 'default',
                  background: activeSegment === s.index ? theme.hl : 'transparent',
                  borderRadius: '6px', padding: '2px 4px', transition: 'background 250ms',
                } : {
                  display: s.paragraphBreak ? 'block' : 'inline',
                  marginTop: s.paragraphBreak ? '20px' : 0,
                  fontSize: `${prefs.fontSize}px`, lineHeight: prefs.lineHeight,
                  cursor: voiceId ? 'pointer' : 'default',
                  background: activeSegment === s.index ? theme.hl : 'transparent',
                  borderRadius: '4px', padding: '1px 3px', transition: 'background 250ms',
                }}
              >
                {activeSegment === s.index
                  ? <ActiveSegment text={s.text} wordIndex={activeWord} bionic={prefs.bionic} />
                  : (prefs.bionic && !s.heading ? <Bionic text={s.text} /> : s.text)}{' '}
              </span>
            ))}
            {!contentLoading && !contentError && !segments.length && <p style={{ opacity: 0.6 }}>Kein Text in diesem Kapitel.</p>}
          </div>
        </div>
      </div>

      {/* Player-Deck */}
      <div className="player-deck">
        <div className="player-cluster reader-transport" style={{ gap: '4px' }}>
          <button className="btn btn-ghost btn-sm" aria-label="Segment zurück" onClick={() => skip(-1)}>
            <svg width="15" height="15" viewBox="0 0 20 20" fill="currentColor"><path d="M5 4h2v12H5zM17 4l-9 6 9 6z"/></svg>
          </button>
          <div style={{ position: 'relative', width: '56px', height: '56px', flexShrink: 0 }}>
            <AudioReactiveGlow active={playing} />
            <button
              aria-label={playing ? 'Pause' : 'Abspielen'}
              onClick={togglePlay}
              disabled={!voiceId && !playing}
              style={{
                position: 'relative', zIndex: 1,
                width: '56px', height: '56px', borderRadius: '50%', cursor: 'pointer',
                border: '1px solid rgba(255,255,255,0.18)',
                background: voiceId || playing
                  ? 'linear-gradient(160deg, rgba(140,115,255,0.95), rgba(105,80,230,0.9))'
                  : 'rgba(255,255,255,0.1)',
                color: '#fff', fontSize: '19px',
                boxShadow: playing
                  ? '0 0 26px rgba(123,97,255,0.6), inset 0 1px 0 rgba(255,255,255,0.35)'
                  : 'inset 0 1px 0 rgba(255,255,255,0.25)',
                transition: 'box-shadow 300ms, transform 150ms',
              }}
            >{playing ? '❚❚' : '▶'}</button>
          </div>
          <button className="btn btn-ghost btn-sm" aria-label="Segment vor" onClick={() => skip(1)}>
            <svg width="15" height="15" viewBox="0 0 20 20" fill="currentColor"><path d="M13 4h2v12h-2zM3 4l9 6-9 6z"/></svg>
          </button>
        </div>

        <div className="reader-progress" style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: '12px', marginBottom: '5px' }}>
            <span style={{ fontSize: '11px', fontFamily: 'var(--font-mono)', opacity: 0.7 }}>
              {String((activeSegment ?? 0) + 1).padStart(3, '0')} / {String(segments.length).padStart(3, '0')}
            </span>
            <div style={{ flex: 1 }}>
              <AudioReactiveBars active={playing} bars={28} height={16} />
            </div>
          </div>
          <div style={{ height: '5px', borderRadius: '999px', background: 'rgba(255,255,255,0.08)', overflow: 'hidden', boxShadow: 'inset 0 1px 2px rgba(0,0,0,0.4)' }}>
            <div style={{
              height: '100%', transition: 'width 300ms',
              background: 'linear-gradient(90deg, rgba(123,97,255,0.85), rgba(150,125,255,1))',
              boxShadow: '0 0 8px rgba(123,97,255,0.55)',
              width: segments.length ? `${(((activeSegment ?? -1) + 1) / segments.length) * 100}%` : '0%',
            }} />
          </div>
        </div>

        <div className="player-cluster reader-playback-options">
          <button className="btn btn-ghost btn-sm" aria-label="Lesezeichen setzen" title="Lesezeichen (b)" onClick={() => void addBookmark()}>⚑</button>
          <button
            className="btn btn-ghost btn-sm" aria-label={muted ? 'Ton an' : 'Stumm'}
            title="Stumm (m)" onClick={() => setMuted((v) => !v)}
            style={{ opacity: muted ? 1 : 0.7 }}
          >
            <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 4L5 8H2v4h3l4 4V4z" fill="currentColor" stroke="none"/>
              {muted
                ? <path d="M13 8l4 4M17 8l-4 4" />
                : <path d="M13 7a4.2 4.2 0 010 6M15.5 5a7 7 0 010 10" />}
            </svg>
          </button>
          <input
            type="range" min={0} max={1} step={0.05} value={muted ? 0 : volume} aria-label="Lautstärke"
            onChange={(e) => { setMuted(false); setVolume(Number(e.target.value)); }}
            style={{ width: '84px' }} className="reader-volume"
          />
          <button
            className="btn btn-ghost btn-sm" aria-label="Geschwindigkeit"
            style={{ fontFamily: 'var(--font-mono)', fontSize: '11.5px', minWidth: '46px' }}
            onClick={() => setSpeed(SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length])}
          >{speed}×</button>
        </div>
      </div>

      {showShortcuts && (
        <div
          role="dialog" aria-label="Tastaturkürzel"
          onClick={() => setShowShortcuts(false)}
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 60,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <div className="card" style={{ padding: '22px 28px', fontSize: '13px', lineHeight: 2 }}>
            <strong style={{ display: 'block', marginBottom: '6px' }}>Tastaturkürzel</strong>
            <div><kbd>Leertaste</kbd> — Play / Pause</div>
            <div><kbd>←</kbd> / <kbd>→</kbd> — Segment zurück / vor</div>
            <div><kbd>Shift</kbd>+<kbd>←</kbd>/<kbd>→</kbd> — Kapitel zurück / vor</div>
            <div><kbd>b</kbd> — Lesezeichen setzen · <kbd>m</kbd> — Stumm</div>
            <div><kbd>−</kbd>/<kbd>+</kbd> — Tempo · <kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd> — 1× / 1.5× / 2×</div>
            <div><kbd>?</kbd> — dieses Fenster · <kbd>Esc</kbd> — schließen</div>
          </div>
        </div>
      )}

      {sleepToast && (
        <div role="status" style={{
          position: 'fixed', bottom: '100px', left: '50%', transform: 'translateX(-50%)',
          zIndex: 70, padding: '10px 20px', borderRadius: '12px',
          background: 'rgba(20,20,26,0.92)', backdropFilter: 'blur(10px)',
          border: '1px solid rgba(255,255,255,0.12)', fontSize: '13px',
        }}>
          ☾ Sleep-Timer abgelaufen — Wiedergabe pausiert
        </div>
      )}
    </div>
  );
}
