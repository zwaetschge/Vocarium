import { useCallback, useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import type { Voice } from '../types';
import { getVoiceAudio, generate, deleteVoice } from '../api';
import AudioPlayer from './AudioPlayer';
import { useAudio } from '../hooks/useAudio';
import WaveformBars from './WaveformBars';
import { describeVoice, isFallbackVoice } from '../voiceUtils';

interface VoiceCardProps {
  voice: Voice;
  index: number;
  onDeleted: () => void;
}

const SAMPLE_TEXT = 'Guten Tag. So klingt diese Stimme in einem ganz normalen Satz.';

/**
 * Kurzproben, die wir selbst rendern mussten, überleben das Auf- und Zuklappen
 * der Karte. Ohne das kostet jedes zweite Antippen einer Kikiri-Stimme wieder
 * eine volle Synthese, obwohl sich nichts geändert hat.
 */
const sampleCache = new Map<string, Blob>();

/**
 * Es läuft immer nur eine Probe.
 *
 * Jede Kachel bringt ihren eigenen Player mit; ohne diese Schranke stapeln sich
 * bei sechzig Stimmen beliebig viele gleichzeitig laufende Stimmen übereinander,
 * sobald jemand zwei Play-Tasten hintereinander trifft.
 */
let currentStop: (() => void) | null = null;

function takeStage(stop: () => void) {
  if (currentStop && currentStop !== stop) currentStop();
  currentStop = stop;
}

export default function VoiceCard({ voice, index, onDeleted }: VoiceCardProps) {
  const audio = useAudio();
  const genAudio = useAudio();
  const [expanded, setExpanded] = useState(false);
  const [testText, setTestText] = useState(SAMPLE_TEXT);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [loadingPreview, setLoadingPreview] = useState(false);

  const { stop: stopAudio } = audio;
  const { stop: stopGenAudio } = genAudio;
  const stopSelf = useCallback(() => {
    stopAudio();
    stopGenAudio();
  }, [stopAudio, stopGenAudio]);

  useEffect(() => () => {
    if (currentStop === stopSelf) currentStop = null;
  }, [stopSelf]);

  const isOmni = voice.source === 'omnivoice';
  // Nur OmniVoice hat ein Referenzpaar auf der Platte. Für Kikiri gibt es nichts
  // zum Abspielen — dort ist die Probe eine echte, kurze Synthese.
  const hasReference = isOmni;
  // Löschen greift nur bei OmniVoice: Kikiri-Stimmen sind Modelldateien des
  // Workers, kein Nutzerinhalt, und die API antwortet dort mit 404.
  const canDelete = isOmni;

  const play = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (audio.currentId === voice.id) {
      takeStage(stopSelf);
      audio.toggle();
      return;
    }
    takeStage(stopSelf);
    setLoadingPreview(true);
    setError('');
    try {
      const cached = sampleCache.get(voice.id);
      if (cached) {
        audio.play(cached, voice.id);
      } else if (hasReference) {
        const blob = await getVoiceAudio(voice.id);
        sampleCache.set(voice.id, blob);
        audio.play(blob, voice.id);
      } else {
        const { blob } = await generate({ text: SAMPLE_TEXT, voice_id: voice.id });
        sampleCache.set(voice.id, blob);
        audio.play(blob, voice.id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Probe nicht verfügbar');
      setExpanded(true);
    }
    setLoadingPreview(false);
  };

  const handleGenerate = async () => {
    if (!testText.trim()) return;
    takeStage(stopSelf);
    setGenerating(true);
    setError('');
    try {
      const { blob } = await generate({ text: testText, voice_id: voice.id });
      genAudio.play(blob, 'gen-' + voice.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Synthese fehlgeschlagen');
    }
    setGenerating(false);
  };

  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirmDelete) {
      setConfirmDelete(true);
      setTimeout(() => setConfirmDelete(false), 4000);
      return;
    }
    try {
      await deleteVoice(voice.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Löschen fehlgeschlagen');
      setExpanded(true);
      setConfirmDelete(false);
    }
  };

  const isPlaying = audio.playing && audio.currentId === voice.id;
  const initial = voice.name.trim().charAt(0).toUpperCase() || '?';

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.015, 0.18), duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
      onClick={() => setExpanded(!expanded)}
      className={`voice-tile${expanded ? ' voice-tile-open' : ''}`}
      role="button"
      tabIndex={0}
      aria-expanded={expanded}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          setExpanded((v) => !v);
        }
      }}
    >
      <div className="voice-tile-head">
        <span className={`voice-tile-avatar${isOmni ? ' voice-tile-avatar-omni' : ''}`}>{initial}</span>

        <span className="voice-tile-identity">
          <span className="voice-tile-name">{voice.name}</span>
          <span className="voice-tile-meta">{describeVoice(voice)}</span>
        </span>

        <button
          type="button"
          onClick={play}
          disabled={loadingPreview}
          aria-label={isPlaying ? 'Probe pausieren' : 'Probe anhören'}
          title={hasReference ? 'Referenzaufnahme anhören' : 'Kurze Probe synthetisieren'}
          className={`voice-tile-play${isPlaying ? ' voice-tile-play-active' : ''}`}
        >
          {loadingPreview ? (
            <motion.span
              animate={{ rotate: 360 }}
              transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
              style={{ lineHeight: 0 }}
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                <path d="M7 1a6 6 0 105.2 3" />
              </svg>
            </motion.span>
          ) : isPlaying ? (
            <WaveformBars active size="sm" bars={4} color="text" />
          ) : (
            <svg width="12" height="12" viewBox="0 0 14 14" fill="currentColor">
              <path d="M4 1.5v11l8-5.5z" />
            </svg>
          )}
        </button>

        <svg
          width="10"
          height="6"
          viewBox="0 0 10 6"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
          className="voice-tile-chevron"
          style={{ transform: expanded ? 'rotate(180deg)' : 'none' }}
        >
          <path d="M1 1l4 4 4-4" />
        </svg>
      </div>

      <AnimatePresence>
        {audio.currentId === voice.id && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.22, ease: [0.25, 0.46, 0.45, 0.94] }}
            style={{ overflow: 'hidden' }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="voice-tile-player">
              <AudioPlayer
                playing={audio.playing}
                progress={audio.progress}
                duration={audio.duration}
                onToggle={audio.toggle}
                onSeek={audio.seek}
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.24, ease: [0.25, 0.46, 0.45, 0.94] }}
            style={{ overflow: 'hidden' }}
          >
            <div className="voice-tile-body" onClick={(e) => e.stopPropagation()}>
              {error && <div className="voice-tile-error">{error}</div>}

              {voice.ref_text && (
                <div className="voice-tile-quote">
                  <span className="speech-field-label">Referenztext</span>
                  <p>„{voice.ref_text}“</p>
                </div>
              )}
              {voice.notes && !voice.ref_text && <p className="voice-tile-note">{voice.notes}</p>}

              <textarea
                value={testText}
                onChange={(e) => setTestText(e.target.value)}
                rows={2}
                className="input-field"
                style={{
                  width: '100%',
                  padding: '10px 12px',
                  fontSize: '13px',
                  resize: 'vertical',
                  lineHeight: 1.5,
                  fontFamily: 'var(--font-body)',
                  boxSizing: 'border-box',
                  height: 'auto',
                }}
                placeholder="Text für die Probe…"
              />

              <div className="voice-tile-actions">
                <button
                  type="button"
                  onClick={handleGenerate}
                  disabled={generating || !testText.trim()}
                  className="btn btn-primary"
                  style={{ flex: 1, height: '36px', fontSize: '13px', gap: '8px' }}
                >
                  {generating ? (
                    <>
                      <motion.span
                        animate={{ rotate: 360 }}
                        transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                        style={{ lineHeight: 0 }}
                      >
                        <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                          <path d="M7 1a6 6 0 105.2 3" />
                        </svg>
                      </motion.span>
                      Synthetisiere…
                    </>
                  ) : (
                    <>
                      <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M2 7h10M8 3l4 4-4 4" />
                      </svg>
                      Vorschau erzeugen
                    </>
                  )}
                </button>

                {canDelete && (
                  <button
                    type="button"
                    onClick={handleDelete}
                    className={`voice-tile-delete${confirmDelete ? ' voice-tile-delete-armed' : ''}`}
                    title={
                      confirmDelete
                        ? 'Wirklich löschen — entfernt Aufnahme und Transkript'
                        : 'Stimme löschen'
                    }
                  >
                    {confirmDelete ? (
                      'Wirklich löschen?'
                    ) : (
                      <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                        <path d="M2 3.5h10M5.5 3.5V2h3v1.5M3.5 3.5v8a1 1 0 001 1h5a1 1 0 001-1v-8" />
                      </svg>
                    )}
                  </button>
                )}
              </div>

              <AnimatePresence>
                {genAudio.currentId === 'gen-' + voice.id && (
                  <motion.div
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: 4 }}
                    transition={{ duration: 0.22 }}
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

              {isFallbackVoice(voice) && (
                <p className="voice-tile-note">
                  Fallback-Stimme: läuft auf der CPU und wird nur genutzt, wenn OmniVoice nicht rendern kann.
                </p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
