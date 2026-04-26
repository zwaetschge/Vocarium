import { useState, useEffect, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Link } from 'react-router-dom';
import { getVoices } from '../api';
import type { Voice } from '../types';
import VoiceCard from '../components/VoiceCard';
import SkeletonCard from '../components/SkeletonCard';
import WaveformBars from '../components/WaveformBars';

type SourceFilter = 'all' | 'clone' | 'design';

export default function VoicesPage() {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<SourceFilter>('all');

  const fetchVoices = async () => {
    try {
      const data = await getVoices();
      setVoices(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load voices');
    }
    setLoading(false);
  };

  useEffect(() => {
    fetchVoices();
  }, []);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return voices.filter((v) => {
      if (filter === 'clone' && v.source !== 'clone') return false;
      if (filter === 'design' && v.source !== 'design') return false;
      if (!q) return true;
      return v.name.toLowerCase().includes(q) || v.language.toLowerCase().includes(q);
    });
  }, [voices, search, filter]);

  const counts = useMemo(
    () => ({
      all: voices.length,
      clone: voices.filter((v) => v.source === 'clone').length,
      design: voices.filter((v) => v.source === 'design').length,
    }),
    [voices]
  );

  if (loading) {
    return (
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '28px' }}>
          <div className="skeleton-shimmer" style={{ height: '32px', width: '200px', borderRadius: '10px' }} />
          <div className="skeleton-shimmer" style={{ height: '24px', width: '48px', borderRadius: '999px' }} />
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: '18px' }}>
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

  return (
    <div>
      {/* Toolbar */}
      <div
        className="glass"
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '14px',
          padding: '14px 18px',
          borderRadius: '16px',
          marginBottom: '28px',
          flexWrap: 'wrap',
        }}
      >
        {/* Count pill */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <motion.span
            initial={{ scale: 0.8, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ type: 'spring', stiffness: 400, damping: 22, delay: 0.08 }}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              minWidth: '28px',
              height: '26px',
              padding: '0 9px',
              fontSize: '12px',
              fontWeight: 700,
              borderRadius: '999px',
              background: 'rgba(123,97,255,0.14)',
              color: 'var(--color-accent)',
              fontFamily: 'var(--font-mono)',
              border: '1px solid rgba(123,97,255,0.28)',
              letterSpacing: '0.02em',
            }}
          >
            {filtered.length}
          </motion.span>
          <span
            style={{
              fontSize: '13px',
              color: 'var(--color-text-secondary)',
              letterSpacing: '-0.005em',
            }}
          >
            {filtered.length === 1 ? 'voice' : 'voices'}
            {search && (
              <span style={{ color: 'var(--color-text-dim)' }}>
                {' '}matching "{search}"
              </span>
            )}
          </span>
        </div>

        <div style={{ flex: 1 }} />

        {/* Search */}
        <div style={{ position: 'relative', width: '240px' }}>
          <svg
            width="14"
            height="14"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            style={{
              position: 'absolute',
              left: '12px',
              top: '50%',
              transform: 'translateY(-50%)',
              color: 'var(--color-text-dim)',
              pointerEvents: 'none',
            }}
          >
            <circle cx="7" cy="7" r="5" />
            <path d="M14 14l-3-3" />
          </svg>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search voices..."
            className="input-field"
            style={{
              width: '100%',
              height: '36px',
              paddingLeft: '34px',
              paddingRight: '12px',
              fontSize: '13px',
              boxSizing: 'border-box',
            }}
          />
        </div>

        {/* Filter tabs */}
        <div
          role="tablist"
          style={{
            display: 'flex',
            padding: '3px',
            borderRadius: '10px',
            background: 'rgba(255,255,255,0.04)',
            border: '1px solid rgba(255,255,255,0.06)',
          }}
        >
          {(['all', 'clone', 'design'] as SourceFilter[]).map((f) => {
            const active = filter === f;
            return (
              <button
                key={f}
                role="tab"
                aria-selected={active}
                onClick={() => setFilter(f)}
                style={{
                  position: 'relative',
                  padding: '6px 12px',
                  fontSize: '12px',
                  fontWeight: 500,
                  letterSpacing: '0.01em',
                  textTransform: 'capitalize',
                  color: active ? 'var(--color-text)' : 'var(--color-text-secondary)',
                  background: 'transparent',
                  border: 'none',
                  cursor: 'pointer',
                  borderRadius: '7px',
                  transition: 'color 0.15s ease',
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '7px',
                }}
              >
                {active && (
                  <motion.span
                    layoutId="voices-filter-indicator"
                    style={{
                      position: 'absolute',
                      inset: 0,
                      background: 'rgba(255,255,255,0.08)',
                      borderRadius: '7px',
                      border: '1px solid rgba(255,255,255,0.1)',
                      boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.06)',
                    }}
                    transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                  />
                )}
                <span style={{ position: 'relative' }}>{f}</span>
                <span
                  style={{
                    position: 'relative',
                    fontSize: '10.5px',
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--color-text-dim)',
                    letterSpacing: '0.02em',
                  }}
                >
                  {counts[f]}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Grid */}
      {filtered.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="card-subtle"
          style={{
            padding: '60px 32px',
            textAlign: 'center',
          }}
        >
          <div
            style={{
              width: '44px',
              height: '44px',
              borderRadius: '12px',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: 'rgba(255,255,255,0.04)',
              border: '1px solid rgba(255,255,255,0.08)',
              color: 'var(--color-text-dim)',
              marginBottom: '16px',
            }}
          >
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <circle cx="9" cy="9" r="6" />
              <path d="M18 18l-4.5-4.5" />
            </svg>
          </div>
          <h4
            style={{
              fontSize: '15px',
              fontWeight: 600,
              fontFamily: 'var(--font-display)',
              color: 'var(--color-text)',
              margin: '0 0 6px',
              letterSpacing: '-0.02em',
            }}
          >
            No matches
          </h4>
          <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)', margin: 0 }}>
            Try a different search or filter.
          </p>
        </motion.div>
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
            gap: '18px',
          }}
        >
          <AnimatePresence mode="popLayout">
            {filtered.map((voice, i) => (
              <VoiceCard key={voice.id} voice={voice} index={i} onDeleted={fetchVoices} />
            ))}
          </AnimatePresence>
        </div>
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
          letterSpacing: '-0.02em',
        }}
      >
        Couldn't load voices
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
        Try again
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
          letterSpacing: '-0.035em',
          color: 'var(--color-text)',
        }}
      >
        Your voice library is empty
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
        Clone a voice from a short audio sample, or compose one from a text description.
        Voices live here for reuse across Speech.
      </p>
      <div style={{ display: 'flex', gap: '12px' }}>
        <Link to="/clone" style={{ textDecoration: 'none' }}>
          <motion.button whileTap={{ scale: 0.97 }} whileHover={{ scale: 1.02 }} className="btn btn-primary" style={{ padding: '11px 22px', fontSize: '13.5px', gap: '8px' }}>
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="8" cy="6" r="3" />
              <path d="M2.5 14v-.5a4 4 0 014-4h3a4 4 0 014 4V14" />
              <path d="M13 3l1.5 1.5L13 6" />
            </svg>
            Clone a voice
          </motion.button>
        </Link>
        <Link to="/design" style={{ textDecoration: 'none' }}>
          <motion.button whileTap={{ scale: 0.97 }} whileHover={{ scale: 1.02 }} className="btn btn-ghost" style={{ padding: '11px 22px', fontSize: '13.5px', gap: '8px' }}>
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M2 12l3-1 8.5-8.5a1.8 1.8 0 00-2.5-2.5L2.5 8.5 2 12z" transform="translate(1 1)" />
            </svg>
            Design a voice
          </motion.button>
        </Link>
      </div>
    </motion.div>
  );
}
