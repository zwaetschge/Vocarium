import Dialog from '../components/Dialog';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import {
  getHosts,
  createHost,
  updateHost,
  deleteHost,
  getHostPresets,
  createHostFromPreset,
  getVoices,
} from '../api';
import type { Host, HostPreset, HostPresetCategory, HostRole, Voice } from '../types';
import { VoiceOptions } from '../components/VoiceOptions';
import { engineVoices as listEngineVoices } from '../voiceUtils';

/**
 * Host-Hub: die 40 fertigen Persönlichkeiten links, die eigene Besetzung rechts.
 *
 * Ein Preset ist nur eine Vorlage — übernommen wird eine echte Kopie, die
 * danach frei editierbar ist. Deshalb bleibt ein Preset auch nach dem
 * Übernehmen sichtbar (nur mit Häkchen), statt aus dem Katalog zu verschwinden.
 */
export default function PodcastHostsPage() {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [presets, setPresets] = useState<HostPreset[]>([]);
  const [categories, setCategories] = useState<HostPresetCategory[]>([]);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [category, setCategory] = useState<string>('all');
  const [search, setSearch] = useState('');
  const [adding, setAdding] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [manualOpen, setManualOpen] = useState(false);

  const engineVoices = useMemo(() => listEngineVoices(voices), [voices]);

  const load = async () => {
    setLoading(true);
    try {
      const [h, p, v] = await Promise.all([getHosts(), getHostPresets(), getVoices()]);
      setHosts(h);
      setPresets(p.presets);
      setCategories(p.categories);
      setVoices(v);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Laden fehlgeschlagen');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const takenNames = useMemo(() => new Set(hosts.map((h) => h.name)), [hosts]);

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    return presets.filter((p) => {
      if (category !== 'all' && p.category !== category) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) ||
        p.tagline.toLowerCase().includes(q) ||
        p.personality.toLowerCase().includes(q) ||
        p.speaking_style.toLowerCase().includes(q)
      );
    });
  }, [presets, category, search]);

  const handleAdopt = async (preset: HostPreset) => {
    setAdding(preset.id);
    try {
      const host = await createHostFromPreset(preset.id);
      setHosts((prev) => [...prev, host]);
      if (host.voice_missing) {
        setError(`${host.name} wurde ohne Stimme angelegt — „${preset.voice}“ ist gerade nicht verfügbar.`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Übernehmen fehlgeschlagen');
    } finally {
      setAdding(null);
    }
  };

  const handleVoiceChange = async (id: string, voiceId: string) => {
    const before = hosts;
    setHosts((prev) => prev.map((h) => (h.id === id ? { ...h, voice_id: voiceId } : h)));
    try {
      await updateHost(id, { voice_id: voiceId });
    } catch (e) {
      setHosts(before);
      setError(e instanceof Error ? e.message : 'Stimme konnte nicht geändert werden');
    }
  };

  const handleDelete = async (host: Host) => {
    if (!confirm(`„${host.name}“ aus der Besetzung entfernen?`)) return;
    try {
      await deleteHost(host.id);
      setHosts((prev) => prev.filter((h) => h.id !== host.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Löschen fehlgeschlagen');
    }
  };

  const countFor = (id: string) =>
    id === 'all' ? presets.length : presets.filter((p) => p.category === id).length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-end',
          justifyContent: 'space-between',
          gap: '16px',
          flexWrap: 'wrap',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <span className="label-eyebrow" style={{ fontSize: '10px' }}>Podcast</span>
          <h1 style={{ fontSize: '28px', color: 'var(--color-text)' }}>Host-Hub</h1>
          <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)', maxWidth: '58ch' }}>
            40 fertige Persönlichkeiten mit Charakter, Sprechstil und passender Stimme.
            Übernehmen, anpassen, in einen Podcast besetzen.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '10px' }}>
          <button className="btn btn-secondary" onClick={() => setManualOpen(true)} style={{ height: '40px' }}>
            Eigener Sprecher
          </button>
          <Link to="/podcast" className="btn btn-primary" style={{ height: '40px' }}>
            Zum Studio
          </Link>
        </div>
      </div>

      <AnimatePresence>
        {error && (
          <motion.div
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className="card-subtle"
            style={{
              padding: '12px 14px',
              fontSize: '12.5px',
              color: 'var(--color-text-secondary)',
              display: 'flex',
              justifyContent: 'space-between',
              gap: '12px',
            }}
          >
            <span>{error}</span>
            <button className="btn btn-ghost btn-sm" onClick={() => setError('')}>
              Schließen
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="host-hub-layout">
        {/* ---- Eigene Besetzung ------------------------------------------ */}
        <aside className="host-hub-aside">
          <div className="card" style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              <span className="label-eyebrow" style={{ fontSize: '10px' }}>Meine Sprecher</span>
              <div style={{ fontSize: '15px', color: 'var(--color-text)' }}>{hosts.length} angelegt</div>
            </div>

            {hosts.length === 0 ? (
              <p style={{ fontSize: '12.5px', color: 'var(--color-text-dim)', lineHeight: 1.5 }}>
                Noch niemand besetzt. Eine Persönlichkeit aus dem Katalog übernehmen — oder oben
                einen eigenen Sprecher anlegen.
              </p>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '58vh', overflowY: 'auto' }}>
                {hosts.map((h) => {
                  const known = engineVoices.some((v) => v.id === h.voice_id);
                  return (
                    <div key={h.id} className="host-mine-row">
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: '13px', color: 'var(--color-text)' }}>{h.name}</div>
                        {h.tagline && (
                          <div style={{ fontSize: '11px', color: 'var(--color-text-dim)' }}>{h.tagline}</div>
                        )}
                        <select
                          className="input-field"
                          value={known ? h.voice_id || '' : ''}
                          onChange={(e) => void handleVoiceChange(h.id, e.target.value)}
                          style={{ height: '30px', fontSize: '11.5px', marginTop: '6px', width: '100%' }}
                        >
                          <option value="">
                            {known ? 'Stimme wählen' : 'Stimme fehlt — bitte wählen'}
                          </option>
                          <VoiceOptions voices={engineVoices} />
                        </select>
                      </div>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => void handleDelete(h)}
                        title="Sprecher entfernen"
                      >
                        ✕
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </aside>        {/* ---- Katalog --------------------------------------------------- */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', minWidth: 0 }}>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
            <input
              className="input-field"
              placeholder="Persönlichkeit suchen …"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ flex: '1 1 200px', minWidth: '180px', height: '36px' }}
            />
            <span className="podcast-script-count">{visible.length} von {presets.length}</span>
          </div>

          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
            {[{ id: 'all', label: 'Alle', description: '' }, ...categories].map((c) => (
              <button
                key={c.id}
                className={`host-chip${category === c.id ? ' host-chip-active' : ''}`}
                onClick={() => setCategory(c.id)}
                title={c.description || undefined}
              >
                {c.label}
                <span className="host-chip-count">{countFor(c.id)}</span>
              </button>
            ))}
          </div>

          {loading ? (
            <div style={{ padding: '32px', textAlign: 'center', fontSize: '13px', color: 'var(--color-text-dim)' }}>
              Lade Persönlichkeiten …
            </div>
          ) : visible.length === 0 ? (
            <div className="card-subtle" style={{ padding: '24px', textAlign: 'center', fontSize: '13px', color: 'var(--color-text-dim)' }}>
              Keine Persönlichkeit passt zu „{search}“.
            </div>
          ) : (
            <div className="host-preset-grid">
              {visible.map((p) => {
                const isTaken = takenNames.has(p.name);
                const isOpen = expanded === p.id;
                return (
                  <motion.div
                    key={p.id}
                    layout
                    className={`host-preset-card${isTaken ? ' host-preset-card-taken' : ''}`}
                  >
                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                      <div className={`host-avatar host-avatar-${p.gender}`} aria-hidden>
                        {p.name.split(' ').map((w) => w[0]).slice(0, 2).join('')}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className="host-preset-name">{p.name}</div>
                        <div className="host-preset-tagline">{p.tagline}</div>
                      </div>
                      {isTaken && <span className="host-preset-badge">✓ dabei</span>}
                    </div>

                    <p className="host-preset-body">
                      {isOpen ? p.personality : `${p.personality.slice(0, 96)}${p.personality.length > 96 ? '…' : ''}`}
                    </p>

                    {isOpen && (
                      <p className="host-preset-style">
                        <span className="label-eyebrow" style={{ fontSize: '9px' }}>Sprechstil</span>
                        <br />
                        {p.speaking_style}
                      </p>
                    )}

                    <div className="host-preset-meta">
                      <span className={`host-role-tag${p.role === 'expert' ? ' host-role-tag-expert' : ''}`}>
                        {p.role === 'expert' ? 'Gast' : 'Moderation'}
                      </span>
                      <span
                        className="host-voice-tag"
                        title={p.voice_available ? undefined : 'Diese Stimme ist gerade nicht verfügbar'}
                      >
                        {p.voice_available ? '' : '⚠ '}
                        {p.voice}
                      </span>
                    </div>

                    <div style={{ display: 'flex', gap: '6px', marginTop: 'auto' }}>
                      <button
                        className="btn btn-primary btn-sm"
                        style={{ flex: 1 }}
                        disabled={adding === p.id}
                        onClick={() => void handleAdopt(p)}
                      >
                        {adding === p.id ? 'Übernehme …' : isTaken ? 'Nochmal übernehmen' : 'Übernehmen'}
                      </button>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => setExpanded(isOpen ? null : p.id)}
                      >
                        {isOpen ? 'Weniger' : 'Mehr'}
                      </button>
                    </div>
                  </motion.div>
                );
              })}
            </div>
          )}
        </div>


      </div>

      <AnimatePresence>
        {manualOpen && (
          <ManualHostModal
            engineVoices={engineVoices}
            onClose={() => setManualOpen(false)}
            onCreated={(h) => {
              setHosts((prev) => [...prev, h]);
              setManualOpen(false);
            }}
            onError={setError}
          />
        )}
      </AnimatePresence>
    </div>
  );
}

function ManualHostModal({
  engineVoices,
  onClose,
  onCreated,
  onError,
}: {
  engineVoices: Voice[];
  onClose: () => void;
  onCreated: (host: Host) => void;
  onError: (msg: string) => void;
}) {
  const [name, setName] = useState('');
  const [tagline, setTagline] = useState('');
  const [personality, setPersonality] = useState('');
  const [speakingStyle, setSpeakingStyle] = useState('');
  const [voiceId, setVoiceId] = useState('');
  const [role, setRole] = useState<HostRole>('host');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim() || !voiceId) return;
    setBusy(true);
    try {
      const host = await createHost({
        name: name.trim(),
        tagline: tagline.trim() || undefined,
        personality: personality.trim() || undefined,
        speaking_style: speakingStyle.trim() || undefined,
        voice_id: voiceId,
        role,
      });
      onCreated(host);
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Sprecher anlegen fehlgeschlagen');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="Eigener Sprecher" onClose={onClose}>
      <div className="studio-field">
        <p style={{ fontSize: '12px', color: 'var(--color-text-dim)', lineHeight: 1.5 }}>
          Persönlichkeit und Sprechstil landen wörtlich im Skript-Prompt — je konkreter,
          desto eigener klingt die Figur.
        </p>

        <label className="label-block">
          Name
          <input className="input-field" value={name} onChange={(e) => setName(e.target.value)} placeholder="z. B. Lena Brandt" />
        </label>
        <label className="label-block">
          Kurzbeschreibung
          <input className="input-field" value={tagline} onChange={(e) => setTagline(e.target.value)} placeholder="z. B. Die warme Gastgeberin" />
        </label>
        <label className="label-block">
          Persönlichkeit
          <textarea
            className="text-input"
            rows={3}
            value={personality}
            onChange={(e) => setPersonality(e.target.value)}
            placeholder="Wer spricht hier? Haltung, Erfahrung, Eigenheiten."
          />
        </label>
        <label className="label-block">
          Sprechstil
          <input
            className="input-field"
            value={speakingStyle}
            onChange={(e) => setSpeakingStyle(e.target.value)}
            placeholder="z. B. ruhig, kurze Sätze, viele Rückfragen"
          />
        </label>

        <div style={{ display: 'flex', gap: '10px' }}>
          <label className="label-block" style={{ flex: 1 }}>
            Stimme
            <select className="input-field" value={voiceId} onChange={(e) => setVoiceId(e.target.value)}>
              <option value="">Stimme wählen</option>
              <VoiceOptions voices={engineVoices} />
            </select>
          </label>
          <label className="label-block" style={{ flex: 1 }}>
            Rolle
            <select className="input-field" value={role} onChange={(e) => setRole(e.target.value as HostRole)}>
              <option value="host">Moderation</option>
              <option value="expert">Gast</option>
            </select>
          </label>
        </div>

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
          <button className="btn btn-ghost" onClick={onClose}>Abbrechen</button>
          <button className="btn btn-primary" disabled={busy || !name.trim() || !voiceId} onClick={() => void submit()}>
            {busy ? 'Lege an …' : 'Anlegen'}
          </button>
        </div>
      </div>
    </Dialog>
  );
}
