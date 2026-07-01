import { useState, useRef, useCallback, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { generateMusic, getMusicAudio, getMusicHealth } from '../api';
import AudioPlayer from '../components/AudioPlayer';
import { useAudio } from '../hooks/useAudio';
import WaveformBars from '../components/WaveformBars';

const KEY_SCALES = [
  'C Major', 'C Minor', 'C# Major', 'C# Minor',
  'D Major', 'D Minor', 'D# Major', 'D# Minor',
  'E Major', 'E Minor',
  'F Major', 'F Minor', 'F# Major', 'F# Minor',
  'G Major', 'G Minor', 'G# Major', 'G# Minor',
  'A Major', 'A Minor', 'A# Major', 'A# Minor',
  'B Major', 'B Minor',
];

const EXAMPLES = [
  {
    title: 'Indie Pop',
    badge: 'Vocals',
    prompt: 'upbeat indie pop with acoustic guitar, catchy melody, warm female vocals',
    lyrics:
      '[Verse 1]\nWalking through the morning light\nEverything is feeling right\n\n[Chorus]\nWe are golden, we are free\nThis is where we want to be',
    accent: 'aurora-2',
  },
  {
    title: 'Lo-fi Hip Hop',
    badge: 'Instrumental',
    prompt: 'lo-fi hip hop beat, chill vibes, jazzy piano chords, vinyl crackle, mellow bass',
    lyrics: '',
    accent: 'aurora-3',
  },
  {
    title: 'Epic Cinematic',
    badge: 'Instrumental',
    prompt:
      'epic cinematic orchestral, dramatic strings, powerful brass, building crescendo, movie trailer',
    lyrics: '',
    accent: 'accent',
  },
  {
    title: 'Synth Pop',
    badge: 'Vocals',
    prompt: 'retro synth pop, 80s vibes, gated reverb drums, warm analog synths, soaring vocals',
    lyrics:
      '[Verse 1]\nNeon lights across the sky\nWe were never meant to fly\n\n[Chorus]\nHold on tight, we burn so bright\nLost in the glow of the city lights',
    accent: 'aurora-4',
  },
] as const;

const auroraToColor = (key: string): string => {
  switch (key) {
    case 'aurora-1': return 'var(--color-aurora-1)';
    case 'aurora-2': return 'var(--color-aurora-2)';
    case 'aurora-3': return 'var(--color-aurora-3)';
    case 'aurora-4': return 'var(--color-aurora-4)';
    default: return 'var(--color-accent)';
  }
};

export default function MusicPage() {
  const audio = useAudio();
  const [prompt, setPrompt] = useState('');
  const [lyrics, setLyrics] = useState('');
  const [duration, setDuration] = useState(60);
  const [bpm, setBpm] = useState<string>('');
  const [keyScale, setKeyScale] = useState('');
  const [timeSig, setTimeSig] = useState('');
  const [thinking, setThinking] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [statusText, setStatusText] = useState('');
  const [error, setError] = useState('');
  const [elapsed, setElapsed] = useState(0);
  const [lastBlob, setLastBlob] = useState<Blob | null>(null);
  const [musicOnline, setMusicOnline] = useState<boolean | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval>>();

  useEffect(() => {
    getMusicHealth()
      .then((h) => setMusicOnline(h.status === 'ok'))
      .catch(() => setMusicOnline(false));
  }, []);

  const handleGenerate = useCallback(async () => {
    if (!prompt.trim()) return;
    setGenerating(true);
    setError('');
    setStatusText('Queued — waiting for GPU…');
    setLastBlob(null);
    audio.stop();
    const start = Date.now();
    setElapsed(0);

    timerRef.current = setInterval(() => setElapsed(Date.now() - start), 200);

    try {
      setStatusText('Generating… (may take 1–2 min)');
      const res = await generateMusic({
        prompt: prompt.trim(),
        lyrics: lyrics.trim() || undefined,
        audio_duration: duration,
        bpm: bpm ? parseInt(bpm) : undefined,
        key_scale: keyScale || undefined,
        time_signature: timeSig || undefined,
        thinking,
      });

      const tasks = res.result?.data || [];
      const task = tasks[0];
      if (task?.status === 1 && task.result) {
        setStatusText('Downloading audio…');
        const resultArr = JSON.parse(task.result);
        if (resultArr.length > 0 && resultArr[0].file) {
          const audioPath = resultArr[0].file.replace('/v1/audio?path=', '');
          const blob = await getMusicAudio(audioPath);
          setLastBlob(blob);
          audio.play(blob, 'music');
        } else {
          setError('No audio in result');
        }
      } else {
        setError('Generation failed or returned no result');
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Unknown error');
    } finally {
      setGenerating(false);
      setStatusText('');
      if (timerRef.current) clearInterval(timerRef.current);
    }
  }, [prompt, lyrics, duration, bpm, keyScale, timeSig, thinking, audio]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  const handleDownload = useCallback(() => {
    if (!lastBlob) return;
    const url = URL.createObjectURL(lastBlob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `music_${Date.now()}.wav`;
    a.click();
    URL.revokeObjectURL(url);
  }, [lastBlob]);

  const loadExample = (ex: typeof EXAMPLES[number]) => {
    setPrompt(ex.prompt);
    setLyrics(ex.lyrics);
  };

  const formatTime = (ms: number) => {
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    return m > 0 ? `${m}:${String(s % 60).padStart(2, '0')}` : `${s}s`;
  };

  const canGenerate = !generating && prompt.trim().length > 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '22px', maxWidth: '1040px' }}>
      {/* Intro strip */}
      <div
        className="card-subtle"
        style={{
          padding: '14px 18px',
          display: 'flex',
          alignItems: 'center',
          gap: '14px',
          flexWrap: 'wrap',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            padding: '5px 10px',
            borderRadius: '999px',
            background: musicOnline === false ? 'rgba(255,90,101,0.08)' : 'rgba(123,97,255,0.08)',
            border: `1px solid ${
              musicOnline === false ? 'rgba(255,90,101,0.22)' : 'rgba(123,97,255,0.22)'
            }`,
            fontSize: '10.5px',
            fontFamily: 'var(--font-mono)',
            color: musicOnline === false ? 'var(--color-danger)' : 'var(--color-accent)',
            textTransform: 'uppercase',
            letterSpacing: 0,
          }}
        >
          <span
            style={{
              width: '6px',
              height: '6px',
              borderRadius: '50%',
              background: musicOnline === false ? 'var(--color-danger)' : 'var(--color-accent)',
              boxShadow:
                musicOnline === false
                  ? '0 0 6px rgba(255,90,101,0.5)'
                  : '0 0 8px rgba(123,97,255,0.7)',
            }}
          />
          ACE-Step ·{' '}
          {musicOnline === null
            ? 'checking…'
            : musicOnline
              ? 'lazy-start'
              : 'proxy offline'}
        </div>
        <p style={{ fontSize: '12.5px', color: 'var(--color-text-secondary)', margin: 0 }}>
          Describe a style, optionally add lyrics, and ACE-Step composes at 44.1 kHz.
        </p>
      </div>

      {/* Example presets */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        <div className="label-eyebrow" style={{ fontSize: '10.5px' }}>Quick start</div>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
            gap: '10px',
          }}
        >
          {EXAMPLES.map((ex) => (
            <motion.button
              key={ex.title}
              whileHover={{ y: -2 }}
              whileTap={{ scale: 0.98 }}
              transition={{ type: 'spring', stiffness: 400, damping: 28 }}
              onClick={() => loadExample(ex)}
              className="card-subtle"
              style={{
                padding: '14px 16px',
                cursor: 'pointer',
                textAlign: 'left',
                display: 'flex',
                flexDirection: 'column',
                gap: '8px',
                background: 'rgba(255,255,255,0.02)',
                border: '1px solid rgba(255,255,255,0.06)',
                transition: 'background 0.18s, border-color 0.18s',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'rgba(255,255,255,0.04)';
                e.currentTarget.style.borderColor = 'rgba(255,255,255,0.14)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'rgba(255,255,255,0.02)';
                e.currentTarget.style.borderColor = 'rgba(255,255,255,0.06)';
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
                <span
                  style={{
                    width: '26px',
                    height: '26px',
                    borderRadius: '8px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: `${auroraToColor(ex.accent)}1f`,
                    border: `1px solid ${auroraToColor(ex.accent)}40`,
                    color: auroraToColor(ex.accent),
                  }}
                >
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="4.5" cy="12.5" r="2" />
                    <circle cx="12.5" cy="10.5" r="2" />
                    <path d="M6.5 12.5V4l8-2.2V10.5" />
                  </svg>
                </span>
                <span
                  style={{
                    fontSize: '9.5px',
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--color-text-dim)',
                    textTransform: 'uppercase',
                    letterSpacing: 0,
                  }}
                >
                  {ex.badge}
                </span>
              </div>
              <div
                style={{
                  fontSize: '13.5px',
                  fontWeight: 600,
                  fontFamily: 'var(--font-display)',
                  letterSpacing: 0,
                  color: 'var(--color-text)',
                }}
              >
                {ex.title}
              </div>
              <div style={{ fontSize: '11.5px', color: 'var(--color-text-dim)', lineHeight: 1.45 }}>
                {ex.prompt.length > 70 ? `${ex.prompt.slice(0, 70)}…` : ex.prompt}
              </div>
            </motion.button>
          ))}
        </div>
      </div>

      {/* Two columns: prompt/lyrics + settings */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1.15fr) minmax(0, 1fr)',
          gap: '20px',
          alignItems: 'start',
        }}
      >
        {/* Left: prompt + lyrics */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <label className="label-eyebrow" style={{ fontSize: '10.5px' }}>
                Music description
              </label>
              <span style={{ fontSize: '10.5px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                {prompt.length} chars
              </span>
            </div>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Describe style, genre, mood, instruments, vibe…"
              rows={4}
              className="input-field"
              style={{ resize: 'vertical', minHeight: '100px', lineHeight: 1.55 }}
            />
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <label className="label-eyebrow" style={{ fontSize: '10.5px' }}>
                Lyrics · optional
              </label>
              <span style={{ fontSize: '10.5px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                {lyrics.length} chars
              </span>
            </div>
            <textarea
              value={lyrics}
              onChange={(e) => setLyrics(e.target.value)}
              placeholder={'[Verse 1]\nYour lyrics here…\n\n[Chorus]\nChorus lyrics…'}
              rows={8}
              className="input-field"
              style={{
                resize: 'vertical',
                minHeight: '200px',
                fontFamily: 'var(--font-mono)',
                fontSize: '13px',
                lineHeight: 1.6,
              }}
            />
          </div>
        </div>

        {/* Right: settings */}
        <div
          className="card-subtle"
          style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '18px' }}
        >
          {/* Duration */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
              <label className="label-eyebrow" style={{ fontSize: '10.5px' }}>
                Duration
              </label>
              <span
                style={{
                  fontSize: '15px',
                  fontWeight: 600,
                  fontFamily: 'var(--font-mono)',
                  color: 'var(--color-accent)',
                  letterSpacing: 0,
                }}
              >
                {duration}s
              </span>
            </div>
            <input
              type="range"
              min={10}
              max={300}
              step={5}
              value={duration}
              onChange={(e) => setDuration(parseInt(e.target.value))}
              style={{
                width: '100%',
                accentColor: 'var(--color-accent)',
                cursor: 'pointer',
              }}
            />
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                fontSize: '10px',
                fontFamily: 'var(--font-mono)',
                color: 'var(--color-text-dim)',
                letterSpacing: 0,
              }}
            >
              <span>10s</span>
              <span>1 min</span>
              <span>5 min</span>
            </div>
          </div>

          <div style={{ height: '1px', background: 'rgba(255,255,255,0.06)' }} />

          {/* BPM + Time signature */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <label className="label-eyebrow" style={{ fontSize: '10.5px' }}>
                BPM
              </label>
              <input
                type="number"
                min={30}
                max={300}
                value={bpm}
                onChange={(e) => setBpm(e.target.value)}
                placeholder="Auto"
                className="input-field"
                style={{ fontFamily: 'var(--font-mono)' }}
              />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <label className="label-eyebrow" style={{ fontSize: '10.5px' }}>
                Time signature
              </label>
              <select
                value={timeSig}
                onChange={(e) => setTimeSig(e.target.value)}
                className="input-field"
                style={{ fontFamily: 'var(--font-mono)', cursor: 'pointer' }}
              >
                <option value="">Auto</option>
                <option value="2">2/4</option>
                <option value="3">3/4</option>
                <option value="4">4/4</option>
                <option value="6">6/8</option>
              </select>
            </div>
          </div>

          {/* Key scale */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <label className="label-eyebrow" style={{ fontSize: '10.5px' }}>
              Key
            </label>
            <select
              value={keyScale}
              onChange={(e) => setKeyScale(e.target.value)}
              className="input-field"
              style={{ fontFamily: 'var(--font-mono)', cursor: 'pointer' }}
            >
              <option value="">Auto</option>
              {KEY_SCALES.map((k) => (
                <option key={k} value={k}>{k}</option>
              ))}
            </select>
          </div>

          <div style={{ height: '1px', background: 'rgba(255,255,255,0.06)' }} />

          {/* Thinking toggle */}
          <label
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              gap: '12px',
              cursor: 'pointer',
              padding: '10px 12px',
              borderRadius: '10px',
              background: thinking ? 'rgba(123,97,255,0.07)' : 'rgba(255,255,255,0.02)',
              border: `1px solid ${thinking ? 'rgba(123,97,255,0.25)' : 'rgba(255,255,255,0.06)'}`,
              transition: 'background 0.18s, border-color 0.18s',
            }}
          >
            <div
              role="checkbox"
              aria-checked={thinking}
              onClick={() => setThinking((v) => !v)}
              style={{
                width: '18px',
                height: '18px',
                borderRadius: '5px',
                flexShrink: 0,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                marginTop: '1px',
                background: thinking
                  ? 'linear-gradient(135deg, var(--color-accent), var(--color-aurora-2))'
                  : 'transparent',
                border: `1.5px solid ${thinking ? 'transparent' : 'rgba(255,255,255,0.2)'}`,
                boxShadow: thinking ? '0 2px 8px rgba(123,97,255,0.35)' : 'none',
                transition: 'background 0.15s',
              }}
            >
              <input
                type="checkbox"
                checked={thinking}
                onChange={(e) => setThinking(e.target.checked)}
                style={{ display: 'none' }}
              />
              {thinking && (
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M2 5l2 2 4-4" />
                </svg>
              )}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
              <div
                style={{
                  fontSize: '13px',
                  fontWeight: 500,
                  color: thinking ? 'var(--color-text)' : 'var(--color-text-secondary)',
                  letterSpacing: 0,
                }}
              >
                Smart prompt enhancement
              </div>
              <div style={{ fontSize: '11.5px', color: 'var(--color-text-dim)', lineHeight: 1.45 }}>
                LM refines the prompt, lyrics, and metadata before generation.
              </div>
            </div>
          </label>
        </div>
      </div>

      {/* Generate button */}
      <motion.button
        whileHover={canGenerate ? { scale: 1.005 } : undefined}
        whileTap={canGenerate ? { scale: 0.99 } : undefined}
        transition={{ type: 'spring', stiffness: 300, damping: 22 }}
        disabled={!canGenerate}
        onClick={handleGenerate}
        className="btn btn-primary"
        style={{
          width: '100%',
          padding: '16px',
          fontSize: '14.5px',
          fontWeight: 600,
          letterSpacing: 0,
          opacity: !canGenerate && !generating ? 0.5 : 1,
          cursor: canGenerate ? 'pointer' : 'not-allowed',
          gap: '12px',
        }}
      >
        {generating ? (
          <>
            <WaveformBars active size="sm" bars={5} color="text" />
            <span>{statusText || 'Generating…'}</span>
            <span
              style={{
                fontSize: '12.5px',
                fontFamily: 'var(--font-mono)',
                opacity: 0.8,
                marginLeft: '6px',
              }}
            >
              {formatTime(elapsed)}
            </span>
          </>
        ) : (
          <>
            <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10 3v10M6 7v2M14 5v6M2 9v2M18 8v4" />
            </svg>
            Generate music
          </>
        )}
      </motion.button>

      {/* Error */}
      <AnimatePresence>
        {error && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.2 }}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
              padding: '12px 16px',
              borderRadius: '12px',
              background: 'rgba(255,90,101,0.08)',
              border: '1px solid rgba(255,90,101,0.22)',
              fontSize: '13px',
              color: 'var(--color-danger)',
            }}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="8" cy="8" r="6" />
              <path d="M8 5.5v3M8 10.5h.01" />
            </svg>
            {error}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Result */}
      <AnimatePresence>
        {lastBlob && !generating && (
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
            className="card"
            style={{ padding: '22px', display: 'flex', flexDirection: 'column', gap: '14px' }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <span
                  style={{
                    width: '30px',
                    height: '30px',
                    borderRadius: '9px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: 'linear-gradient(135deg, rgba(123,97,255,0.22), rgba(255,77,210,0.2))',
                    border: '1px solid rgba(123,97,255,0.3)',
                    color: 'var(--color-accent)',
                  }}
                >
                  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="4.5" cy="12.5" r="2" />
                    <circle cx="12.5" cy="10.5" r="2" />
                    <path d="M6.5 12.5V4l8-2.2V10.5" />
                  </svg>
                </span>
                <div>
                  <div
                    style={{
                      fontSize: '14.5px',
                      fontWeight: 600,
                      fontFamily: 'var(--font-display)',
                      letterSpacing: 0,
                      color: 'var(--color-text)',
                    }}
                  >
                    Generated music
                  </div>
                  {elapsed > 0 && (
                    <div
                      style={{
                        fontSize: '11px',
                        fontFamily: 'var(--font-mono)',
                        color: 'var(--color-text-dim)',
                        letterSpacing: 0,
                        marginTop: '2px',
                      }}
                    >
                      Rendered in {formatTime(elapsed)}
                    </div>
                  )}
                </div>
              </div>
              <motion.button
                whileHover={{ y: -1 }}
                whileTap={{ scale: 0.97 }}
                onClick={handleDownload}
                style={{
                  padding: '8px 14px',
                  borderRadius: '9px',
                  background: 'rgba(123,97,255,0.1)',
                  border: '1px solid rgba(123,97,255,0.28)',
                  color: 'var(--color-accent)',
                  fontSize: '12.5px',
                  fontWeight: 500,
                  cursor: 'pointer',
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '7px',
                  transition: 'background 0.18s',
                }}
              >
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M8 2v9M4.5 7.5L8 11l3.5-3.5M3 14h10" />
                </svg>
                Download WAV
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
