import { useState, useEffect, useRef, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { getVoices, generate, generateStream, getLanguages } from '../api';
import type { StreamChunk, StreamDone } from '../api';
import { useAudio } from '../hooks/useAudio';
import AudioPlayer from '../components/AudioPlayer';
import WaveformBars from '../components/WaveformBars';
import type { Voice, GenerationMeta } from '../types';
import { getRandomSample } from '../sampleTexts';

export default function SpeechPage() {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [languages, setLanguages] = useState<string[]>([]);
  const [selectedVoice, setSelectedVoice] = useState('');
  const [selectedLang, setSelectedLang] = useState('');
  const [selectedModel] = useState('1.7b-base');
  const [text, setText] = useState('');
  const [generating, setGenerating] = useState(false);
  const [genStartTime, setGenStartTime] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState('');
  const [meta, setMeta] = useState<GenerationMeta | null>(null);
  const [lastBlob, setLastBlob] = useState<Blob | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [streamChunks, setStreamChunks] = useState<{ index: number; total: number; duration: number }[]>([]);
  const [streamTotal, setStreamTotal] = useState(0);
  const audioChunksRef = useRef<Blob[]>([]);
  const audio = useAudio();

  useEffect(() => {
    if (!generating) return;
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - genStartTime) / 1000)), 1000);
    return () => clearInterval(id);
  }, [generating, genStartTime]);

  useEffect(() => {
    getVoices().then((v) => {
      setVoices(v);
      if (v.length > 0 && !selectedVoice) {
        const def = v.find((x) => x.id === 'default');
        setSelectedVoice(def ? def.id : v[0].id);
      }
    }).catch(() => {});
    getLanguages().then(setLanguages).catch(() => setLanguages(['English', 'Chinese', 'German']));
  }, []);

  const base64ToBlob = useCallback((b64: string): Blob => {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: 'audio/wav' });
  }, []);

  const handleGenerate = async () => {
    if (!text.trim() || !selectedVoice) return;
    if (streaming) return handleGenerateStream();

    setGenerating(true);
    setGenStartTime(Date.now());
    setElapsed(0);
    setError('');
    setMeta(null);
    audio.stop();

    try {
      const result = await generate({
        text: text.trim(),
        voice_id: selectedVoice,
        model_id: selectedModel,
        language: selectedLang || undefined,
      });
      setMeta(result.meta);
      setLastBlob(result.blob);
      audio.play(result.blob, 'speech-result');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Generation failed');
    } finally {
      setGenerating(false);
    }
  };

  const handleGenerateStream = async () => {
    setGenerating(true);
    setGenStartTime(Date.now());
    setElapsed(0);
    setError('');
    setMeta(null);
    setStreamChunks([]);
    setStreamTotal(0);
    audioChunksRef.current = [];
    audio.stop();

    let firstChunkPlayed = false;

    await generateStream(
      {
        text: text.trim(),
        voice_id: selectedVoice,
        model_id: selectedModel,
        language: selectedLang || undefined,
      },
      (chunk: StreamChunk) => {
        const blob = base64ToBlob(chunk.audio);
        audioChunksRef.current.push(blob);
        setStreamChunks((prev) => [...prev, { index: chunk.index, total: chunk.total, duration: chunk.duration }]);
        setStreamTotal(chunk.total);
        if (!firstChunkPlayed) {
          firstChunkPlayed = true;
          audio.play(blob, 'speech-result');
        }
      },
      (done: StreamDone) => {
        const combined = new Blob(audioChunksRef.current, { type: 'audio/wav' });
        setLastBlob(combined);
        setMeta({
          audioDuration: String(done.total_duration),
          generationTime: String(done.generation_time),
          rtf: String(done.rtf),
          model: done.model,
          voice: done.voice,
        });
        audio.play(combined, 'speech-result');
        setGenerating(false);
      },
      (errMsg: string) => {
        setError(errMsg);
        setGenerating(false);
      },
    );
  };

  const handleSampleText = () => {
    setText(getRandomSample(selectedLang || 'English'));
  };

  const canGenerate = text.trim() && selectedVoice && !generating;
  const selectedVoiceObj = voices.find((v) => v.id === selectedVoice);

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
      style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}
    >
      {/* Composer card */}
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ position: 'relative' }}>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) handleGenerate(); }}
            placeholder="Write or paste what you want your voice to say…"
            style={{
              display: 'block',
              width: '100%',
              minHeight: '220px',
              padding: '22px 24px 52px',
              border: 'none',
              background: 'transparent',
              color: 'var(--color-text)',
              fontSize: '16px',
              lineHeight: 1.6,
              fontFamily: 'var(--font-body)',
              resize: 'vertical',
              outline: 'none',
              boxSizing: 'border-box',
            }}
          />
          <div
            style={{
              position: 'absolute',
              bottom: '14px',
              left: '24px',
              right: '24px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              pointerEvents: 'none',
            }}
          >
            <button
              onClick={handleSampleText}
              className="btn-ghost"
              style={{
                pointerEvents: 'auto',
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                padding: '4px 10px',
                borderRadius: '999px',
                fontSize: '11.5px',
                color: 'var(--color-text-dim)',
                border: '1px solid rgba(255,255,255,0.06)',
              }}
            >
              <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M6 1v10M1 6h10" />
              </svg>
              Sample text
            </button>
            <span
              style={{
                fontSize: '11px',
                fontVariantNumeric: 'tabular-nums',
                fontFamily: 'var(--font-mono)',
                color: 'var(--color-text-dim)',
                letterSpacing: '0.03em',
              }}
            >
              {text.length.toLocaleString()} chars
            </span>
          </div>
        </div>
      </div>

      {/* Control bar */}
      <div
        className="glass"
        style={{
          display: 'grid',
          gridTemplateColumns: '1.4fr 1fr auto auto',
          gap: '14px',
          alignItems: 'end',
          padding: '16px 18px',
          borderRadius: '18px',
        }}
      >
        <Field label="Voice">
          <select
            value={selectedVoice}
            onChange={(e) => setSelectedVoice(e.target.value)}
            className="input-field"
            style={selectStyle}
          >
            {voices.length === 0 && <option value="">No voices available</option>}
            {voices.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name} · {v.source === 'design' ? 'Designed' : 'Cloned'}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Language">
          <select
            value={selectedLang}
            onChange={(e) => setSelectedLang(e.target.value)}
            className="input-field"
            style={selectStyle}
          >
            <option value="">Auto detect</option>
            {languages.map((l) => (<option key={l} value={l}>{l}</option>))}
          </select>
        </Field>

        <Field label="Stream">
          <button
            type="button"
            onClick={() => setStreaming(!streaming)}
            aria-pressed={streaming}
            title="Stream long texts chunk by chunk for faster first audio"
            style={{
              height: '42px',
              padding: '0 14px',
              borderRadius: '10px',
              border: `1px solid ${streaming ? 'rgba(123,97,255,0.4)' : 'rgba(255,255,255,0.08)'}`,
              background: streaming ? 'var(--color-accent-dim)' : 'rgba(255,255,255,0.03)',
              color: streaming ? 'var(--color-accent-hover)' : 'var(--color-text-secondary)',
              fontSize: '12.5px',
              fontWeight: 500,
              display: 'inline-flex',
              alignItems: 'center',
              gap: '8px',
              whiteSpace: 'nowrap',
              transition: 'all 0.18s ease',
            }}
          >
            <span
              className={streaming ? 'status-dot status-dot-online' : 'status-dot'}
              style={streaming ? undefined : { background: 'var(--color-text-dim)' }}
            />
            {streaming ? 'On' : 'Off'}
          </button>
        </Field>

        <motion.button
          whileTap={{ scale: 0.97 }}
          onClick={handleGenerate}
          disabled={!canGenerate}
          className="btn btn-primary"
          style={{
            height: '42px',
            padding: '0 28px',
            fontSize: '13.5px',
            fontWeight: 600,
            marginTop: '22px',
          }}
        >
          {generating ? (
            <>
              <svg style={{ width: 15, height: 15, animation: 'spin 0.9s linear infinite' }} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <circle cx="12" cy="12" r="10" strokeDasharray="60" strokeDashoffset="20" strokeLinecap="round" opacity="0.8" />
              </svg>
              Synthesizing
            </>
          ) : (
            <>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                <path d="M3.5 1.5v13l10-6.5z" />
              </svg>
              Generate
            </>
          )}
        </motion.button>
      </div>

      {/* Error */}
      <AnimatePresence>
        {error && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            style={{
              padding: '12px 16px',
              borderRadius: '12px',
              background: 'var(--color-danger-dim)',
              border: '1px solid rgba(255, 90, 101, 0.3)',
              color: 'var(--color-danger)',
              fontSize: '13.5px',
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
            }}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <circle cx="8" cy="8" r="6.5" />
              <path d="M8 5v3.5M8 10.5v.5" />
            </svg>
            {error}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Result */}
      <AnimatePresence>
        {(audio.playing || audio.currentId === 'speech-result') && !generating && (
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
          >
            <div className="card" style={{ padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                <span className="label-eyebrow" style={{ fontSize: '10px' }}>Result</span>
                <div style={{ flex: 1, height: '1px', background: 'rgba(255,255,255,0.06)' }} />
                {lastBlob && (
                  <button
                    onClick={() => {
                      const url = URL.createObjectURL(lastBlob);
                      const a = document.createElement('a');
                      a.href = url;
                      a.download = 'vocarium-output.wav';
                      a.click();
                      URL.revokeObjectURL(url);
                    }}
                    className="btn btn-ghost"
                    style={{ padding: '6px 10px', fontSize: '12px', display: 'inline-flex', alignItems: 'center', gap: '6px' }}
                    title="Download WAV"
                  >
                    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                      <path d="M8 2v9M4 8l4 4 4-4M2 14h12" />
                    </svg>
                    Download
                  </button>
                )}
              </div>

              <AudioPlayer
                playing={audio.playing}
                progress={audio.progress}
                duration={audio.duration}
                onToggle={audio.toggle}
                onSeek={audio.seek}
              />

              {meta && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px' }}>
                  <MetaChip label="Audio" value={`${parseFloat(meta.audioDuration || '0').toFixed(1)}s`} />
                  <MetaChip label="Generation" value={`${parseFloat(meta.generationTime || '0').toFixed(1)}s`} />
                  <MetaChip label="RTF" value={`${parseFloat(meta.rtf || '0').toFixed(2)}×`} emphasis />
                  {meta.model && <MetaChip label="Model" value={meta.model} />}
                  {selectedVoiceObj && <MetaChip label="Voice" value={selectedVoiceObj.name} />}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Generating state */}
      <AnimatePresence>
        {generating && (
          <motion.div
            initial={{ opacity: 0, scale: 0.97 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.97 }}
            transition={{ duration: 0.24 }}
          >
            <div
              className="glass-strong"
              style={{
                padding: '36px',
                borderRadius: '20px',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: '18px',
                position: 'relative',
                overflow: 'hidden',
              }}
            >
              <div
                aria-hidden
                style={{
                  position: 'absolute',
                  inset: 0,
                  background:
                    'radial-gradient(ellipse 60% 60% at 50% 20%, rgba(123,97,255,0.22), transparent 70%)',
                  pointerEvents: 'none',
                }}
              />
              <WaveformBars active size="lg" bars={22} color="accent" />
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '6px' }}>
                <p
                  style={{
                    fontSize: '15px',
                    fontFamily: 'var(--font-display)',
                    fontWeight: 600,
                    letterSpacing: '-0.015em',
                    color: 'var(--color-text)',
                  }}
                >
                  Synthesizing speech
                </p>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '14px',
                    fontSize: '12px',
                    color: 'var(--color-text-dim)',
                    fontFamily: 'var(--font-mono)',
                  }}
                >
                  <span>
                    {Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, '0')}
                  </span>
                  {streamTotal > 1 && (
                    <>
                      <span style={{ opacity: 0.4 }}>·</span>
                      <span>
                        chunk {streamChunks.length}/{streamTotal}
                      </span>
                    </>
                  )}
                  {streamTotal === 0 && text.length > 200 && (
                    <>
                      <span style={{ opacity: 0.4 }}>·</span>
                      <span>~{Math.ceil(text.length / 200)} parts</span>
                    </>
                  )}
                </div>
              </div>
              {streamTotal > 1 && (
                <div
                  style={{
                    width: '240px',
                    height: '3px',
                    borderRadius: '999px',
                    background: 'rgba(255,255,255,0.08)',
                    overflow: 'hidden',
                  }}
                >
                  <motion.div
                    animate={{ width: `${(streamChunks.length / streamTotal) * 100}%` }}
                    transition={{ duration: 0.3, ease: 'easeOut' }}
                    style={{
                      height: '100%',
                      borderRadius: '999px',
                      background: 'linear-gradient(90deg, var(--color-accent), var(--color-aurora-2))',
                      boxShadow: '0 0 12px rgba(123,97,255,0.6)',
                    }}
                  />
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Empty state hints */}
      {!meta && !generating && !error && !audio.playing && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '14px' }}>
          <Hint
            step="01"
            title="Pick a voice"
            desc="Select from your cloned or designed voices"
            icon={
              <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <circle cx="10" cy="6" r="3" />
                <path d="M3 18v-1a5 5 0 015-5h4a5 5 0 015 5v1" />
              </svg>
            }
          />
          <Hint
            step="02"
            title="Write the text"
            desc="Type, paste, or use a sample snippet"
            icon={
              <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M4 5h12M4 9h8M4 13h10M4 17h6" />
              </svg>
            }
          />
          <Hint
            step="03"
            title="Synthesize"
            desc="Generate or stream — Ctrl+Enter shortcut"
            icon={
              <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <polygon points="5,3 19,10 5,17" />
              </svg>
            }
          />
        </div>
      )}
    </motion.div>
  );
}

const selectStyle: React.CSSProperties = {
  height: '42px',
  padding: '0 12px',
  fontSize: '13.5px',
  cursor: 'pointer',
  appearance: 'none',
  WebkitAppearance: 'none',
  backgroundImage:
    "url(\"data:image/svg+xml;charset=UTF-8,%3csvg width='10' height='6' viewBox='0 0 10 6' xmlns='http://www.w3.org/2000/svg'%3e%3cpath d='M1 1l4 4 4-4' stroke='white' stroke-opacity='0.5' stroke-width='1.4' stroke-linecap='round' stroke-linejoin='round' fill='none'/%3e%3c/svg%3e\")",
  backgroundRepeat: 'no-repeat',
  backgroundPosition: 'right 12px center',
  paddingRight: '30px',
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', minWidth: 0 }}>
      <span className="label-eyebrow" style={{ fontSize: '10px' }}>
        {label}
      </span>
      {children}
    </div>
  );
}

function MetaChip({ label, value, emphasis }: { label: string; value: string; emphasis?: boolean }) {
  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '8px',
        padding: '5px 11px',
        borderRadius: '999px',
        background: emphasis ? 'var(--color-accent-dim)' : 'rgba(255,255,255,0.04)',
        border: `1px solid ${emphasis ? 'rgba(123,97,255,0.28)' : 'rgba(255,255,255,0.06)'}`,
        fontSize: '11px',
      }}
    >
      <span
        style={{
          color: emphasis ? 'var(--color-accent-hover)' : 'var(--color-text-dim)',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
          fontWeight: 500,
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          color: emphasis ? 'var(--color-text)' : 'var(--color-text-secondary)',
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {value}
      </span>
    </div>
  );
}

function Hint({ step, title, desc, icon }: { step: string; title: string; desc: string; icon: React.ReactNode }) {
  return (
    <div
      className="card-subtle"
      style={{ padding: '18px', display: 'flex', flexDirection: 'column', gap: '12px' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div
          style={{
            width: '34px',
            height: '34px',
            borderRadius: '10px',
            background: 'var(--color-accent-dim)',
            border: '1px solid rgba(123,97,255,0.22)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--color-accent-hover)',
          }}
        >
          {icon}
        </div>
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '10px',
            color: 'var(--color-text-faint)',
            letterSpacing: '0.1em',
          }}
        >
          {step}
        </span>
      </div>
      <div>
        <div
          style={{
            fontSize: '14px',
            fontWeight: 500,
            color: 'var(--color-text)',
            letterSpacing: '-0.01em',
            marginBottom: '4px',
          }}
        >
          {title}
        </div>
        <div style={{ fontSize: '12.5px', color: 'var(--color-text-dim)', lineHeight: 1.45 }}>
          {desc}
        </div>
      </div>
    </div>
  );
}
