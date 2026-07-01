import { useState, useRef, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { transcribe } from '../api';
import AudioPlayer from '../components/AudioPlayer';
import { useAudio } from '../hooks/useAudio';
import WaveformBars from '../components/WaveformBars';

type InputMode = 'file' | 'url' | 'mic';

export default function TranscribePage() {
  const audio = useAudio();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [mode, setMode] = useState<InputMode>('file');
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState('');
  const [dragging, setDragging] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const [recording, setRecording] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const handleFile = useCallback((f: File) => {
    setFile(f);
    setError(null);
    setResult(null);
    audio.play(f, 'preview');
  }, [audio]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  }, [handleFile]);

  const handleRecord = async () => {
    if (recording) {
      mediaRecorderRef.current?.stop();
      setRecording(false);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      mediaRecorderRef.current = recorder;
      chunksRef.current = [];

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: 'audio/webm' });
        const f = new File([blob], 'recording.webm', { type: 'audio/webm' });
        setMode('file');
        handleFile(f);
        stream.getTracks().forEach((t) => t.stop());
      };
      recorder.start();
      setRecording(true);
    } catch {
      setError('Microphone access denied');
    }
  };

  const handleTranscribe = async () => {
    setTranscribing(true);
    setError(null);
    setResult(null);

    try {
      let res: { text: string };
      if (mode === 'url' && url.trim()) {
        res = await transcribe({ url: url.trim() });
      } else if (file) {
        res = await transcribe({ file });
      } else {
        setError('No input provided');
        setTranscribing(false);
        return;
      }
      setResult(res.text);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Transcription failed');
    }
    setTranscribing(false);
  };

  const handleCopy = () => {
    if (!result) return;
    navigator.clipboard.writeText(result);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const canTranscribe = mode === 'url' ? url.trim().length > 0 : !!file;

  const modeButtons: { key: InputMode; label: string; icon: JSX.Element }[] = [
    {
      key: 'file',
      label: 'File Upload',
      icon: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M8 2v8M4 6l4-4 4 4" />
          <path d="M2 11v2a1 1 0 001 1h10a1 1 0 001-1v-2" />
        </svg>
      ),
    },
    {
      key: 'url',
      label: 'Media URL',
      icon: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M6.5 5l3 3-3 3" />
          <rect x="1" y="2" width="14" height="12" rx="2" />
        </svg>
      ),
    },
    {
      key: 'mic',
      label: 'Microphone',
      icon: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <rect x="5" y="1" width="6" height="9" rx="3" />
          <path d="M2 7a6 6 0 0012 0M8 13v2" />
        </svg>
      ),
    },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '22px', maxWidth: '820px' }}>
      {/* Intro strip */}
      <div
        className="card-subtle"
        style={{
          padding: '14px 18px',
          display: 'flex',
          alignItems: 'center',
          gap: '12px',
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
            background: 'rgba(123,97,255,0.08)',
            border: '1px solid rgba(123,97,255,0.22)',
            fontSize: '10.5px',
            fontFamily: 'var(--font-mono)',
            color: 'var(--color-accent)',
            textTransform: 'uppercase',
            letterSpacing: 0,
          }}
        >
          <span
            style={{
              width: '6px',
              height: '6px',
              borderRadius: '50%',
              background: 'var(--color-accent)',
              boxShadow: '0 0 8px rgba(123,97,255,0.7)',
            }}
          />
          Qwen3-ASR · lazy-load
        </div>
        <p style={{ fontSize: '12.5px', color: 'var(--color-text-secondary)', margin: 0 }}>
          Transcribe audio, video, or web media. Auto-unloads after idle to free the TTS GPU.
        </p>
      </div>

      {/* Mode selector */}
      <div style={{ display: 'flex', gap: '8px' }}>
        {modeButtons.map((m) => {
          const active = mode === m.key;
          return (
            <motion.button
              key={m.key}
              whileTap={{ scale: 0.97 }}
              onClick={() => { setMode(m.key); setError(null); }}
              style={{
                flex: 1,
                padding: '11px 12px',
                fontSize: '13px',
                fontWeight: 500,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '8px',
                borderRadius: '12px',
                cursor: 'pointer',
                transition: 'background 0.2s, border-color 0.2s, color 0.2s',
                background: active ? 'rgba(123,97,255,0.14)' : 'rgba(255,255,255,0.03)',
                border: `1px solid ${active ? 'rgba(123,97,255,0.38)' : 'rgba(255,255,255,0.08)'}`,
                color: active ? 'var(--color-text)' : 'var(--color-text-secondary)',
              }}
            >
              <span style={{ color: active ? 'var(--color-accent)' : 'var(--color-text-dim)' }}>{m.icon}</span>
              {m.label}
            </motion.button>
          );
        })}
      </div>

      {/* Input area */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {mode === 'file' && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
            <motion.div
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
              animate={{
                borderColor: dragging
                  ? 'rgba(123, 97, 255, 0.55)'
                  : file
                    ? 'rgba(123, 97, 255, 0.28)'
                    : 'rgba(255, 255, 255, 0.1)',
              }}
              transition={{ duration: 0.2 }}
              className="card-subtle"
              style={{
                cursor: 'pointer',
                padding: '36px',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                textAlign: 'center',
                borderStyle: 'dashed',
                borderWidth: '1.5px',
                minHeight: '190px',
                background: dragging
                  ? 'rgba(123,97,255,0.06)'
                  : file
                    ? 'rgba(123,97,255,0.03)'
                    : 'rgba(255,255,255,0.015)',
              }}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept="audio/*,video/*,.mp4,.mkv,.avi,.mov,.webm,.flv,.wav,.mp3,.flac,.ogg,.m4a"
                style={{ display: 'none' }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); }}
              />

              {file ? (
                <motion.div
                  initial={{ opacity: 0, scale: 0.96 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
                  style={{ display: 'flex', flexDirection: 'column', gap: '14px', width: '100%', alignItems: 'center' }}
                >
                  <WaveformBars active={audio.playing} size="md" bars={14} color="accent" />
                  <div style={{ textAlign: 'center' }}>
                    <p
                      style={{
                        fontSize: '14px',
                        fontWeight: 600,
                        fontFamily: 'var(--font-display)',
                        letterSpacing: 0,
                        color: 'var(--color-text)',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        maxWidth: '420px',
                        margin: 0,
                      }}
                    >
                      {file.name}
                    </p>
                    <p
                      style={{
                        fontSize: '11px',
                        fontFamily: 'var(--font-mono)',
                        color: 'var(--color-text-dim)',
                        marginTop: '4px',
                        letterSpacing: 0,
                      }}
                    >
                      {(file.size / (1024 * 1024)).toFixed(1)} MB
                    </p>
                  </div>
                  <div style={{ width: '100%', maxWidth: '520px' }}>
                    <AudioPlayer
                      playing={audio.playing}
                      progress={audio.progress}
                      duration={audio.duration}
                      onToggle={audio.toggle}
                      onSeek={audio.seek}
                    />
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); setFile(null); audio.stop(); setResult(null); }}
                    style={{
                      fontSize: '12px',
                      color: 'var(--color-text-dim)',
                      background: 'transparent',
                      border: 'none',
                      cursor: 'pointer',
                      transition: 'color 0.2s',
                      letterSpacing: 0,
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--color-danger)'; }}
                    onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--color-text-dim)'; }}
                  >
                    Remove file
                  </button>
                </motion.div>
              ) : (
                <>
                  <motion.div
                    style={{ marginBottom: '14px', color: dragging ? 'var(--color-accent)' : 'var(--color-text-dim)' }}
                    animate={{ y: [0, -4, 0] }}
                    transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
                  >
                    <svg width="42" height="42" viewBox="0 0 44 44" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M22 5v22M14 13l8-8 8 8" />
                      <path d="M5 28v7a4 4 0 004 4h26a4 4 0 004-4v-7" />
                    </svg>
                  </motion.div>
                  <p
                    style={{
                      fontSize: '14.5px',
                      fontWeight: 600,
                      fontFamily: 'var(--font-display)',
                      letterSpacing: 0,
                      color: 'var(--color-text)',
                      marginBottom: '6px',
                    }}
                  >
                    Drop audio or video here
                  </p>
                  <p style={{ fontSize: '12px', color: 'var(--color-text-dim)', letterSpacing: 0 }}>
                    WAV · MP3 · FLAC · MP4 · MKV · WEBM · and more
                  </p>
                </>
              )}
            </motion.div>
          </motion.div>
        )}

        {mode === 'url' && (
          <motion.div
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2 }}
            style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}
          >
            <label className="label-eyebrow" style={{ fontSize: '10.5px' }}>Media URL</label>
            <input
              type="text"
              value={url}
              onChange={(e) => { setUrl(e.target.value); setError(null); setResult(null); }}
              className="input-field"
              style={{
                padding: '14px 16px',
                fontSize: '14px',
                fontFamily: 'var(--font-mono)',
                letterSpacing: 0,
              }}
              placeholder="https://www.youtube.com/watch?v=..."
            />
            <p style={{ fontSize: '11px', color: 'var(--color-text-dim)', letterSpacing: 0 }}>
              Supports YouTube, Vimeo, and most streams via yt-dlp.
            </p>
          </motion.div>
        )}

        {mode === 'mic' && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="card-subtle"
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: '20px',
              padding: '42px 24px',
            }}
          >
            {/* Pulse halo when recording */}
            <div style={{ position: 'relative', width: '112px', height: '112px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              {recording && (
                <motion.span
                  aria-hidden
                  initial={{ opacity: 0.55, scale: 1 }}
                  animate={{ opacity: 0, scale: 1.8 }}
                  transition={{ duration: 1.6, repeat: Infinity, ease: 'easeOut' }}
                  style={{
                    position: 'absolute',
                    inset: 0,
                    borderRadius: '50%',
                    background: 'radial-gradient(circle, rgba(255,90,101,0.45), transparent 60%)',
                    pointerEvents: 'none',
                  }}
                />
              )}
              <motion.button
                whileTap={{ scale: 0.94 }}
                whileHover={{ scale: 1.04 }}
                transition={{ type: 'spring', stiffness: 300, damping: 22 }}
                onClick={handleRecord}
                aria-label={recording ? 'Stop recording' : 'Start recording'}
                style={{
                  width: '96px',
                  height: '96px',
                  borderRadius: '50%',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  cursor: 'pointer',
                  position: 'relative',
                  background: recording
                    ? 'linear-gradient(135deg, rgba(255,90,101,0.9), rgba(255,77,120,0.75))'
                    : 'linear-gradient(135deg, rgba(123,97,255,0.9), rgba(255,77,210,0.7))',
                  border: `1px solid ${recording ? 'rgba(255,120,130,0.35)' : 'rgba(255,255,255,0.2)'}`,
                  color: '#fff',
                  boxShadow: recording
                    ? '0 10px 34px rgba(255,90,101,0.4), inset 0 1px 0 rgba(255,255,255,0.2)'
                    : '0 10px 34px rgba(123,97,255,0.35), inset 0 1px 0 rgba(255,255,255,0.22)',
                }}
              >
                {recording ? (
                  <svg width="28" height="28" viewBox="0 0 32 32" fill="currentColor">
                    <rect x="9" y="9" width="14" height="14" rx="3" />
                  </svg>
                ) : (
                  <svg width="34" height="34" viewBox="0 0 36 36" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="12" y="4" width="12" height="18" rx="6" />
                    <path d="M6 16a12 12 0 0024 0M18 28v4" />
                  </svg>
                )}
              </motion.button>
            </div>
            <p
              style={{
                fontSize: '13.5px',
                color: recording ? 'var(--color-danger)' : 'var(--color-text-secondary)',
                fontFamily: 'var(--font-mono)',
                letterSpacing: 0,
                margin: 0,
              }}
            >
              {recording ? 'Recording · click to stop' : 'Click to start recording'}
            </p>
            {file && !recording && (
              <motion.div
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.25 }}
                style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '10px', width: '100%', maxWidth: '440px' }}
              >
                <WaveformBars active={audio.playing} size="sm" bars={12} color="accent" />
                <p style={{ fontSize: '12px', color: 'var(--color-accent)', fontFamily: 'var(--font-mono)', letterSpacing: 0 }}>
                  Recording ready
                </p>
                <AudioPlayer
                  playing={audio.playing}
                  progress={audio.progress}
                  duration={audio.duration}
                  onToggle={audio.toggle}
                  onSeek={audio.seek}
                />
              </motion.div>
            )}
          </motion.div>
        )}
      </div>

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

      {/* Transcribe button */}
      <motion.button
        whileTap={{ scale: 0.98 }}
        whileHover={canTranscribe && !transcribing ? { scale: 1.01 } : undefined}
        transition={{ type: 'spring', stiffness: 300, damping: 22 }}
        onClick={handleTranscribe}
        disabled={transcribing || !canTranscribe}
        className="btn btn-primary"
        style={{
          width: '100%',
          padding: '14px 0',
          fontSize: '14px',
          fontWeight: 600,
          letterSpacing: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '10px',
          opacity: transcribing || !canTranscribe ? 0.5 : 1,
          cursor: transcribing || !canTranscribe ? 'not-allowed' : 'pointer',
        }}
      >
        {transcribing ? (
          <>
            <motion.div animate={{ rotate: 360 }} transition={{ duration: 0.9, repeat: Infinity, ease: 'linear' }}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
                <path d="M8 1a7 7 0 106 3.5" />
              </svg>
            </motion.div>
            Transcribing
          </>
        ) : (
          <>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M2 4h10M2 8h8M2 12h6" />
              <circle cx="13" cy="11" r="2" />
              <path d="M14.5 12.5L16 14" />
            </svg>
            Transcribe
          </>
        )}
      </motion.button>

      {/* Result */}
      <AnimatePresence>
        {result !== null && (
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
            className="card"
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: '14px',
              padding: '22px',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <span
                  style={{
                    width: '28px',
                    height: '28px',
                    borderRadius: '8px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: 'linear-gradient(135deg, rgba(123,97,255,0.22), rgba(74,222,128,0.18))',
                    border: '1px solid rgba(123,97,255,0.25)',
                    color: 'var(--color-accent)',
                  }}
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M3 3h10v10H3z" />
                    <path d="M5.5 6h5M5.5 8.5h4M5.5 11h3" />
                  </svg>
                </span>
                <h3 className="label-eyebrow" style={{ fontSize: '10.5px' }}>
                  Transcription
                </h3>
                <span
                  style={{
                    fontSize: '10.5px',
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--color-text-dim)',
                    letterSpacing: 0,
                  }}
                >
                  {result.length} chars
                </span>
              </div>
              <motion.button
                whileTap={{ scale: 0.96 }}
                onClick={handleCopy}
                style={{
                  padding: '7px 12px',
                  fontSize: '12px',
                  fontWeight: 500,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  borderRadius: '8px',
                  cursor: 'pointer',
                  background: copied ? 'rgba(74,222,128,0.1)' : 'rgba(255,255,255,0.04)',
                  border: `1px solid ${copied ? 'rgba(74,222,128,0.32)' : 'rgba(255,255,255,0.1)'}`,
                  color: copied ? 'var(--color-success)' : 'var(--color-text-secondary)',
                  transition: 'all 0.2s',
                }}
              >
                {copied ? (
                  <>
                    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M2 6l3 3 5-5" />
                    </svg>
                    Copied
                  </>
                ) : (
                  <>
                    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="4" y="4" width="7" height="7" rx="1.2" />
                      <path d="M8 4V2.5A1.5 1.5 0 006.5 1h-4A1.5 1.5 0 001 2.5v4A1.5 1.5 0 002.5 8H4" />
                    </svg>
                    Copy
                  </>
                )}
              </motion.button>
            </div>
            <div
              className="glass-subtle"
              style={{
                padding: '18px 20px',
                borderRadius: '14px',
                fontSize: '14.5px',
                lineHeight: 1.7,
                color: 'var(--color-text)',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                maxHeight: '440px',
                overflowY: 'auto',
                fontFamily: 'var(--font-body)',
                letterSpacing: 0,
              }}
            >
              {result || (
                <span style={{ color: 'var(--color-text-dim)', fontStyle: 'italic' }}>
                  (empty transcription)
                </span>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
