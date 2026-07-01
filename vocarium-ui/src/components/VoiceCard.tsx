import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import type { Voice } from '../types';
import { getVoiceAudio, generate, deleteVoice } from '../api';
import AudioPlayer from './AudioPlayer';
import { useAudio } from '../hooks/useAudio';
import WaveformBars from './WaveformBars';
import { voiceSourceLabel } from '../voiceUtils';

interface VoiceCardProps {
  voice: Voice;
  index: number;
  onDeleted: () => void;
}

export default function VoiceCard({ voice, index, onDeleted }: VoiceCardProps) {
  const audio = useAudio();
  const genAudio = useAudio();
  const [expanded, setExpanded] = useState(false);
  const [testText, setTestText] = useState('Hello, this is a test of voice synthesis.');
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const canPlayReference = voice.has_audio && voice.id !== 'default';

  const handlePlay = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!canPlayReference) return;
    if (audio.playing && audio.currentId === voice.id) {
      audio.toggle();
      return;
    }
    setLoadingPreview(true);
    setError('');
    try {
      const blob = await getVoiceAudio(voice.id);
      audio.play(blob, voice.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Preview unavailable');
      setExpanded(true);
    }
    setLoadingPreview(false);
  };

  const handleGenerate = async () => {
    if (!testText.trim()) return;
    setGenerating(true);
    setError('');
    try {
      const { blob } = await generate({ text: testText, voice_id: voice.id });
      genAudio.play(blob, 'gen-' + voice.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Generation failed');
    }
    setGenerating(false);
  };

  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirmDelete) {
      setConfirmDelete(true);
      setTimeout(() => setConfirmDelete(false), 3000);
      return;
    }
    try {
      await deleteVoice(voice.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed');
    }
  };

  const isPlaying = audio.playing && audio.currentId === voice.id;
  const isCloned = voice.source === 'clone' || voice.id === 'default';
  const initial = voice.name.trim().charAt(0).toUpperCase() || '?';
  const accentTint = isCloned
    ? 'linear-gradient(135deg, rgba(123,97,255,0.45), rgba(50,181,255,0.3))'
    : 'linear-gradient(135deg, rgba(255,77,210,0.42), rgba(123,97,255,0.3))';

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 18 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.04, 0.3), duration: 0.38, ease: [0.25, 0.46, 0.45, 0.94] }}
      whileHover={{ y: -3 }}
      onClick={() => setExpanded(!expanded)}
      className="card-subtle"
      style={{
        cursor: 'pointer',
        overflow: 'hidden',
        transition: 'box-shadow 0.25s ease, border-color 0.25s ease',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.boxShadow = '0 10px 36px rgba(0,0,0,0.35), 0 0 0 1px rgba(123,97,255,0.18), inset 0 1px 0 rgba(255,255,255,0.06)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.boxShadow = '';
      }}
    >
      <div style={{ padding: '20px 22px' }}>
        {/* Header row */}
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '14px', marginBottom: '16px' }}>
          {/* Avatar */}
          <div
            style={{
              width: '40px',
              height: '40px',
              borderRadius: '12px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '15px',
              fontWeight: 600,
              fontFamily: 'var(--font-display)',
              color: '#fff',
              background: accentTint,
              border: '1px solid rgba(255,255,255,0.14)',
              boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.2)',
              flexShrink: 0,
              letterSpacing: 0,
            }}
          >
            {initial}
          </div>

          {/* Name + meta */}
          <div style={{ minWidth: 0, flex: 1 }}>
            <h3
              style={{
                fontSize: '15.5px',
                fontWeight: 600,
                margin: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontFamily: 'var(--font-display)',
                letterSpacing: 0,
                color: 'var(--color-text)',
                lineHeight: 1.2,
              }}
            >
              {voice.name}
            </h3>
            <p
              style={{
                fontSize: '11px',
                marginTop: '4px',
                margin: '4px 0 0',
                fontFamily: 'var(--font-mono)',
                color: 'var(--color-text-dim)',
                letterSpacing: 0,
              }}
            >
              {new Date(voice.created_at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}
            </p>
          </div>

          {/* Controls */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
            <motion.button
              whileTap={{ scale: 0.9 }}
              whileHover={{ scale: 1.06 }}
              transition={{ type: 'spring', stiffness: 300, damping: 22 }}
              onClick={handlePlay}
              disabled={loadingPreview || !canPlayReference}
              aria-label={isPlaying ? 'Pause preview' : 'Play preview'}
              title={canPlayReference ? 'Play reference audio' : 'No reference audio for this voice'}
              style={{
                width: '36px',
                height: '36px',
                borderRadius: '50%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                border: '1px solid rgba(255,255,255,0.14)',
                cursor: loadingPreview ? 'wait' : canPlayReference ? 'pointer' : 'not-allowed',
                background: isPlaying
                  ? 'linear-gradient(135deg, rgba(123,97,255,0.95), rgba(255,77,210,0.85))'
                  : 'rgba(255,255,255,0.05)',
                color: canPlayReference ? (isPlaying ? '#fff' : 'var(--color-accent)') : 'var(--color-text-faint)',
                opacity: canPlayReference ? 1 : 0.55,
                boxShadow: isPlaying
                  ? '0 6px 18px rgba(123,97,255,0.35), inset 0 1px 0 rgba(255,255,255,0.22)'
                  : 'inset 0 1px 0 rgba(255,255,255,0.05)',
                padding: 0,
              }}
            >
              {loadingPreview ? (
                <motion.div animate={{ rotate: 360 }} transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}>
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                    <path d="M7 1a6 6 0 105.2 3" />
                  </svg>
                </motion.div>
              ) : isPlaying ? (
                <WaveformBars active size="sm" bars={4} color="text" />
              ) : (
                <svg width="12" height="12" viewBox="0 0 14 14" fill="currentColor">
                  <path d="M4 1.5v11l8-5.5z" />
                </svg>
              )}
            </motion.button>

            <motion.button
              whileTap={{ scale: 0.9 }}
              onClick={handleDelete}
              aria-label={confirmDelete ? 'Confirm delete' : 'Delete voice'}
              style={{
                width: '32px',
                height: '32px',
                borderRadius: '50%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                border: '1px solid transparent',
                cursor: 'pointer',
                background: confirmDelete ? 'var(--color-danger-dim)' : 'transparent',
                color: confirmDelete ? 'var(--color-danger)' : 'var(--color-text-dim)',
                transition: 'background 0.15s ease, color 0.15s ease, border-color 0.15s ease',
                padding: 0,
              }}
              onMouseEnter={(e) => {
                if (!confirmDelete) {
                  e.currentTarget.style.color = 'var(--color-danger)';
                  e.currentTarget.style.background = 'var(--color-danger-dim)';
                }
              }}
              onMouseLeave={(e) => {
                if (!confirmDelete) {
                  e.currentTarget.style.color = 'var(--color-text-dim)';
                  e.currentTarget.style.background = 'transparent';
                }
              }}
            >
              {confirmDelete ? (
                <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                  <path d="M2 7l3.5 3.5L12 3" />
                </svg>
              ) : (
                <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                  <path d="M2 3.5h10M5.5 3.5V2h3v1.5M3.5 3.5v8a1 1 0 001 1h5a1 1 0 001-1v-8" />
                </svg>
              )}
            </motion.button>
          </div>
        </div>

        {/* Badges */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <SourceBadge voice={voice} />
          <LanguageBadge lang={voice.language} />
        </div>

        {/* Inline preview player */}
        <AnimatePresence>
          {isPlaying && (
            <motion.div
              initial={{ opacity: 0, height: 0, marginTop: 0 }}
              animate={{ opacity: 1, height: 'auto', marginTop: 16 }}
              exit={{ opacity: 0, height: 0, marginTop: 0 }}
              transition={{ duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
              style={{ overflow: 'hidden' }}
            >
              <AudioPlayer
                playing={audio.playing}
                progress={audio.progress}
                duration={audio.duration}
                onToggle={audio.toggle}
                onSeek={audio.seek}
              />
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Expanded test area */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.28, ease: [0.25, 0.46, 0.45, 0.94] }}
            style={{ overflow: 'hidden' }}
          >
            <div
              onClick={(e) => e.stopPropagation()}
              style={{
                padding: '16px 22px 22px',
                display: 'flex',
                flexDirection: 'column',
                gap: '12px',
                borderTop: '1px solid rgba(255,255,255,0.06)',
                background: 'rgba(255,255,255,0.02)',
              }}
            >
              <div className="label-eyebrow" style={{ fontSize: '10px' }}>
                Quick test
              </div>
              {error && (
                <div
                  style={{
                    padding: '8px 10px',
                    borderRadius: '8px',
                    background: 'var(--color-danger-dim)',
                    color: 'var(--color-danger)',
                    fontSize: '12px',
                  }}
                >
                  {error}
                </div>
              )}
              <textarea
                value={testText}
                onChange={(e) => setTestText(e.target.value)}
                rows={2}
                className="input-field"
                style={{
                  width: '100%',
                  padding: '12px 14px',
                  fontSize: '13.5px',
                  resize: 'none',
                  lineHeight: 1.5,
                  fontFamily: 'var(--font-body)',
                  boxSizing: 'border-box',
                  height: 'auto',
                }}
                placeholder="Enter text to synthesize..."
              />
              <motion.button
                whileTap={{ scale: 0.98 }}
                onClick={handleGenerate}
                disabled={generating || !testText.trim()}
                className="btn btn-primary"
                style={{
                  width: '100%',
                  height: '40px',
                  fontSize: '13.5px',
                  fontWeight: 600,
                  letterSpacing: 0,
                  gap: '8px',
                }}
              >
                {generating ? (
                  <>
                    <motion.div animate={{ rotate: 360 }} transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}>
                      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                        <path d="M7 1a6 6 0 105.2 3" />
                      </svg>
                    </motion.div>
                    Synthesizing…
                  </>
                ) : (
                  <>
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M2 7h10M8 3l4 4-4 4" />
                    </svg>
                    Generate preview
                  </>
                )}
              </motion.button>

              <AnimatePresence>
                {genAudio.currentId === 'gen-' + voice.id && (
                  <motion.div
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: 4 }}
                    transition={{ duration: 0.25 }}
                  >
                    <AudioPlayer
                      playing={genAudio.playing}
                      progress={genAudio.progress}
                      duration={genAudio.duration}
                      onToggle={genAudio.toggle}
                      onSeek={genAudio.seek}
                    />
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function SourceBadge({ voice }: { voice: Voice }) {
  const label = voiceSourceLabel(voice);
  const isCloneLike = voice.source === 'clone' || voice.id === 'default';
  const isCustom = voice.source === 'custom';
  const color = isCloneLike
    ? 'var(--color-accent)'
    : isCustom
      ? 'var(--color-aurora-3)'
      : 'var(--color-aurora-2)';
  const background = isCloneLike
    ? 'rgba(123,97,255,0.14)'
    : isCustom
      ? 'rgba(50,181,255,0.14)'
      : 'rgba(255,77,210,0.14)';
  const border = isCloneLike
    ? 'rgba(123,97,255,0.25)'
    : isCustom
      ? 'rgba(50,181,255,0.25)'
      : 'rgba(255,77,210,0.25)';

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '5px',
        padding: '3px 10px',
        fontSize: '10.5px',
        fontWeight: 600,
        letterSpacing: 0,
        textTransform: 'uppercase',
        borderRadius: '999px',
        background,
        color,
        border: `1px solid ${border}`,
        fontFamily: 'var(--font-mono)',
      }}
    >
      <span
        style={{
          width: '5px',
          height: '5px',
          borderRadius: '50%',
          background: 'currentColor',
          boxShadow: '0 0 6px currentColor',
        }}
      />
      {label}
    </span>
  );
}

function LanguageBadge({ lang }: { lang: string }) {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '3px 10px',
        fontSize: '10.5px',
        fontWeight: 500,
        letterSpacing: 0,
        textTransform: 'uppercase',
        borderRadius: '999px',
        background: 'rgba(255,255,255,0.05)',
        color: 'var(--color-text-secondary)',
        border: '1px solid rgba(255,255,255,0.08)',
        fontFamily: 'var(--font-mono)',
      }}
    >
      {lang}
    </span>
  );
}
