import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import {AgentSettingsPanel, ProviderAccessPanel, IntegrationsPanel} from './hoerspiele/HoerspieleSettingsPage';
import CliMaintenance from '../components/settings/CliMaintenance';
import LLMProviderSettings from '../components/settings/LLMProviderSettings';
import { VoiceOptions } from '../components/VoiceOptions';
import { engineVoices as selectableVoices } from '../voiceUtils';
import {
  getHealth,
  getReaderPrefs,
  getSettingsPrefs,
  getVoices,
  putReaderPrefs,
  saveSettingsPrefs,
} from '../api';
import type { GeneralPrefs, HealthStatus, LabPrefs, PodcastPrefs, Voice } from '../types';

/**
 * Einstellungen als Hub: ein Reiter pro Bereich plus "Allgemein".
 *
 * Die Reiter spiegeln die drei Bereiche der App, damit man dort sucht, wo man
 * gerade arbeitet — und der LLM-Anbieter, den jeder Bereich braucht, liegt
 * unter Allgemein statt versteckt im Lab.
 */
const TABS = [
  { id: 'allgemein', label: 'Allgemein', hint: 'Startbereich, Anbieter & System' },
  { id: 'ki', label: 'CLIs & Modelle', hint: 'CLI-Versionen, Updates, Zugänge und Modellkataloge' },
  { id: 'hoerspiele', label: 'Hörspiele', hint: 'Quellen und Verbindungen' },
  { id: 'hoerbuecher', label: 'Hörbücher', hint: 'Darstellung im Reader' },
  { id: 'podcasts', label: 'Podcasts', hint: 'Voreinstellungen neuer Podcasts' },
  { id: 'lab', label: 'Sprachstudio', hint: 'Sprachausgabe & Engines' },
] as const;

type TabId = (typeof TABS)[number]['id'];

export default function SettingsPage() {
  const { tab } = useParams<{ tab?: string }>();
  const navigate = useNavigate();
  const active: TabId = (TABS.find((t) => t.id === tab)?.id ?? 'allgemein') as TabId;

  return (
    <div style={{ maxWidth: '1040px', margin: '0 auto', padding: '28px 24px 48px', display: 'flex', flexDirection: 'column', gap: '22px' }}>
      <header style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
        <span className="label-eyebrow" style={{ fontSize: '10px' }}>Vocarium</span>
        <h1 style={{ fontSize: '26px', color: 'var(--color-text)' }}>Einstellungen</h1>
        <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>
          {TABS.find((t) => t.id === active)?.hint}
        </p>
      </header>

      <nav className="settings-tabs" aria-label="Einstellungsbereiche">
        {TABS.map((t) => (
          <button
            key={t.id}
            aria-current={active === t.id ? 'page' : undefined}
            className={`settings-tab${active === t.id ? ' settings-tab-active' : ''}`}
            onClick={() => navigate(`/settings/${t.id}`)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <AnimatePresence mode="wait">
        <motion.div
          key={active}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
        >
          {active === 'allgemein' && <GeneralTab />}
          {active === 'ki' && <div className="hs-page settings-ai"><CliMaintenance /><AgentSettingsPanel /><ProviderAccessPanel /></div>}
          {active === 'hoerspiele' && <div className="hs-page"><IntegrationsPanel /><Link to="/settings/ki" className="btn btn-outline">CLIs &amp; Modelle verwalten</Link></div>}
          {active === 'hoerbuecher' && <AudiobookTab />}
          {active === 'podcasts' && <PodcastTab />}
          {active === 'lab' && <LabTab />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

// --- Bausteine --------------------------------------------------------------

function Card({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <section className="card" style={{ padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: '14px' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
        <div style={{ fontSize: '15px', color: 'var(--color-text)' }}>{title}</div>
        {description && (
          <p style={{ fontSize: '12.5px', color: 'var(--color-text-dim)', lineHeight: 1.55, margin: 0 }}>{description}</p>
        )}
      </div>
      {children}
    </section>
  );
}

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="settings-row">
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: '13px', color: 'var(--color-text)' }}>{label}</div>
        {hint && <div style={{ fontSize: '11.5px', color: 'var(--color-text-dim)' }}>{hint}</div>}
      </div>
      <div className="settings-row-control">{children}</div>
    </div>
  );
}

function SaveHint({ state }: { state: 'idle' | 'saving' | 'saved' | 'error' }) {
  if (state === 'idle') return null;
  const text = state === 'saving' ? 'Speichere …' : state === 'saved' ? 'Gespeichert' : 'Speichern fehlgeschlagen';
  return (
    <span style={{ fontSize: '11.5px', color: state === 'error' ? 'var(--color-danger)' : 'var(--color-text-dim)' }}>
      {text}
    </span>
  );
}

/**
 * Ein Namespace-Formular: liest beim Mount, schreibt debounced zurück.
 * Optimistische Anzeige — der Server ist die Wahrheit, aber niemand soll auf
 * ihn warten, um einen Schalter umzulegen.
 */
function useNamespacePrefs<T extends object>(namespace: 'podcast' | 'lab' | 'general') {
  const [prefs, setPrefs] = useState<T | null>(null);
  const [state, setState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');

  useEffect(() => {
    getSettingsPrefs<T>(namespace)
      .then(setPrefs)
      .catch(() => setPrefs(null));
  }, [namespace]);

  const update = useCallback(
    (patch: Partial<T>) => {
      setPrefs((p) => (p ? { ...p, ...patch } : p));
      setState('saving');
      saveSettingsPrefs<T>(namespace, patch)
        .then((next) => {
          setPrefs(next);
          setState('saved');
          window.setTimeout(() => setState('idle'), 1500);
        })
        .catch(() => setState('error'));
    },
    [namespace],
  );

  return { prefs, update, state };
}

// --- Allgemein --------------------------------------------------------------

function GeneralTab() {
  const { prefs, update, state } = useNamespacePrefs<GeneralPrefs>('general');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <Card
        title="Start"
        description="Welcher Bereich beim Öffnen von Vocarium erscheint."
      >
        <Row label="Startbereich">
          <select
            className="input-field"
            value={prefs?.area_default ?? 'audiobooks'}
            onChange={(e) => update({ area_default: e.target.value } as Partial<GeneralPrefs>)}
            disabled={!prefs}
          >
            <option value="audiobooks">Hörbücher</option>
            <option value="podcasts">Podcasts</option>
            <option value="hoerspiele">Hörspiele</option>
            <option value="lab">Sprachstudio</option>
          </select>
          <SaveHint state={state} />
        </Row>
      </Card>

      <Card title="LLM-Anbieter">
        <LLMProviderSettings />
      </Card>

      <SystemCard />
    </div>
  );
}

function StatusValue({ text, tone }: { text: string; tone: 'ok' | 'warn' | 'off' }) {
  const color =
    tone === 'ok' ? 'var(--color-success)' : tone === 'warn' ? 'var(--color-warning)' : 'var(--color-text-dim)';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '7px' }}>
      <span style={{ width: 7, height: 7, borderRadius: '50%', background: color, flexShrink: 0 }} />
      <span className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{text}</span>
    </span>
  );
}

/** `healthy`/`ok`/`bereit` gelten als gut, alles andere als Warnung. */
function toneOf(value: string | undefined | null): 'ok' | 'warn' | 'off' {
  if (!value) return 'off';
  const v = value.toLowerCase();
  if (v.includes('healthy') || v === 'ok' || v.includes('ready') || v.includes('bereit') || v.includes('online')) return 'ok';
  return 'warn';
}

function SystemCard() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch((e) => setErr(e instanceof Error ? e.message : 'Status nicht abrufbar'));
  }, []);

  return (
    <Card title="System" description="Kurzer Blick auf die laufenden Dienste.">
      {err ? (
        <div style={{ fontSize: '12.5px', color: 'var(--color-danger)' }}>{err}</div>
      ) : !health ? (
        <div style={{ fontSize: '12.5px', color: 'var(--color-text-dim)' }}>Lade Status …</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <Row label="API" hint="Gateway auf Port 8280">
            <StatusValue text={health.api} tone={toneOf(health.api)} />
          </Row>
          <Row label="Sprachausgabe" hint={health.tts?.current_model || 'OmniVoice auf der GPU, Kikiri als Fallback'}>
            <StatusValue text={health.tts?.status ?? 'unbekannt'} tone={toneOf(health.tts?.status)} />
          </Row>
          {health.gpu_resources && (
            <Row label="GPU-Koordination" hint="FIFO-Warteschlange vor der GPU">
              <StatusValue
                text={health.gpu_resources.available ? 'bereit' : health.gpu_resources.error || 'nicht verfügbar'}
                tone={health.gpu_resources.available ? 'ok' : 'warn'}
              />
            </Row>
          )}
        </div>
      )}
    </Card>
  );
}

// --- Hörbücher --------------------------------------------------------------

interface ReaderPrefsShape {
  fontSize: number;
  lineHeight: number;
  fontFamily: 'sans' | 'serif' | 'mono';
  theme: 'app' | 'sepia' | 'paper' | 'night';
  bionic: boolean;
}

const READER_DEFAULTS: ReaderPrefsShape = {
  fontSize: 16,
  lineHeight: 1.85,
  fontFamily: 'sans',
  theme: 'app',
  bionic: false,
};

/**
 * Dieselben Prefs wie im Reader-Panel — nur zentral erreichbar. Beide Wege
 * schreiben nach `/audiobooks/prefs` und in denselben localStorage-Schlüssel,
 * damit ein Wechsel hier sofort im Reader ankommt.
 */
function AudiobookTab() {
  const [prefs, setPrefs] = useState<ReaderPrefsShape>(() => {
    try {
      const raw = localStorage.getItem('ab-reader-prefs');
      return raw ? { ...READER_DEFAULTS, ...JSON.parse(raw) } : READER_DEFAULTS;
    } catch {
      return READER_DEFAULTS;
    }
  });
  const [state, setState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');

  useEffect(() => {
    getReaderPrefs()
      .then((remote) => {
        if (remote && typeof remote.fontSize === 'number') {
          setPrefs((p) => ({ ...p, ...(remote as unknown as ReaderPrefsShape) }));
        }
      })
      .catch(() => undefined);
  }, []);

  const set = <K extends keyof ReaderPrefsShape>(key: K, value: ReaderPrefsShape[K]) => {
    const next = { ...prefs, [key]: value };
    setPrefs(next);
    localStorage.setItem('ab-reader-prefs', JSON.stringify(next));
    setState('saving');
    putReaderPrefs(next as unknown as Record<string, unknown>)
      .then(() => {
        setState('saved');
        window.setTimeout(() => setState('idle'), 1500);
      })
      .catch(() => setState('error'));
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <Card
        title="Darstellung im Reader"
        description="Gilt geräteübergreifend. Dieselben Regler findest du auch direkt im Reader."
      >
        <Row label="Schriftgröße" hint={`${prefs.fontSize} px`}>
          <input
            type="range"
            min={13}
            max={26}
            step={1}
            value={prefs.fontSize}
            onChange={(e) => set('fontSize', Number(e.target.value))}
            style={{ width: '160px' }}
          />
        </Row>
        <Row label="Zeilenhöhe" hint={prefs.lineHeight.toFixed(2)}>
          <input
            type="range"
            min={1.4}
            max={2.4}
            step={0.05}
            value={prefs.lineHeight}
            onChange={(e) => set('lineHeight', Number(e.target.value))}
            style={{ width: '160px' }}
          />
        </Row>
        <Row label="Schriftart">
          <select className="input-field" value={prefs.fontFamily} onChange={(e) => set('fontFamily', e.target.value as ReaderPrefsShape['fontFamily'])}>
            <option value="sans">Serifenlos</option>
            <option value="serif">Serif</option>
            <option value="mono">Monospace</option>
          </select>
        </Row>
        <Row label="Thema">
          <select className="input-field" value={prefs.theme} onChange={(e) => set('theme', e.target.value as ReaderPrefsShape['theme'])}>
            <option value="app">App</option>
            <option value="sepia">Sepia</option>
            <option value="paper">Papier</option>
            <option value="night">Nacht</option>
          </select>
        </Row>
        <Row label="Bionic Reading" hint="Wortanfänge fett — hilft manchen beim Tempo">
          <input type="checkbox" checked={prefs.bionic} onChange={(e) => set('bionic', e.target.checked)} />
          <SaveHint state={state} />
        </Row>
      </Card>

      <Card title="Weiteres" description="Bibliothek, Aussprache und Statistiken haben eigene Seiten.">
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <Link to="/audiobooks/pronunciation" className="btn btn-secondary btn-sm">Aussprache</Link>
          <Link to="/audiobooks/stats" className="btn btn-secondary btn-sm">Statistiken</Link>
          <Link to="/audiobooks" className="btn btn-ghost btn-sm">Zur Bibliothek</Link>
        </div>
      </Card>
    </div>
  );
}

// --- Podcasts ---------------------------------------------------------------

function PodcastTab() {
  const { prefs, update, state } = useNamespacePrefs<PodcastPrefs>('podcast');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <Card
        title="Voreinstellungen für neue Podcasts"
        description="Diese Werte sind beim Anlegen vorbelegt — pro Podcast bleibt alles änderbar."
      >
        <Row label="Format">
          <select
            className="input-field"
            value={prefs?.format ?? 'dialog'}
            onChange={(e) => update({ format: e.target.value } as Partial<PodcastPrefs>)}
            disabled={!prefs}
          >
            <option value="dialog">Dialog</option>
            <option value="monolog">Monolog</option>
            <option value="custom">Frei</option>
          </select>
        </Row>
        <Row label="Länge">
          <select
            className="input-field"
            value={prefs?.duration ?? 'medium'}
            onChange={(e) => update({ duration: e.target.value } as Partial<PodcastPrefs>)}
            disabled={!prefs}
          >
            <option value="short">Kurz</option>
            <option value="medium">Mittel</option>
            <option value="long">Lang</option>
          </select>
        </Row>
        <Row label="Audioformat">
          <select
            className="input-field"
            value={prefs?.audio_format ?? 'mp3'}
            onChange={(e) => update({ audio_format: e.target.value } as Partial<PodcastPrefs>)}
            disabled={!prefs}
          >
            <option value="mp3">MP3</option>
            <option value="wav">WAV</option>
          </select>
        </Row>
        <Row label="Sprache">
          <select
            className="input-field"
            value={prefs?.language ?? 'German'}
            onChange={(e) => update({ language: e.target.value } as Partial<PodcastPrefs>)}
            disabled={!prefs}
          >
            <option value="German">Deutsch</option>
            <option value="English">Englisch</option>
          </select>
        </Row>
        <Row
          label="Sprechpausen & Füllwörter"
          hint={['aus', 'dezent', 'natürlich', 'stark'][prefs?.disfluency_level ?? 2]}
        >
          <input
            type="range"
            min={0}
            max={3}
            step={1}
            value={prefs?.disfluency_level ?? 2}
            onChange={(e) => update({ disfluency_level: Number(e.target.value) } as Partial<PodcastPrefs>)}
            disabled={!prefs}
            style={{ width: '140px' }}
          />
          <SaveHint state={state} />
        </Row>
      </Card>

      <Card title="Besetzung" description="Sprecher verwaltest du im Sprecherbereich — 40 fertige Persönlichkeiten inklusive.">
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <Link to="/podcast/hosts" className="btn btn-primary btn-sm">Sprecherbereich öffnen</Link>
          <Link to="/podcast" className="btn btn-ghost btn-sm">Zum Studio</Link>
        </div>
      </Card>
    </div>
  );
}

// --- Lab --------------------------------------------------------------------

function LabTab() {
  const { prefs, update, state } = useNamespacePrefs<LabPrefs>('lab');
  const [voices, setVoices] = useState<Voice[]>([]);

  useEffect(() => {
    getVoices().then(setVoices).catch(() => undefined);
  }, []);

  /* Zwei Engines, zwei Zahlen — die rohen `source`-Strings sagen niemandem
     etwas, der nicht die Compose-Datei kennt. */
  const engines = useMemo(() => {
    const list = selectableVoices(voices);
    return [
      {
        key: 'omnivoice',
        label: 'OmniVoice',
        hint: 'GPU · Klonstimmen',
        count: list.filter((v) => v.source === 'omnivoice').length,
      },
      {
        key: 'kikiri',
        label: 'Kikiri',
        hint: 'CPU · Fallback',
        count: list.filter((v) => v.source === 'kikiri').length,
      },
    ].filter((e) => e.count > 0);
  }, [voices]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <Card title="Sprachausgabe" description="Vorbelegung der Sprachausgabe im Sprachstudio.">
        <Row label="Standardstimme" hint="Gilt für neu geöffnete Speech-Seiten">
          <select
            className="input-field"
            value={prefs?.speech_voice ?? ''}
            onChange={(e) => update({ speech_voice: e.target.value } as Partial<LabPrefs>)}
            disabled={!prefs}
          >
            <option value="">Keine Vorauswahl</option>
            <VoiceOptions voices={voices} />
          </select>
        </Row>
        <Row label="Ausgabeformat" hint="Beide Engines liefern WAV; MP3 wird serverseitig umgewandelt">
          <select
            className="input-field"
            value={prefs?.speech_format ?? 'mp3'}
            onChange={(e) => update({ speech_format: e.target.value } as Partial<LabPrefs>)}
            disabled={!prefs}
          >
            <option value="mp3">MP3</option>
            <option value="wav">WAV</option>
          </select>
        </Row>
        <Row label="Tempo" hint={`${(prefs?.speech_speed ?? 1).toFixed(2)}×`}>
          <input
            type="range"
            min={0.5}
            max={1.5}
            step={0.05}
            value={prefs?.speech_speed ?? 1}
            onChange={(e) => update({ speech_speed: Number(e.target.value) } as Partial<LabPrefs>)}
            disabled={!prefs}
            style={{ width: '140px' }}
          />
          <SaveHint state={state} />
        </Row>
      </Card>

      <Card title="Engines" description="Was gerade tatsächlich Stimmen anbietet.">
        {engines.length === 0 ? (
          <div style={{ fontSize: '12.5px', color: 'var(--color-text-dim)' }}>Lade Stimmen …</div>
        ) : (
          <div className="settings-engines">
            {engines.map((e) => (
              <div key={e.key} className="settings-engine">
                <span className="settings-engine-count">{e.count}</span>
                <span className="settings-engine-body">
                  <span>{e.label}</span>
                  <span>{e.hint}</span>
                </span>
              </div>
            ))}
          </div>
        )}
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <Link to="/voices" className="btn btn-secondary btn-sm">Stimmen ansehen</Link>
          <Link to="/clone" className="btn btn-ghost btn-sm">Stimme klonen</Link>
        </div>
      </Card>
    </div>
  );
}
