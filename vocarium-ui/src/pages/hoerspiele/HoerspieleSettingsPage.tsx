import { useCallback, useEffect, useRef, useState } from 'react';
import {Link} from 'react-router-dom';
import { hs, jsonBody } from '../../lib/hoerspiele';
import type {
  AgentCandidate, AgentProfile, AgentProvider, AgentProviderStatus, AgentSettings, CliLoginSession,
} from '../../lib/hoerspiele';

type Integration = { status: string; label: string; configured?: boolean; authenticated?: boolean };
type Integrations = Record<string, Integration>;

const INTEGRATION_LABELS: Record<string, string> = {
  plex: 'Plex-Bibliothek',
  vocarium: 'Transkription',
  aligner: 'Zeitmarken',
  tts: 'Erzählstimme',
  codex: 'KI-Runner',
};

const HEALTHY = ['connected', 'local', 'terminal'];

/** Die API liefert technische Statuswerte; die Oberfläche ist deutsch. */
const STATUS_LABELS: Record<string, string> = {
  connected: 'Verbunden',
  local: 'Lokal',
  terminal: 'Terminal',
  demo: 'Demo',
  unconfigured: 'Nicht konfiguriert',
  offline: 'Offline',
  error: 'Fehler',
  login_required: 'Anmeldung nötig',
  pending: 'Ausstehend',
  waiting_for_code: 'Wartet auf Code',
  completed: 'Abgeschlossen',
  failed: 'Fehlgeschlagen',
  expired: 'Abgelaufen',
  cancelled: 'Abgebrochen',
};
function statusLabel(value: string) {
  return STATUS_LABELS[value] || value;
}

/* --- Integrationen ---------------------------------------------------- */

export function IntegrationsPanel() {
  const [integrations, setIntegrations] = useState<Integrations>({});
  const [error, setError] = useState('');

  useEffect(() => {
    hs<Integrations>('/integrations')
      .then(setIntegrations)
      .catch((problem) => setError((problem as Error).message));
  }, []);

  return (
    <section className="card hs-panel hs-panel-wide">
      <div className="hs-panel-head"><h2>Integrationen</h2></div>
      {error && <div className="hs-error">{error}</div>}
      <ul className="hs-list">
        {Object.entries(integrations).map(([key, entry]) => (
          <li key={key} className="hs-item">
            <div className="hs-item-head">
              <span className="hs-item-title">{INTEGRATION_LABELS[key] || key}</span>
              <span className="status-chip">
                <span className={`status-dot ${HEALTHY.includes(entry.status) ? 'status-dot-online' : 'status-dot-offline'}`} />
                {statusLabel(entry.status)}
              </span>
            </div>
            <span className="hs-item-note">{entry.label}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/* --- Provider-Zugaenge ------------------------------------------------ */

const PROVIDERS: { id: AgentProvider; title: string; hint: string }[] = [
  { id: 'codex', title: 'Codex CLI', hint: 'ChatGPT Device Auth' },
  { id: 'claude', title: 'Claude Code CLI', hint: 'Anthropic-Konto per Browser' },
  { id: 'zai', title: 'Z.AI Coding Plan', hint: 'API-Key und Anthropic-kompatible URL' },
];

export function ProviderAccessPanel() {
  const [status, setStatus] = useState<AgentProviderStatus | null>(null);
  const [session, setSession] = useState<CliLoginSession | null>(null);
  const [code, setCode] = useState('');
  const [zaiKey, setZaiKey] = useState('');
  const [zaiUrl, setZaiUrl] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const poll = useRef<number | null>(null);

  const reload = useCallback(async () => {
    try {
      setStatus(await hs<AgentProviderStatus>('/settings/agents/status'));
    } catch (problem) {
      setError((problem as Error).message);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => () => { if (poll.current) window.clearInterval(poll.current); }, []);

  // Der CLI-Login läuft im Backend als Sitzung; wir fragen den Fortschritt ab,
  // bis der Code bestätigt ist oder der Versuch scheitert.
  function watch(sessionId: string) {
    if (poll.current) window.clearInterval(poll.current);
    poll.current = window.setInterval(async () => {
      try {
        const update = await hs<CliLoginSession>(`/settings/agents/login-sessions/${sessionId}`);
        setSession(update);
        if (update.status === 'completed' || update.status === 'error') {
          if (poll.current) window.clearInterval(poll.current);
          poll.current = null;
          await reload();
        }
      } catch {
        /* Ein verpasster Tick ist harmlos. */
      }
    }, 1500);
  }

  async function login(provider: 'codex' | 'claude') {
    setBusy(true);
    setError('');
    try {
      const created = await hs<CliLoginSession>(`/settings/agents/${provider}/login-sessions`, { method: 'POST' });
      setSession(created);
      watch(created.id);
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function submitCode() {
    if (!session) return;
    setBusy(true);
    try {
      const update = await hs<CliLoginSession>(`/settings/agents/login-sessions/${session.id}/code`, {
        method: 'POST',
        ...jsonBody({ code }),
      });
      setSession(update);
      setCode('');
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!session) return;
    if (poll.current) { window.clearInterval(poll.current); poll.current = null; }
    try { await hs(`/settings/agents/login-sessions/${session.id}`, { method: 'DELETE' }); } catch { /* egal */ }
    setSession(null);
  }

  async function saveZai() {
    setBusy(true);
    setError('');
    try {
      await hs('/settings/agents/zai-credentials', { method: 'PUT', ...jsonBody({ api_key: zaiKey, base_url: zaiUrl }) });
      // Der Key wird nie zurückgeliefert — das Feld bleibt daher leer.
      setZaiKey('');
      await reload();
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card hs-panel hs-panel-wide">
      <div className="hs-panel-head">
        <h2>Provider-Zugänge</h2>
        {status && <span className="status-chip">{status.label}</span>}
      </div>
      {error && <div className="hs-error">{error}</div>}

      <ul className="hs-list">
        {PROVIDERS.map((provider) => {
          const state = status?.providers?.[provider.id];
          return (
            <li key={provider.id} className="hs-item">
              <div className="hs-item-head">
                <span className="hs-item-title">{provider.title}</span>
                <span className="status-chip">
                  <span className={`status-dot ${state?.authenticated ? 'status-dot-online' : 'status-dot-offline'}`} />
                  {state?.status || 'unbekannt'}
                </span>
              </div>
              <span className="hs-item-note">{state?.label || provider.hint}</span>
              {provider.id !== 'zai' && (
                <div className="hs-actions">
                  <button
                    type="button"
                    className="btn btn-sm btn-outline"
                    onClick={() => void login(provider.id as 'codex' | 'claude')}
                    disabled={busy}
                  >
                    {state?.authenticated ? 'Erneut anmelden' : 'Anmelden'}
                  </button>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {session && (
        <div className="hs-item">
          <div className="hs-item-head">
            <span className="hs-item-title">Anmeldung {session.provider}</span>
            <span className="status-chip">{statusLabel(session.status)}</span>
          </div>
          {session.login_url && (
            <a className="hs-item-note" href={session.login_url} target="_blank" rel="noreferrer">{session.login_url}</a>
          )}
          {session.verification_code && <span className="hs-meta">Bestätigungscode: {session.verification_code}</span>}
          {session.error && <div className="hs-error">{session.error}</div>}
          {session.status === 'awaiting_code' && (
            <div className="hs-row">
              <div className="hs-field">
                <label htmlFor="hs-login-code">Code aus dem Browser</label>
                <input
                  id="hs-login-code"
                  className="input-field"
                  value={code}
                  onChange={(event) => setCode(event.target.value)}
                  autoComplete="one-time-code"
                />
              </div>
            </div>
          )}
          <div className="hs-actions">
            {session.status === 'awaiting_code' && (
              <button type="button" className="btn btn-sm btn-primary" onClick={() => void submitCode()} disabled={busy || !code}>
                Code bestätigen
              </button>
            )}
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => void cancel()}>Abbrechen</button>
          </div>
        </div>
      )}

      <h3>Z.AI Zugangsdaten</h3>
      <div className="hs-row">
        <div className="hs-field">
          <label htmlFor="hs-zai-key">API-Key</label>
          <input
            id="hs-zai-key"
            className="input-field"
            type="password"
            autoComplete="new-password"
            placeholder="Wird nach dem Speichern nicht mehr angezeigt"
            value={zaiKey}
            onChange={(event) => setZaiKey(event.target.value)}
          />
        </div>
        <div className="hs-field">
          <label htmlFor="hs-zai-url">Basis-URL</label>
          <input
            id="hs-zai-url"
            className="input-field"
            value={zaiUrl}
            onChange={(event) => setZaiUrl(event.target.value)}
            placeholder={status?.providers?.zai?.base_url || 'https://api.z.ai/api/anthropic'}
          />
        </div>
      </div>
      <div className="hs-actions">
        <button type="button" className="btn btn-sm btn-outline" onClick={() => void saveZai()} disabled={busy || !zaiKey}>
          Zugangsdaten speichern
        </button>
      </div>
    </section>
  );
}

/* --- KI-Modelle ------------------------------------------------------- */

function CandidateRow({
  candidate, settings, used, disabled, onChange,
}: {
  candidate: AgentCandidate;
  settings: AgentSettings;
  used: AgentProvider[];
  disabled: boolean;
  onChange: (next: AgentCandidate) => void;
}) {
  const availableModels = settings.model_options[candidate.provider] || [];
  const models = availableModels.some(option => option.value === candidate.model) ? availableModels : [...availableModels, {value: candidate.model, label: candidate.model + ' · bisherige Auswahl'}];
  return (
    <div className="hs-row">
      <div className="hs-field">
        <label>CLI</label>
        <select
          className="input-field"
          value={candidate.provider}
          disabled={disabled}
          onChange={(event) => {
            const provider = event.target.value as AgentProvider;
            const available = settings.model_options[provider] || [];
            // Nach dem Wechsel muss ein Modell gewählt sein, das die neue CLI
            // überhaupt kennt.
            const model = available.some((entry) => entry.value === candidate.model)
              ? candidate.model
              : available[0]?.value || candidate.model;
            onChange({ ...candidate, provider, model });
          }}
        >
          {settings.provider_options.map((option) => (
            <option
              key={option.value}
              value={option.value}
              disabled={option.value !== candidate.provider && used.includes(option.value)}
            >
              {option.label}
            </option>
          ))}
        </select>
      </div>
      <div className="hs-field">
        <label>Modell</label>
        <select
          className="input-field"
          value={candidate.model}
          disabled={disabled}
          onChange={(event) => onChange({ ...candidate, model: event.target.value })}
        >
          {models.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </div>
      <div className="hs-field">
        <label>Denkstufe</label>
        <select
          className="input-field"
          value={candidate.reasoning_effort}
          disabled={disabled}
          onChange={(event) => onChange({ ...candidate, reasoning_effort: event.target.value as AgentCandidate['reasoning_effort'] })}
        >
          {settings.reasoning_options.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </div>
      <div className="hs-field">
        <label>Zeitlimit (s)</label>
        <input
          className="input-field"
          type="number"
          min={60}
          max={1800}
          step={30}
          value={candidate.timeout_seconds}
          disabled={disabled}
          onChange={(event) => onChange({ ...candidate, timeout_seconds: Number(event.target.value) })}
        />
      </div>
    </div>
  );
}

function ProfileEditor({
  title, profile, settings, disabled, onChange,
}: {
  title: string;
  profile: AgentProfile;
  settings: AgentSettings;
  disabled: boolean;
  onChange: (next: AgentProfile) => void;
}) {
  const chain: AgentCandidate[] = [profile, ...profile.fallbacks];
  const used = chain.map((entry) => entry.provider);

  /** Ein neues Ersatzmodell nimmt die erste CLI, die in der Kette noch frei ist. */
  function addFallback() {
    const free = settings.provider_options.find((option) => !used.includes(option.value));
    if (!free) return;
    const models = settings.model_options[free.value] || [];
    onChange({
      ...profile,
      fallbacks: [...profile.fallbacks, {
        provider: free.value,
        model: models[0]?.value || profile.model,
        reasoning_effort: profile.reasoning_effort,
        timeout_seconds: profile.timeout_seconds,
      }],
    });
  }

  return (
    <li className="hs-item">
      <div className="hs-item-head">
        <span className="hs-item-title">{title}</span>
        <span className="hs-meta">1 Hauptmodell + {profile.fallbacks.length} Ersatz</span>
      </div>

      <span className="hs-meta">Hauptmodell</span>
      <CandidateRow
        candidate={profile}
        settings={settings}
        used={used.slice(1)}
        disabled={disabled}
        onChange={(next) => onChange({ ...profile, ...next })}
      />

      {profile.fallbacks.map((fallback, index) => (
        <div key={index}>
          <span className="hs-meta">Ersatz {index + 1}</span>
          <CandidateRow
            candidate={fallback}
            settings={settings}
            used={used.filter((_, position) => position !== index + 1)}
            disabled={disabled}
            onChange={(next) => {
              const fallbacks = [...profile.fallbacks];
              fallbacks[index] = next;
              onChange({ ...profile, fallbacks });
            }}
          />
          <div className="hs-actions">
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              disabled={disabled}
              onClick={() => onChange({ ...profile, fallbacks: profile.fallbacks.filter((_, position) => position !== index) })}
            >
              Ersatz entfernen
            </button>
          </div>
        </div>
      ))}

      {profile.fallbacks.length < 2 && used.length < settings.provider_options.length && (
        <div className="hs-actions">
          <button type="button" className="btn btn-sm btn-ghost" disabled={disabled} onClick={addFallback}>
            Ersatzmodell hinzufügen
          </button>
        </div>
      )}
    </li>
  );
}

/** Die Laufzeitgrenzen kommen als technische Marker; die Oberflaeche ist sonst deutsch. */
const RUNTIME_LABELS: Record<string, string> = {
  live: 'aktiv',
  disabled: 'aus',
  required: 'erforderlich',
  ignored: 'wird ignoriert',
  never: 'nie',
  'read-only': 'nur lesend',
};

function runtimeLabel(value: unknown) {
  const raw = String(value);
  return RUNTIME_LABELS[raw] || raw;
}

export function AgentSettingsPanel() {
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    hs<AgentSettings>('/settings/agents')
      .then(setSettings)
      .catch((problem) => setError((problem as Error).message));
  }, []);

  useEffect(() => {
    const reloadCatalog = () => { hs<AgentSettings>('/settings/agents').then(fresh => setSettings(current => current ? {...current, model_options: fresh.model_options} : fresh)).catch(problem => setError((problem as Error).message)); };
    window.addEventListener('vocarium-models-updated', reloadCatalog);
    return () => window.removeEventListener('vocarium-models-updated', reloadCatalog);
  }, []);

  async function save() {
    if (!settings) return;
    setBusy(true);
    setError('');
    try {
      const updated = await hs<AgentSettings>('/settings/agents', {
        method: 'PUT',
        ...jsonBody({ research: settings.research, scripting: settings.scripting }),
      });
      setSettings(updated);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!settings) {
    return (
      <section className="card hs-panel hs-panel-wide">
        <div className="hs-panel-head"><h2>KI-Modelle</h2></div>
        {error ? <div className="hs-error">{error}</div>
          : <div className="skeleton-shimmer" style={{ height: 120, borderRadius: 'var(--radius-panel)' }} />}
      </section>
    );
  }

  const runtime = settings.fixed_runtime;

  return (
    <section className="card hs-panel hs-panel-wide">
      <div className="hs-panel-head">
        <h2>KI-Modelle</h2>
        {saved && <span className="status-chip">gespeichert</span>}
      </div>
      {error && <div className="hs-error">{error}</div>}

      <ul className="hs-list">
        <ProfileEditor
          title="Recherche"
          profile={settings.research}
          settings={settings}
          disabled={busy}
          onChange={(research) => setSettings({ ...settings, research })}
        />
        <ProfileEditor
          title="Skript"
          profile={settings.scripting}
          settings={settings}
          disabled={busy}
          onChange={(scripting) => setSettings({ ...settings, scripting })}
        />
      </ul>

      <div className="hs-actions">
        <button type="button" className="btn btn-sm btn-primary" onClick={() => void save()} disabled={busy}>
          {busy ? 'Wird gespeichert …' : 'Modelle speichern'}
        </button>
      </div>

      {runtime && (
        <>
          <h3>Feste Laufzeitgrenzen</h3>
          <ul className="hs-list">
            <li className="hs-item-note">Websuche bei der Zuordnung: {runtimeLabel(runtime.mapping_web_search)}</li>
            <li className="hs-item-note">Websuche beim Episodenkontext: {runtimeLabel(runtime.episode_context_web_search)}</li>
            <li className="hs-item-note">Websuche bei der Szenenausrichtung: {runtimeLabel(runtime.scene_alignment_web_search)}</li>
            <li className="hs-item-note">Sandbox: {runtimeLabel(runtime.sandbox)} · Freigabe: {runtimeLabel(runtime.approval_policy)}</li>
          </ul>
        </>
      )}
    </section>
  );
}

/* --- Seite ------------------------------------------------------------ */

const TABS = [
  { id: 'integrationen', label: 'Integrationen' },
  { id: 'zugaenge', label: 'Zugänge' },
  { id: 'modelle', label: 'KI-Modelle' },
] as const;

export default function HoerspieleSettingsPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]['id']>('integrationen');

  return (
    <div className="app-content hs-page">
      <header className="hs-head">
        <div className="hs-head-text">
          <span className="label-eyebrow">Hörspiele</span>
          <h1 className="font-display">Einstellungen</h1>
          <p>Quellen, Zugänge und Modellwahl für die Hörspiel-Pipeline.</p><Link to="/settings/ki" className="btn btn-outline">CLIs aktualisieren &amp; Modelle verwalten</Link>
        </div>
      </header>

      <div className="hs-tabs" role="tablist">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={tab === entry.id}
            className={`hs-tab${tab === entry.id ? ' hs-tab-on' : ''}`}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {tab === 'integrationen' && <IntegrationsPanel />}
      {tab === 'zugaenge' && <ProviderAccessPanel />}
      {tab === 'modelle' && <AgentSettingsPanel />}
    </div>
  );
}
