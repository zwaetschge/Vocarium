import { useState, useEffect, useRef, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { cloneVoice, getLanguages } from '../api';
import AudioPlayer from '../components/AudioPlayer';
import { useAudio } from '../hooks/useAudio';
import WaveformBars from '../components/WaveformBars';

const CLIP_DURATION = 10; // seconds

/** Downsample AudioBuffer to peaks for waveform drawing */
function getPeaks(buffer: AudioBuffer, numBars: number): number[] {
  const data = buffer.getChannelData(0);
  const step = Math.floor(data.length / numBars);
  const peaks: number[] = [];
  for (let i = 0; i < numBars; i++) {
    let max = 0;
    for (let j = 0; j < step; j++) {
      const v = Math.abs(data[i * step + j] || 0);
      if (v > max) max = v;
    }
    peaks.push(max);
  }
  const maxPeak = Math.max(...peaks, 0.01);
  return peaks.map((p) => p / maxPeak);
}

/** Trim AudioBuffer to start/end seconds and export as WAV File */
function trimToWav(buffer: AudioBuffer, start: number, end: number): File {
  const sr = buffer.sampleRate;
  const s0 = Math.max(0, Math.floor(start * sr));
  const s1 = Math.min(buffer.length, Math.floor(end * sr));
  const len = s1 - s0;
  const numCh = buffer.numberOfChannels;

  const mono = new Float32Array(len);
  for (let ch = 0; ch < numCh; ch++) {
    const chData = buffer.getChannelData(ch);
    for (let i = 0; i < len; i++) {
      mono[i] += chData[s0 + i] / numCh;
    }
  }

  const bitsPerSample = 16;
  const byteRate = sr * (bitsPerSample / 8);
  const dataSize = len * (bitsPerSample / 8);
  const buf = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buf);

  const writeStr = (off: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + dataSize, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sr, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, bitsPerSample / 8, true);
  view.setUint16(34, bitsPerSample, true);
  writeStr(36, 'data');
  view.setUint32(40, dataSize, true);

  let off = 44;
  for (let i = 0; i < len; i++) {
    const s = Math.max(-1, Math.min(1, mono[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    off += 2;
  }

  return new File([buf], 'clip.wav', { type: 'audio/wav' });
}

export default function ClonePage() {
  const navigate = useNavigate();
  const audio = useAudio();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const waveContainerRef = useRef<HTMLDivElement>(null);

  const [name, setName] = useState('');
  const [language, setLanguage] = useState('English');
  const [refText, setRefText] = useState('');
  const [autoTranscribe, setAutoTranscribe] = useState(true);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [languages, setLanguages] = useState<string[]>([]);
  const [cloning, setCloning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [success, setSuccess] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const [audioBuffer, setAudioBuffer] = useState<AudioBuffer | null>(null);
  const [peaks, setPeaks] = useState<number[]>([]);
  const [totalDuration, setTotalDuration] = useState(0);
  const [clipStart, setClipStart] = useState(0);
  const [isDraggingClip, setIsDraggingClip] = useState(false);
  const dragOffsetRef = useRef(0);

  const needsTrimming = totalDuration > CLIP_DURATION + 0.5;
  const clipEnd = Math.min(clipStart + CLIP_DURATION, totalDuration);

  useEffect(() => {
    getLanguages().then(setLanguages).catch(() => setLanguages(['English', 'Chinese']));
  }, []);

  useEffect(() => {
    if (!file) {
      setAudioBuffer(null);
      setPeaks([]);
      setTotalDuration(0);
      setClipStart(0);
      return;
    }
    const ctx = new AudioContext();
    file.arrayBuffer().then((ab) => ctx.decodeAudioData(ab)).then((buf) => {
      setAudioBuffer(buf);
      setTotalDuration(buf.duration);
      setPeaks(getPeaks(buf, 200));
      setClipStart(0);
      ctx.close();
    }).catch(() => {
      setAudioBuffer(null);
      setPeaks([]);
      setTotalDuration(0);
    });
  }, [file]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || peaks.length === 0) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);

    const barW = w / peaks.length;
    const selStartPx = (clipStart / totalDuration) * w;
    const selEndPx = (clipEnd / totalDuration) * w;

    for (let i = 0; i < peaks.length; i++) {
      const x = i * barW;
      const barH = peaks[i] * (h * 0.8);
      const inSelection = x >= selStartPx && x <= selEndPx;
      ctx.fillStyle = inSelection ? 'rgba(123, 97, 255, 0.85)' : 'rgba(255, 255, 255, 0.14)';
      ctx.fillRect(x + 0.5, (h - barH) / 2, Math.max(barW - 1, 1), barH);
    }

    if (needsTrimming) {
      ctx.fillStyle = 'rgba(123, 97, 255, 0.14)';
      ctx.fillRect(selStartPx, 0, selEndPx - selStartPx, h);

      ctx.strokeStyle = 'rgba(123, 97, 255, 0.7)';
      ctx.lineWidth = 2;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(selStartPx, 0); ctx.lineTo(selStartPx, h);
      ctx.moveTo(selEndPx, 0); ctx.lineTo(selEndPx, h);
      ctx.stroke();

      // Glow markers at top/bottom of each handle
      ctx.fillStyle = 'rgba(123, 97, 255, 1)';
      const handleSize = 3;
      ctx.fillRect(selStartPx - handleSize / 2, 0, handleSize, 8);
      ctx.fillRect(selStartPx - handleSize / 2, h - 8, handleSize, 8);
      ctx.fillRect(selEndPx - handleSize / 2, 0, handleSize, 8);
      ctx.fillRect(selEndPx - handleSize / 2, h - 8, handleSize, 8);
    }
  }, [peaks, clipStart, clipEnd, totalDuration, needsTrimming]);

  const handleFile = useCallback((f: File) => {
    setFile(f);
    setError(null);
    audio.play(f, 'preview');
  }, [audio]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f && f.type.startsWith('audio/')) handleFile(f);
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
      recorder.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: 'audio/webm' });
        handleFile(new File([blob], 'recording.webm', { type: 'audio/webm' }));
        stream.getTracks().forEach((t) => t.stop());
      };
      recorder.start();
      setRecording(true);
    } catch {
      setError('Microphone access denied');
    }
  };

  const handleWaveMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!needsTrimming || !waveContainerRef.current) return;
    e.preventDefault();
    const rect = waveContainerRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const pct = x / rect.width;
    const clickTime = pct * totalDuration;
    if (clickTime >= clipStart && clickTime <= clipEnd) {
      setIsDraggingClip(true);
      dragOffsetRef.current = clickTime - clipStart;
    } else {
      const newStart = Math.max(0, Math.min(totalDuration - CLIP_DURATION, clickTime - CLIP_DURATION / 2));
      setClipStart(newStart);
    }
  };

  useEffect(() => {
    if (!isDraggingClip) return;
    const handleMove = (e: MouseEvent) => {
      if (!waveContainerRef.current) return;
      const rect = waveContainerRef.current.getBoundingClientRect();
      const pct = (e.clientX - rect.left) / rect.width;
      const t = pct * totalDuration - dragOffsetRef.current;
      setClipStart(Math.max(0, Math.min(totalDuration - CLIP_DURATION, t)));
    };
    const handleUp = () => setIsDraggingClip(false);
    window.addEventListener('mousemove', handleMove);
    window.addEventListener('mouseup', handleUp);
    return () => { window.removeEventListener('mousemove', handleMove); window.removeEventListener('mouseup', handleUp); };
  }, [isDraggingClip, totalDuration]);

  const handlePlayClip = () => {
    if (!audioBuffer) return;
    const clipped = trimToWav(audioBuffer, clipStart, clipEnd);
    audio.play(clipped, 'clip-preview');
  };

  const handleClone = async () => {
    if (!file || !name.trim()) return;
    setCloning(true);
    setError(null);

    let audioFile = file;
    if (needsTrimming && audioBuffer) {
      audioFile = trimToWav(audioBuffer, clipStart, clipEnd);
    }

    const formData = new FormData();
    formData.append('name', name.trim());
    formData.append('language', language);
    formData.append('ref_audio', audioFile);
    formData.append('auto_transcribe', String(autoTranscribe));
    if (refText.trim() && !autoTranscribe) {
      formData.append('ref_text', refText.trim());
    }

    try {
      await cloneVoice(formData);
      setSuccess(true);
      setTimeout(() => navigate('/voices'), 1500);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Clone failed');
    }
    setCloning(false);
  };

  const fmtTime = (s: number) => {
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60);
    return `${m}:${sec.toString().padStart(2, '0')}`;
  };

  if (success) {
    return (
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '96px 0', textAlign: 'center' }}
      >
        <motion.div
          initial={{ scale: 0 }}
          animate={{ scale: 1 }}
          transition={{ type: 'spring', stiffness: 300, damping: 20, delay: 0.1 }}
          style={{
            width: '88px',
            height: '88px',
            borderRadius: '50%',
            background: 'linear-gradient(135deg, rgba(74,222,128,0.25), rgba(50,181,255,0.12))',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginBottom: '28px',
            border: '1px solid rgba(74,222,128,0.35)',
            boxShadow: '0 10px 32px rgba(74,222,128,0.2), inset 0 1px 0 rgba(255,255,255,0.15)',
          }}
        >
          <motion.svg
            width="38"
            height="38"
            viewBox="0 0 36 36"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{ color: 'var(--color-success)' }}
            initial={{ pathLength: 0 }}
            animate={{ pathLength: 1 }}
            transition={{ duration: 0.5, delay: 0.3 }}
          >
            <path d="M8 18l7 7L28 11" />
          </motion.svg>
        </motion.div>
        <h3
          style={{
            fontSize: '24px',
            fontWeight: 600,
            fontFamily: 'var(--font-display)',
            letterSpacing: 0,
            marginBottom: '8px',
            color: 'var(--color-text)',
          }}
        >
          Voice cloned
        </h3>
        <p style={{ fontSize: '14px', color: 'var(--color-text-secondary)' }}>Redirecting to your library…</p>
      </motion.div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
      style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '28px', alignItems: 'start' }}
    >
      {/* Left: Reference audio */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
        <div className="label-eyebrow">Reference audio</div>

        {/* Drop zone */}
        <motion.div
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          onClick={() => !file && fileInputRef.current?.click()}
          animate={{
            borderColor: dragging
              ? 'rgba(123, 97, 255, 0.55)'
              : file
                ? 'rgba(123, 97, 255, 0.28)'
                : 'rgba(255, 255, 255, 0.1)',
          }}
          transition={{ duration: 0.2 }}
          style={{
            position: 'relative',
            cursor: file ? 'default' : 'pointer',
            padding: file ? '22px' : '36px 22px',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            textAlign: 'center',
            borderRadius: 'var(--radius-panel)',
            borderWidth: '1.5px',
            borderStyle: 'dashed',
            minHeight: file ? 'auto' : '240px',
            background: dragging
              ? 'rgba(123, 97, 255, 0.08)'
              : file
                ? 'rgba(123, 97, 255, 0.04)'
                : 'rgba(255, 255, 255, 0.02)',
            backdropFilter: 'blur(18px) saturate(140%)',
            WebkitBackdropFilter: 'blur(18px) saturate(140%)',
            boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.04)',
            transition: 'background 0.2s ease',
          }}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept="audio/*"
            style={{ display: 'none' }}
            onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); }}
          />

          {file ? (
            <motion.div
              initial={{ opacity: 0, scale: 0.97 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
              style={{ display: 'flex', flexDirection: 'column', gap: '14px', width: '100%', alignItems: 'center' }}
            >
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <WaveformBars active={audio.playing} size="md" bars={16} color="accent" />
              </div>
              <p
                style={{
                  fontSize: '13.5px',
                  fontWeight: 500,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  color: 'var(--color-text)',
                  maxWidth: '100%',
                  letterSpacing: 0,
                }}
              >
                {file.name}
              </p>
              <p
                style={{
                  fontSize: '11px',
                  fontFamily: 'var(--font-mono)',
                  color: 'var(--color-text-dim)',
                  letterSpacing: 0,
                }}
              >
                {(file.size / 1024).toFixed(1)} KB
                {totalDuration > 0 ? `  ·  ${fmtTime(totalDuration)}` : ''}
              </p>
              <AudioPlayer
                playing={audio.playing}
                progress={audio.progress}
                duration={audio.duration}
                onToggle={audio.toggle}
                onSeek={audio.seek}
              />
              <button
                onClick={(e) => { e.stopPropagation(); setFile(null); audio.stop(); }}
                style={{
                  fontSize: '11.5px',
                  color: 'var(--color-text-dim)',
                  background: 'none',
                  border: 'none',
                  cursor: 'pointer',
                  padding: '4px 8px',
                  letterSpacing: 0,
                  transition: 'color 0.15s',
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
                style={{ marginBottom: '18px', color: 'var(--color-text-dim)' }}
                animate={{ y: [0, -4, 0] }}
                transition={{ duration: 2.4, repeat: Infinity, ease: 'easeInOut' }}
              >
                <svg width="44" height="44" viewBox="0 0 44 44" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M22 5v22M14 13l8-8 8 8" />
                  <path d="M5 28v7a4 4 0 004 4h26a4 4 0 004-4v-7" />
                </svg>
              </motion.div>
              <p
                style={{
                  fontSize: '14px',
                  fontWeight: 500,
                  marginBottom: '6px',
                  color: 'var(--color-text)',
                  letterSpacing: 0,
                }}
              >
                Drop audio file here
              </p>
              <p style={{ fontSize: '12px', color: 'var(--color-text-dim)' }}>
                or click to browse. WAV, MP3, FLAC supported.
              </p>
            </>
          )}
        </motion.div>

        {/* Audio Trimmer */}
        <AnimatePresence>
          {file && peaks.length > 0 && needsTrimming && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
              style={{ overflow: 'hidden' }}
            >
              <div
                className="glass-subtle"
                style={{
                  padding: '16px',
                  borderRadius: '14px',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '12px',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span className="label-eyebrow" style={{ fontSize: '10.5px' }}>
                    Select {CLIP_DURATION}s clip
                  </span>
                  <span
                    style={{
                      fontSize: '11.5px',
                      fontFamily: 'var(--font-mono)',
                      color: 'var(--color-accent)',
                      letterSpacing: 0,
                    }}
                  >
                    {fmtTime(clipStart)} – {fmtTime(clipEnd)}
                  </span>
                </div>

                <div
                  ref={waveContainerRef}
                  onMouseDown={handleWaveMouseDown}
                  style={{
                    position: 'relative',
                    height: '68px',
                    cursor: isDraggingClip ? 'grabbing' : 'pointer',
                    userSelect: 'none',
                    borderRadius: '10px',
                    overflow: 'hidden',
                    background: 'rgba(0,0,0,0.35)',
                    border: '1px solid rgba(255,255,255,0.04)',
                  }}
                >
                  <canvas ref={canvasRef} style={{ width: '100%', height: '100%', display: 'block' }} />
                </div>

                <motion.button
                  whileTap={{ scale: 0.97 }}
                  onClick={handlePlayClip}
                  className="btn btn-ghost"
                  style={{
                    padding: '9px 0',
                    fontSize: '12.5px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '8px',
                  }}
                >
                  <svg width="12" height="12" viewBox="0 0 14 14" fill="currentColor">
                    <path d="M4 1.5v11l8-5.5z" />
                  </svg>
                  Preview clip
                </motion.button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <AnimatePresence>
          {file && peaks.length > 0 && !needsTrimming && totalDuration > 0 && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              style={{
                fontSize: '11.5px',
                color: 'var(--color-text-dim)',
                textAlign: 'center',
                padding: '4px 0',
                letterSpacing: 0,
              }}
            >
              Audio is {fmtTime(totalDuration)} — no trimming needed
            </motion.div>
          )}
        </AnimatePresence>

        <motion.button
          whileTap={{ scale: 0.97 }}
          onClick={handleRecord}
          style={{
            width: '100%',
            padding: '12px 0',
            fontSize: '13px',
            fontWeight: 500,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '10px',
            borderRadius: '12px',
            cursor: 'pointer',
            background: recording ? 'var(--color-danger-dim)' : 'rgba(255,255,255,0.03)',
            border: recording ? '1px solid rgba(255,90,101,0.4)' : '1px solid rgba(255,255,255,0.08)',
            color: recording ? 'var(--color-danger)' : 'var(--color-text-secondary)',
            backdropFilter: 'blur(12px)',
            WebkitBackdropFilter: 'blur(12px)',
            transition: 'background 0.15s, border-color 0.15s, color 0.15s',
            letterSpacing: 0,
          }}
        >
          {recording ? (
            <>
              <motion.span
                style={{ width: '8px', height: '8px', borderRadius: '50%', background: 'var(--color-danger)', boxShadow: '0 0 10px rgba(255,90,101,0.8)' }}
                animate={{ opacity: [1, 0.3, 1] }}
                transition={{ duration: 1.2, repeat: Infinity, ease: 'easeInOut' }}
              />
              Stop recording
            </>
          ) : (
            <>
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <rect x="5" y="1" width="6" height="9" rx="3" />
                <path d="M2 7a6 6 0 0012 0M8 13v2" />
              </svg>
              Record audio
            </>
          )}
        </motion.button>
      </div>

      {/* Right: Voice details */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
        <div className="label-eyebrow">Voice details</div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <label
            style={{
              fontSize: '11.5px',
              fontWeight: 500,
              color: 'var(--color-text-secondary)',
              letterSpacing: 0,
            }}
          >
            Voice name <span style={{ color: 'var(--color-accent)' }}>*</span>
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="input-field"
            placeholder="e.g. Morgan, Whisper, Narrator…"
          />
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <label
            style={{
              fontSize: '11.5px',
              fontWeight: 500,
              color: 'var(--color-text-secondary)',
              letterSpacing: 0,
            }}
          >
            Language
          </label>
          <div style={{ position: 'relative' }}>
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              className="input-field"
              style={{
                appearance: 'none',
                cursor: 'pointer',
                paddingRight: '36px',
              }}
            >
              {languages.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
            <svg
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              style={{
                position: 'absolute',
                right: '14px',
                top: '50%',
                transform: 'translateY(-50%)',
                color: 'var(--color-text-dim)',
                pointerEvents: 'none',
              }}
            >
              <path d="M3 4.5l3 3 3-3" />
            </svg>
          </div>
        </div>

        <label
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: '12px',
            cursor: 'pointer',
            padding: '14px',
            borderRadius: '12px',
            background: 'rgba(255,255,255,0.02)',
            border: '1px solid rgba(255,255,255,0.06)',
          }}
          onClick={() => setAutoTranscribe(!autoTranscribe)}
        >
          <motion.div
            animate={{
              background: autoTranscribe
                ? 'linear-gradient(135deg, var(--color-accent), var(--color-aurora-2))'
                : 'transparent',
              borderColor: autoTranscribe ? 'rgba(123,97,255,0.6)' : 'rgba(255,255,255,0.2)',
            }}
            transition={{ duration: 0.15 }}
            style={{
              width: '20px',
              height: '20px',
              borderRadius: '6px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
              borderWidth: '1px',
              borderStyle: 'solid',
              marginTop: '1px',
              boxShadow: autoTranscribe ? 'inset 0 1px 0 rgba(255,255,255,0.2)' : 'none',
            }}
          >
            {autoTranscribe && (
              <motion.svg
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                width="12"
                height="12"
                viewBox="0 0 12 12"
                fill="none"
                stroke="white"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M2.5 6l2.5 2.5 5-5" />
              </motion.svg>
            )}
          </motion.div>
          <div style={{ flex: 1 }}>
            <div
              style={{
                fontSize: '13px',
                color: 'var(--color-text)',
                fontWeight: 500,
                letterSpacing: 0,
              }}
            >
              Auto-transcribe reference
            </div>
            <p style={{ fontSize: '11.5px', color: 'var(--color-text-dim)', marginTop: '3px', lineHeight: 1.5 }}>
              Use ASR to transcribe the clip automatically. Disable to write the reference text yourself.
            </p>
          </div>
        </label>

        <AnimatePresence>
          {!autoTranscribe && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2, ease: [0.25, 0.46, 0.45, 0.94] }}
              style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column', gap: '8px' }}
            >
              <label
                style={{
                  fontSize: '11.5px',
                  fontWeight: 500,
                  color: 'var(--color-text-secondary)',
                  letterSpacing: 0,
                }}
              >
                Reference text
              </label>
              <textarea
                value={refText}
                onChange={(e) => setRefText(e.target.value)}
                rows={3}
                className="input-field"
                style={{ resize: 'vertical', minHeight: '80px', fontFamily: 'var(--font-body)' }}
                placeholder="Exactly what's said in the reference audio…"
              />
            </motion.div>
          )}
        </AnimatePresence>

        <AnimatePresence>
          {error && (
            <motion.div
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.18 }}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                padding: '12px 14px',
                borderRadius: '12px',
                background: 'var(--color-danger-dim)',
                border: '1px solid rgba(255,90,101,0.25)',
                fontSize: '13px',
                color: 'var(--color-danger)',
              }}
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                <circle cx="8" cy="8" r="6" />
                <path d="M8 5.5v3M8 10.5h.01" />
              </svg>
              <span style={{ flex: 1 }}>{error}</span>
            </motion.div>
          )}
        </AnimatePresence>

        <motion.button
          whileTap={{ scale: 0.98 }}
          whileHover={!cloning && file && name.trim() ? { y: -1 } : undefined}
          transition={{ type: 'spring', stiffness: 300, damping: 22 }}
          onClick={handleClone}
          disabled={cloning || !file || !name.trim()}
          className="btn btn-primary"
          style={{
            width: '100%',
            padding: '14px 0',
            fontSize: '13.5px',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '10px',
            cursor: cloning || !file || !name.trim() ? 'not-allowed' : 'pointer',
            opacity: cloning || !file || !name.trim() ? 0.45 : 1,
            letterSpacing: 0,
          }}
        >
          {cloning ? (
            <>
              <motion.div
                animate={{ rotate: 360 }}
                transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                style={{ display: 'flex' }}
              >
                <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                  <path d="M8 1a7 7 0 106 3.5" />
                </svg>
              </motion.div>
              Cloning voice…
            </>
          ) : (
            <>
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                <path d="M8 3v10M3 8h10" />
              </svg>
              Clone voice
            </>
          )}
        </motion.button>
      </div>
    </motion.div>
  );
}
