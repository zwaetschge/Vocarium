import { useState, useEffect, useCallback, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  getLLMProviders,
  createLLMProvider,
  updateLLMProvider,
  deleteLLMProvider,
  setActiveLLMProvider,
  testLLMProvider,
} from '../../api';
import type { LLMProvider } from '../../types';

/** Fehlertext aus einem unbekannten catch-Wert. */
function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e ?? '');
}

const DEFAULT_PROVIDER = {
  base_url: '',
  api_key: '',
  model: '',
  temperature: 0.8,
  max_tokens: 16384,
  provider_type: 'openai',
};

// --- Helpers ---------------------------------------------------------------
function validateUrl(url: string): string | null {
  try {
    new URL(url);
    return null;
  } catch {
    return 'Muss eine gültige URL sein (z. B. http://host:port/v1)';
  }
}

// --- Components ------------------------------------------------------------

export default function LLMProviderSettings() {
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const modelRequest = useRef(0);
  const [models, setModels] = useState<string[]>([]);
  const [modelBusy, setModelBusy] = useState(false);
  const [modelNotice, setModelNotice] = useState('');
  const [editingId, setEditingId] = useState<string | null>(null);

  const [form, setForm] = useState({
    name: '',
    base_url: DEFAULT_PROVIDER.base_url,
    api_key: DEFAULT_PROVIDER.api_key,
    model: DEFAULT_PROVIDER.model,
    temperature: String(DEFAULT_PROVIDER.temperature),
    max_tokens: String(DEFAULT_PROVIDER.max_tokens),
    provider_type: DEFAULT_PROVIDER.provider_type,
  });

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getLLMProviders();
      setProviders(data);
      const active = data.find((p) => p.is_active);
      if (active) setActiveId(active.id);
    } catch (e) {
      setError(errorMessage(e) || 'Anbieter konnten nicht geladen werden');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const startEditing = (provider: LLMProvider) => {
    setEditingId(provider.id);
    modelRequest.current++; setModelBusy(false); setModels([]); setModelNotice('');
    setForm({
      name: provider.name,
      base_url: provider.base_url,
      api_key: '', // empty means "keep current"
      model: provider.model,
      temperature: String(provider.temperature ?? 0.8),
      max_tokens: String(provider.max_tokens ?? 16384),
      provider_type: provider.provider_type || 'openai',
    });
    setTestResult(null);
  };

  const startNew = () => {
    setEditingId('NEW');
    modelRequest.current++; setModelBusy(false); setModels([]); setModelNotice('');
    setForm({
      name: '',
      base_url: DEFAULT_PROVIDER.base_url,
      api_key: DEFAULT_PROVIDER.api_key,
      model: DEFAULT_PROVIDER.model,
      temperature: String(DEFAULT_PROVIDER.temperature),
      max_tokens: String(DEFAULT_PROVIDER.max_tokens),
      provider_type: DEFAULT_PROVIDER.provider_type,
    });
    setTestResult(null);
  };

  const cancelEdit = () => {
    modelRequest.current++; setModelBusy(false);
    setEditingId(null);
    setTestResult(null);
  };

  const save = async () => {
    const urlErr = validateUrl(form.base_url);
    if (urlErr) {
      setError(urlErr);
      return;
    }
    if (!form.name.trim()) {
      setError('Name ist erforderlich');
      return;
    }
    if (!form.model.trim()) {
      setError('Modell ist erforderlich');
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const payload = {
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        api_key: form.api_key,
        model: form.model.trim(),
        temperature: parseFloat(form.temperature) || 0.8,
        max_tokens: parseInt(form.max_tokens, 10) || 16384,
        provider_type: form.provider_type,
      };
      if (editingId === 'NEW') {
        await createLLMProvider(payload);
      } else if (editingId) {
        const patch: Parameters<typeof updateLLMProvider>[1] = {
          name: payload.name,
          base_url: payload.base_url,
          model: payload.model,
          temperature: payload.temperature,
          max_tokens: payload.max_tokens,
          provider_type: payload.provider_type,
        };
        if (payload.api_key) patch.api_key = payload.api_key;
        await updateLLMProvider(editingId, patch);
      }
      cancelEdit();
      await load();
    } catch (e) {
      setError(errorMessage(e) || 'Speichern fehlgeschlagen');
    } finally {
      setSaving(false);
    }
  };

  const makeActive = async (id: string) => {
    try {
      await setActiveLLMProvider(id);
      await load();
    } catch (e) {
      setError(errorMessage(e) || 'Aktivieren fehlgeschlagen');
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm('Diesen Anbieter löschen?')) return;
    try {
      await deleteLLMProvider(id);
      await load();
    } catch (e) {
      setError(errorMessage(e) || 'Löschen fehlgeschlagen');
    }
  };

  const refreshModels = async () => {
    if (!editingId || editingId === 'NEW') return;
    const request = ++modelRequest.current;
    setModelBusy(true); setModelNotice('');
    try {
      const response = await fetch(`/api/llm/providers/${encodeURIComponent(editingId)}/models`);
      const result = await response.json();
      if (request !== modelRequest.current) return;
      if (!response.ok) throw new Error(result.detail || 'Modellliste nicht erreichbar');
      setModels(result.models);
      setModelNotice(`${result.models.length} Modelle aktualisiert. Deine Auswahl bleibt erhalten.`);
    } catch (e) { if (request === modelRequest.current) setModelNotice(errorMessage(e)); }
    finally { if (request === modelRequest.current) setModelBusy(false); }
  };

  const test = async () => {
    if (!form.base_url || !form.model) {
      setError('Für den Test werden Basis-URL und Modell gebraucht');
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const res = await testLLMProvider({
        base_url: form.base_url.trim(),
        api_key: form.api_key,
        model: form.model.trim(),
      });
      if (res.status === 'ok') {
        setTestResult({ ok: true, msg: `Test erfolgreich. Antwort: "${res.response}"` });
      } else {
        setTestResult({ ok: false, msg: `Test fehlgeschlagen: ${res.detail || res.status || 'Unbekannter Fehler'}` });
      }
    } catch (e) {
      setTestResult({ ok: false, msg: errorMessage(e) || 'Test fehlgeschlagen' });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div>
      <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)', maxWidth: '60ch', lineHeight: 1.55, marginBottom: 22 }}>
        Sprachmodell für die Podcast-Skripte. Der aktive Anbieter wird bei jeder
        Skript-Generierung verwendet — ohne ihn bleibt der Generator stumm.
      </p>

      {error && (
        <div
          style={{
            background: 'rgba(255,77,77,0.1)',
            border: '1px solid rgba(255,77,77,0.25)',
            borderRadius: '10px',
            padding: '12px 16px',
            marginBottom: 20,
            color: '#ffb3b3',
            fontSize: '13px',
          }}
        >
          {error}
        </div>
      )}

      {/* Active provider summary */}
      <section style={{ marginBottom: 28 }}>
        <div className="label-eyebrow" style={{ marginBottom: 10 }}>
          Aktiver Anbieter
        </div>
        <div
          className="glass"
          style={{
            borderRadius: '16px',
            padding: '16px 20px',
            display: 'flex',
            alignItems: 'center',
            gap: 16,
          }}
        >
          <div
            style={{
              width: 10,
              height: 10,
              borderRadius: '50%',
              background: '#22c55e',
              boxShadow: '0 0 8px rgba(34,197,94,0.5)',
              flexShrink: 0,
            }}
          />
          {loading ? (
            <span style={{ fontSize: '14px', color: 'var(--color-text-dim)' }}>Lade …</span>
          ) : (
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: '14px', fontWeight: 600 }}>
                {providers.find((p) => p.id === activeId)?.name ?? 'Keiner'}
              </div>
              <div
                style={{
                  fontSize: '12px',
                  color: 'var(--color-text-dim)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  fontFamily: 'var(--font-mono)',
                }}
              >
                {providers.find((p) => p.id === activeId)?.base_url ?? 'Kein Anbieter eingerichtet'}
              </div>
            </div>
          )}
        </div>
      </section>

      {/* Provider list */}
      <section style={{ marginBottom: 28 }}>
        <div className="label-eyebrow" style={{ marginBottom: 10, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>LLM-Anbieter</span>
          {!editingId && (
            <button className="btn btn-sm" onClick={startNew}>
              + Neuer Anbieter
            </button>
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {loading ? (
            <div style={{ color: 'var(--color-text-dim)', fontSize: '14px', padding: '20px 0' }}>Lade …</div>
          ) : providers.length === 0 ? (
            <div style={{ color: 'var(--color-text-dim)', fontSize: '14px', padding: '20px 0' }}>
              Noch kein Anbieter eingerichtet. Lege unten einen an.
            </div>
          ) : (
            providers.map((p) => (
              <motion.div
                key={p.id}
                layout
                className="glass"
                style={{
                  borderRadius: '14px',
                  padding: '14px 18px',
                  border: p.is_active ? '1px solid rgba(34,197,94,0.35)' : '1px solid rgba(255,255,255,0.08)',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                      <span style={{ fontSize: '14px', fontWeight: 600 }}>{p.name}</span>
                      {p.is_active && (
                        <span
                          style={{
                            fontSize: '10px',
                            fontWeight: 600,
                            textTransform: 'uppercase',
                            letterSpacing: 0,
                            padding: '2px 6px',
                            borderRadius: '100px',
                            background: 'rgba(34,197,94,0.15)',
                            color: '#22c55e',
                          }}
                        >
                          Aktiv
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                      {p.base_url}
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--color-text-dim)', marginTop: 2 }}>
                      Modell: {p.model} · Temp: {p.temperature} · Max: {p.max_tokens}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                    {!p.is_active && (
                      <button className="btn btn-sm btn-outline" onClick={() => makeActive(p.id)}>
                        Aktivieren
                      </button>
                    )}
                    <button className="btn btn-sm btn-outline" onClick={() => startEditing(p)}>
                      Bearbeiten
                    </button>
                    <button className="btn btn-sm btn-danger" onClick={() => remove(p.id)}>
                      Löschen
                    </button>
                  </div>
                </div>
              </motion.div>
            ))
          )}
        </div>
      </section>

      {/* Edit / Create form */}
      <AnimatePresence>
        {editingId && (
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 12 }}
            transition={{ duration: 0.18 }}
            className="glass"
            style={{ borderRadius: 'var(--radius-panel)', padding: '22px 24px', border: '1px solid rgba(123,97,255,0.22)' }}
          >
            <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: 16, letterSpacing: 0 }}>
              {editingId === 'NEW' ? 'Neuer LLM-Anbieter' : 'Anbieter bearbeiten'}
            </h3>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div>
                <label className="label-block">Name</label>
                <input
                  className="text-input"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="z. B. Lokales LM Studio"
                />
              </div>

              <div>
                <label className="label-block">Basis-URL</label>
                <input
                  className="text-input"
                  value={form.base_url}
                  onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                  placeholder="https://api.openai.com/v1"
                />
              </div>

              <div>
                <label className="label-block">
                  API-Key {editingId !== 'NEW' && <span style={{ fontSize: '11px', color: 'var(--color-text-dim)' }}>(leer lassen = unverändert)</span>}
                </label>
                <div style={{ display: 'flex', gap: 8 }}>
                  <input
                    className="text-input"
                    type="text"
                    value={form.api_key}
                    onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                    placeholder="sk-… oder leer lassen"
                    style={{ flex: 1 }}
                  />
                </div>
              </div>

              <div>
                <label className="label-block">Modell</label>
                <input
                  className="text-input"
                  list="provider-models"
                  value={form.model}
                  onChange={(e) => setForm({ ...form, model: e.target.value })}
                  placeholder="Modell-ID eingeben oder aus der Liste wählen"
                />
                <datalist id="provider-models">{models.map(model => <option key={model} value={model} />)}</datalist>
                <button type="button" className="btn btn-outline btn-sm" disabled={modelBusy || editingId === 'NEW'} onClick={() => void refreshModels()}>{modelBusy ? 'Modelle werden abgerufen …' : 'Modellliste aktualisieren'}</button>
                <p style={{fontSize:12, marginTop:8}}>Abfrage mit den gespeicherten Zugangsdaten. Neue Anbieter und geänderte Zugangsdaten zuerst speichern.</p>
                {modelNotice && <p role="status" style={{fontSize:13, marginTop:8}}>{modelNotice}</p>}
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                <div>
                  <label className="label-block">Temperatur</label>
                  <input
                    className="text-input"
                    type="number"
                    step="0.1"
                    min="0"
                    max="2"
                    value={form.temperature}
                    onChange={(e) => setForm({ ...form, temperature: e.target.value })}
                  />
                </div>
                <div>
                  <label className="label-block">Max. Tokens</label>
                  <input
                    className="text-input"
                    type="number"
                    step="1024"
                    min="256"
                    max="128000"
                    value={form.max_tokens}
                    onChange={(e) => setForm({ ...form, max_tokens: e.target.value })}
                  />
                </div>
              </div>

              <div>
                <label className="label-block">Anbietertyp</label>
                <select
                  className="text-input"
                  value={form.provider_type}
                  onChange={(e) => setForm({ ...form, provider_type: e.target.value })}
                  style={{ padding: '10px 14px', appearance: 'auto' }}
                >
                  <option value="openai">OpenAI</option>
                  <option value="openai-compatible">
                    OpenAI-kompatibel (llama.cpp, vLLM, ...)
                  </option>
                </select>
              </div>

              {testResult && (
                <div
                  style={{
                    padding: '10px 14px',
                    borderRadius: '10px',
                    fontSize: '12px',
                    background: testResult.ok ? 'rgba(34,197,94,0.1)' : 'rgba(255,77,77,0.1)',
                    border: testResult.ok ? '1px solid rgba(34,197,94,0.25)' : '1px solid rgba(255,77,77,0.25)',
                    color: testResult.ok ? '#86efac' : '#ffb3b3',
                  }}
                >
                  {testResult.msg}
                </div>
              )}

              <div style={{ display: 'flex', gap: 10, marginTop: 6 }}>
                <button className="btn btn-primary" onClick={save} disabled={saving}>
                  {saving ? 'Speichere …' : editingId === 'NEW' ? 'Anbieter anlegen' : 'Änderungen speichern'}
                </button>
                <button className="btn btn-outline" onClick={test} disabled={testing}>
                  {testing ? 'Teste …' : 'Verbindung testen'}
                </button>
                <button className="btn btn-ghost" onClick={cancelEdit}>
                  Abbrechen
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
