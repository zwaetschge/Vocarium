import { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { getSpeakers, previewCustomVoice, saveCustomVoice, getLanguages } from '../api';
import type { Speaker } from '../types';
import AudioPlayer from '../components/AudioPlayer';
import { useAudio } from '../hooks/useAudio';
import { getDefaultSample, isKnownSample } from '../sampleTexts';

const GENDER_COLORS: Record<string, { bg: string; fg: string; border: string }> = {
  female: {
    bg: 'rgba(255, 77, 210, 0.12)',
    fg: 'rgba(255, 155, 225, 0.95)',
    border: 'rgba(255, 77, 210, 0.28)',
  },
  male: {
    bg: 'rgba(50, 181, 255, 0.12)',
    fg: 'rgba(140, 210, 255, 0.95)',
    border: 'rgba(50, 181, 255, 0.28)',
  },
};

export default function CustomPage() {
  const navigate = useNavigate();
  const audio = useAudio();

  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [selectedSpeaker, setSelectedSpeaker] = useState<string | null>(null);
  const [instruct, setInstruct] = useState('');
  const [language, setLanguage] = useState('English');
  const [languages, setLanguages] = useState<string[]>([]);
  const [sampleText, setSampleText] = useState(getDefaultSample('English'));
  const [previewing, setPreviewing] = useState(false);
  const [hasPreviewed, setHasPreviewed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showNameInput, setShowNameInput] = useState(false);
  const [voiceName, setVoiceName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loadingSpeakers, setLoadingSpeakers] = useState(true);

  useEffect(() => {
    getSpeakers()
      .then((list) => {
        setSpeakers(list);
        if (list.length > 0) {
          setSelectedSpeaker(list[0].id);
          setLanguage(list[0].language_hint || 'English');
        }
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load speakers'))
      .finally(() => setLoadingSpeakers(false));
  }, []);

  useEffect(() => {
    getLanguages().then(setLanguages).catch(() => setLanguages(['English', 'Chinese']));
  }, []);

  useEffect(() => {
    setSampleText((prev) => (isKnownSample(prev) ? getDefaultSample(language) : prev));
  }, [language]);

  const selected = speakers.find((s) => s.id === selectedSpeaker) ?? null;

  const handleSelectSpeaker = (speaker: Speaker) => {
    setSelectedSpeaker(speaker.id);
    setLanguage(speaker.language_hint || 'English');
    setHasPreviewed(false);
    setShowNameInput(false);
  };

  const handlePreview = async () => {
    if (!selectedSpeaker || !sampleText.trim()) return;
    setPreviewing(true);
    setError(null);
    try {
      const blob = await previewCustomVoice({
        text: sampleText,
        speaker: selectedSpeaker,
        language,
        instruct: instruct.trim() || null,
      });
      audio.play(blob, 'custom-preview');
      setHasPreviewed(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Preview failed');
    }
    setPreviewing(false);
  };

  const handleSave = async () => {
    if (!voiceName.trim() || !selectedSpeaker) return;
    setSaving(true);
    setError(null);
    try {
      await saveCustomVoice({
        name: voiceName.trim(),
        speaker: selectedSpeaker,
        instruct: instruct.trim() || null,
        language,
      });
      navigate('/voices');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed');
    }
    setSaving(false);
  };

  const canPreview = !!selectedSpeaker && sampleText.trim().length > 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
      style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}
    >
      {/* Intro strip */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '16px' }}>
        <p style={{ fontSize: '13.5px', color: 'var(--color-text-secondary)', maxWidth: '620px', lineHeight: 1.55 }}>
          Pick one of the built-in speakers and optionally steer the performance with a free-text
          instruction (tone, pace, emotion). Save presets to your library to reuse them for podcasts.
        </p>
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '8px',
            padding: '6px 12px',
            borderRadius: '999px',
            background: 'rgba(123,97,255,0.08)',
            border: '1px solid rgba(123,97,255,0.22)',
            fontSize: '10.5px',
            fontFamily: 'var(--font-mono)',
            color: 'var(--color-accent)',
            letterSpacing: 0,
            textTransform: 'uppercase',
            flexShrink: 0,
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
          Auto-loads CustomVoice 1.7B
        </div>
      </div>

      {/* Speaker grid */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
          <label className="label-eyebrow">Prebuilt speakers</label>
          <span
            style={{
              fontSize: '10.5px',
              fontFamily: 'var(--font-mono)',
              color: 'var(--color-text-dim)',
              letterSpacing: 0,
            }}
          >
            {speakers.length} available
          </span>
        </div>

        {loadingSpeakers ? (
          <div
            style={{
              padding: '24px',
              textAlign: 'center',
              fontSize: '13px',
              color: 'var(--color-text-dim)',
            }}
          >
            Loading speakers…
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(170px, 1fr))', gap: '10px' }}>
            {speakers.map((sp, i) => {
              const active = selectedSpeaker === sp.id;
              const tone = GENDER_COLORS[sp.gender] ?? GENDER_COLORS.female;
              return (
                <motion.button
                  key={sp.id}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.04 * i + 0.06, duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
                  whileHover={{ y: -2 }}
                  whileTap={{ scale: 0.97 }}
                  onClick={() => handleSelectSpeaker(sp)}
                  className="card-subtle"
                  style={{
                    padding: '14px 14px',
                    textAlign: 'left',
                    cursor: 'pointer',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '8px',
                    background: active ? 'rgba(123,97,255,0.12)' : 'rgba(255,255,255,0.025)',
                    border: active
                      ? '1px solid rgba(123,97,255,0.4)'
                      : '1px solid rgba(255,255,255,0.08)',
                    boxShadow: active
                      ? 'inset 0 1px 0 rgba(255,255,255,0.1), 0 6px 20px rgba(123,97,255,0.18)'
                      : undefined,
                    transition: 'background 0.18s, border-color 0.18s, box-shadow 0.18s',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <div
                      style={{
                        width: '28px',
                        height: '28px',
                        borderRadius: '50%',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        background: tone.bg,
                        border: `1px solid ${tone.border}`,
                        color: tone.fg,
                        fontSize: '12px',
                        fontWeight: 600,
                        textTransform: 'uppercase',
                        flexShrink: 0,
                      }}
                    >
                      {sp.name.charAt(0)}
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '2px', minWidth: 0 }}>
                      <div
                        style={{
                          fontSize: '13px',
                          fontWeight: 500,
                          color: 'var(--color-text)',
                          letterSpacing: 0,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {sp.name}
                      </div>
                      <div
                        style={{
                          fontSize: '10.5px',
                          fontFamily: 'var(--font-mono)',
                          color: 'var(--color-text-dim)',
                          letterSpacing: 0,
                          textTransform: 'uppercase',
                        }}
                      >
                        {sp.gender} · {sp.language_hint}
                      </div>
                    </div>
                  </div>
                </motion.button>
              );
            })}
          </div>
        )}
      </div>

      {/* Steering prompt */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
          <label className="label-eyebrow">Steering prompt (optional)</label>
          <span
            style={{
              fontSize: '10.5px',
              fontFamily: 'var(--font-mono)',
              color: 'var(--color-text-dim)',
              fontVariantNumeric: 'tabular-nums',
              letterSpacing: 0,
            }}
          >
            {instruct.length} chars
          </span>
        </div>
        <textarea
          value={instruct}
          onChange={(e) => setInstruct(e.target.value)}
          rows={3}
          className="input-field"
          style={{ fontSize: '14px', resize: 'none', lineHeight: 1.55, fontFamily: 'var(--font-body)' }}
          placeholder="e.g. Speak with warm enthusiasm, slightly slower than normal, like telling a story to a friend."
        />
      </div>

      {/* Sample text + Language */}
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: '16px' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <label className="label-eyebrow">Sample text</label>
          <textarea
            value={sampleText}
            onChange={(e) => setSampleText(e.target.value)}
            rows={2}
            className="input-field"
            style={{ resize: 'none', fontFamily: 'var(--font-body)' }}
          />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <label className="label-eyebrow">Language</label>
          <div style={{ position: 'relative' }}>
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              className="input-field"
              style={{ appearance: 'none', cursor: 'pointer', paddingRight: '36px' }}
            >
              {languages.map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
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
      </div>

      {/* Actions */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <motion.button
          whileTap={{ scale: 0.98 }}
          whileHover={canPreview && !previewing ? { y: -1 } : undefined}
          transition={{ type: 'spring', stiffness: 300, damping: 22 }}
          onClick={handlePreview}
          disabled={previewing || !canPreview}
          className="btn btn-primary"
          style={{
            padding: '12px 26px',
            fontSize: '13px',
            fontWeight: 600,
            display: 'inline-flex',
            alignItems: 'center',
            gap: '10px',
            cursor: previewing || !canPreview ? 'not-allowed' : 'pointer',
            opacity: previewing || !canPreview ? 0.45 : 1,
            letterSpacing: 0,
          }}
        >
          {previewing ? (
            <>
              <motion.div
                animate={{ rotate: 360 }}
                transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                style={{ display: 'flex' }}
              >
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                  <path d="M7 1a6 6 0 105.2 3" />
                </svg>
              </motion.div>
              Generating…
            </>
          ) : (
            <>
              <svg width="13" height="13" viewBox="0 0 14 14" fill="currentColor">
                <path d="M3 1.5v11l9-5.5z" />
              </svg>
              {hasPreviewed ? 'Re-preview' : selected ? `Preview ${selected.name}` : 'Preview voice'}
            </>
          )}
        </motion.button>

        <AnimatePresence>
          {hasPreviewed && !showNameInput && (
            <motion.button
              initial={{ opacity: 0, x: -8, scale: 0.95 }}
              animate={{ opacity: 1, x: 0, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              transition={{ type: 'spring', stiffness: 400, damping: 28 }}
              whileTap={{ scale: 0.97 }}
              whileHover={{ y: -1 }}
              onClick={() => setShowNameInput(true)}
              style={{
                padding: '12px 22px',
                fontSize: '13px',
                fontWeight: 600,
                borderRadius: '12px',
                border: '1px solid rgba(123,97,255,0.35)',
                background: 'rgba(123,97,255,0.08)',
                color: 'var(--color-accent)',
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                gap: '8px',
                letterSpacing: 0,
                backdropFilter: 'blur(14px)',
                WebkitBackdropFilter: 'blur(14px)',
                transition: 'background 0.15s, border-color 0.15s',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(123,97,255,0.14)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'rgba(123,97,255,0.08)'; }}
            >
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 3h7l3 3v7a1 1 0 01-1 1H4a1 1 0 01-1-1V4a1 1 0 011-1z" />
                <path d="M6 3v3h4V3" />
              </svg>
              Save to library
            </motion.button>
          )}
        </AnimatePresence>
      </div>

      {/* Audio player */}
      <AnimatePresence>
        {audio.currentId === 'custom-preview' && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
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

      {/* Save name input */}
      <AnimatePresence>
        {showNameInput && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] }}
            style={{ overflow: 'hidden' }}
          >
            <div
              className="card-subtle"
              style={{
                display: 'flex',
                alignItems: 'flex-end',
                gap: '12px',
                padding: '18px 20px',
                borderColor: 'rgba(123,97,255,0.25)',
              }}
            >
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '8px' }}>
                <label
                  style={{
                    fontSize: '11.5px',
                    fontWeight: 500,
                    color: 'var(--color-text-secondary)',
                    letterSpacing: 0,
                  }}
                >
                  Voice name
                </label>
                <input
                  type="text"
                  value={voiceName}
                  onChange={(e) => setVoiceName(e.target.value)}
                  autoFocus
                  className="input-field"
                  placeholder={selected ? `${selected.name} preset` : 'Name your custom voice'}
                  onKeyDown={(e) => e.key === 'Enter' && handleSave()}
                />
              </div>
              <motion.button
                whileTap={{ scale: 0.97 }}
                whileHover={!saving && voiceName.trim() ? { y: -1 } : undefined}
                onClick={handleSave}
                disabled={saving || !voiceName.trim()}
                className="btn btn-primary"
                style={{
                  padding: '12px 22px',
                  fontSize: '13px',
                  fontWeight: 600,
                  cursor: saving || !voiceName.trim() ? 'not-allowed' : 'pointer',
                  opacity: saving || !voiceName.trim() ? 0.45 : 1,
                  whiteSpace: 'nowrap',
                  letterSpacing: 0,
                }}
              >
                {saving ? (
                  <>
                    <motion.div
                      animate={{ rotate: 360 }}
                      transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                      style={{ display: 'inline-flex', marginRight: '8px' }}
                    >
                      <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                        <path d="M7 1a6 6 0 105.2 3" />
                      </svg>
                    </motion.div>
                    Saving…
                  </>
                ) : (
                  'Save voice'
                )}
              </motion.button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Error */}
      <AnimatePresence>
        {error && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
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
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              style={{ flexShrink: 0 }}
            >
              <circle cx="8" cy="8" r="6" />
              <path d="M8 5.5v3M8 10.5h.01" />
            </svg>
            <span style={{ flex: 1 }}>{error}</span>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
