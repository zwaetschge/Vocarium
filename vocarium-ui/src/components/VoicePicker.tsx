import { useEffect, useMemo, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import type { Voice } from '../types';
import { DEFAULT_VOICE, describeVoice, groupEngineVoices, voiceSourceLabel } from '../voiceUtils';

interface Props {
  voices: Voice[];
  value: string;
  onChange: (voiceId: string) => void;
  /** Zeigt den eingebauten `default`-Eintrag über den Gruppen. */
  includeDefault?: boolean;
  disabled?: boolean;
}

/**
 * Stimmenauswahl mit Suche.
 *
 * Bewusst kein `<select>` mit `<VoiceOptions>`: mit rund sechzig Stimmen ist
 * eine Liste ohne Suchfeld und ohne Herkunftsangabe unbenutzbar. Die
 * Gruppierung kommt trotzdem aus `groupEngineVoices` — derselben Quelle, aus
 * der `VoiceOptions` seine `<optgroup>`-Struktur baut —, damit beide Picker
 * dieselbe Rangfolge zeigen: OmniVoice zuerst, der Kikiri-Fallback darunter.
 */
export default function VoicePicker({ voices, value, onChange, includeDefault = true, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const groups = useMemo(() => groupEngineVoices(voices), [voices]);
  const all = useMemo(
    () => (includeDefault ? [DEFAULT_VOICE, ...groups.flatMap((g) => g.voices)] : groups.flatMap((g) => g.voices)),
    [groups, includeDefault],
  );
  const selected = all.find((v) => v.id === value);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const match = (v: Voice) =>
      !q ||
      v.name.toLowerCase().includes(q) ||
      v.id.toLowerCase().includes(q) ||
      (v.gender || '').toLowerCase().includes(q) ||
      (v.backend || '').toLowerCase().includes(q) ||
      voiceSourceLabel(v).toLowerCase().includes(q);
    return groups
      .map((g) => ({ ...g, voices: g.voices.filter(match) }))
      .filter((g) => g.voices.length > 0);
  }, [groups, query]);

  const defaultMatches = useMemo(() => {
    const q = query.trim().toLowerCase();
    return includeDefault && (!q || DEFAULT_VOICE.name.toLowerCase().includes(q) || 'default'.includes(q));
  }, [includeDefault, query]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    requestAnimationFrame(() => searchRef.current?.focus());
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const pick = (id: string) => {
    onChange(id);
    setOpen(false);
    setQuery('');
  };

  const totalVisible = filtered.reduce((n, g) => n + g.voices.length, 0) + (defaultMatches ? 1 : 0);

  return (
    <div ref={rootRef} style={{ position: 'relative' }}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="voice-picker-trigger"
      >
        <span className="voice-picker-trigger-body">
          <span className="voice-picker-trigger-name">{selected ? selected.name : 'Stimme wählen'}</span>
          <span className="voice-picker-trigger-meta">
            {selected ? describeVoice(selected) : `${all.length} verfügbar`}
          </span>
        </span>
        <svg width="10" height="6" viewBox="0 0 10 6" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" style={{ opacity: 0.5, flexShrink: 0 }}>
          <path d="M1 1l4 4 4-4" />
        </svg>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.985 }}
            transition={{ duration: 0.14, ease: [0.25, 0.46, 0.45, 0.94] }}
            className="glass-strong voice-picker-panel"
            role="listbox"
          >
            <div className="voice-picker-search">
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" style={{ opacity: 0.45 }}>
                <circle cx="7" cy="7" r="4.5" />
                <path d="M10.5 10.5L14 14" />
              </svg>
              <input
                ref={searchRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Stimme suchen…"
                aria-label="Stimme suchen"
              />
              {query && (
                <button type="button" onClick={() => setQuery('')} aria-label="Suche leeren" className="btn-ghost" style={{ padding: 2, borderRadius: 6, lineHeight: 0 }}>
                  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                    <path d="M2 2l8 8M10 2l-8 8" />
                  </svg>
                </button>
              )}
            </div>

            <div className="voice-picker-list">
              {defaultMatches && (
                <VoiceRow voice={DEFAULT_VOICE} active={value === DEFAULT_VOICE.id} onPick={pick} />
              )}
              {filtered.map((group) => (
                <div key={group.label}>
                  {/* Ohne den Hinweis: im 300 px schmalen Panel kürzt er sonst den
                      Gruppennamen weg, und jede Zeile darunter nennt Engine und
                      Gerät ohnehin schon. */}
                  <div className="voice-picker-group">
                    <span>{group.label}</span>
                    <span className="voice-picker-group-hint">{group.voices.length}</span>
                  </div>
                  {group.voices.map((v) => (
                    <VoiceRow key={v.id} voice={v} active={v.id === value} onPick={pick} />
                  ))}
                </div>
              ))}
              {totalVisible === 0 && (
                <div style={{ padding: '18px 14px', fontSize: '12.5px', color: 'var(--color-text-dim)', textAlign: 'center' }}>
                  Keine Stimme passt zu „{query}“.
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function VoiceRow({ voice, active, onPick }: { voice: Voice; active: boolean; onPick: (id: string) => void }) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      onClick={() => onPick(voice.id)}
      className={`voice-picker-row${active ? ' voice-picker-row-active' : ''}`}
    >
      <span className="voice-picker-row-main">
        <span className="voice-picker-row-name">{voice.name}</span>
        <span className="voice-picker-row-meta">{describeVoice(voice)}</span>
      </span>
      {active && (
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--color-accent-hover)', flexShrink: 0 }}>
          <path d="M3 8.5l3.5 3.5L13 4.5" />
        </svg>
      )}
    </button>
  );
}
