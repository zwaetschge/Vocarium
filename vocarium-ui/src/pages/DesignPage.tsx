import { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { designPreview, designSave, getLanguages } from '../api';
import AudioPlayer from '../components/AudioPlayer';
import { useAudio } from '../hooks/useAudio';
import { getDefaultSample, isKnownSample } from '../sampleTexts';

const placeholders = [
  'A warm female narrator with gentle pace, suitable for audiobooks…',
  'A deep male announcer with authoritative tone for news broadcasting…',
  'An energetic young male with dynamic rhythm for podcast hosting…',
  'A calm, soothing female voice for meditation and relaxation apps…',
  'A crisp, professional voice for corporate training materials…',
];

const presets = [
  {
    title: 'Audiobook narrator',
    desc: 'A warm female narrator with gentle pace, suitable for audiobooks',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 4h2v12H3zM7 2h6v16H7zM15 4h2v12h-2z" />
      </svg>
    ),
  },
  {
    title: 'News anchor',
    desc: 'A deep male announcer with authoritative tone for news broadcasting',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="10" cy="7" r="4" />
        <path d="M3 18v-1a5 5 0 015-5h4a5 5 0 015 5v1" />
      </svg>
    ),
  },
  {
    title: 'Podcast host',
    desc: 'An energetic young male with dynamic rhythm for podcast hosting',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="6" y="1" width="8" height="11" rx="4" />
        <path d="M3 9a7 7 0 0014 0M10 16v3" />
      </svg>
    ),
  },
  {
    title: 'Meditation guide',
    desc: 'A calm, soothing female voice for meditation and relaxation apps',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="10" cy="10" r="7" />
        <path d="M10 6v4l2.5 2.5" />
      </svg>
    ),
  },
];

export default function DesignPage() {
  const navigate = useNavigate();
  const audio = useAudio();

  const [description, setDescription] = useState('');
  const [sampleText, setSampleText] = useState(getDefaultSample('English'));
  const [language, setLanguage] = useState('English');
  const [languages, setLanguages] = useState<string[]>([]);
  const [previewing, setPreviewing] = useState(false);
  const [hasPreviewed, setHasPreviewed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showNameInput, setShowNameInput] = useState(false);
  const [voiceName, setVoiceName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [placeholderIdx, setPlaceholderIdx] = useState(0);
  const intervalRef = useRef<ReturnType<typeof setInterval>>();

  useEffect(() => {
    getLanguages().then(setLanguages).catch(() => setLanguages(['English', 'Chinese']));
  }, []);

  useEffect(() => {
    setSampleText((prev) => (isKnownSample(prev) ? getDefaultSample(language) : prev));
  }, [language]);

  useEffect(() => {
    intervalRef.current = setInterval(() => {
      setPlaceholderIdx((i) => (i + 1) % placeholders.length);
    }, 3000);
    return () => clearInterval(intervalRef.current);
  }, []);

  const handlePreview = async () => {
    if (!description.trim() || !sampleText.trim()) return;
    setPreviewing(true);
    setError(null);
    try {
      const blob = await designPreview({ text: sampleText, description, language });
      audio.play(blob, 'design-preview');
      setHasPreviewed(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Preview failed');
    }
    setPreviewing(false);
  };

  const handleSave = async () => {
    if (!voiceName.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await designSave({ name: voiceName.trim(), description, text: sampleText, language });
      navigate('/voices');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed');
    }
    setSaving(false);
  };

  const canPreview = description.trim().length > 0 && sampleText.trim().length > 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
      style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}
    >
      {/* Intro strip */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '16px' }}>
        <p style={{ fontSize: '13.5px', color: 'var(--color-text-secondary)', maxWidth: '560px', lineHeight: 1.55 }}>
          Describe the voice you envision. The designer composes a unique voice matching your description, no reference audio required.
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
          Auto-loads Design 1.7B
        </div>
      </div>

      {/* Description textarea */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
          <label className="label-eyebrow">Voice description</label>
          <span
            style={{
              fontSize: '10.5px',
              fontFamily: 'var(--font-mono)',
              color: 'var(--color-text-dim)',
              fontVariantNumeric: 'tabular-nums',
              letterSpacing: 0,
            }}
          >
            {description.length} chars
          </span>
        </div>
        <div style={{ position: 'relative' }}>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
            className="input-field"
            style={{
              fontSize: '15px',
              resize: 'none',
              lineHeight: 1.55,
              fontFamily: 'var(--font-body)',
            }}
            placeholder={placeholders[placeholderIdx]}
          />
        </div>
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
              <motion.div animate={{ rotate: 360 }} transition={{ duration: 1, repeat: Infinity, ease: 'linear' }} style={{ display: 'flex' }}>
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
              {hasPreviewed ? 'Re-preview' : 'Preview voice'}
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
        {audio.currentId === 'design-preview' && (
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
                  placeholder="Name your designed voice"
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
                    <motion.div animate={{ rotate: 360 }} transition={{ duration: 1, repeat: Infinity, ease: 'linear' }} style={{ display: 'inline-flex', marginRight: '8px' }}>
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
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
              <circle cx="8" cy="8" r="6" />
              <path d="M8 5.5v3M8 10.5h.01" />
            </svg>
            <span style={{ flex: 1 }}>{error}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Preset cards */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', paddingTop: '4px' }}>
        <div className="label-eyebrow">Starter descriptions</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: '12px' }}>
          {presets.map((preset, i) => (
            <motion.button
              key={preset.title}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.08 * i + 0.1, duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
              whileHover={{ y: -2 }}
              whileTap={{ scale: 0.98 }}
              onClick={() => setDescription(preset.desc)}
              className="card-subtle"
              style={{
                textAlign: 'left',
                padding: '16px',
                cursor: 'pointer',
                background: 'rgba(255,255,255,0.025)',
                transition: 'box-shadow 0.18s, border-color 0.18s',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.boxShadow = '0 8px 28px rgba(0,0,0,0.3), 0 0 0 1px rgba(123,97,255,0.25), inset 0 1px 0 rgba(255,255,255,0.06)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.boxShadow = '';
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
                <div
                  style={{
                    width: '30px',
                    height: '30px',
                    borderRadius: '8px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: 'rgba(123,97,255,0.12)',
                    border: '1px solid rgba(123,97,255,0.22)',
                    color: 'var(--color-accent)',
                    flexShrink: 0,
                  }}
                >
                  {preset.icon}
                </div>
                <div
                  style={{
                    fontSize: '13px',
                    fontWeight: 500,
                    color: 'var(--color-text)',
                    letterSpacing: 0,
                  }}
                >
                  {preset.title}
                </div>
              </div>
              <p
                style={{
                  fontSize: '12px',
                  lineHeight: 1.55,
                  color: 'var(--color-text-secondary)',
                }}
              >
                {preset.desc}
              </p>
            </motion.button>
          ))}
        </div>
      </div>
    </motion.div>
  );
}
