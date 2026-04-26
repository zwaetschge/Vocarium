import { useState, useCallback, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { generateSfx, getSfxHealth } from '../api';
import AudioPlayer from '../components/AudioPlayer';
import WaveformBars from '../components/WaveformBars';
import { useAudio } from '../hooks/useAudio';

type Accent = 'aurora-1' | 'aurora-2' | 'aurora-3' | 'aurora-4';

type Example = {
  title: string;
  prompt: string;
  accent: Accent;
  icon: JSX.Element;
};

const EXAMPLES: Example[] = [
  {
    title: 'Thunder',
    prompt: 'rolling thunder with rain, dramatic storm',
    accent: 'aurora-2',
    icon: (
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M11 2L4 11h5l-2 7 8-10h-5l1-6z" />
      </svg>
    ),
  },
  {
    title: 'Footsteps',
    prompt: 'footsteps walking on gravel path, slow pace',
    accent: 'aurora-1',
    icon: (
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="6" cy="8" rx="2.5" ry="3.5" />
        <ellipse cx="14" cy="12" rx="2.5" ry="3.5" />
        <circle cx="4" cy="4" r="0.8" />
        <circle cx="8" cy="3.5" r="0.8" />
        <circle cx="12" cy="7.5" r="0.8" />
        <circle cx="16" cy="7" r="0.8" />
      </svg>
    ),
  },
  {
    title: 'Explosion',
    prompt: 'massive explosion with debris and fire, cinematic',
    accent: 'aurora-2',
    icon: (
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10 2v4M10 14v4M2 10h4M14 10h4M4.2 4.2l2.8 2.8M13 13l2.8 2.8M4.2 15.8L7 13M13 7l2.8-2.8" />
        <circle cx="10" cy="10" r="2.2" />
      </svg>
    ),
  },
  {
    title: 'Ocean Waves',
    prompt: 'gentle ocean waves crashing on sandy beach',
    accent: 'aurora-3',
    icon: (
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2 8c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 4 2" />
        <path d="M2 13c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 4 2" />
      </svg>
    ),
  },
  {
    title: 'Sword Fight',
    prompt: 'metal swords clashing, sword fight, combat',
    accent: 'aurora-4',
    icon: (
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 3l9 9M17 3l-9 9M5 17l4-4M15 17l-4-4" />
      </svg>
    ),
  },
  {
    title: 'Car Engine',
    prompt: 'sports car engine revving, acceleration',
    accent: 'aurora-1',
    icon: (
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2 12h16M4 12V8l3-3h6l3 3v4" />
        <circle cx="6" cy="14" r="1.5" />
        <circle cx="14" cy="14" r="1.5" />
      </svg>
    ),
  },
  {
    title: 'Birds',
    prompt: 'morning birds singing in forest, peaceful nature ambience',
    accent: 'aurora-3',
    icon: (
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 6c-2 0-4 1-5 3-1-1-3-2-5-1 0 3 2 5 5 5s5-2 5-5c1 0 2 0 3-1" />
        <circle cx="15" cy="5" r="0.7" fill="currentColor" />
      </svg>
    ),
  },
  {
    title: 'Door Creak',
    prompt: 'old wooden door creaking open slowly, horror',
    accent: 'aurora-4',
    icon: (
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M5 3h8v14H5zM13 3l4 2v12l-4 2" />
        <circle cx="10" cy="10" r="0.6" fill="currentColor" />
      </svg>
    ),
  },
];

function accentToColor(a: Accent): string {
  switch (a) {
    case 'aurora-1': return '#6B4BFF';
    case 'aurora-2': return '#FF4DD2';
    case 'aurora-3': return '#32B5FF';
    case 'aurora-4': return '#FFB84D';
  }
}

export default function SoundEffectsPage() {
  const audio = useAudio();
  const [prompt, setPrompt] = useState('');
  const [negativePrompt, setNegativePrompt] = useState('');
  const [duration, setDuration] = useState(8);
  const [cfgStrength, setCfgStrength] = useState(4.5);
  const [numSteps, setNumSteps] = useState(25);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');
  const [elapsed, setElapsed] = useState(0);
  const [lastBlob, setLastBlob] = useState<Blob | null>(null);
  const [sfxOnline, setSfxOnline] = useState<boolean | null>(null);
  const [activeExample, setActiveExample] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    getSfxHealth()
      .then((h) => setSfxOnline(h.status === 'ok'))
      .catch(() => setSfxOnline(false));
  }, []);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  const handleGenerate = useCallback(async () => {
    if (!prompt.trim()) return;
    setGenerating(true);
    setError('');
    setLastBlob(null);
    setElapsed(0);
    audio.stop();

    const start = Date.now();
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => setElapsed(Date.now() - start), 200);

    try {
      const blob = await generateSfx({
        prompt: prompt.trim(),
        negative_prompt: negativePrompt.trim(),
        duration,
        cfg_strength: cfgStrength,
        num_steps: numSteps,
      });
      if (timerRef.current) clearInterval(timerRef.current);
      setElapsed(Date.now() - start);
      setLastBlob(blob);
      audio.play(blob, 'sfx');
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Unknown error');
    } finally {
      if (timerRef.current) clearInterval(timerRef.current);
      setGenerating(false);
    }
  }, [prompt, negativePrompt, duration, cfgStrength, numSteps, audio]);

  const handleDownload = useCallback(() => {
    if (!lastBlob) return;
    const url = URL.createObjectURL(lastBlob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `sfx_${Date.now()}.wav`;
    a.click();
    URL.revokeObjectURL(url);
  }, [lastBlob]);

  const loadExample = (ex: Example) => {
    setPrompt(ex.prompt);
    setActiveExample(ex.title);
  };

  const formatElapsed = (ms: number) => {
    const s = Math.floor(ms / 1000);
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '28px', maxWidth: '1200px' }}>
      {/* Header */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <h1 style={{ fontSize: '32px', fontWeight: 600, fontFamily: 'var(--font-display)', letterSpacing: '-0.035em', lineHeight: 1.05, color: 'var(--color-text)' }}>
          Sound Effects
        </h1>
        <p style={{ fontSize: '14px', color: 'var(--color-text-secondary)', lineHeight: 1.55, maxWidth: '640px' }}>
          Render cinematic audio from a sentence. MMAudio interprets the prompt and returns a layered WAV — one-shots, ambience, or atmospheric beds up to 30 seconds.
        </p>
      </div>

      {/* Status strip */}
      <motion.div
        className="card-subtle"
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.25, 0.46, 0.45, 0.94] }}
        style={{
          padding: '14px 18px',
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: '12px',
        }}
      >
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '8px',
            padding: '5px 11px',
            borderRadius: '999px',
            background: sfxOnline === false ? 'rgba(255,90,101,0.12)' : 'rgba(123,97,255,0.08)',
            border: sfxOnline === false ? '1px solid rgba(255,90,101,0.28)' : '1px solid rgba(123,97,255,0.22)',
            fontSize: '11px',
            color: sfxOnline === false ? 'var(--color-danger)' : 'var(--color-text-secondary)',
            letterSpacing: '0.02em',
            fontFamily: 'var(--font-mono)',
          }}
        >
          <span
            style={{
              width: '6px',
              height: '6px',
              borderRadius: '50%',
              background: sfxOnline === false ? 'var(--color-danger)' : sfxOnline ? 'var(--color-success)' : 'var(--color-warning)',
              boxShadow: sfxOnline
                ? '0 0 6px var(--color-success)'
                : sfxOnline === false
                  ? '0 0 6px var(--color-danger)'
                  : 'none',
            }}
          />
          <span>
            MMAudio · {sfxOnline === null ? 'checking…' : sfxOnline ? 'lazy-start · 600s idle unload' : 'proxy offline'}
          </span>
        </div>
        <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', letterSpacing: '0.04em', textTransform: 'uppercase', fontWeight: 500 }}>
          Shared GPU with Music
        </span>
      </motion.div>

      {/* Quick Start presets */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span className="label-eyebrow" style={{ fontSize: '10px' }}>Quick Start</span>
          <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
            {EXAMPLES.length} presets
          </span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '10px' }}>
          {EXAMPLES.map((ex, i) => {
            const color = accentToColor(ex.accent);
            const active = activeExample === ex.title;
            return (
              <motion.button
                key={ex.title}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.35, delay: 0.04 * i, ease: [0.25, 0.46, 0.45, 0.94] }}
                whileHover={{ y: -2 }}
                whileTap={{ scale: 0.98 }}
                onClick={() => loadExample(ex)}
                style={{
                  padding: '12px 14px',
                  borderRadius: '12px',
                  background: active ? `${color}14` : 'rgba(255,255,255,0.03)',
                  border: active ? `1px solid ${color}55` : '1px solid rgba(255,255,255,0.07)',
                  cursor: 'pointer',
                  textAlign: 'left',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '11px',
                  fontFamily: 'inherit',
                  transition: 'background 0.2s ease, border-color 0.2s ease',
                  overflow: 'hidden',
                  position: 'relative',
                }}
              >
                <span
                  style={{
                    flexShrink: 0,
                    width: '26px',
                    height: '26px',
                    borderRadius: '8px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: `${color}1f`,
                    border: `1px solid ${color}40`,
                    color,
                  }}
                >
                  {ex.icon}
                </span>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: '12.5px', fontWeight: 500, color: 'var(--color-text)', letterSpacing: '-0.005em' }}>
                    {ex.title}
                  </div>
                  <div
                    style={{
                      fontSize: '10.5px',
                      color: 'var(--color-text-dim)',
                      marginTop: '2px',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {ex.prompt}
                  </div>
                </div>
              </motion.button>
            );
          })}
        </div>
      </div>

      {/* Prompt + settings grid */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1.15fr) minmax(0, 1fr)',
          gap: '20px',
          alignItems: 'start',
        }}
      >
        {/* Left column: prompts */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <label className="label-eyebrow" style={{ fontSize: '10px' }}>Sound prompt</label>
              <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                {prompt.length} chars
              </span>
            </div>
            <textarea
              value={prompt}
              onChange={(e) => {
                setPrompt(e.target.value);
                if (activeExample) setActiveExample(null);
              }}
              placeholder="e.g. heavy rain on a metal roof, distant thunder, city traffic below"
              rows={4}
              className="input-field"
              style={{ resize: 'vertical', minHeight: '108px', lineHeight: 1.55 }}
            />
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <label className="label-eyebrow" style={{ fontSize: '10px' }}>
              Negative prompt <span style={{ color: 'var(--color-text-dim)', textTransform: 'none', letterSpacing: 0, fontWeight: 400 }}>(optional)</span>
            </label>
            <input
              value={negativePrompt}
              onChange={(e) => setNegativePrompt(e.target.value)}
              placeholder="sounds to avoid — music, speech, noise floor"
              className="input-field"
            />
          </div>
        </div>

        {/* Right column: settings */}
        <div className="card-subtle" style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '18px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span className="label-eyebrow" style={{ fontSize: '10px' }}>Render settings</span>
            <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
              MMAudio
            </span>
          </div>

          {/* Duration */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)', fontWeight: 500 }}>
                Duration
              </span>
              <span style={{ fontSize: '12px', color: 'var(--color-accent)', fontFamily: 'var(--font-mono)', fontWeight: 500 }}>
                {duration.toFixed(1)}s
              </span>
            </div>
            <input
              type="range"
              min={1}
              max={30}
              step={0.5}
              value={duration}
              onChange={(e) => setDuration(parseFloat(e.target.value))}
              style={{ width: '100%', accentColor: 'var(--color-accent)' }}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
              <span>1s</span>
              <span>15s</span>
              <span>30s</span>
            </div>
          </div>

          {/* CFG Strength */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)', fontWeight: 500 }}>
                Guidance <span style={{ color: 'var(--color-text-dim)', fontWeight: 400 }}>(CFG)</span>
              </span>
              <span style={{ fontSize: '12px', color: 'var(--color-accent)', fontFamily: 'var(--font-mono)', fontWeight: 500 }}>
                {cfgStrength.toFixed(1)}
              </span>
            </div>
            <input
              type="range"
              min={1}
              max={10}
              step={0.5}
              value={cfgStrength}
              onChange={(e) => setCfgStrength(parseFloat(e.target.value))}
              style={{ width: '100%', accentColor: 'var(--color-accent)' }}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
              <span>loose</span>
              <span>balanced</span>
              <span>strict</span>
            </div>
          </div>

          {/* Steps */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)', fontWeight: 500 }}>
                Inference steps
              </span>
              <span style={{ fontSize: '12px', color: 'var(--color-accent)', fontFamily: 'var(--font-mono)', fontWeight: 500 }}>
                {numSteps}
              </span>
            </div>
            <input
              type="range"
              min={10}
              max={50}
              step={1}
              value={numSteps}
              onChange={(e) => setNumSteps(parseInt(e.target.value))}
              style={{ width: '100%', accentColor: 'var(--color-accent)' }}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
              <span>fast</span>
              <span>default</span>
              <span>quality</span>
            </div>
          </div>

          <div
            style={{
              marginTop: '2px',
              padding: '10px 12px',
              borderRadius: '10px',
              background: 'rgba(123,97,255,0.05)',
              border: '1px dashed rgba(123,97,255,0.18)',
              fontSize: '11px',
              color: 'var(--color-text-secondary)',
              lineHeight: 1.55,
            }}
          >
            Higher steps give cleaner transients but cost linearly more GPU time. 25 is the sweet spot for most effects.
          </div>
        </div>
      </div>

      {/* Generate button */}
      <motion.button
        whileHover={!generating && prompt.trim() ? { y: -1 } : {}}
        whileTap={!generating && prompt.trim() ? { scale: 0.99 } : {}}
        transition={{ type: 'spring', stiffness: 300, damping: 22 }}
        onClick={handleGenerate}
        disabled={generating || !prompt.trim()}
        className="btn btn-primary"
        style={{
          width: '100%',
          padding: '16px 28px',
          fontSize: '15px',
          fontWeight: 600,
          letterSpacing: '-0.01em',
          cursor: generating ? 'not-allowed' : !prompt.trim() ? 'not-allowed' : 'pointer',
          opacity: !prompt.trim() && !generating ? 0.5 : 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '12px',
        }}
      >
        {generating ? (
          <>
            <WaveformBars active size="sm" bars={5} color="text" />
            <span>Rendering sound</span>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', opacity: 0.8, letterSpacing: '0.02em' }}>
              {formatElapsed(elapsed)}
            </span>
          </>
        ) : (
          <>
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M11 5L6 9H2v6h4l5 4V5z" />
              <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
              <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
            </svg>
            <span>Generate sound effect</span>
          </>
        )}
      </motion.button>

      {/* Error */}
      <AnimatePresence>
        {error && (
          <motion.div
            initial={{ opacity: 0, y: -6, height: 0 }}
            animate={{ opacity: 1, y: 0, height: 'auto' }}
            exit={{ opacity: 0, y: -6, height: 0 }}
            transition={{ duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
            style={{
              padding: '13px 16px',
              background: 'rgba(255,90,101,0.08)',
              border: '1px solid rgba(255,90,101,0.28)',
              borderRadius: '12px',
              color: 'var(--color-danger)',
              fontSize: '13px',
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
            }}
          >
            <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
              <circle cx="10" cy="10" r="7.5" />
              <path d="M10 6v4M10 13.5v0.5" />
            </svg>
            <span style={{ flex: 1 }}>{error}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Result */}
      <AnimatePresence>
        {lastBlob && !generating && (
          <motion.div
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 10 }}
            transition={{ duration: 0.4, ease: [0.25, 0.46, 0.45, 0.94] }}
            className="card"
            style={{ padding: '22px', display: 'flex', flexDirection: 'column', gap: '16px' }}
          >
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '16px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', minWidth: 0 }}>
                <div
                  style={{
                    flexShrink: 0,
                    width: '30px',
                    height: '30px',
                    borderRadius: '9px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: 'linear-gradient(135deg, rgba(123,97,255,0.35), rgba(255,77,210,0.25))',
                    border: '1px solid rgba(255,255,255,0.12)',
                    boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.18)',
                    color: '#fff',
                  }}
                >
                  <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M11 5L6 9H2v2h4l5 4V5z" />
                    <path d="M15.54 6.46a5 5 0 010 7.07" />
                  </svg>
                </div>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: '14px', fontWeight: 500, color: 'var(--color-text)', letterSpacing: '-0.005em' }}>
                    Sound effect ready
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--color-text-dim)', marginTop: '2px', fontFamily: 'var(--font-mono)', letterSpacing: '0.02em' }}>
                    Rendered in {formatElapsed(elapsed)} · {duration.toFixed(1)}s audio
                  </div>
                </div>
              </div>
              <motion.button
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.97 }}
                onClick={handleDownload}
                className="btn btn-ghost"
                style={{
                  padding: '8px 14px',
                  fontSize: '12px',
                  fontWeight: 500,
                  letterSpacing: '-0.005em',
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '7px',
                  color: 'var(--color-accent)',
                  border: '1px solid rgba(123,97,255,0.28)',
                  background: 'rgba(123,97,255,0.08)',
                }}
              >
                <svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M10 3v10m0 0l-4-4m4 4l4-4M4 15v2h12v-2" />
                </svg>
                <span>Download WAV</span>
              </motion.button>
            </div>

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
  );
}
