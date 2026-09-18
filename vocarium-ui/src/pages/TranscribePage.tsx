import { useMemo, useRef, useState, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { transcribe } from '../api';
import type { TranscriptionResult } from '../api';
import AudioPlayer from '../components/AudioPlayer';
import { useAudio } from '../hooks/useAudio';
import WaveformBars from '../components/WaveformBars';

type InputMode = 'file' | 'url' | 'mic';

/**
 * Die beiden Whisper-Profile, die der Worker tatsächlich kennt.
 *
 * `swiss` ist bewusst nicht die Vorgabe: das Flix-Finetune übersetzt sauberes
 * Hochdeutsch gelegentlich nach Englisch, obwohl `language=de` und
 * `task=transcribe` gesetzt sind. Für Mundart ist es trotzdem klar besser.
 */
const PROFILES = [
  { id: 'german', label: 'Standard', hint: 'large-v3' },
  { id: 'swiss', label: 'Mundart', hint: 'Flix-Finetune' },
] as const;

const LANGUAGES = [
  { id: '', label: 'Deutsch', hint: 'fest' },
  { id: 'auto', label: 'Automatisch', hint: 'erkennen' },
] as const;

function formatTimestamp(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
    : `${m}:${s.toString().padStart(2, '0')}`;
}

/** SRT-Zeitcode: `00:01:02,500` — Komma statt Punkt, das ist im Format so. */
function srtTime(seconds: number): string {
  const ms = Math.max(0, Math.round(seconds * 1000));
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  const rest = ms % 1000;
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s
    .toString()
    .padStart(2, '0')},${rest.toString().padStart(3, '0')}`;
}

function download(name: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export default function TranscribePage() {
  const audio = useAudio();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [mode, setMode] = useState<InputMode>('file');
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState('');
  const [profile, setProfile] = useState<string>('german');
  const [language, setLanguage] = useState<string>('');
  const [dragging, setDragging] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TranscriptionResult | null>(null);
  const [copied, setCopied] = useState(false);
  const [withTimes, setWithTimes] = useState(true);

  const [recording, setRecording] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const handleFile = useCallback((f: File) => {
    setFile(f);
    setError(null);
    setResult(null);
    audio.play(f, 'preview');
  }, [audio]);

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

  const canRun = mode === 'url' ? url.trim().length > 0 : !!file;

  const run = async () => {
    setTranscribing(true);
    setError(null);
    setResult(null);
    try {
      const opts = { model: profile, language };
      if (mode === 'url' && url.trim()) setResult(await transcribe({ url: url.trim(), ...opts }));
      else if (file) setResult(await transcribe({ file, ...opts }));
      else setError('Es fehlt eine Datei oder ein Link.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Transkription fehlgeschlagen');
    }
    setTranscribing(false);
  };

  const plain = useMemo(() => (result?.segments.length ? result.segments.map((s) => s.text.trim()).join(' ') : result?.text ?? ''), [result]);

  const timed = useMemo(() => {
    if (!result?.segments.length) return plain;
    return result.segments
      .map((s) => `[${formatTimestamp(s.start)} – ${formatTimestamp(s.end)}] ${s.text.trim()}`)
      .join('\n');
  }, [result, plain]);

  const copy = () => {
    navigator.clipboard.writeText(withTimes ? timed : plain);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };

  const saveSrt = () => {
    if (!result?.segments.length) return;
    const srt = result.segments
      .map((s, i) => `${i + 1}\n${srtTime(s.start)} --> ${srtTime(s.end)}\n${s.text.trim()}\n`)
      .join('\n');
    download('transkript.srt', srt);
  };

  const baseName = file ? file.name.replace(/\.[^.]+$/, '') : 'transkript';

  /* Ein Klick auf ein Segment springt in der Vorschau an diese Stelle — nur
     sinnvoll, solange die Quelle noch geladen ist und ihre Länge kennt. */
  const jumpTo = (seconds: number) => {
    if (!audio.duration) return;
    audio.seek(Math.min(0.999, seconds / audio.duration));
  };

  const modes: { key: InputMode; label: string; icon: JSX.Element }[] = [
    { key: 'file', label: 'Datei', icon: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M8 2v8M4 6l4-4 4 4" /><path d="M2 11v2a1 1 0 001 1h10a1 1 0 001-1v-2" />
      </svg>) },
    { key: 'url', label: 'Link', icon: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M6.5 5l3 3-3 3" /><rect x="1" y="2" width="14" height="12" rx="2" />
      </svg>) },
    { key: 'mic', label: 'Mikrofon', icon: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="5" y="1" width="6" height="9" rx="3" /><path d="M2 7a6 6 0 0012 0M8 13v2" />
      </svg>) },
  ];

  return (
    <div className="tx-page">
      <header className="speech-header">
        <div>
          <h1 style={{ fontSize: '26px', marginBottom: '4px' }}>Transkription</h1>
          <p style={{ fontSize: '13.5px', color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
            Audio, Video oder ein YouTube-Link werden zu Text mit Zeitmarken.
          </p>
        </div>
        <div className="speech-route-pill" title="Whisper lädt beim ersten Auftrag und gibt die GPU nach fünf Minuten Leerlauf wieder frei">
          <span className="status-dot status-dot-online" />
          <span>Whisper large-v3</span>
        </div>
      </header>

      <div className="tx-work">
        {/* Quelle und Einstellungen leben in einem Panel — die Modellwahl
            verändert das Ergebnis und gehört sichtbar neben die Datei. */}
        <aside className="card tx-panel">
          <div className="tx-sec">
            <div className="tx-modes">
              {modes.map((m) => (
                <button
                  key={m.key}
                  type="button"
                  onClick={() => { setMode(m.key); setError(null); }}
                  className={`tx-mode${mode === m.key ? ' tx-mode-active' : ''}`}
                >
                  <span className="tx-mode-icon">{m.icon}</span>
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          <div className="tx-sec tx-sec-input">
            {mode === 'file' && (
              <div
                onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={(e) => { e.preventDefault(); setDragging(false); const f = e.dataTransfer.files[0]; if (f) handleFile(f); }}
                onClick={() => !file && fileInputRef.current?.click()}
                className={`tx-drop${dragging ? ' tx-drop-over' : ''}${file ? ' tx-drop-filled' : ''}`}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="audio/*,video/*,.mp4,.mkv,.avi,.mov,.webm,.flv,.wav,.mp3,.flac,.ogg,.m4a"
                  style={{ display: 'none' }}
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); }}
                />
                {file ? (
                  <div className="tx-file">
                    <div className="tx-file-head">
                      <WaveformBars active={audio.playing} size="sm" bars={8} color="accent" />
                      <div className="tx-file-name">
                        <span>{file.name}</span>
                        <span className="tx-file-size">{(file.size / (1024 * 1024)).toFixed(1)} MB</span>
                      </div>
                      <button
                        type="button"
                        className="tx-file-drop"
                        onClick={(e) => { e.stopPropagation(); setFile(null); audio.stop(); setResult(null); }}
                        aria-label="Datei entfernen"
                      >
                        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                          <path d="M4 4l8 8M12 4l-8 8" />
                        </svg>
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
                  </div>
                ) : (
                  <>
                    <svg width="26" height="26" viewBox="0 0 44 44" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.4 }}>
                      <path d="M22 5v22M14 13l8-8 8 8" /><path d="M5 28v7a4 4 0 004 4h26a4 4 0 004-4v-7" />
                    </svg>
                    <p className="tx-drop-title">Datei hierher ziehen</p>
                    <p className="tx-drop-hint">oder klicken — Audio und Video</p>
                  </>
                )}
              </div>
            )}

            {mode === 'url' && (
              <div className="tx-url">
                <input
                  type="text"
                  value={url}
                  onChange={(e) => { setUrl(e.target.value); setError(null); setResult(null); }}
                  className="input-field"
                  style={{ fontFamily: 'var(--font-mono)', fontSize: '12.5px' }}
                  placeholder="https://www.youtube.com/watch?v=…"
                />
                <p className="tx-drop-hint">YouTube und Vimeo. Der Ton wird auf dem Server geholt und umgewandelt.</p>
              </div>
            )}

            {mode === 'mic' && (
              <div className="tx-mic">
                <div className="tx-mic-ring">
                  {recording && (
                    <motion.span
                      aria-hidden
                      initial={{ opacity: 0.5, scale: 1 }}
                      animate={{ opacity: 0, scale: 1.8 }}
                      transition={{ duration: 1.6, repeat: Infinity, ease: 'easeOut' }}
                      className="tx-mic-pulse"
                    />
                  )}
                  <button
                    type="button"
                    onClick={handleRecord}
                    aria-label={recording ? 'Aufnahme beenden' : 'Aufnahme starten'}
                    className={`tx-mic-btn${recording ? ' tx-mic-btn-rec' : ''}`}
                  >
                    {recording ? (
                      <svg width="20" height="20" viewBox="0 0 32 32" fill="currentColor"><rect x="9" y="9" width="14" height="14" rx="3" /></svg>
                    ) : (
                      <svg width="24" height="24" viewBox="0 0 36 36" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                        <rect x="12" y="4" width="12" height="18" rx="6" /><path d="M6 16a12 12 0 0024 0M18 28v4" />
                      </svg>
                    )}
                  </button>
                </div>
                <p className={`tx-mic-label${recording ? ' tx-mic-label-rec' : ''}`}>
                  {recording ? 'Nimmt auf — zum Beenden klicken' : 'Klicken und sprechen'}
                </p>
                {file && !recording && (
                  <AudioPlayer
                    playing={audio.playing}
                    progress={audio.progress}
                    duration={audio.duration}
                    onToggle={audio.toggle}
                    onSeek={audio.seek}
                    compact
                  />
                )}
              </div>
            )}
          </div>

          <div className="tx-sec">
            <span className="tx-sec-label">Modell</span>
            <div className="tx-choice">
              {PROFILES.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => setProfile(p.id)}
                  className={`tx-choice-btn${profile === p.id ? ' tx-choice-on' : ''}`}
                >
                  <span className="tx-choice-label">{p.label}</span>
                  <span className="tx-choice-hint">{p.hint}</span>
                </button>
              ))}
            </div>
            <AnimatePresence>
              {profile === 'swiss' && (
                <motion.p
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.2 }}
                  className="tx-warn"
                >
                  Bei sauberem Hochdeutsch kippt dieses Modell manchmal ins Englische — nur für Mundart nehmen.
                </motion.p>
              )}
            </AnimatePresence>
          </div>

          <div className="tx-sec">
            <span className="tx-sec-label">Sprache</span>
            <div className="tx-choice">
              {LANGUAGES.map((l) => (
                <button
                  key={l.id || 'de'}
                  type="button"
                  onClick={() => setLanguage(l.id)}
                  className={`tx-choice-btn${language === l.id ? ' tx-choice-on' : ''}`}
                >
                  <span className="tx-choice-label">{l.label}</span>
                  <span className="tx-choice-hint">{l.hint}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="tx-foot">
            <AnimatePresence>
              {error && (
                <motion.div
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  transition={{ duration: 0.18 }}
                  className="tx-error"
                >
                  {error}
                </motion.div>
              )}
            </AnimatePresence>
            <button
              type="button"
              onClick={run}
              disabled={transcribing || !canRun}
              className="btn btn-primary tx-submit"
            >
              {transcribing ? (
                <>
                  <motion.span animate={{ rotate: 360 }} transition={{ duration: 0.9, repeat: Infinity, ease: 'linear' }} style={{ lineHeight: 0 }}>
                    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><path d="M8 1a7 7 0 106 3.5" /></svg>
                  </motion.span>
                  Transkribiere…
                </>
              ) : 'Transkribieren'}
            </button>
          </div>
        </aside>

        {/* Das Transkript ist der eigentliche Zweck der Seite und bekommt
            deshalb dauerhaft die grosse Fläche, auch wenn es noch leer ist. */}
        <section className="card tx-out">
          {result === null ? (
            <div className="tx-out-empty">
              {transcribing ? (
                <>
                  <WaveformBars active size="lg" bars={14} color="accent" />
                  <p className="tx-out-empty-title">Whisper hört zu…</p>
                  <p className="tx-out-empty-hint">
                    Nach längerer Pause lädt das Modell zuerst auf die GPU — der erste Lauf dauert dann etwas.
                  </p>
                </>
              ) : (
                <>
                  <svg width="34" height="34" viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.3 }}>
                    <path d="M6 10h20M6 16h20M6 22h12" />
                  </svg>
                  <p className="tx-out-empty-title">Noch kein Transkript</p>
                  <p className="tx-out-empty-hint">
                    Quelle links wählen und transkribieren. Das Ergebnis erscheint hier mit Zeitmarken;
                    ein Klick auf eine Zeile springt in der Vorschau an die Stelle.
                  </p>
                </>
              )}
            </div>
          ) : (
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
              className="tx-out-full"
            >
              <div className="tx-out-head">
                <div className="tx-out-stats">
                  <strong>Transkript</strong>
                  <span>
                    {result.segments.length || 0} Segmente · {plain.length} Zeichen · {result.language}
                    {' · '}{result.model === 'swiss' ? 'Mundart' : 'Standard'}
                  </span>
                </div>
                <div className="tx-out-actions">
                  {result.segments.length > 0 && (
                    <button type="button" className={`tx-chip${withTimes ? ' tx-chip-on' : ''}`} onClick={() => setWithTimes((v) => !v)}>
                      Zeitmarken
                    </button>
                  )}
                  <button type="button" className={`tx-chip${copied ? ' tx-chip-ok' : ''}`} onClick={copy}>
                    {copied ? 'Kopiert' : 'Kopieren'}
                  </button>
                  <button type="button" className="tx-chip" onClick={() => download(`${baseName}.txt`, plain)}>TXT</button>
                  {result.segments.length > 0 && (
                    <button type="button" className="tx-chip" onClick={saveSrt}>SRT</button>
                  )}
                </div>
              </div>

              <div className="tx-out-body">
                {result.segments.length > 0 ? (
                  result.segments.map((s, i) => (
                    <button
                      key={`${s.start}-${i}`}
                      type="button"
                      className="tx-seg"
                      onClick={() => jumpTo(s.start)}
                      title={audio.duration ? 'In der Vorschau an diese Stelle springen' : undefined}
                    >
                      <span className="tx-seg-time">{formatTimestamp(s.start)}</span>
                      <span className="tx-seg-text">{s.text.trim()}</span>
                    </button>
                  ))
                ) : plain ? (
                  <p className="tx-seg-plain">{plain}</p>
                ) : (
                  <p className="tx-seg-plain" style={{ color: 'var(--color-text-dim)' }}>Der Ton enthielt keine erkennbare Sprache.</p>
                )}
              </div>
            </motion.div>
          )}
        </section>
      </div>
    </div>
  );
}
