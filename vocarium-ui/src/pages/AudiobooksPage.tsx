import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { AudiobookOfflineMenu, OfflineBadge } from '../components/OfflineControls';
import { motion } from 'framer-motion';
import {
  abQueueEventsUrl,
  addBookToCollection,
  audiobookCoverUrl,
  cancelAbQueueJob,
  createPodcast,
  getAudiobookContent,
  uploadPodcastSource,
  createAbCollection,
  deleteAbCollection,
  deleteAudiobook,
  getVoices,
  listAbCollections,
  listAudiobooks,
  patchAudiobook,
  publishBookToStore,
  removeBookFromCollection,
  uploadAudiobook,
  uploadAudiobookCover,
} from '../api';
import type { AbQueueJob } from '../api';
import type { AbBook, AbCollection, Voice } from '../types';
import { VoiceOptions } from '../components/VoiceOptions';
import { engineVoices as listEngineVoices } from '../voiceUtils';

const SWATCHES = ['#c59f5f', '#5a9fd4', '#57ab5a', '#e5534b', '#986ee2', '#db61a2'];
const ONBOARDING_KEY = 'ab-onboarding-done';

// Deterministic hue per book id — als dezenter Farbschleier über dem dunklen
// Platzhalter-Panel, damit die Kacheln editorial bleiben statt bunt zu leuchten.
function coverHue(id: string): number {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function coverGradient(id: string): string {
  const h = coverHue(id);
  return `linear-gradient(160deg, hsl(${h} 45% 28%) 0%, hsl(${(h + 40) % 360} 55% 16%) 100%)`;
}

// Getönte Platzhalterfläche: Buchfarbe nur als Schleier über dem dunklen Panel.
function coverTinted(id: string): string {
  const h = coverHue(id);
  return [
    `linear-gradient(165deg, hsl(${h} 42% 30% / 0.5), hsl(${(h + 40) % 360} 45% 14% / 0.35) 70%)`,
    'radial-gradient(ellipse 90% 60% at 50% 0%, rgba(123, 97, 255, 0.16), transparent 70%)',
    'linear-gradient(170deg, rgba(30, 28, 44, 0.94), rgba(13, 12, 20, 0.97))',
  ].join(', ');
}

function OnboardingTour({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState(0);
  const steps = [
    { title: 'Willkommen in deiner Bibliothek', body: 'Lade PDF, EPUB, DOCX oder TXT hoch — Vocarium zerlegt das Buch in Kapitel und Segmente und vertont es mit deinen eigenen Stimmen.' },
    { title: 'Stimmen', body: 'OmniVoice-Klone und Kikiri-Finetunes stehen als Vorleser bereit. Die Stimme lässt sich pro Buch festlegen und jederzeit wechseln — jede Stimme hat ihren eigenen Audio-Cache.' },
    { title: 'Sammlungen & Ordnung', body: 'Ordne Bücher in farbige Sammlungen, verstecke Titel aus dem Regal und passe die Kachelgröße mit dem Regler an.' },
  ];
  const s = steps[step];
  return (
    <div role="dialog" aria-modal="true" aria-label="Einführung" style={{
      position: 'fixed', inset: 0, zIndex: 70, background: 'rgba(0,0,0,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '20px',
    }}>
      <div className="card" style={{ maxWidth: '440px', padding: '28px', textAlign: 'center' }}>
        <div style={{ display: 'flex', gap: '6px', justifyContent: 'center', marginBottom: '16px' }}>
          {steps.map((_, i) => (
            <span key={i} style={{
              width: i === step ? '22px' : '8px', height: '8px', borderRadius: '999px',
              background: i === step ? 'var(--color-accent, #7b61ff)' : 'rgba(255,255,255,0.2)',
              transition: 'width 250ms',
            }} />
          ))}
        </div>
        <h2 style={{ fontSize: '19px', margin: '0 0 10px' }}>{s.title}</h2>
        <p style={{ fontSize: '13.5px', opacity: 0.75, lineHeight: 1.7, margin: '0 0 22px' }}>{s.body}</p>
        <div style={{ display: 'flex', gap: '10px', justifyContent: 'center' }}>
          <button className="btn btn-ghost btn-sm" onClick={onDone}>Überspringen</button>
          <button className="btn btn-primary btn-sm" autoFocus onClick={() => (step < steps.length - 1 ? setStep(step + 1) : onDone())}>
            {step < steps.length - 1 ? 'Weiter' : 'Los geht’s'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function AudiobooksPage() {
  const navigate = useNavigate();
  const [books, setBooks] = useState<AbBook[]>([]);
  const [collections, setCollections] = useState<AbCollection[]>([]);
  const [selectedCollection, setSelectedCollection] = useState('');
  const [showHidden, setShowHidden] = useState(false);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [error, setError] = useState('');
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [voiceId, setVoiceId] = useState('');
  const [gridSize, setGridSize] = useState(() => Number(localStorage.getItem('library-grid-size')) || 150);
  const [menuBook, setMenuBook] = useState<string | null>(null);
  const [newCollection, setNewCollection] = useState<{ name: string; color: string } | null>(null);
  const [onboarding, setOnboarding] = useState(() => !localStorage.getItem(ONBOARDING_KEY));
  const [queue, setQueue] = useState<AbQueueJob[]>([]);
  const [offHours, setOffHours] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const coverRef = useRef<HTMLInputElement>(null);
  const coverTargetRef = useRef('');

  const engineVoices = useMemo(() => listEngineVoices(voices), [voices]);

  const visibleBooks = useMemo(() => {
    let list = books.filter((b) => !b.is_hidden);
    if (selectedCollection) list = list.filter((b) => b.collectionIds?.includes(selectedCollection));
    return list;
  }, [books, selectedCollection]);
  const hiddenBooks = useMemo(() => books.filter((b) => b.is_hidden), [books]);
  const lastBook = useMemo(() => visibleBooks.filter(b=>b.progress && !b.progress.completed).sort((a,b)=>(b.progress?.updatedAt || '').localeCompare(a.progress?.updatedAt || ''))[0] ?? null, [visibleBooks]);

  const refresh = useCallback(async () => {
    try {
      setBooks((await listAudiobooks()).books);
      setCollections((await listAbCollections()).collections);
      setError('');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Bibliothek konnte nicht geladen werden');
    }
  }, []);

  useEffect(() => {
    void refresh();
    getVoices().then(setVoices).catch(() => undefined);
  }, [refresh]);

  useEffect(() => {
    const close = () => setMenuBook(null);
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setMenuBook(null); };
    window.addEventListener('keydown', escape);
    window.addEventListener('mousedown', close);
    return () => { window.removeEventListener('mousedown', close); window.removeEventListener('keydown', escape); };
  }, []);

  // Live-Queue über SSE; bei Abschluss eines Jobs Bibliothek neu laden.
  useEffect(() => {
    const es = new EventSource(abQueueEventsUrl());
    let hadActive = false;
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as { jobs: AbQueueJob[]; offHours: boolean };
        setQueue(data.jobs);
        setOffHours(data.offHours);
        const active = data.jobs.some((j) => j.status === 'processing' || j.status === 'pending');
        if (hadActive && !active) void refresh();
        hadActive = active;
      } catch { /* Zeile ignorieren */ }
    };
    return () => es.close();
  }, [refresh]);

  const doUpload = useCallback(async (file: File) => {
    setUploading(true);
    setError('');
    try {
      const book = await uploadAudiobook({ file, voice_id: voiceId || undefined });
      await refresh();
      navigate(`/audiobooks/${book.id}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Upload fehlgeschlagen');
    } finally {
      setUploading(false);
    }
  }, [voiceId, refresh, navigate]);

  const dismissOnboarding = useCallback(() => {
    localStorage.setItem(ONBOARDING_KEY, '1');
    setOnboarding(false);
  }, []);

  const renderCard = (book: AbBook) => {
    const pct = book.progress && book.total_chapters
      ? Math.round((book.progress.chapterIndex / book.total_chapters) * 100)
      : 0;
    const statusBadge = !book.progress
      ? { label: 'Ungelesen', bg: 'rgba(255,255,255,0.12)', fg: 'rgba(255,255,255,0.7)' }
      : book.progress.completed === true
        ? { label: 'Fertig', bg: 'rgba(7,202,107,0.25)', fg: 'rgb(120,235,175)' }
        : { label: `Kapitel ${book.progress.chapterIndex + 1}/${book.total_chapters}`, bg: 'rgba(123,97,255,0.3)', fg: 'rgb(196,181,255)' };
    const bookCollections = collections.filter((c) => book.collectionIds?.includes(c.id));
    return (
      <motion.div key={book.id} layout whileHover={{ y: -4 }} style={{ position: 'relative' }}>
        <Link to={`/audiobooks/${book.id}`} className="book-card" style={{ textDecoration: 'none', color: 'inherit', display: 'block' }}>
          <div className="book-cover" style={{ aspectRatio: '2 / 3', position: 'relative', overflow: 'hidden' }}>
            {book.has_cover ? (
              <img
                src={audiobookCoverUrl(book.id)} alt=""
                style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }}
              />
            ) : (
              <div className="cover-placeholder" style={{ background: coverTinted(book.id) }}>
                <span className="corner tl" /><span className="corner tr" />
                <span className="corner bl" /><span className="corner br" />
                <span className="initial">{book.title.slice(0, 1).toUpperCase()}</span>
                <span className="format">{(book.format || 'txt').toUpperCase()}</span>
              </div>
            )}
            <span className="cover-scrim" aria-hidden />

            {bookCollections.length > 0 && (
              <div style={{ position: 'absolute', top: '9px', left: '9px', display: 'flex', gap: '4px', zIndex: 3 }}>
                {bookCollections.slice(0, 4).map((c) => (
                  <span key={c.id} title={c.name} style={{
                    width: '8px', height: '8px', borderRadius: '50%', background: c.color,
                    boxShadow: '0 0 0 1.5px rgba(0,0,0,0.45)',
                  }} />
                ))}
              </div>
            )}
            <span className="status-chip" style={{
              position: 'absolute', bottom: '10px', left: '10px', zIndex: 3,
              background: statusBadge.bg, color: statusBadge.fg,
            }}>{statusBadge.label}</span>
            <span className="cover-meta">{book.total_chapters} Kap.</span>
            <span style={{ position: 'absolute', top: '9px', right: '44px', zIndex: 3 }}><OfflineBadge kind="audiobook" id={book.id} /></span>
            {pct > 0 && (
              <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, height: '3px', background: 'rgba(0,0,0,0.4)', zIndex: 3 }}>
                <div style={{ width: `${pct}%`, height: '100%', background: 'var(--color-accent)' }} />
              </div>
            )}
          </div>
          <div className="book-card-info">
            <div className="book-card-title">{book.title}</div>
            <div className="book-card-author">
              {book.author || (book.voice_id ? `gelesen von ${book.voice_id}` : 'ohne Stimme')}
            </div>
          </div>
        </Link>

            <button
              className="card-menu-btn"
              aria-expanded={menuBook === book.id}
              aria-label={`Menü für ${book.title}`}
              onMouseDown={(e) => e.stopPropagation()}
              onClick={(e) => { e.preventDefault(); setMenuBook(menuBook === book.id ? null : book.id); }}
            >⋯</button>
        {menuBook === book.id && (
          <div
            className="card" onMouseDown={(e) => e.stopPropagation()}
            style={{
              position: 'absolute', right: 0, top: '100%', zIndex: 40, width: '200px',
              padding: '6px', display: 'flex', flexDirection: 'column', gap: '2px',
            }}
          >
            {collections.length > 0 && <div className="label-eyebrow" style={{ padding: '4px 8px' }}>Sammlungen</div>}
            {collections.map((c) => {
              const inside = book.collectionIds?.includes(c.id);
              return (
                <button key={c.id} className="btn btn-ghost btn-sm" style={{ justifyContent: 'flex-start', gap: '8px', textAlign: 'left' }}
                  onClick={async () => {
                    if (inside) await removeBookFromCollection(c.id, book.id);
                    else await addBookToCollection(c.id, book.id);
                    await refresh();
                  }}>
                  <span style={{ width: '9px', height: '9px', borderRadius: '50%', background: c.color }} />
                  <span style={{ flex: 1 }}>{c.name}</span>
                  {inside && '✓'}
                </button>
              );
            })}
            <AudiobookOfflineMenu book={book} onError={setError} />
            <button className="btn btn-ghost btn-sm" style={{ justifyContent: 'flex-start' }}
              onClick={() => { coverTargetRef.current = book.id; coverRef.current?.click(); setMenuBook(null); }}>
              Cover hochladen…
            </button>
            <button className="btn btn-ghost btn-sm" style={{ justifyContent: 'flex-start' }}
              onClick={async () => {
                const genre = window.prompt('Genre (optional):', '');
                if (genre === null) { setMenuBook(null); return; }
                try { await publishBookToStore(book.id, genre.trim()); }
                catch (exc) { setError(exc instanceof Error ? exc.message : 'Teilen fehlgeschlagen'); }
                setMenuBook(null);
              }}>
              Im Store teilen
            </button>
            <button className="btn btn-ghost btn-sm" style={{ justifyContent: 'flex-start' }}
              onClick={async () => { await patchAudiobook(book.id, { is_hidden: !book.is_hidden }); setMenuBook(null); await refresh(); }}>
              {book.is_hidden ? 'Wieder anzeigen' : 'Verstecken'}
            </button>
            <button className="btn btn-ghost btn-sm" style={{ justifyContent: 'flex-start' }}
              onClick={async () => {
                setMenuBook(null);
                try {
                  // Volltext einsammeln und als Quelle an einen neuen Podcast hängen.
                  const podcast = await createPodcast({ topic: book.title });
                  const parts: string[] = [];
                  for (let ci = 0; ci < book.total_chapters; ci++) {
                    const content = await getAudiobookContent(book.id, ci);
                    parts.push(content.segments.map((s) => s.text).join(' '));
                  }
                  const file = new File([parts.join('\n\n')], `${book.title.slice(0, 60)}.txt`, { type: 'text/plain' });
                  await uploadPodcastSource(podcast.id, file);
                  navigate('/podcast');
                } catch (exc) {
                  setError(exc instanceof Error ? exc.message : 'Podcast konnte nicht angelegt werden');
                }
              }}>
              Als Podcast
            </button>
            <button className="btn btn-ghost btn-sm" style={{ justifyContent: 'flex-start', color: '#f87171' }}
              onClick={async () => {
                if (!window.confirm(`„${book.title}" samt Audio löschen?`)) return;
                await deleteAudiobook(book.id);
                setMenuBook(null);
                await refresh();
              }}>
              Löschen
            </button>
          </div>
        )}
      </motion.div>
    );
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '28px', maxWidth: '1480px' }}>
      {onboarding && <OnboardingTour onDone={dismissOnboarding} />}

      <div style={{ display: 'flex', alignItems: 'flex-end', gap: '16px', flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: '240px' }}>
          <div className="section-kicker" style={{ marginBottom: '10px' }}>
            Vocarium · Bibliothek
          </div>
          <h1 className="display-serif" style={{ fontSize: 'clamp(34px, 4.4vw, 46px)', margin: 0 }}>
            Deine Sammlung
          </h1>
          <p className="serif-sub" style={{ margin: '10px 0 0', maxWidth: '58ch' }}>
            {books.length ? (
              <>
                Hörbücher, die du hörst oder noch entdecken wirst.
                <span className="no-wrap">{' · '}<b>{books.length}</b> {books.length === 1 ? 'Werk' : 'Werke'}</span>
                {books.filter((b) => b.progress).length > 0 && <span className="no-wrap">, <b>{books.filter((b) => b.progress).length}</b> angefangen</span>}
                {collections.length > 0 && <span className="no-wrap">{' · '}<b>{collections.length}</b> {collections.length === 1 ? 'Sammlung' : 'Sammlungen'}</span>}
              </>
            ) : 'Noch nichts im Regal — lade dein erstes Buch hoch.'}
          </p>
          <div className="accent-rule" />
        </div>
        <select
          className="input-field" value={voiceId} onChange={(e) => setVoiceId(e.target.value)}
          style={{ width: '215px' }} aria-label="Standardstimme für neue Bücher"
        >
          <option value="">Stimme später wählen</option>
          <VoiceOptions voices={engineVoices} />
        </select>
        <button className="btn btn-primary" disabled={uploading} onClick={() => fileRef.current?.click()}>
          {uploading ? 'Wird verarbeitet…' : '+ Buch hochladen'}
        </button>
        <input
          ref={fileRef} type="file" accept=".pdf,.epub,.docx,.txt" hidden
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void doUpload(f); e.target.value = ''; }}
        />
        <input
          ref={coverRef} type="file" accept=".jpg,.jpeg,.png,.webp,.gif" hidden
          onChange={async (e) => {
            const f = e.target.files?.[0];
            if (f && coverTargetRef.current) {
              try { await uploadAudiobookCover(coverTargetRef.current, f); await refresh(); }
              catch (exc) { setError(exc instanceof Error ? exc.message : 'Cover-Upload fehlgeschlagen'); }
            }
            e.target.value = '';
          }}
        />
      </div>
      {/* Sammlungs-Leiste + Grid-Regler */}
      <div>
        <div className="section-row">
          <div className="section-kicker">
            Sammlungen <span className="kicker-count">{String(collections.length).padStart(2, '0')}</span>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        {collections.length > 0 && (
          <button
            className={selectedCollection === '' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
            onClick={() => setSelectedCollection('')}
          >Alle</button>
        )}
        {collections.map((c) => (
          <span key={c.id} style={{ position: 'relative', display: 'inline-flex' }}>
            <button
              className={selectedCollection === c.id ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
              style={{ display: 'inline-flex', alignItems: 'center', gap: '7px' }}
              onClick={() => setSelectedCollection(selectedCollection === c.id ? '' : c.id)}
            >
              <span style={{ width: '9px', height: '9px', borderRadius: '50%', background: c.color }} />
              {c.name}
              <span
                role="button" aria-label={`Sammlung ${c.name} löschen`}
                style={{ opacity: 0.55, marginLeft: '2px' }}
                onClick={async (e) => {
                  e.stopPropagation();
                  await deleteAbCollection(c.id);
                  if (selectedCollection === c.id) setSelectedCollection('');
                  await refresh();
                }}
              >✕</span>
            </button>
          </span>
        ))}
        {newCollection ? (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
            <input
              className="input-field" autoFocus placeholder="Name" maxLength={64}
              value={newCollection.name} style={{ width: '130px' }}
              onChange={(e) => setNewCollection({ ...newCollection, name: e.target.value })}
              onKeyDown={async (e) => {
                if (e.key === 'Enter' && newCollection.name.trim()) {
                  await createAbCollection(newCollection.name.trim(), newCollection.color);
                  setNewCollection(null);
                  await refresh();
                }
                if (e.key === 'Escape') setNewCollection(null);
              }}
            />
            {SWATCHES.map((s) => (
              <button key={s} aria-label={`Farbe ${s}`} onClick={() => setNewCollection({ ...newCollection, color: s })}
                style={{
                  width: '18px', height: '18px', borderRadius: '50%', background: s, cursor: 'pointer',
                  border: newCollection.color === s ? '2px solid #fff' : '2px solid transparent',
                }} />
            ))}
          </span>
        ) : (
          <button className="add-pill" onClick={() => setNewCollection({ name: '', color: SWATCHES[0] })}>
            <span aria-hidden>+</span> Neue Sammlung
          </button>
        )}
        </div>
      </div>

      {error && <div role="alert" className="hs-error">{error} <button className="btn btn-secondary" onClick={()=>void refresh()}>Erneut laden</button></div>}

      {queue.some((j) => j.status === 'processing' || j.status === 'pending') && (
        <div className="card" style={{ padding: '14px 18px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span className="label-eyebrow">Warteschlange</span>
            {offHours
              ? <span style={{ fontSize: '11px', color: 'rgba(7,202,107,0.85)' }}>· außerhalb der Geschäftszeiten — Nacht-Jobs laufen</span>
              : <span style={{ fontSize: '11px', opacity: 0.55 }}>· Nacht-Jobs warten bis nach Geschäftsschluss</span>}
          </div>
          {queue.filter((j) => j.status === 'processing' || j.status === 'pending').map((j) => (
            <div key={j.id} style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <span style={{ width: '8px', height: '8px', borderRadius: '50%', flexShrink: 0,
                background: j.status === 'processing' ? 'rgba(7,202,107,0.9)' : 'rgba(232,149,88,0.9)' }} />
              <span style={{ flex: 1, minWidth: 0, fontSize: '13px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {j.title}{j.chapter !== null ? ` · Kapitel ${j.chapter + 1}` : ''}
                {j.priority >= 8 && ' · ☾'}
              </span>
              <span style={{ fontSize: '11px', fontFamily: 'var(--font-mono)', opacity: 0.7 }}>
                {j.status === 'processing' ? `${j.done}/${j.total}` : 'wartet'}
              </span>
              <div style={{ width: '110px', height: '4px', borderRadius: '999px', background: 'rgba(255,255,255,0.08)', overflow: 'hidden' }}>
                <div style={{ height: '100%', background: 'var(--color-accent, #7b61ff)', transition: 'width 400ms',
                  width: j.total ? `${(j.done / j.total) * 100}%` : '0%' }} />
              </div>
              <button className="btn btn-ghost btn-sm" aria-label="Job abbrechen"
                onClick={async () => { await cancelAbQueueJob(j.id); }}>✕</button>
            </div>
          ))}
        </div>
      )}

      {lastBook && (
        <div>
          <div className="section-kicker" style={{ marginBottom: '11px' }}>
            <span aria-hidden>▸</span> Weiterhören
          </div>
          <Link to={`/audiobooks/${lastBook.id}`} style={{ textDecoration: 'none', color: 'inherit' }}>
            <div className="hero-continue">
              <div style={{
                width: '62px', height: '92px', borderRadius: '6px', flexShrink: 0,
                background: coverGradient(lastBook.id), overflow: 'hidden', position: 'relative',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                boxShadow: '0 10px 26px rgba(0,0,0,0.5)',
              }}>
                {lastBook.has_cover
                  ? <img src={audiobookCoverUrl(lastBook.id)} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                  : <span style={{ fontFamily: 'var(--font-serif)', fontSize: '30px', color: 'rgba(255,255,255,0.85)' }}>
                      {lastBook.title.slice(0, 1).toUpperCase()}
                    </span>}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="display-serif" style={{
                  fontSize: '24px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{lastBook.title}</div>
                <div className="book-card-author" style={{ fontSize: '13.5px', marginTop: '3px' }}>
                  {lastBook.author || 'unbekannter Autor'}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '11px', marginTop: '12px' }}>
                  <div style={{ flex: 1, maxWidth: '260px', height: '3px', borderRadius: '999px', background: 'rgba(255,255,255,0.1)', overflow: 'hidden' }}>
                    <div style={{
                      height: '100%', background: 'var(--color-accent, #7b61ff)',
                      width: `${Math.max(2, Math.round(((lastBook.progress?.chapterIndex ?? 0) / Math.max(1, lastBook.total_chapters)) * 100))}%`,
                    }} />
                  </div>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: '10.5px', letterSpacing: '0.1em', opacity: 0.55 }}>
                    KAP. {(lastBook.progress?.chapterIndex ?? 0) + 1} / {lastBook.total_chapters}
                  </span>
                </div>
              </div>
              <span className="hero-play" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }} aria-hidden>▶</span>
            </div>
          </Link>
        </div>
      )}

      {/* Regal */}
      <div>
        <div className="section-row">
          <div className="section-kicker">
            {selectedCollection ? 'Sammlung' : 'Alle Bücher'}{' '}
            <span className="kicker-count">{String(visibleBooks.length).padStart(2, '0')}</span>
          </div>
          <div className="view-control">
            <span className="view-control-label">Ansicht</span>
            <input
              type="range" min={120} max={300} value={gridSize} aria-label="Kachelgröße"
              onChange={(e) => {
                const v = Number(e.target.value);
                setGridSize(v);
                localStorage.setItem('library-grid-size', String(v));
              }}
            />
          </div>
        </div>
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault(); setDragOver(false);
          const f = e.dataTransfer.files?.[0];
          if (f) void doUpload(f);
        }}
        style={{
          display: 'grid', gridTemplateColumns: `repeat(auto-fill, minmax(${gridSize}px, 1fr))`, gap: '18px',
          padding: dragOver ? '14px' : 0, borderRadius: '16px',
          outline: dragOver ? '2px dashed rgba(123,97,255,0.6)' : 'none',
          transition: 'padding 150ms, outline 150ms',
        }}
      >
        {visibleBooks.map(renderCard)}
        {!visibleBooks.length && !error && (
          <div
            className="card-subtle"
            style={{
              gridColumn: '1 / -1', padding: '46px 20px', textAlign: 'center',
              fontSize: '14px', opacity: 0.7, cursor: 'pointer',
            }}
            onClick={() => fileRef.current?.click()}
          >
            <div style={{ fontSize: '30px', marginBottom: '8px' }}>📚</div>
            {selectedCollection
              ? 'Keine Bücher in dieser Sammlung.'
              : <>PDF, EPUB, DOCX oder TXT hierher ziehen<br />oder klicken zum Hochladen</>}
          </div>
        )}
      </div>
      </div>

      {hiddenBooks.length > 0 && (
        <div>
          <button className="btn btn-ghost btn-sm" onClick={() => setShowHidden(!showHidden)}>
            {showHidden ? '▾' : '▸'} Versteckt · {hiddenBooks.length}
          </button>
          {showHidden && (
            <div style={{
              display: 'grid', gridTemplateColumns: `repeat(auto-fill, minmax(${gridSize}px, 1fr))`,
              gap: '18px', marginTop: '12px', opacity: 0.75,
            }}>
              {hiddenBooks.map(renderCard)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
