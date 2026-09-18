import { useState, useEffect, useRef, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { cloneVoice } from '../api';
import AudioPlayer from '../components/AudioPlayer';
import { useAudio } from '../hooks/useAudio';
import WaveformBars from '../components/WaveformBars';

/**
 * OmniVoice hört sich einen kurzen Ausschnitt an und spricht danach in dieser
 * Stimme. Referenzen dürfen bis zu 20 Sekunden lang sein; längere Aufnahmen
 * werden vor dem Upload auf den ausgewählten Ausschnitt zugeschnitten.
 */
const CLIP_DURATION = 20;

/** AudioBuffer auf `numBars` Spitzenwerte herunterrechnen (Wellenform). */
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

/** Ausschnitt zwischen start/end als Mono-16-Bit-WAV herausschneiden. */
function trimToWav(buffer: AudioBuffer, start: number, end: number): File {
  const sr = buffer.sampleRate;
  const s0 = Math.max(0, Math.floor(start * sr));
  const s1 = Math.min(buffer.length, Math.floor(end * sr), s0 + Math.floor(CLIP_DURATION * sr));
  const len = s1 - s0;
  const numCh = buffer.numberOfChannels;

  const mono = new Float32Array(len);
  for (let ch = 0; ch < numCh; ch++) {
    const chData = buffer.getChannelData(ch);
    for (let i = 0; i < len; i++) mono[i] += chData[s0 + i] / numCh;
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

const TIPS = [
  'Drei bis zehn Sekunden empfohlen, bis zu 20 Sekunden möglich.',
  'Kein Hall, keine Musik, keine zweite Stimme im Hintergrund.',
  'Normale Sprechlautstärke — geflüstert oder geschrien klont schlecht.',
];

export default function ClonePage() {
  const navigate = useNavigate();
  const audio = useAudio();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const waveContainerRef = useRef<HTMLDivElement>(null);

  const [name, setName] = useState('');
  const [refText, setRefText] = useState('');
  const [autoTranscribe, setAutoTranscribe] = useState(true);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [cloning, setCloning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [created, setCreated] = useState<string | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const [audioBuffer, setAudioBuffer] = useState<AudioBuffer | null>(null);
  const [decodedFile, setDecodedFile] = useState<File | null>(null);
  const [peaks, setPeaks] = useState<number[]>([]);
  const [totalDuration, setTotalDuration] = useState(0);
  const [clipStart, setClipStart] = useState(0);
  const [isDraggingClip, setIsDraggingClip] = useState(false);
  const dragOffsetRef = useRef(0);

  const needsTrimming = totalDuration > CLIP_DURATION;
  const clipEnd = Math.min(clipStart + CLIP_DURATION, totalDuration);
  /* Kürzer als drei Sekunden reicht der Referenz nicht für eine stabile Stimme. */
  const tooShort = totalDuration > 0 && totalDuration < 3;

  useEffect(() => {
    setDecodedFile(null);
    setAudioBuffer(null);
    setPeaks([]);
    setTotalDuration(0);
    setClipStart(0);
    setError(null);
    if (!file) return;
    let cancelled = false;
    const ctx = new AudioContext();
    file.arrayBuffer().then((ab) => ctx.decodeAudioData(ab)).then((buf) => {
      if (cancelled) return;
      setAudioBuffer(buf);
      setDecodedFile(file);
      setTotalDuration(buf.duration);
      setPeaks(getPeaks(buf, 200));
      setClipStart(0);
    }).catch(() => {
      if (!cancelled) setError('Unable to read reference audio. Please choose a supported audio file.');
    }).finally(() => {
      if (ctx.state !== 'closed') void ctx.close().catch(() => {});
    });
    return () => { cancelled = true; };
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
      ctx.beginPath();
      ctx.moveTo(selStartPx, 0); ctx.lineTo(selStartPx, h);
      ctx.moveTo(selEndPx, 0); ctx.lineTo(selEndPx, h);
      ctx.stroke();
      ctx.fillStyle = 'rgba(123, 97, 255, 1)';
      const hs = 3;
      ctx.fillRect(selStartPx - hs / 2, 0, hs, 8);
      ctx.fillRect(selStartPx - hs / 2, h - 8, hs, 8);
      ctx.fillRect(selEndPx - hs / 2, 0, hs, 8);
      ctx.fillRect(selEndPx - hs / 2, h - 8, hs, 8);
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
        handleFile(new File([blob], 'aufnahme.webm', { type: 'audio/webm' }));
        stream.getTracks().forEach((t) => t.stop());
      };
      recorder.start();
      setRecording(true);
    } catch {
      setError('Kein Zugriff auf das Mikrofon.');
    }
  };

  const handleWaveMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!needsTrimming || !waveContainerRef.current) return;
    e.preventDefault();
    const rect = waveContainerRef.current.getBoundingClientRect();
    const clickTime = ((e.clientX - rect.left) / rect.width) * totalDuration;
    if (clickTime >= clipStart && clickTime <= clipEnd) {
      setIsDraggingClip(true);
      dragOffsetRef.current = clickTime - clipStart;
    } else {
      setClipStart(Math.max(0, Math.min(totalDuration - CLIP_DURATION, clickTime - CLIP_DURATION / 2)));
    }
  };

  useEffect(() => {
    if (!isDraggingClip) return;
    const handleMove = (e: MouseEvent) => {
      if (!waveContainerRef.current) return;
      const rect = waveContainerRef.current.getBoundingClientRect();
      const t = ((e.clientX - rect.left) / rect.width) * totalDuration - dragOffsetRef.current;
      setClipStart(Math.max(0, Math.min(totalDuration - CLIP_DURATION, t)));
    };
    const handleUp = () => setIsDraggingClip(false);
    window.addEventListener('mousemove', handleMove);
    window.addEventListener('mouseup', handleUp);
    return () => { window.removeEventListener('mousemove', handleMove); window.removeEventListener('mouseup', handleUp); };
  }, [isDraggingClip, totalDuration]);

  const handlePlayClip = () => {
    if (!audioBuffer || decodedFile !== file) return;
    audio.play(trimToWav(audioBuffer, clipStart, clipEnd), 'clip-preview');
  };

  const handleClone = async () => {
    if (!file || !name.trim() || !audioBuffer || decodedFile !== file) return;
    setCloning(true);
    setError(null);

    const audioFile = needsTrimming ? trimToWav(audioBuffer, clipStart, clipEnd) : file;

    /* `language` bleibt weg: OmniVoice registriert die Referenz sprachneutral
       und der Server setzt für den Altpfad selbst einen Standard. */
    const formData = new FormData();
    formData.append('name', name.trim());
    formData.append('ref_audio', audioFile);
    formData.append('auto_transcribe', String(autoTranscribe));
    if (refText.trim() && !autoTranscribe) formData.append('ref_text', refText.trim());

    try {
      await cloneVoice(formData);
      setCreated(name.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Klonen fehlgeschlagen');
    }
    setCloning(false);
  };

  const fmtTime = (s: number) => `${Math.floor(s / 60)}:${Math.floor(s % 60).toString().padStart(2, '0')}`;

  if (created) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="clone-done">
        <motion.div
          initial={{ scale: 0 }}
          animate={{ scale: 1 }}
          transition={{ type: 'spring', stiffness: 300, damping: 20, delay: 0.1 }}
          className="clone-done-badge"
        >
          <motion.svg
            width="38" height="38" viewBox="0 0 36 36" fill="none" stroke="currentColor"
            strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
            style={{ color: 'var(--color-success)' }}
            initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.5, delay: 0.3 }}
          >
            <path d="M8 18l7 7L28 11" />
          </motion.svg>
        </motion.div>
        <h2 className="clone-done-title">„{created}“ ist geklont</h2>
        <p className="clone-done-text">
          Die Stimme steht ab sofort in der Bibliothek und in jeder Stimmenauswahl bereit.
        </p>
        <div className="clone-done-actions">
          <button type="button" className="btn btn-primary" onClick={() => navigate('/voices')}>
            Zur Bibliothek
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              setCreated(null);
              setName('');
              setRefText('');
              setFile(null);
              audio.stop();
            }}
          >
            Weitere Stimme klonen
          </button>
        </div>
      </motion.div>
    );
  }

  const ready = !!file && !!name.trim() && !!audioBuffer && decodedFile === file;

  return (
    <div className="clone-page">
      <header className="speech-header">
        <div>
          <h1 style={{ fontSize: '26px', marginBottom: '4px' }}>Stimme klonen</h1>
          <p style={{ fontSize: '13.5px', color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
            Ein kurzer, sauberer Ausschnitt genügt. OmniVoice übernimmt Klangfarbe und Sprechweise daraus.
          </p>
        </div>
        <div className="speech-route-pill" title="Geklonte Stimmen laufen auf OmniVoice (GPU)">
          <span className="status-dot status-dot-online" />
          <span>OmniVoice</span>
        </div>
      </header>

      {/* Beide Schritte stecken in einer Karte: die Angaben sind ohne die
          Aufnahme wertlos, getrennte Kästen haben das nur zerrissen. */}
      <div className="clone-body">
        <div className="card clone-card">
          <div className="clone-steps">
            <section className="clone-step">
              <div className="clone-step-head">
                <span className="clone-step-num">1</span>
                <div>
                  <strong>Referenzaufnahme</strong>
                  <span>Bis zu 20 Sekunden Referenzaudio</span>
                </div>
              </div>

              <motion.div
                onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={handleDrop}
                onClick={() => !file && fileInputRef.current?.click()}
                className={`clone-drop${dragging ? ' clone-drop-over' : ''}${file ? ' clone-drop-filled' : ''}`}
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
                    className="clone-file"
                  >
                    <div className="clone-file-head">
                      <WaveformBars active={audio.playing} size="sm" bars={10} color="accent" />
                      <div className="clone-file-name">
                        <span>{file.name}</span>
                        <span>
                          {(file.size / 1024).toFixed(0)} KB{totalDuration > 0 ? ` · ${fmtTime(totalDuration)}` : ''}
                        </span>
                      </div>
                      <button
                        type="button"
                        className="clone-file-drop"
                        onClick={(e) => { e.stopPropagation(); setFile(null); audio.stop(); }}
                      >
                        Entfernen
                      </button>
                    </div>
                    <AudioPlayer
                      playing={audio.playing}
                      progress={audio.progress}
                      duration={audio.duration}
                      onToggle={audio.toggle}
                      onSeek={audio.seek}
                      compact
                    />
                  </motion.div>
                ) : (
                  <>
                    <motion.div
                      style={{ color: 'var(--color-text-dim)' }}
                      animate={{ y: [0, -4, 0] }}
                      transition={{ duration: 2.4, repeat: Infinity, ease: 'easeInOut' }}
                    >
                      <svg width="30" height="30" viewBox="0 0 44 44" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M22 5v22M14 13l8-8 8 8" />
                        <path d="M5 28v7a4 4 0 004 4h26a4 4 0 004-4v-7" />
                      </svg>
                    </motion.div>
                    <p className="clone-drop-title">Audiodatei hierher ziehen</p>
                    <p className="clone-drop-hint">oder klicken — WAV, MP3, FLAC, M4A</p>
                  </>
                )}
              </motion.div>

              <button
                type="button"
                onClick={handleRecord}
                className={`clone-rec${recording ? ' clone-rec-on' : ''}`}
              >
                {recording ? (
                  <>
                    <motion.span
                      className="clone-rec-dot"
                      animate={{ opacity: [1, 0.3, 1] }}
                      transition={{ duration: 1.2, repeat: Infinity, ease: 'easeInOut' }}
                    />
                    Aufnahme beenden
                  </>
                ) : (
                  <>
                    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="5" y="1" width="6" height="9" rx="3" />
                      <path d="M2 7a6 6 0 0012 0M8 13v2" />
                    </svg>
                    Direkt aufnehmen
                  </>
                )}
              </button>

              <AnimatePresence>
                {file && peaks.length > 0 && needsTrimming && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
                    style={{ overflow: 'hidden' }}
                  >
                    <div className="clone-trim">
                      <div className="clone-trim-head">
                        <span className="clone-trim-label">Ausschnitt · {CLIP_DURATION} s</span>
                        <span className="clone-trim-range">{fmtTime(clipStart)} – {fmtTime(clipEnd)}</span>
                      </div>
                      <div
                        ref={waveContainerRef}
                        onMouseDown={handleWaveMouseDown}
                        className="clone-wave"
                        style={{ cursor: isDraggingClip ? 'grabbing' : 'pointer' }}
                      >
                        <canvas ref={canvasRef} style={{ width: '100%', height: '100%', display: 'block' }} />
                      </div>
                      <div className="clone-trim-foot">
                        <p className="clone-trim-hint">
                          Fenster verschieben — nur dieser Ausschnitt wird hochgeladen.
                        </p>
                        <button type="button" onClick={handlePlayClip} className="clone-trim-play">
                          <svg width="11" height="11" viewBox="0 0 14 14" fill="currentColor"><path d="M4 1.5v11l8-5.5z" /></svg>
                          Anhören
                        </button>
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>

              {file && peaks.length > 0 && !needsTrimming && totalDuration > 0 && (
                <p className={`clone-len${tooShort ? ' clone-len-warn' : ''}`}>
                  {tooShort
                    ? `Nur ${fmtTime(totalDuration)} — unter drei Sekunden wird die Stimme unzuverlässig.`
                    : `${fmtTime(totalDuration)} — passt, kein Zuschnitt nötig.`}
                </p>
              )}
            </section>

            <section className="clone-step">
              <div className="clone-step-head">
                <span className="clone-step-num">2</span>
                <div>
                  <strong>Angaben</strong>
                  <span>Name und Referenztext</span>
                </div>
              </div>

              <div className="clone-field">
                <label htmlFor="clone-name">Name <span style={{ color: 'var(--color-accent)' }}>*</span></label>
                <input
                  id="clone-name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="input-field"
                  placeholder="z. B. Erzählerin, Anna, Nachrichtenstimme…"
                />
                <p className="clone-field-hint">Unter diesem Namen erscheint die Stimme in jeder Auswahl.</p>
              </div>

              <label className="clone-check" onClick={() => setAutoTranscribe(!autoTranscribe)}>
                <motion.span
                  animate={{
                    background: autoTranscribe ? 'linear-gradient(135deg, var(--color-accent), var(--color-aurora-2))' : 'transparent',
                    borderColor: autoTranscribe ? 'rgba(123,97,255,0.6)' : 'rgba(255,255,255,0.2)',
                  }}
                  transition={{ duration: 0.15 }}
                  className="clone-check-box"
                >
                  {autoTranscribe && (
                    <motion.svg initial={{ scale: 0 }} animate={{ scale: 1 }} width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M2.5 6l2.5 2.5 5-5" />
                    </motion.svg>
                  )}
                </motion.span>
                <span className="clone-check-text">
                  <span>Referenztext automatisch erkennen</span>
                  <span>Whisper transkribiert den Ausschnitt selbst. Ausschalten, um den Text von Hand einzutragen.</span>
                </span>
              </label>

              <AnimatePresence>
                {!autoTranscribe && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.2, ease: [0.25, 0.46, 0.45, 0.94] }}
                    style={{ overflow: 'hidden' }}
                  >
                    <div className="clone-field">
                      <label htmlFor="clone-ref">Referenztext</label>
                      <textarea
                        id="clone-ref"
                        value={refText}
                        onChange={(e) => setRefText(e.target.value)}
                        rows={3}
                        className="input-field"
                        style={{ resize: 'vertical', minHeight: '84px', fontFamily: 'var(--font-body)' }}
                        placeholder="Wortwörtlich das, was in der Aufnahme gesagt wird…"
                      />
                      <p className="clone-field-hint">Muss exakt zur Aufnahme passen — Abweichungen verzerren den Klon.</p>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>

              <div className="clone-step-foot">
                <AnimatePresence>
                  {error && (
                    <motion.div
                      initial={{ opacity: 0, y: -4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -4 }}
                      transition={{ duration: 0.18 }}
                      className="clone-error"
                    >
                      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                        <circle cx="8" cy="8" r="6" />
                        <path d="M8 5.5v3M8 10.5h.01" />
                      </svg>
                      <span>{error}</span>
                    </motion.div>
                  )}
                </AnimatePresence>

                <button
                  type="button"
                  onClick={handleClone}
                  disabled={cloning || !ready}
                  className="btn btn-primary clone-submit"
                >
                  {cloning ? (
                    <>
                      <motion.span animate={{ rotate: 360 }} transition={{ duration: 1, repeat: Infinity, ease: 'linear' }} style={{ lineHeight: 0 }}>
                        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M8 1a7 7 0 106 3.5" /></svg>
                      </motion.span>
                      Klone Stimme…
                    </>
                  ) : (
                    <>
                      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M8 3v10M3 8h10" /></svg>
                      Stimme klonen
                    </>
                  )}
                </button>

                {!ready && (
                  <p className="clone-field-hint" style={{ textAlign: 'center' }}>
                    {!file ? 'Es fehlt noch die Referenzaufnahme.'
                      : !name.trim() ? 'Es fehlt noch ein Name.'
                        : error ? 'Bitte eine lesbare Referenzaufnahme wählen.'
                          : 'Referenzaufnahme wird geprüft…'}
                  </p>
                )}
              </div>
            </section>
          </div>

          {/* Die Hinweise gelten für beide Spalten und schliessen die Karte ab. */}
          <ul className="clone-tips">
            {TIPS.map((t) => (
              <li key={t}>
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 8.5l3.5 3.5L13 4.5" />
                </svg>
                {t}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
