import {useDraftField} from '../state/EditorDrafts';
import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { getVoices, generate, generateStream, getLanguages, getSegmentTags } from '../api';
import type { StreamChunk, StreamDone, SegmentTag } from '../api';
import {useSpeechAudio} from '../hooks/useSpeechAudio';
import {pause as pauseSharedAudio} from '../state/player';
import AudioPlayer from '../components/AudioPlayer';
import WaveformBars from '../components/WaveformBars';
import type { Voice, GenerationMeta, TtsEngine } from '../types';
import { getRandomSample } from '../sampleTexts';
import { DEFAULT_VOICE, isKikiriVoice, withDefaultVoice } from '../voiceUtils';
import VoicePicker from '../components/VoicePicker';

/** Serverseitiges Limit aus `MAX_TTS_TEXT_CHARS` in vocarium-api. */
const MAX_CHARS = 20000;
/** Grobe Sprechrate für die Längenschätzung (Zeichen pro Sekunde, deutsch). */
const CHARS_PER_SECOND = 14;
const HISTORY_LIMIT = 12;

interface Clip {
  id: string;
  blob: Blob;
  text: string;
  voiceId: string;
  voiceName: string;
  engine: string;
  streamed: boolean;
  meta: GenerationMeta | null;
  createdAt: number;
}

const ENGINE_LABEL: Record<string, string> = {
  kikiri: 'Kikiri · CPU',
  omnivoice: 'OmniVoice · GPU',
  vibevoice: 'VibeVoice (stillgelegt)',
  qwen: 'keine Engine',
};

export default function SpeechPage({visible=true}:{visible?:boolean}) {
  const visibleRef=useRef(visible);
  visibleRef.current=visible;
  const [voices, setVoices] = useState<Voice[]>([]);
  const [languages, setLanguages] = useState<string[]>([]);
  const [selectedVoice, setSelectedVoice] = useState('default');
  const [selectedLang, setSelectedLang] = useState('');
  const [engine, setEngine] = useState<TtsEngine>('auto');
  const [text, setText] = useDraftField('speech:text', '');
  const [generating, setGenerating] = useState(false);
  const [genStartTime, setGenStartTime] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamChunks, setStreamChunks] = useState<{ index: number; total: number; duration: number }[]>([]);
  const [streamTotal, setStreamTotal] = useState(0);
  const [history, setHistory] = useState<Clip[]>([]);
  const [activeClipId, setActiveClipId] = useState<string | null>(null);
  const [tags, setTags] = useState<SegmentTag[]>([]);
  const [tagsOpen, setTagsOpen] = useState(false);
  const audioChunksRef = useRef<Uint8Array[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Streaming playback: an <audio> element cannot append arriving chunks, so
  // streamed chunks are decoded and scheduled back-to-back on a Web Audio
  // timeline instead. The cursor is the end time of the last scheduled chunk.
  const streamCtxRef = useRef<AudioContext | null>(null);
  const streamCursorRef = useRef(0);
  const streamDecodeChainRef = useRef<Promise<void>>(Promise.resolve());

  const stopStreamPlayback = useCallback(() => {
    if (streamCtxRef.current) {
      void streamCtxRef.current.close().catch(() => undefined);
      streamCtxRef.current = null;
    }
    streamCursorRef.current = 0;
    streamDecodeChainRef.current = Promise.resolve();
  }, []);

  useEffect(() => stopStreamPlayback, [stopStreamPlayback]);
  useEffect(()=>{if(!visible) stopStreamPlayback();},[visible,stopStreamPlayback]);

  const enqueueStreamChunk = useCallback((bytes: Uint8Array) => {
    if(!visibleRef.current) return;
    let ctx = streamCtxRef.current;
    if (!ctx) {
      ctx = new AudioContext();
      streamCtxRef.current = ctx;
      streamCursorRef.current = 0;
    }
    const context = ctx;
    // decodeAudioData resolves out of order; the chain keeps scheduling order.
    streamDecodeChainRef.current = streamDecodeChainRef.current
      .then(async () => {
        if (streamCtxRef.current !== context) return;
        const buffer = await context.decodeAudioData(bytes.slice().buffer);
        if (streamCtxRef.current !== context) return;
        const source = context.createBufferSource();
        source.buffer = buffer;
        source.connect(context.destination);
        const startAt = Math.max(context.currentTime + 0.05, streamCursorRef.current);
        source.start(startAt);
        streamCursorRef.current = startAt + buffer.duration;
      })
      .catch(() => undefined);
  }, []);

  const audio = useSpeechAudio();
  const generationVoices = useMemo(() => withDefaultVoice(voices), [voices]);
  const activeVoice = useMemo(
    () => generationVoices.find((voice) => voice.id === selectedVoice),
    [generationVoices, selectedVoice],
  );
  // "Auto" folgt der Stimme: OmniVoice ist die Hauptengine auf GPU 0, Kikiri
  // der CPU-Fallback. Die eingebaute „default“-Stimme gehört zu keiner Engine
  // und wird von der API an OmniVoice geschickt — das spiegeln wir hier, statt
  // das stillgelegte Qwen zu nennen. VibeVoice ist ebenfalls stillgelegt: eine
  // Stimme, die dorthin auflöst, ist eine Altlast und lässt sich nicht rendern.
  const voiceEngine: 'kikiri' | 'vibevoice' | 'omnivoice' | 'qwen' = activeVoice
    ? activeVoice.id === DEFAULT_VOICE.id
      ? 'omnivoice'
      : isKikiriVoice(activeVoice)
        ? 'kikiri'
        : activeVoice.source === 'vibevoice'
          ? 'vibevoice'
          : activeVoice.source === 'omnivoice'
            ? 'omnivoice'
            : 'qwen'
    : 'omnivoice';
  const resolvedEngine = engine === 'auto' ? voiceEngine : engine;
  const engineMismatch =
    engine !== 'auto' && engine !== 'qwen' && activeVoice != null && voiceEngine !== engine;
  // Nonverbale Tags rendert nur OmniVoice als echten Laut; Kikiri würde sie
  // buchstabieren, deshalb blendet die Palette dort aus.
  const tagsSupported = resolvedEngine === 'omnivoice';

  const chars = text.length;
  const overLimit = chars > MAX_CHARS;
  const trimmed = text.trim();
  const estimatedSeconds = Math.round(trimmed.length / CHARS_PER_SECOND);
  // Spiegelt `split_sentences()` in omnivoice-api/server.py: Satzgrenzen,
  // kurze Sätze wandern zum Vorgänger. Nur beim Streaming relevant.
  const estimatedParts = useMemo(() => {
    if (!trimmed) return 0;
    const parts = trimmed.split(/(?<=[.!?…:])\s+/).filter(Boolean);
    const merged: string[] = [];
    for (const part of parts) {
      if (merged.length > 1 && (merged[merged.length - 1].length < 60 || part.length < 30)) {
        merged[merged.length - 1] = `${merged[merged.length - 1]} ${part}`;
      } else {
        merged.push(part);
      }
    }
    return Math.max(merged.length, 1);
  }, [trimmed]);

  useEffect(() => {
    if (!generating) return;
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - genStartTime) / 1000)), 1000);
    return () => clearInterval(id);
  }, [generating, genStartTime]);

  useEffect(() => {
    getVoices().then(setVoices).catch(() => {});
    getLanguages().then(setLanguages).catch(() => setLanguages(['English', 'Chinese', 'German']));
    getSegmentTags().then(setTags).catch(() => setTags([]));
  }, []);

  const base64ToBytes = useCallback((b64: string): Uint8Array => {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }, []);

  const combineWavChunks = useCallback((chunks: Uint8Array[]): Blob => {
    if (chunks.length === 0) return new Blob([], { type: 'audio/wav' });
    if (chunks.length === 1) return new Blob([chunks[0]], { type: 'audio/wav' });

    const text = (bytes: Uint8Array, start: number, len: number) =>
      String.fromCharCode(...bytes.slice(start, start + len));
    const writeText = (view: DataView, offset: number, value: string) => {
      for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
    };

    const parse = (bytes: Uint8Array) => {
      if (text(bytes, 0, 4) !== 'RIFF' || text(bytes, 8, 4) !== 'WAVE') {
        throw new Error('Invalid WAV chunk');
      }
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      let offset = 12;
      let fmt: { format: number; channels: number; sampleRate: number; byteRate: number; blockAlign: number; bitsPerSample: number } | null = null;
      let data: Uint8Array | null = null;
      while (offset + 8 <= bytes.byteLength) {
        const id = text(bytes, offset, 4);
        const size = view.getUint32(offset + 4, true);
        const start = offset + 8;
        if (id === 'fmt ') {
          fmt = {
            format: view.getUint16(start, true),
            channels: view.getUint16(start + 2, true),
            sampleRate: view.getUint32(start + 4, true),
            byteRate: view.getUint32(start + 8, true),
            blockAlign: view.getUint16(start + 12, true),
            bitsPerSample: view.getUint16(start + 14, true),
          };
        } else if (id === 'data') {
          data = bytes.slice(start, start + size);
        }
        offset = start + size + (size % 2);
      }
      if (!fmt || !data) throw new Error('Incomplete WAV chunk');
      return { fmt, data };
    };

    const parsed = chunks.map(parse);
    const first = parsed[0].fmt;
    for (const item of parsed.slice(1)) {
      if (
        item.fmt.format !== first.format ||
        item.fmt.channels !== first.channels ||
        item.fmt.sampleRate !== first.sampleRate ||
        item.fmt.bitsPerSample !== first.bitsPerSample
      ) {
        throw new Error('Streaming WAV chunks use incompatible formats');
      }
    }

    const dataSize = parsed.reduce((sum, item) => sum + item.data.byteLength, 0);
    const output = new Uint8Array(44 + dataSize);
    const view = new DataView(output.buffer);
    writeText(view, 0, 'RIFF');
    view.setUint32(4, 36 + dataSize, true);
    writeText(view, 8, 'WAVE');
    writeText(view, 12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, first.format, true);
    view.setUint16(22, first.channels, true);
    view.setUint32(24, first.sampleRate, true);
    view.setUint32(28, first.byteRate, true);
    view.setUint16(32, first.blockAlign, true);
    view.setUint16(34, first.bitsPerSample, true);
    writeText(view, 36, 'data');
    view.setUint32(40, dataSize, true);

    let cursor = 44;
    for (const item of parsed) {
      output.set(item.data, cursor);
      cursor += item.data.byteLength;
    }
    return new Blob([output], { type: 'audio/wav' });
  }, []);

  const pushClip = useCallback(
    (clip: Omit<Clip, 'id' | 'createdAt'>): Clip => {
      const full: Clip = { ...clip, id: `clip-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, createdAt: Date.now() };
      setHistory((prev) => [full, ...prev].slice(0, HISTORY_LIMIT));
      setActiveClipId(full.id);
      return full;
    },
    [],
  );

  const handleGenerate = async () => {
    if (!trimmed || !selectedVoice || generating || overLimit) return;
    if (streaming) return handleGenerateStream();

    setGenerating(true);
    setGenStartTime(Date.now());
    setElapsed(0);
    setError('');
    audio.stop();
    stopStreamPlayback();

    try {
      const result = await generate({
        text: trimmed,
        voice_id: selectedVoice,
        engine: engine === 'auto' ? undefined : engine,
        language: selectedLang || undefined,
      });
      const clip = pushClip({
        blob: result.blob,
        text: trimmed,
        voiceId: selectedVoice,
        voiceName: activeVoice?.name || selectedVoice,
        engine: resolvedEngine,
        streamed: false,
        meta: result.meta,
      });
      if(visibleRef.current) audio.play(result.blob, clip.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Generierung fehlgeschlagen');
    } finally {
      setGenerating(false);
    }
  };

  const handleGenerateStream = async () => {
    pauseSharedAudio();
    setGenerating(true);
    setGenStartTime(Date.now());
    setElapsed(0);
    setError('');
    setStreamChunks([]);
    setStreamTotal(0);
    audioChunksRef.current = [];
    audio.stop();
    stopStreamPlayback();

    try {
      await generateStream(
        {
          text: trimmed,
          voice_id: selectedVoice,
          engine: engine === 'auto' ? undefined : engine,
          language: selectedLang || undefined,
        },
        (chunk: StreamChunk) => {
          const bytes = base64ToBytes(chunk.audio);
          audioChunksRef.current.push(bytes);
          setStreamChunks((prev) => [...prev, { index: chunk.index, total: chunk.total, duration: chunk.duration }]);
          setStreamTotal(chunk.total);
          enqueueStreamChunk(bytes);
        },
        (done: StreamDone) => {
          try {
            // Nicht automatisch abspielen: die Chunks laufen bereits über die
            // Web-Audio-Zeitachse, ein zweiter Player würde sich überlagern.
            // Der Clip landet in der Historie und ist dort erneut abspielbar.
            const combined = combineWavChunks(audioChunksRef.current);
            pushClip({
              blob: combined,
              text: trimmed,
              voiceId: selectedVoice,
              voiceName: activeVoice?.name || selectedVoice,
              engine: resolvedEngine,
              streamed: true,
              meta: {
                audioDuration: String(done.total_duration),
                generationTime: String(done.generation_time),
                rtf: String(done.rtf),
                model: done.model,
                voice: done.voice,
              },
            });
          } catch (err) {
            setError(err instanceof Error ? err.message : 'Stream-Audio konnte nicht zusammengefügt werden');
          }
          setGenerating(false);
        },
        (errMsg: string) => {
          setError(errMsg);
          setGenerating(false);
        },
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Stream fehlgeschlagen');
      setGenerating(false);
    }
  };

  const handleSampleText = () => {
    setText(getRandomSample(selectedLang || 'German'));
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  /** Tag an der Cursorposition einsetzen — die Wirkung hängt an der Stelle im Satz. */
  const insertTag = (tag: string) => {
    const el = textareaRef.current;
    const token = `[${tag}]`;
    if (!el) {
      setText((t) => (t ? `${t} ${token}` : token));
      return;
    }
    const start = el.selectionStart ?? text.length;
    const end = el.selectionEnd ?? start;
    const before = text.slice(0, start);
    const after = text.slice(end);
    const padBefore = before && !/\s$/.test(before) ? ' ' : '';
    const padAfter = after && !/^\s/.test(after) ? ' ' : '';
    setText(`${before}${padBefore}${token}${padAfter}${after}`);
    requestAnimationFrame(() => {
      const pos = before.length + padBefore.length + token.length;
      el.focus();
      el.setSelectionRange(pos, pos);
    });
  };

  const activeClip = history.find((c) => c.id === activeClipId) || null;
  const clipIsLoaded = activeClip != null && audio.currentId === activeClip.id;
  const canGenerate = Boolean(trimmed) && Boolean(selectedVoice) && !generating && !overLimit;

  const playClip = (clip: Clip) => {
    stopStreamPlayback();
    setActiveClipId(clip.id);
    if (audio.currentId === clip.id) audio.toggle();
    else audio.play(clip.blob, clip.id);
  };

  const downloadClip = (clip: Clip) => {
    const url = URL.createObjectURL(clip.blob);
    const a = document.createElement('a');
    const stamp = new Date(clip.createdAt).toISOString().replace(/[:.]/g, '-').slice(0, 19);
    a.href = url;
    a.download = `vocarium-${clip.voiceName.replace(/[^\w-]+/g, '_')}-${stamp}.wav`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const removeClip = (clip: Clip) => {
    if (audio.currentId === clip.id) audio.stop();
    setHistory((prev) => prev.filter((c) => c.id !== clip.id));
    setActiveClipId((id) => (id === clip.id ? null : id));
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
      style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}
    >
      <header className="speech-header">
        <div>
          <h1 style={{ fontSize: '26px', marginBottom: '4px' }}>Speech</h1>
          <p style={{ fontSize: '13.5px', color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
            Text zu Sprache — Stimme wählen, schreiben, rendern. Ergebnisse bleiben in dieser Sitzung abrufbar.
          </p>
        </div>
        <div className="speech-route-pill" title="Diese Engine würde jetzt rendern">
          <span
            className={resolvedEngine === 'omnivoice' || resolvedEngine === 'kikiri' ? 'status-dot status-dot-online' : 'status-dot'}
            style={resolvedEngine === 'omnivoice' || resolvedEngine === 'kikiri' ? undefined : { background: 'var(--color-warning)' }}
          />
          <span>{ENGINE_LABEL[resolvedEngine] || resolvedEngine}</span>
        </div>
      </header>

      <div className="speech-layout">
        {/* ── Komposition ─────────────────────────────────────────────── */}
        <div className="speech-main">
          <div className="card speech-composer">
            <div className="speech-composer-toolbar">
              <span className="label-eyebrow" style={{ fontSize: '10px' }}>Text</span>
              <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
                {tagsSupported && tags.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setTagsOpen((o) => !o)}
                    aria-pressed={tagsOpen}
                    className="speech-chip-btn"
                    title="Nonverbale Laute einfügen — nur OmniVoice rendert sie"
                  >
                    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                      <path d="M3.5 2v8M8.5 2v8" />
                    </svg>
                    Laute
                  </button>
                )}
                <button type="button" onClick={handleSampleText} className="speech-chip-btn">
                  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                    <path d="M6 1v10M1 6h10" />
                  </svg>
                  Beispiel
                </button>
                <button
                  type="button"
                  onClick={() => { setText(''); textareaRef.current?.focus(); }}
                  disabled={!text}
                  className="speech-chip-btn"
                >
                  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                    <path d="M2 2l8 8M10 2l-8 8" />
                  </svg>
                  Leeren
                </button>
              </div>
            </div>

            <AnimatePresence initial={false}>
              {tagsOpen && tagsSupported && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.18, ease: [0.25, 0.46, 0.45, 0.94] }}
                  style={{ overflow: 'hidden' }}
                >
                  <div className="speech-tag-palette">
                    {tags.map((tag) => (
                      <button
                        key={tag.id}
                        type="button"
                        onClick={() => insertTag(tag.id)}
                        className="speech-tag-chip"
                        title={tag.hint}
                      >
                        {tag.label}
                      </button>
                    ))}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            <textarea
              ref={textareaRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) handleGenerate(); }}
              placeholder="Schreib oder füge ein, was die Stimme sagen soll…"
              className="speech-textarea"
              aria-label="Text für die Sprachausgabe"
            />

            <div className="speech-composer-footer">
              <div className="speech-stats">
                <span className={overLimit ? 'speech-stat-danger' : undefined}>
                  {chars.toLocaleString('de-DE')} / {MAX_CHARS.toLocaleString('de-DE')} Zeichen
                </span>
                {trimmed && (
                  <>
                    <span className="speech-stat-sep">·</span>
                    <span title="Grobe Schätzung aus der Textlänge">≈ {formatDuration(estimatedSeconds)}</span>
                  </>
                )}
                {streaming && estimatedParts > 1 && (
                  <>
                    <span className="speech-stat-sep">·</span>
                    <span title="Streaming rendert Satz für Satz">≈ {estimatedParts} Teile</span>
                  </>
                )}
              </div>
              <div className="speech-composer-actions">
                <span className="speech-shortcut" title="Strg+Enter rendert">
                  <kbd>Strg</kbd>
                  <kbd>⏎</kbd>
                </span>
                <motion.button
                  whileTap={{ scale: 0.98 }}
                  onClick={handleGenerate}
                  disabled={!canGenerate}
                  className="btn btn-primary speech-generate-button"
                >
                  {generating ? (
                    <>
                      <svg style={{ width: 15, height: 15, animation: 'spin 0.9s linear infinite' }} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                        <circle cx="12" cy="12" r="10" strokeDasharray="60" strokeDashoffset="20" strokeLinecap="round" opacity="0.8" />
                      </svg>
                      Rendert…
                    </>
                  ) : (
                    <>
                      <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                        <path d="M3.5 1.5v13l10-6.5z" />
                      </svg>
                      {streaming ? 'Streamen' : 'Generieren'}
                    </>
                  )}
                </motion.button>
              </div>
            </div>
          </div>

          {overLimit && (
            <div className="speech-notice speech-notice-danger">
              Der Text überschreitet das Limit um {(chars - MAX_CHARS).toLocaleString('de-DE')} Zeichen.
              Kürze ihn oder teile ihn auf.
            </div>
          )}

          {/* Fehler */}
          <AnimatePresence>
            {error && (
              <motion.div
                initial={{ opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                className="speech-notice speech-notice-danger"
                style={{ display: 'flex', alignItems: 'center', gap: '10px' }}
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" style={{ flexShrink: 0 }}>
                  <circle cx="8" cy="8" r="6.5" />
                  <path d="M8 5v3.5M8 10.5v.5" />
                </svg>
                {error}
              </motion.div>
            )}
          </AnimatePresence>

          {/* Rendervorgang */}
          <AnimatePresence>
            {generating && (
              <motion.div
                initial={{ opacity: 0, scale: 0.97 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.97 }}
                transition={{ duration: 0.24 }}
              >
                <div className="glass-strong speech-progress">
                  <div
                    aria-hidden
                    style={{
                      position: 'absolute',
                      inset: 0,
                      background: 'radial-gradient(ellipse 60% 60% at 50% 20%, rgba(123,97,255,0.22), transparent 70%)',
                      pointerEvents: 'none',
                    }}
                  />
                  <WaveformBars active size="lg" bars={22} color="accent" />
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '6px' }}>
                    <p style={{ fontSize: '15px', fontFamily: 'var(--font-display)', fontWeight: 600, color: 'var(--color-text)' }}>
                      {ENGINE_LABEL[resolvedEngine]?.split(' · ')[0] || 'Engine'} synthetisiert
                    </p>
                    <div className="speech-progress-meta">
                      <span>{Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, '0')}</span>
                      {streamTotal > 1 && (
                        <>
                          <span style={{ opacity: 0.4 }}>·</span>
                          <span>Teil {streamChunks.length}/{streamTotal}</span>
                        </>
                      )}
                    </div>
                  </div>
                  {streamTotal > 1 && (
                    <div style={{ width: '240px', height: '3px', borderRadius: '999px', background: 'rgba(255,255,255,0.08)', overflow: 'hidden' }}>
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

          {/* Ergebnis */}
          <AnimatePresence>
            {activeClip && !generating && (
              <motion.div
                key={activeClip.id}
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
              >
                <div className="card speech-result" style={{ padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                    <span className="label-eyebrow" style={{ fontSize: '10px' }}>Ergebnis</span>
                    <div style={{ flex: 1, height: '1px', background: 'rgba(255,255,255,0.06)' }} />
                    <button
                      onClick={() => downloadClip(activeClip)}
                      className="btn btn-ghost"
                      style={{ padding: '6px 10px', fontSize: '12px', display: 'inline-flex', alignItems: 'center', gap: '6px' }}
                      title="WAV herunterladen"
                    >
                      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                        <path d="M8 2v9M4 8l4 4 4-4M2 14h12" />
                      </svg>
                      Download
                    </button>
                  </div>

                  <AudioPlayer
                    playing={clipIsLoaded && audio.playing}
                    progress={clipIsLoaded ? audio.progress : 0}
                    duration={clipIsLoaded ? audio.duration : parseFloat(activeClip.meta?.audioDuration || '0')}
                    onToggle={() => playClip(activeClip)}
                    onSeek={(pct) => { if (clipIsLoaded) audio.seek(pct); }}
                  />

                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px' }}>
                    <MetaChip label="Stimme" value={activeClip.voiceName} />
                    <MetaChip label="Engine" value={ENGINE_LABEL[activeClip.engine]?.split(' · ')[0] || activeClip.engine} />
                    {activeClip.meta && (
                      <>
                        <MetaChip label="Audio" value={`${parseFloat(activeClip.meta.audioDuration || '0').toFixed(1)}s`} />
                        <MetaChip label="Render" value={`${parseFloat(activeClip.meta.generationTime || '0').toFixed(1)}s`} />
                        <MetaChip label="RTF" value={`${parseFloat(activeClip.meta.rtf || '0').toFixed(2)}×`} emphasis />
                      </>
                    )}
                    {activeClip.streamed && <MetaChip label="Modus" value="Stream" />}
                  </div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Historie */}
          {history.length > 1 && (
            <div className="card" style={{ padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                <span className="label-eyebrow" style={{ fontSize: '10px' }}>Historie</span>
                <div style={{ flex: 1, height: '1px', background: 'rgba(255,255,255,0.06)' }} />
                <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                  {history.length}/{HISTORY_LIMIT}
                </span>
                <button
                  onClick={() => { audio.stop(); setHistory([]); setActiveClipId(null); }}
                  className="btn btn-ghost btn-sm"
                  style={{ fontSize: '11.5px' }}
                >
                  Leeren
                </button>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {history.map((clip) => {
                  const isPlaying = audio.currentId === clip.id && audio.playing;
                  return (
                    <div
                      key={clip.id}
                      className={`speech-history-row${clip.id === activeClipId ? ' speech-history-row-active' : ''}`}
                    >
                      <button
                        type="button"
                        onClick={() => playClip(clip)}
                        className="speech-history-play"
                        aria-label={isPlaying ? 'Pause' : 'Abspielen'}
                      >
                        {isPlaying ? (
                          <svg width="10" height="10" viewBox="0 0 12 12" fill="currentColor"><rect x="2" y="1.5" width="3" height="9" rx="1" /><rect x="7" y="1.5" width="3" height="9" rx="1" /></svg>
                        ) : (
                          <svg width="10" height="10" viewBox="0 0 12 12" fill="currentColor"><path d="M3 1.5v9l7-4.5z" /></svg>
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={() => setActiveClipId(clip.id)}
                        className="speech-history-body"
                        title={clip.text}
                      >
                        <span className="speech-history-text">{clip.text}</span>
                        <span className="speech-history-meta">
                          {clip.voiceName} · {parseFloat(clip.meta?.audioDuration || '0').toFixed(1)}s · {formatClock(clip.createdAt)}
                        </span>
                      </button>
                      <div className="speech-history-actions">
                        <button type="button" onClick={() => setText(clip.text)} title="Text übernehmen" aria-label="Text übernehmen">
                          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                            <rect x="5" y="5" width="8" height="9" rx="1.5" />
                            <path d="M11 5V3.5A1.5 1.5 0 009.5 2h-5A1.5 1.5 0 003 3.5v6A1.5 1.5 0 004.5 11H5" />
                          </svg>
                        </button>
                        <button type="button" onClick={() => downloadClip(clip)} title="Download" aria-label="Download">
                          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                            <path d="M8 2v9M4 8l4 4 4-4M2 14h12" />
                          </svg>
                        </button>
                        <button type="button" onClick={() => removeClip(clip)} title="Entfernen" aria-label="Entfernen">
                          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                            <path d="M3 3l10 10M13 3L3 13" />
                          </svg>
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* ── Einstellungen ───────────────────────────────────────────── */}
        <aside className="speech-sidebar">
          <div className="card speech-settings">
            <div className="speech-field speech-field-voice">
              <span className="speech-field-label">Stimme</span>
              <VoicePicker voices={voices} value={selectedVoice} onChange={setSelectedVoice} />
              {activeVoice?.notes && <p className="speech-field-note">{activeVoice.notes}</p>}
              {voices.length === 0 && <p className="speech-field-note">Keine Stimmen geladen — läuft der Stack?</p>}
            </div>

            <div className="speech-field">
              <span className="speech-field-label">Engine</span>
              <div className="speech-segmented" role="group" aria-label="Engine">
                {([
                  { id: 'auto', label: 'Auto' },
                  { id: 'omnivoice', label: 'OmniVoice' },
                  { id: 'kikiri', label: 'Kikiri' },
                ] as { id: TtsEngine; label: string }[]).map((opt) => (
                  <button
                    key={opt.id}
                    type="button"
                    onClick={() => setEngine(opt.id)}
                    aria-pressed={engine === opt.id}
                    className={engine === opt.id ? 'speech-segment speech-segment-active' : 'speech-segment'}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
              <p className="speech-field-note">
                {engine === 'auto'
                  ? `Folgt der Stimme — aktuell ${ENGINE_LABEL[resolvedEngine] || resolvedEngine}.`
                  : engine === 'omnivoice'
                    ? 'Klonstimmen auf der GPU.'
                    : 'CPU-Fallback ohne Klone.'}
              </p>
              {engineMismatch && (
                <p className="speech-field-note speech-field-note-warn">
                  {activeVoice?.name} gehört nicht zu dieser Engine — mit „Auto“ passt es.
                </p>
              )}
            </div>

            <div className="speech-field">
              <span className="speech-field-label">Sprache</span>
              <select
                value={selectedLang}
                onChange={(e) => setSelectedLang(e.target.value)}
                className="input-field"
                style={selectStyle}
              >
                <option value="">Automatisch erkennen</option>
                {languages.map((l) => (<option key={l} value={l}>{l}</option>))}
              </select>
            </div>

            <div className="speech-field">
              <div className="speech-field-row">
                <span className="speech-field-label">Streaming</span>
                <button
                  type="button"
                  onClick={() => setStreaming((s) => !s)}
                  aria-pressed={streaming}
                  aria-label="Streaming umschalten"
                  className={streaming ? 'speech-switch speech-switch-on' : 'speech-switch'}
                >
                  <span className="speech-switch-knob" />
                </button>
              </div>
              <p className="speech-field-note">
                {streaming
                  ? 'Spielt ab dem ersten Satz — der Gesamtclip landet danach in der Historie.'
                  : 'Rendert Satz für Satz und spielt sofort ab. Gut für lange Texte.'}
              </p>
            </div>
          </div>
        </aside>
      </div>

    </motion.div>
  );
}

function formatDuration(seconds: number): string {
  if (!seconds || !isFinite(seconds)) return '0:00';
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function formatClock(ts: number): string {
  return new Date(ts).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
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
        maxWidth: '100%',
      }}
    >
      <span
        style={{
          color: emphasis ? 'var(--color-accent-hover)' : 'var(--color-text-dim)',
          textTransform: 'uppercase',
          fontWeight: 500,
          whiteSpace: 'nowrap',
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          color: emphasis ? 'var(--color-text)' : 'var(--color-text-secondary)',
          fontVariantNumeric: 'tabular-nums',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {value}
      </span>
    </div>
  );
}
