import { useState, useEffect, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Link } from 'react-router-dom';
import { getVoices } from '../api';
import type { Voice } from '../types';
import VoiceCard from '../components/VoiceCard';
import SkeletonCard from '../components/SkeletonCard';
import WaveformBars from '../components/WaveformBars';
import { describeVoice, engineVoices, isFallbackVoice } from '../voiceUtils';

/**
 * Filter entlang der Engines, nicht entlang der alten `source`-Werte.
 *
 * Vorher gab es Reiter für `clone`, `design` und `custom` — Quellen, die es
 * seit der Umstellung auf OmniVoice und Kikiri nicht mehr gibt. Drei von vier
 * Reitern waren dauerhaft leer. Das hier sind die Unterscheidungen, die die
 * API tatsächlich liefert und die beim Rendern etwas bedeuten.
 */
type Bucket = 'all' | 'omnivoice' | 'finetune' | 'fallback';

const BUCKET_LABEL: Record<Bucket, string> = {
  all: 'Alle',
  omnivoice: 'OmniVoice',
  finetune: 'Finetunes',
  fallback: 'Fallback',
};

function bucketOf(voice: Voice): Exclude<Bucket, 'all'> {
  if (voice.source === 'omnivoice') return 'omnivoice';
  return isFallbackVoice(voice) ? 'fallback' : 'finetune';
}

interface Section {
  key: string;
  title: string;
  hint: string;
  voices: Voice[];
}

export default function VoicesPage() {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [bucket, setBucket] = useState<Bucket>('all');

  const fetchVoices = async () => {
    try {
      const data = await getVoices();
      setVoices(engineVoices(data));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Stimmen konnten nicht geladen werden');
    }
    setLoading(false);
  };

  useEffect(() => {
    fetchVoices();
  }, []);

  const counts = useMemo(() => {
    const base: Record<Bucket, number> = { all: voices.length, omnivoice: 0, finetune: 0, fallback: 0 };
    for (const v of voices) base[bucketOf(v)] += 1;
    return base;
  }, [voices]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return voices.filter((v) => {
      if (bucket !== 'all' && bucketOf(v) !== bucket) return false;
      if (!q) return true;
      return (
        v.name.toLowerCase().includes(q)
        || v.id.toLowerCase().includes(q)
        || describeVoice(v).toLowerCase().includes(q)
        || (v.notes || '').toLowerCase().includes(q)
      );
    });
  }, [voices, search, bucket]);

  /* Die Liste bleibt nach Engine gegliedert, auch gefiltert: sechzig Kacheln am
     Stück sagen nichts darüber, welche davon auf der GPU laufen. */
  const sections = useMemo<Section[]>(() => {
    const pick = (k: Exclude<Bucket, 'all'>) => filtered.filter((v) => bucketOf(v) === k);
    return [
      {
        key: 'omnivoice',
        title: 'OmniVoice — Klonstimmen',
        hint: 'Hauptengine, GPU, jede Stimme mit eigener Referenzaufnahme',
        voices: pick('omnivoice'),
      },
      {
        key: 'finetune',
        title: 'Kikiri — Finetunes',
        hint: 'Auf der CPU trainierte Kokoro-Stimmen',
        voices: pick('finetune'),
      },
      {
        key: 'fallback',
        title: 'Kikiri — Fallback',
        hint: 'Piper-Bank auf der CPU, greift nur wenn OmniVoice ausfällt',
        voices: pick('fallback'),
      },
    ].filter((s) => s.voices.length > 0);
  }, [filtered]);

  if (loading) {
    return (
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '22px' }}>
          <div className="skeleton-shimmer" style={{ height: '32px', width: '200px', borderRadius: '10px' }} />
          <div className="skeleton-shimmer" style={{ height: '24px', width: '48px', borderRadius: '999px' }} />
        </div>
        <div className="voice-grid">
          {Array.from({ length: 6 }).map((_, i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return <ErrorState message={error} onRetry={() => { setLoading(true); fetchVoices(); }} />;
  }

  if (voices.length === 0) {
    return <EmptyState />;
  }

  let tileIndex = 0;

  return (
    <div className="voices-page">
      <header className="speech-header">
        <div>
          <h1 style={{ fontSize: '26px', marginBottom: '4px' }}>Stimmen</h1>
          <p style={{ fontSize: '13.5px', color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
            Alles, was gerade rendern kann. Antippen öffnet die Kurzprobe, die Play-Taste spielt sofort.
          </p>
        </div>
        <div className="speech-route-pill" title="Verteilung über die beiden Engines">
          <span className="status-dot status-dot-online" />
          <span>{counts.omnivoice} GPU · {counts.finetune + counts.fallback} CPU</span>
        </div>
      </header>

      <div className="voices-toolbar">
        <div className="voices-tabs" role="tablist">
          {(['all', 'omnivoice', 'finetune', 'fallback'] as Bucket[]).map((b) => {
            const active = bucket === b;
            return (
              <button
                key={b}
                role="tab"
                aria-selected={active}
                onClick={() => setBucket(b)}
                className={`voices-tab${active ? ' voices-tab-active' : ''}`}
              >
                {active && (
                  <motion.span
                    layoutId="voices-filter-indicator"
                    className="voices-tab-indicator"
                    transition={{ type: 'spring', stiffness: 420, damping: 34 }}
                  />
                )}
                <span style={{ position: 'relative' }}>{BUCKET_LABEL[b]}</span>
                <span className="voices-tab-count">{counts[b]}</span>
              </button>
            );
          })}
        </div>

        <div className="voices-search">
          <svg
            width="14"
            height="14"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            style={{ opacity: 0.45, flexShrink: 0 }}
          >
            <circle cx="7" cy="7" r="5" />
            <path d="M14 14l-3-3" />
          </svg>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Stimme suchen…"
            aria-label="Stimme suchen"
          />
          {search && (
            <button type="button" onClick={() => setSearch('')} aria-label="Suche leeren" className="btn-ghost" style={{ padding: 2, borderRadius: 6, lineHeight: 0 }}>
              <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M2 2l8 8M10 2l-8 8" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {sections.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          className="card-subtle"
          style={{ padding: '52px 32px', textAlign: 'center' }}
        >
          <h4 style={{ fontSize: '15px', fontWeight: 600, fontFamily: 'var(--font-display)', margin: '0 0 6px' }}>
            Keine Treffer
          </h4>
          <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)', margin: 0 }}>
            {search ? <>Keine Stimme passt zu „{search}“.</> : 'In diesem Bereich gibt es derzeit keine Stimmen.'}
          </p>
        </motion.div>
      ) : (
        sections.map((section) => (
          <section key={section.key} className="voices-section">
            <div className="voices-section-head">
              <h2>{section.title}</h2>
              <span className="voices-section-count">{section.voices.length}</span>
              <span className="voices-section-hint">{section.hint}</span>
            </div>
            <div className="voice-grid">
              <AnimatePresence mode="popLayout">
                {section.voices.map((voice) => (
                  <VoiceCard key={voice.id} voice={voice} index={tileIndex++} onDeleted={fetchVoices} />
                ))}
              </AnimatePresence>
            </div>
          </section>
        ))
      )}
    </div>
  );
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="card-subtle"
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '72px 32px',
        textAlign: 'center',
      }}
    >
      <div
        style={{
          width: '56px',
          height: '56px',
          borderRadius: '16px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          marginBottom: '20px',
          background: 'var(--color-danger-dim)',
          border: '1px solid rgba(255,90,101,0.3)',
          color: 'var(--color-danger)',
        }}
      >
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
          <circle cx="12" cy="12" r="10" />
          <path d="M12 8v4M12 16h.01" />
        </svg>
      </div>
      <h3
        style={{
          fontSize: '17px',
          fontWeight: 600,
          fontFamily: 'var(--font-display)',
          color: 'var(--color-text)',
          margin: '0 0 8px',
          letterSpacing: 0,
        }}
      >
        Stimmen nicht erreichbar
      </h3>
      <p
        style={{
          fontSize: '13.5px',
          margin: '0 0 24px',
          color: 'var(--color-text-secondary)',
          maxWidth: '384px',
          lineHeight: 1.55,
        }}
      >
        {message}
      </p>
      <motion.button
        whileTap={{ scale: 0.97 }}
        whileHover={{ scale: 1.02 }}
        onClick={onRetry}
        className="btn btn-ghost"
        style={{ padding: '10px 22px', fontSize: '13px' }}
      >
        Erneut versuchen
      </motion.button>
    </motion.div>
  );
}

function EmptyState() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: [0.25, 0.46, 0.45, 0.94] }}
      className="card-subtle"
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '88px 32px',
        textAlign: 'center',
      }}
    >
      <motion.div
        style={{ marginBottom: '28px', opacity: 0.55 }}
        animate={{ y: [0, -4, 0] }}
        transition={{ duration: 3.2, repeat: Infinity, ease: 'easeInOut' }}
      >
        <WaveformBars active size="lg" bars={24} color="accent" />
      </motion.div>
      <h3
        style={{
          fontSize: '26px',
          fontWeight: 600,
          fontFamily: 'var(--font-display)',
          margin: '0 0 12px',
          letterSpacing: 0,
          color: 'var(--color-text)',
        }}
      >
        Keine Stimme verfügbar
      </h3>
      <p
        style={{
          fontSize: '14px',
          margin: '0 0 32px',
          maxWidth: '460px',
          color: 'var(--color-text-secondary)',
          lineHeight: 1.6,
        }}
      >
        Weder OmniVoice noch Kikiri meldet gerade eine Stimme. Prüfe die Dienste — oder klone eine
        neue Stimme aus einer kurzen Aufnahme.
      </p>
      <Link to="/clone" style={{ textDecoration: 'none' }}>
        <motion.button whileTap={{ scale: 0.97 }} whileHover={{ scale: 1.02 }} className="btn btn-primary" style={{ padding: '11px 22px', fontSize: '13.5px', gap: '8px' }}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="8" cy="6" r="3" />
            <path d="M2.5 14v-.5a4 4 0 014-4h3a4 4 0 014 4V14" />
            <path d="M13 3l1.5 1.5L13 6" />
          </svg>
          Stimme klonen
        </motion.button>
      </Link>
    </motion.div>
  );
}
