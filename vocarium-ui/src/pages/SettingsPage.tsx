import { useState, useEffect, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  getLLMProviders,
  getActiveLLMProvider,
  createLLMProvider,
  updateLLMProvider,
  deleteLLMProvider,
  setActiveLLMProvider,
  testLLMProvider,
} from '../api';
import type { LLMProvider } from '../types';

const DEFAULT_PROVIDER = {
  base_url: '',
  api_key: '',
  model: '',
  temperature: 0.8,
  max_tokens: 16384,
  provider_type: 'openai',
};

// --- Helpers ---------------------------------------------------------------
function maskKey(key: string): string {
  if (!key || key.length < 8) return key || '(none)';
  return `${key.slice(0, 6)}...${key.slice(-4)}`;
}

function validateUrl(url: string): string | null {
  try {
    new URL(url);
    return null;
  } catch {
    return 'Must be a valid URL (e.g. http://host:port/v1)';
  }
}

// --- Components ------------------------------------------------------------

export default function SettingsPage() {
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [showKeyId, setShowKeyId] = useState<string | null>(null);

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
    } catch (e: any) {
      setError(e.message || 'Failed to load providers');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const startEditing = (provider: LLMProvider) => {
    setEditingId(provider.id);
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
      setError('Name is required');
      return;
    }
    if (!form.model.trim()) {
      setError('Model is required');
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
        const patch: any = {
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
    } catch (e: any) {
      setError(e.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const makeActive = async (id: string) => {
    try {
      await setActiveLLMProvider(id);
      await load();
    } catch (e: any) {
      setError(e.message || 'Failed to activate provider');
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm('Delete this provider?')) return;
    try {
      await deleteLLMProvider(id);
      await load();
    } catch (e: any) {
      setError(e.message || 'Delete failed');
    }
  };

  const test = async () => {
    if (!form.base_url || !form.model) {
      setError('Base URL and Model are required to test');
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
        setTestResult({ ok: true, msg: `Test succeeded. Response: "${res.response}"` });
      } else {
        setTestResult({ ok: false, msg: `Test failed: ${res.detail || res.status || 'Unknown error'}` });
      }
    } catch (e: any) {
      setTestResult({ ok: false, msg: e.message || 'Test error' });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div style={{ maxWidth: '720px', margin: '0 auto', padding: '32px 24px' }}>
      <header style={{ marginBottom: 28 }}>
        <h1 style={{ fontSize: '24px', fontWeight: 600, letterSpacing: 0, marginBottom: 6 }}>
          Settings
        </h1>
        <p style={{ fontSize: '14px', color: 'var(--color-text-dim)', maxWidth: 480, lineHeight: '1.5' }}>
          Configure LLM providers for podcast script generation. The active provider is used when generating scripts.
        </p>
      </header>

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
          Active Provider
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
            <span style={{ fontSize: '14px', color: 'var(--color-text-dim)' }}>Loading…</span>
          ) : (
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: '14px', fontWeight: 600 }}>
                {providers.find((p) => p.id === activeId)?.name ?? 'None'}
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
                {providers.find((p) => p.id === activeId)?.base_url ?? 'No provider configured'}
              </div>
            </div>
          )}
        </div>
      </section>

      {/* Provider list */}
      <section style={{ marginBottom: 28 }}>
        <div className="label-eyebrow" style={{ marginBottom: 10, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>LLM Providers</span>
          {!editingId && (
            <button className="btn btn-sm" onClick={startNew}>
              + New Provider
            </button>
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {loading ? (
            <div style={{ color: 'var(--color-text-dim)', fontSize: '14px', padding: '20px 0' }}>Loading…</div>
          ) : providers.length === 0 ? (
            <div style={{ color: 'var(--color-text-dim)', fontSize: '14px', padding: '20px 0' }}>
              No providers configured. Create one below.
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
                          Active
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                      {p.base_url}
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--color-text-dim)', marginTop: 2 }}>
                      Model: {p.model} · Temp: {p.temperature} · Max: {p.max_tokens}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                    {!p.is_active && (
                      <button className="btn btn-sm btn-outline" onClick={() => makeActive(p.id)}>
                        Set Active
                      </button>
                    )}
                    <button className="btn btn-sm btn-outline" onClick={() => startEditing(p)}>
                      Edit
                    </button>
                    <button className="btn btn-sm btn-danger" onClick={() => remove(p.id)}>
                      Delete
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
              {editingId === 'NEW' ? 'New LLM Provider' : 'Edit Provider'}
            </h3>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div>
                <label className="label-block">Name</label>
                <input
                  className="text-input"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="e.g. Local LM Studio"
                />
              </div>

              <div>
                <label className="label-block">Base URL</label>
                <input
                  className="text-input"
                  value={form.base_url}
                  onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                  placeholder="https://api.openai.com/v1"
                />
              </div>

              <div>
                <label className="label-block">
                  API Key {editingId !== 'NEW' && <span style={{ fontSize: '11px', color: 'var(--color-text-dim)' }}>(leave empty to keep current)</span>}
                </label>
                <div style={{ display: 'flex', gap: 8 }}>
                  <input
                    className="text-input"
                    type="text"
                    value={form.api_key}
                    onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                    placeholder="sk-... or leave empty"
                    style={{ flex: 1 }}
                  />
                </div>
              </div>

              <div>
                <label className="label-block">Model</label>
                <input
                  className="text-input"
                  value={form.model}
                  onChange={(e) => setForm({ ...form, model: e.target.value })}
                  placeholder="e.g. gpt-4, llama-3.3"
                />
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                <div>
                  <label className="label-block">Temperature</label>
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
                  <label className="label-block">Max Tokens</label>
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
                <label className="label-block">Provider Type</label>
                <select
                  className="text-input"
                  value={form.provider_type}
                  onChange={(e) => setForm({ ...form, provider_type: e.target.value })}
                  style={{ padding: '10px 14px', appearance: 'auto' }}
                >
                  <option value="openai">OpenAI-compatible</option>
                  <option value="anthropic">Anthropic (Claude)</option>
                  <option value="ollama">Ollama</option>
                  <option value="custom">Custom / Other</option>
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
                  {saving ? 'Saving…' : editingId === 'NEW' ? 'Create Provider' : 'Save Changes'}
                </button>
                <button className="btn btn-outline" onClick={test} disabled={testing}>
                  {testing ? 'Testing…' : 'Test Connection'}
                </button>
                <button className="btn btn-ghost" onClick={cancelEdit}>
                  Cancel
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
