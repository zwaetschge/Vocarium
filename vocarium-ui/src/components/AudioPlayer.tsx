import { motion } from 'framer-motion';
import WaveformBars from './WaveformBars';

interface AudioPlayerProps {
  playing: boolean;
  progress: number;
  duration: number;
  onToggle: () => void;
  onSeek: (pct: number) => void;
  compact?: boolean;
}

function formatTime(s: number): string {
  if (!s || !isFinite(s)) return '0:00';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

export default function AudioPlayer({ playing, progress, duration, onToggle, onSeek }: AudioPlayerProps) {
  const handleBarClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    onSeek(pct);
  };

  return (
    <div
      className="glass-subtle"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '14px',
        width: '100%',
        borderRadius: '14px',
        padding: '10px 14px',
      }}
    >
      {/* Play/Pause */}
      <motion.button
        whileTap={{ scale: 0.92 }}
        whileHover={{ scale: 1.05 }}
        onClick={onToggle}
        aria-label={playing ? 'Pause' : 'Play'}
        style={{
          flexShrink: 0,
          width: '38px',
          height: '38px',
          borderRadius: '50%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'linear-gradient(135deg, rgba(123,97,255,0.9), rgba(255,77,210,0.85))',
          color: '#fff',
          border: '1px solid rgba(255,255,255,0.18)',
          cursor: 'pointer',
          boxShadow: '0 6px 18px rgba(123,97,255,0.35), inset 0 1px 0 rgba(255,255,255,0.25)',
          padding: 0,
        }}
      >
        {playing ? (
          <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
            <rect x="2.5" y="1.5" width="3" height="11" rx="1" />
            <rect x="8.5" y="1.5" width="3" height="11" rx="1" />
          </svg>
        ) : (
          <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
            <path d="M4 1.5v11l8-5.5z" />
          </svg>
        )}
      </motion.button>

      {/* Waveform pulse while active */}
      {playing && (
        <motion.div
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0 }}
          style={{ flexShrink: 0 }}
        >
          <WaveformBars active size="sm" bars={5} color="accent" />
        </motion.div>
      )}

      {/* Progress */}
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: '12px', minWidth: 0 }}>
        <div
          role="slider"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress * 100)}
          tabIndex={0}
          onClick={handleBarClick}
          style={{
            flex: 1,
            height: '6px',
            borderRadius: '999px',
            cursor: 'pointer',
            position: 'relative',
            background: 'rgba(255,255,255,0.08)',
            border: '1px solid rgba(255,255,255,0.05)',
          }}
        >
          <motion.div
            style={{
              height: '100%',
              borderRadius: '999px',
              background: 'linear-gradient(90deg, var(--color-accent), var(--color-aurora-2))',
              position: 'relative',
              width: `${progress * 100}%`,
              boxShadow: '0 0 8px rgba(123,97,255,0.45)',
            }}
            transition={{ duration: 0.1 }}
          >
            <div
              style={{
                position: 'absolute',
                right: '-4px',
                top: '50%',
                transform: 'translateY(-50%)',
                width: '10px',
                height: '10px',
                borderRadius: '50%',
                background: '#fff',
                boxShadow: '0 0 10px rgba(123,97,255,0.8), 0 0 2px rgba(255,255,255,0.8)',
                opacity: progress > 0 ? 1 : 0,
                transition: 'opacity 0.2s',
              }}
            />
          </motion.div>
        </div>

        <span
          style={{
            fontSize: '11px',
            fontVariantNumeric: 'tabular-nums',
            flexShrink: 0,
            fontFamily: 'var(--font-mono)',
            color: 'var(--color-text-dim)',
            letterSpacing: '0.02em',
          }}
        >
          {formatTime(duration * progress)} / {formatTime(duration)}
        </span>
      </div>
    </div>
  );
}
