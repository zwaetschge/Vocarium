import {useRouteField} from '../hooks/useRouteField';
import { isValidElement, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import {
  getHosts,
  getPodcasts,
  getPodcast,
  createPodcast,
  deletePodcast,
  getPodcastSources,
  uploadPodcastSource,
  addUrlSource,
  addTextSource,
  deletePodcastSource,
  reprocessPodcastSource,
  generatePodcastScript,
  updateSegment,
  addSegment,
  deleteSegment,
  generatePodcastAudio,
  getPodcastAudioStreamUrl,
  downloadPodcastAudio,
  getVoices,
  getLanguages,
  getSegmentTags,
  updatePodcastCast,
} from '../api';
import type { SegmentTag } from '../api';
import Modal from '../components/Dialog';
import { PlayEdition } from '../components/StudioPlayer';
import { useDraftField, useHasDrafts, useEditorDraftActions } from '../state/EditorDrafts';
import { SingleOfflineButton } from '../components/OfflineControls';
import type {
  Host,
  Podcast,
  PodcastFormat,
  PodcastDuration,
  PodcastSource,
  ScriptSegment,
  PodcastProgressEvent,
  Voice,
} from '../types';
import { engineVoices as listEngineVoices } from '../voiceUtils';

export default function PodcastPage() {
  const retainDraft = useEditorDraftActions();
  const [hosts, setHosts] = useState<Host[]>([]);
  const [podcasts, setPodcasts] = useState<Podcast[]>([]);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [languages, setLanguages] = useState<string[]>([]);
  const [selectedPodcastId, setSelectedPodcastId] = useRouteField('episode', '');
  const [createPodcastOpen, setCreatePodcastOpen] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  // Podcast-Sprecher kommen aus den Klon-/Finetune-Engines (Qwen ist stillgelegt).
  const engineVoices = useMemo(() => listEngineVoices(voices), [voices]);
  const selectedPodcast = useMemo(
    () => podcasts.find((p) => p.id === selectedPodcastId) || null,
    [podcasts, selectedPodcastId],
  );

  const loadAll = async () => {
    setError('');
    try {
      const [hh, pp, vv] = await Promise.all([getHosts(), getPodcasts(), getVoices()]);
      setHosts(hh);
      setPodcasts(pp);
      setVoices(vv);
      if (!selectedPodcastId && pp.length > 0) setSelectedPodcastId(pp[0].id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Podcasts konnten nicht geladen werden');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAll();
    getLanguages()
      .then(setLanguages)
      .catch(() => setLanguages(['English', 'German', 'Chinese']));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const reloadPodcast = async (id: string) => {
    try {
      const fresh = await getPodcast(id);
      setPodcasts((prev) => {
        const exists = prev.some((p) => p.id === id);
        return exists ? prev.map((p) => (p.id === id ? fresh : p)) : [fresh, ...prev];
      });
    } catch {
      /* ignore */
    }
  };

  const reloadPodcastList = async () => {
    try {
      const pp = await getPodcasts();
      setPodcasts(pp);
    } catch {
      /* ignore */
    }
  };

  const handleCreate = async (payload: Parameters<typeof createPodcast>[0] & { source_text?: string }) => {
    try {
      const { source_text, ...settings } = payload;
      const pod = await createPodcast(settings);
      let sourceProblem = '';
      if (source_text?.trim()) {
        try { await addTextSource(pod.id, source_text.trim(), 'Erste Textquelle'); }
        catch { retainDraft(`podcast-source:${pod.id}:text`, source_text); sourceProblem = 'Podcast angelegt. Die erste Quelle konnte nicht gespeichert werden; ihr Text bleibt im Quellenformular erhalten.'; }
      }
      setPodcasts((prev) => [pod, ...prev]);
      setSelectedPodcastId(pod.id);
      setCreatePodcastOpen(false);
      if (sourceProblem) setError(sourceProblem);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Anlegen fehlgeschlagen');
      throw e;
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Diesen Podcast löschen? Das lässt sich nicht rückgängig machen.')) return;
    try {
      await deletePodcast(id);
      setPodcasts((prev) => prev.filter((p) => p.id !== id));
      if (selectedPodcastId === id) {
        const remaining = podcasts.filter((p) => p.id !== id);
        setSelectedPodcastId(remaining.length > 0 ? remaining[0].id : null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Löschen fehlgeschlagen');
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
      style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}
    >
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '14px',
          flexWrap: 'wrap',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <h1 style={{ fontSize: '28px', color: 'var(--color-text)' }}>Podcast Studio</h1>
          <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>
            Aus Quellen wird ein Skript, aus dem Skript ein mehrstimmiger Podcast.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '10px' }}>
          <Link
            to="/podcast/hosts"
            className="btn btn-secondary"
            style={{ height: '40px' }}
          >
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <circle cx="10" cy="6" r="3" />
              <path d="M3 18v-1a5 5 0 015-5h4a5 5 0 015 5v1" />
            </svg>
            Sprecher ({hosts.length})
          </Link>
          <motion.button
            whileTap={{ scale: 0.97 }}
            className="btn btn-primary"
            onClick={() => setCreatePodcastOpen(true)}
            style={{ height: '40px' }}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M8 2v12M2 8h12" />
            </svg>
            Neuer Podcast
          </motion.button>
        </div>
      </div>

      {/* Custom voice gate hint */}
      {engineVoices.length === 0 && (
        <div
          className="card-subtle"
          style={{
            padding: '12px 16px',
            display: 'flex',
            alignItems: 'center',
            gap: '12px',
            borderColor: 'rgba(244, 193, 82, 0.25)',
            background: 'var(--color-warning-dim)',
          }}
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="var(--color-warning)" strokeWidth="1.5" strokeLinecap="round">
            <path d="M8 1.5l7 13H1l7-13zM8 6v4M8 12v.5" />
          </svg>
          <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>
            Du hast noch keine eigenen Stimmen. Podcasts brauchen geklonte oder feingetunte Stimmen — öffne im Sprachstudio <em>Stimme klonen</em> und kehre anschließend hierher zurück.
          </div>
        </div>
      )}

      {/* Error */}
      <AnimatePresence>
        {error && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            style={{
              padding: '12px 16px',
              borderRadius: '12px',
              background: 'var(--color-danger-dim)',
              border: '1px solid rgba(255, 90, 101, 0.3)',
              color: 'var(--color-danger)',
              fontSize: '13.5px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: '10px',
            }}
          >
            <span>{error}</span>
            <button className="btn-ghost" onClick={() => setError('')} style={{ padding: '2px 8px', fontSize: '11px' }}>
              Dismiss
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Two-pane layout */}
      <div className="podcast-grid">
        {/* Podcast list */}
        <PodcastList
          podcasts={podcasts}
          selectedId={selectedPodcastId}
          onSelect={setSelectedPodcastId}
          onDelete={handleDelete}
          loading={loading}
        />

        {/* Detail pane */}
        {selectedPodcast ? (
          <PodcastDetail
            key={selectedPodcast.id}
            podcast={selectedPodcast}
            hosts={hosts}
            engineVoices={engineVoices}
            onReload={() => reloadPodcast(selectedPodcast.id)}
            onReloadList={reloadPodcastList}
            onError={setError}
          />
        ) : (
          <EmptyPane onNew={() => setCreatePodcastOpen(true)} canCreate={engineVoices.length >= 1 && hosts.length >= 1} />
        )}
      </div>

      <AnimatePresence>
        {createPodcastOpen && (
          <CreatePodcastModal
            hosts={hosts}
            languages={languages}
            onCreate={handleCreate}
            onClose={() => setCreatePodcastOpen(false)}
          />
        )}
      </AnimatePresence>
    </motion.div>
  );
}

/* ────────────────── Podcast list (left column) ────────────────── */

function PodcastList({
  podcasts,
  selectedId,
  onSelect,
  onDelete,
  loading,
}: {
  podcasts: Podcast[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  loading: boolean;
}) {
  return (
    <div className="card podcast-library" style={{ padding: '12px', display: 'flex', flexDirection: 'column', gap: '4px', minHeight: '220px' }}>
      <div style={{ padding: '6px 10px 10px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span className="label-eyebrow" style={{ fontSize: '10px' }}>Bibliothek</span>
        <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>{podcasts.length}</span>
      </div>
      {loading ? (
        <div style={{ padding: '20px 12px', fontSize: '12px', color: 'var(--color-text-dim)' }}>Lädt…</div>
      ) : podcasts.length === 0 ? (
        <div style={{ padding: '18px 12px', fontSize: '12.5px', color: 'var(--color-text-dim)', lineHeight: 1.5 }}>
          Noch keine Podcasts. Leg einen an, um loszulegen.
        </div>
      ) : (
        podcasts.map((p) => {
          const active = p.id === selectedId;
          return (
            <motion.div
              key={p.id}
              whileTap={{ scale: 0.99 }}
              style={{
                padding: '12px 12px',
                borderRadius: '10px',
                cursor: 'pointer',
                background: active ? 'rgba(123,97,255,0.14)' : 'transparent',
                border: `1px solid ${active ? 'rgba(123,97,255,0.28)' : 'transparent'}`,
                transition: 'background 0.15s ease, border-color 0.15s ease',
                display: 'flex',
                alignItems: 'flex-start',
                justifyContent: 'space-between',
                gap: '8px',
              }}
              onMouseEnter={(e) => {
                if (!active) e.currentTarget.style.background = 'rgba(255,255,255,0.04)';
              }}
              onMouseLeave={(e) => {
                if (!active) e.currentTarget.style.background = 'transparent';
              }}
            >
              <button type="button" className="podcast-select" aria-pressed={active} onClick={() => onSelect(p.id)} style={{ minWidth: 0, flex: 1 }}>
                <div
                  style={{
                    fontSize: '13.5px',
                    fontWeight: 500,
                    color: 'var(--color-text)',
                    letterSpacing: 0,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {p.topic || 'Ohne Titel'}
                </div>
                <div
                  style={{
                    marginTop: '4px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    fontSize: '10.5px',
                    color: 'var(--color-text-dim)',
                    fontFamily: 'var(--font-mono)',
                  }}
                >
                  <span>{FORMAT_LABELS[p.format] || p.format}</span>
                  <span style={{ opacity: 0.4 }}>·</span>
                  <span>{DURATION_LABELS[p.duration] || p.duration}</span>
                  <span style={{ opacity: 0.4 }}>·</span>
                  <StatusPill status={p.status} />
                </div>
              </button>
              <button
                className="btn-ghost"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(p.id);
                }}
                title="Löschen"
                style={{
                  padding: '3px 5px',
                  borderRadius: '6px',
                  color: 'var(--color-text-faint)',
                  flexShrink: 0,
                }}
                onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--color-danger)')}
                onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--color-text-faint)')}
              >
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                  <path d="M3 4h10M6 4V2h4v2M5 4l1 10h4l1-10" />
                </svg>
              </button>
            </motion.div>
          );
        })
      )}
    </div>
  );
}

function StatusPill({ status }: { status: Podcast['status'] }) {
  const styles: Record<Podcast['status'], { label: string; color: string; bg: string }> = {
    draft: { label: 'Entwurf', color: 'var(--color-text-dim)', bg: 'rgba(255,255,255,0.05)' },
    generating_script: { label: 'Skript…', color: 'var(--color-warning)', bg: 'var(--color-warning-dim)' },
    script_ready: { label: 'Skript fertig', color: 'var(--color-accent-hover)', bg: 'var(--color-accent-dim)' },
    generating_audio: { label: 'Audio…', color: 'var(--color-warning)', bg: 'var(--color-warning-dim)' },
    ready: { label: 'Fertig', color: 'var(--color-success)', bg: 'var(--color-success-dim)' },
    cancelled: { label: 'Abgebrochen', color: 'var(--color-text-dim)', bg: 'rgba(255,255,255,0.05)' },
    error: { label: 'Fehler', color: 'var(--color-danger)', bg: 'var(--color-danger-dim)' },
  };
  const s = styles[status];
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '1px 6px',
        borderRadius: '4px',
        fontSize: '9px',
        textTransform: 'uppercase',
        letterSpacing: 0,
        color: s.color,
        background: s.bg,
        fontWeight: 500,
      }}
    >
      {s.label}
    </span>
  );
}

function EmptyPane({ onNew, canCreate }: { onNew: () => void; canCreate: boolean }) {
  return (
    <div
      className="card"
      style={{
        padding: '56px 32px',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        textAlign: 'center',
        gap: '14px',
      }}
    >
      <div
        style={{
          width: '56px',
          height: '56px',
          borderRadius: '16px',
          background: 'var(--color-accent-dim)',
          border: '1px solid rgba(123,97,255,0.22)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--color-accent-hover)',
        }}
      >
        <svg width="22" height="22" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <rect x="8" y="2" width="4" height="10" rx="2" />
          <path d="M5 9a5 5 0 0010 0M10 14v3M7 17h6" />
        </svg>
      </div>
      <div style={{ fontSize: '15px', fontWeight: 500, color: 'var(--color-text)', letterSpacing: 0 }}>
        Kein Podcast ausgewählt
      </div>
      <div style={{ fontSize: '13px', color: 'var(--color-text-dim)', maxWidth: '360px', lineHeight: 1.5 }}>
        Leg einen Podcast an: Quellen hochladen, Skript schreiben lassen, Audio rendern.
      </div>
      {canCreate && (
        <button className="btn btn-primary" style={{ marginTop: '6px' }} onClick={onNew}>
          Podcast anlegen
        </button>
      )}
      {!canCreate && (
        <div style={{ fontSize: '11.5px', color: 'var(--color-warning)', marginTop: '6px' }}>
          Dafür brauchst du mindestens eine eigene Stimme und einen Sprecher.
        </div>
      )}
    </div>
  );
}

/* ────────────────── Podcast detail (right column) ────────────────── */


type DetailTab = 'sources' | 'script' | 'audio' | 'cast';

const FORMAT_LABELS: Record<string, string> = {
  dialog: 'Dialog',
  monolog: 'Monolog',
  custom: 'Frei',
};

const DURATION_LABELS: Record<string, string> = {
  short: 'kurz',
  medium: 'mittel',
  long: 'lang',
};

// Die Sprache steht als englischer Name in der DB; die Oberfläche ist deutsch.
const LANGUAGE_LABELS: Record<string, string> = {
  German: 'Deutsch',
  English: 'Englisch',
  French: 'Französisch',
  Spanish: 'Spanisch',
  Italian: 'Italienisch',
};

/**
 * Skript- und Audio-Erzeugung leben hier oben statt in den Panels: die Kopfzeile
 * zeigt den nächsten Schritt als einen Knopf, und der Fortschritt bleibt sichtbar,
 * egal welcher Reiter gerade offen ist.
 */
function usePodcastJobs(
  podcast: Podcast,
  onReload: () => void,
  onReloadList: () => void,
  onError: (msg: string) => void,
) {
  const hasDrafts = useHasDrafts(`podcast:${podcast.id}:`);
  const [localScriptBusy, setScriptBusy] = useState(false);
  const [scriptProgress, setScriptProgress] = useState<PodcastProgressEvent | null>(null);
  const [localAudioBusy, setAudioBusy] = useState(false);
  const [audioProgress, setAudioProgress] = useState<PodcastProgressEvent | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const scriptBusy = localScriptBusy || podcast.status === 'generating_script';
  const audioBusy = localAudioBusy || podcast.status === 'generating_audio';
  const running = useRef(false);
  const reloadRef = useRef(onReload);
  reloadRef.current = onReload;
  useEffect(() => {
    if (!scriptBusy && !audioBusy) return;
    const timer = window.setInterval(() => reloadRef.current(), 3000);
    return () => window.clearInterval(timer);
  }, [scriptBusy, audioBusy]);

  const generateScript = async () => {
    if (hasDrafts) { onError('Bitte Segmententwürfe zuerst speichern oder verwerfen.'); return; }
    if (scriptBusy || audioBusy || running.current) return;
    running.current = true;
    setScriptBusy(true);
    setScriptProgress(null);
    const finish = () => {
      running.current = false;
      setScriptBusy(false);
      setScriptProgress(null);
      onReload();
      onReloadList();
    };
    try {
      await generatePodcastScript(podcast.id, setScriptProgress, finish, (err) => {
        onError(err);
        finish();
      });
    } catch (e) {
      running.current = false;
      onReload();
      setScriptBusy(false);
      setScriptProgress(null);
      onError(e instanceof Error ? e.message : 'Skript-Erzeugung fehlgeschlagen');
    }
  };

  const generateAudio = async (force = false) => {
    if (hasDrafts) { onError('Bitte Segmententwürfe zuerst speichern oder verwerfen.'); return; }
    if (scriptBusy || audioBusy || running.current) return;
    running.current = true;
    const controller = new AbortController();
    abortRef.current = controller;
    setAudioBusy(true);
    setAudioProgress(null);
    const finish = () => {
      running.current = false;
      setAudioBusy(false);
      setAudioProgress(null);
      onReload();
      onReloadList();
    };
    try {
      await generatePodcastAudio(
        podcast.id,
        setAudioProgress,
        finish,
        (err) => {
          if (err !== 'cancelled') onError(err);
          finish();
        },
        force,
        controller.signal,
      );
    } catch (e) {
      running.current = false;
      onReload();
      setAudioBusy(false);
      setAudioProgress(null);
      onError(e instanceof Error ? e.message : 'Audio-Erzeugung fehlgeschlagen');
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const cancelAudio = () => {
    abortRef.current?.abort();
    setAudioBusy(false);
    setAudioProgress(null);
    onReload();
    onReloadList();
  };

  const downloadAudio = async () => {
    try {
      const blob = await downloadPodcastAudio(podcast.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${(podcast.topic || 'podcast').replace(/[^a-z0-9]+/gi, '-').toLowerCase()}.${podcast.audio_format || 'wav'}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Download fehlgeschlagen');
    }
  };

  return {
    hasDrafts,
    scriptBusy,
    scriptProgress,
    generateScript,
    audioBusy,
    audioProgress,
    generateAudio,
    cancelAudio,
    downloadAudio,
  };
}

type PodcastJobs = ReturnType<typeof usePodcastJobs>;

function PodcastDetail({
  podcast,
  hosts,
  engineVoices,
  onReload,
  onReloadList,
  onError,
}: {
  podcast: Podcast;
  hosts: Host[];
  engineVoices: Voice[];
  onReload: () => void;
  onReloadList: () => void;
  onError: (msg: string) => void;
}) {
  const segments = podcast.script?.segments || [];
  const hasAudio = !!podcast.audio_path;
  const jobs = usePodcastJobs(podcast, onReload, onReloadList, onError);
  const [sourceCount, setSourceCount] = useState<number | null>(null);
  // Der Elternteil setzt key={podcast.id}, die Komponente startet also je Podcast
  // neu — der Startreiter darf deshalb aus dem Initialwert kommen.
  const [rawTab, setTab] = useRouteField('view',
    hasAudio ? 'audio' : segments.length > 0 ? 'script' : 'sources',
  );

  const tab = ['sources','script','audio','cast'].includes(rawTab) ? rawTab : 'sources';

  const tabs: { id: DetailTab; label: string; badge?: string }[] = [
    { id: 'sources', label: 'Quellen', badge: sourceCount ? String(sourceCount) : undefined },
    { id: 'script', label: 'Skript', badge: segments.length ? String(segments.length) : undefined },
    { id: 'audio', label: 'Audio', badge: hasAudio ? '✓' : undefined },
    { id: 'cast', label: 'Besetzung', badge: podcast.hosts.length ? String(podcast.hosts.length) : undefined },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', minWidth: 0 }}>
      <DetailHeader
        podcast={podcast}
        jobs={jobs}
        segmentCount={segments.length}
        hasAudio={hasAudio}
        onJump={setTab}
      />

      {jobs.hasDrafts && <p role="status" className="draft-status">Ungespeicherte Segmententwürfe. Vor einer neuen Produktion speichern oder verwerfen.</p>}
      <nav className="podcast-tabs" aria-label="Podcastansichten">
        {tabs.map((t) => (
          <button
            key={t.id}
            aria-current={tab === t.id ? 'page' : undefined}
            className={`podcast-tab${tab === t.id ? ' podcast-tab-active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.badge && <span className="podcast-tab-badge">{t.badge}</span>}
          </button>
        ))}
      </nav>

      {tab === 'sources' && (
        <SourcesCard podcast={podcast} onError={onError} onCount={setSourceCount} />
      )}
      {tab === 'script' && (
        <ScriptCard podcast={podcast} jobs={jobs} onReload={onReload} onError={onError} />
      )}
      {tab === 'audio' && <AudioCard podcast={podcast} jobs={jobs} />}
      {tab === 'cast' && (
        <EpisodeCast podcast={podcast} allHosts={hosts} engineVoices={engineVoices} disabled={jobs.audioBusy || jobs.scriptBusy} onReload={onReload} onError={onError} />
      )}
    </div>
  );
}

/** Titel, Status, Kennzahlen und — immer sichtbar — der nächste Schritt. */
function DetailHeader({
  podcast,
  jobs,
  segmentCount,
  hasAudio,
  onJump,
}: {
  podcast: Podcast;
  jobs: PodcastJobs;
  segmentCount: number;
  hasAudio: boolean;
  onJump: (tab: DetailTab) => void;
}) {
  const busy = jobs.scriptBusy || jobs.audioBusy;
  const progress = jobs.scriptBusy ? jobs.scriptProgress : jobs.audioProgress;

  const primary = (() => {
    if (segmentCount === 0) {
      return {
        label: 'Skript erzeugen',
        run: () => {
          onJump('script');
          jobs.generateScript();
        },
      };
    }
    if (!hasAudio) {
      return {
        label: 'Audio rendern',
        run: () => {
          onJump('audio');
          jobs.generateAudio(false);
        },
      };
    }
    return { label: 'Herunterladen', run: jobs.downloadAudio };
  })();

  return (
    <div className="card podcast-header">
      <div className="podcast-header-row">
        <div style={{ minWidth: 0, flex: 1 }}>
          <h2 className="podcast-header-title">{podcast.topic || 'Ohne Titel'}</h2>
          <div className="podcast-header-meta">
            <StatusPill status={podcast.status} />
            <span>{FORMAT_LABELS[podcast.format] || podcast.format}</span>
            <span className="podcast-meta-dot">·</span>
            <span>{DURATION_LABELS[podcast.duration] || podcast.duration}</span>
            <span className="podcast-meta-dot">·</span>
            <span>{LANGUAGE_LABELS[podcast.language] || podcast.language || 'auto'}</span>
            <span className="podcast-meta-dot">·</span>
            <span>{podcast.hosts.length} Stimmen</span>
            {segmentCount > 0 && (
              <>
                <span className="podcast-meta-dot">·</span>
                <span>
                  {segmentCount} Segmente · {podcast.script?.total_words || 0} Wörter
                </span>
              </>
            )}
            {hasAudio && (
              <>
                <span className="podcast-meta-dot">·</span>
                <span>{formatClock(podcast.audio_duration)}</span>
              </>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
          {jobs.audioBusy && (
            <button className="btn btn-ghost" onClick={jobs.cancelAudio} style={{ height: '38px' }}>
              Abbrechen
            </button>
          )}
          <motion.button
            whileTap={{ scale: 0.97 }}
            className="btn btn-primary"
            onClick={primary.run}
            disabled={busy || (jobs.hasDrafts && !hasAudio)}
            style={{ height: '38px' }}
          >
            {busy ? (
              <>
                <svg
                  style={{ width: 13, height: 13, animation: 'spin 0.9s linear infinite' }}
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                >
                  <circle cx="12" cy="12" r="10" strokeDasharray="60" strokeDashoffset="20" strokeLinecap="round" opacity="0.8" />
                </svg>
                {progress ? progress.stage : 'Läuft'}
              </>
            ) : (
              primary.label
            )}
          </motion.button>
        </div>
      </div>

      <AnimatePresence>{busy && <JobProgress progress={progress} />}</AnimatePresence>

      {podcast.error_message && !busy && (
        <div
          style={{
            marginTop: '12px',
            padding: '10px 12px',
            borderRadius: '10px',
            background: 'var(--color-danger-dim)',
            border: '1px solid rgba(255,90,101,0.25)',
            color: 'var(--color-danger)',
            fontSize: '12.5px',
          }}
        >
          {podcast.error_message}
        </div>
      )}
    </div>
  );
}

/** Der Fortschrittsbalken, den Skript- und Audiolauf sich teilen. */
function JobProgress({ progress }: { progress: PodcastProgressEvent | null }) {
  const percent = progress ? progressPercent(progress.progress) : 0;
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      role="status"
      style={{ marginTop: '14px', overflow: 'hidden' }}
    >
      <div
        style={{
          padding: '12px 14px',
          borderRadius: '12px',
          background: 'var(--color-accent-dim)',
          border: '1px solid rgba(123,97,255,0.24)',
          display: 'flex',
          flexDirection: 'column',
          gap: '10px',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '12.5px', gap: '10px' }}>
          <span style={{ color: 'var(--color-text)' }}>{progress?.message || 'Wird vorbereitet…'}</span>
          <span style={{ color: 'var(--color-accent-hover)', fontFamily: 'var(--font-mono)', fontSize: '11px', flexShrink: 0 }}>
            {Math.round(percent)}%
            {progress?.segment_position != null && <> · Seg. {progress.segment_position + 1}</>}
          </span>
        </div>
        <div style={{ height: '3px', borderRadius: '999px', background: 'rgba(255,255,255,0.08)', overflow: 'hidden' }}>
          <motion.div
            animate={{ width: `${percent}%` }}
            transition={{ duration: 0.25, ease: 'easeOut' }}
            style={{
              height: '100%',
              background: 'linear-gradient(90deg, var(--color-accent), var(--color-aurora-2))',
              boxShadow: '0 0 10px rgba(123,97,255,0.5)',
            }}
          />
        </div>
      </div>
    </motion.div>
  );
}

function formatClock(seconds: number): string {
  if (!seconds || seconds < 0) return '0:00';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}



/* ────────────────── Sources card ────────────────── */

function SourcesCard({
  podcast,
  onError,
  onCount,
}: {
  podcast: Podcast;
  onError: (msg: string) => void;
  onCount?: (n: number) => void;
}) {
  const [sources, setSources] = useState<PodcastSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [url, setUrl] = useState('');
  const [urlTitle, setUrlTitle] = useState('');
  const [textBody, setTextBody] = useDraftField(`podcast-source:${podcast.id}:text`, '');
  const [mode, setMode] = useState<'none' | 'file' | 'url' | 'text'>(textBody ? 'text' : 'none');
  const [textTitle, setTextTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadSources = async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const s = await getPodcastSources(podcast.id);
      setSources(s);
      onCount?.(s.length);
    } catch (e) {
      if (!quiet) onError(e instanceof Error ? e.message : 'Quellen konnten nicht geladen werden');
    } finally {
      if (!quiet) setLoading(false);
    }
  };

  useEffect(() => {
    loadSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [podcast.id]);

  // Verarbeitung laeuft im Hintergrund; ohne Nachfassen bliebe eine Quelle in
  // der Oberflaeche auf "pending" stehen, bis der Nutzer die Seite neu laedt.
  const pending = sources.some((s) => s.status === 'pending');
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => loadSources(true), 2500);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending, podcast.id]);

  const handleFile = async (file: File) => {
    setBusy(true);
    try {
      await uploadPodcastSource(podcast.id, file);
      await loadSources();
      setMode('none');
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Upload fehlgeschlagen');
    } finally {
      setBusy(false);
    }
  };

  const handleUrl = async () => {
    if (!url.trim()) return;
    setBusy(true);
    try {
      const parsed = new URL(url.trim());
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('Bitte eine vollständige HTTP- oder HTTPS-Adresse eingeben.');
      await addUrlSource(podcast.id, url.trim(), urlTitle.trim() || undefined);
      await loadSources();
      setUrl('');
      setUrlTitle('');
      setMode('none');
    } catch (e) {
      onError(e instanceof Error ? e.message : 'URL-Quelle fehlgeschlagen');
    } finally {
      setBusy(false);
    }
  };

  const handleText = async () => {
    if (!textBody.trim()) return;
    setBusy(true);
    try {
      await addTextSource(podcast.id, textBody.trim(), textTitle.trim() || undefined);
      await loadSources();
      setTextBody('');
      setTextTitle('');
      setMode('none');
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Text-Quelle fehlgeschlagen');
    } finally {
      setBusy(false);
    }
  };

  const handleReprocess = async (sourceId: string) => {
    try {
      const updated = await reprocessPodcastSource(podcast.id, sourceId);
      setSources((prev) => prev.map((s) => (s.id === sourceId ? updated : s)));
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Erneute Verarbeitung fehlgeschlagen');
    }
  };

  const handleDelete = async (sourceId: string) => {
    if (!confirm('Diese Quelle löschen?')) return;
    try {
      await deletePodcastSource(podcast.id, sourceId);
      setSources((prev) => {
        const next = prev.filter((s) => s.id !== sourceId);
        onCount?.(next.length);
        return next;
      });
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Löschen fehlgeschlagen');
    }
  };

  return (
    <div className="card" style={{ padding: '22px 24px' }}>
      <SectionHeader
        eyebrow="Quellen"
        title={`${sources.length} ${sources.length === 1 ? 'Quelle' : 'Quellen'}`}
        trailing={
          <div style={{ display: 'flex', gap: '6px' }}>
            <ModeButton active={mode === 'file'} onClick={() => setMode(mode === 'file' ? 'none' : 'file')}>
              Datei
            </ModeButton>
            <ModeButton active={mode === 'url'} onClick={() => setMode(mode === 'url' ? 'none' : 'url')}>
              URL
            </ModeButton>
            <ModeButton active={mode === 'text'} onClick={() => setMode(mode === 'text' ? 'none' : 'text')}>
              Text
            </ModeButton>
          </div>
        }
      />

      <AnimatePresence mode="wait">
        {mode === 'file' && (
          <motion.div
            key="file"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            style={{ overflow: 'hidden', marginTop: '14px' }}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept=".md,.pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.odt,.rtf,.txt,.html,.htm,.epub,.xml"
              style={{ display: 'none' }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) handleFile(f);
                if (fileInputRef.current) fileInputRef.current.value = '';
              }}
            />
            <button
              className="btn btn-secondary"
              onClick={() => fileInputRef.current?.click()}
              disabled={busy}
              style={{ width: '100%', padding: '14px', borderStyle: 'dashed' }}
            >
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M8 2v10M4 6l4-4 4 4M2 14h12" />
              </svg>
              {busy ? 'Lädt hoch…' : 'Datei wählen (PDF, DOCX, MD, TXT, HTML, EPUB…)'}
            </button>
            <div style={{ fontSize: '11px', color: 'var(--color-text-faint)', marginTop: '6px' }}>
              Max 50 MB · wird per Docling geparst
            </div>
          </motion.div>
        )}

        {mode === 'url' && (
          <motion.div
            key="url"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            style={{ overflow: 'hidden', marginTop: '14px', display: 'flex', flexDirection: 'column', gap: '8px' }}
          >
            <input
              type="url"
              placeholder="https://example.com/article"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              className="input-field"
            />
            <input
              type="text"
              placeholder="Titel (optional)"
              value={urlTitle}
              onChange={(e) => setUrlTitle(e.target.value)}
              className="input-field"
            />
            <button className="btn btn-primary" onClick={handleUrl} disabled={!url.trim() || busy}>
              {busy ? 'Lädt…' : 'URL hinzufügen'}
            </button>
          </motion.div>
        )}

        {mode === 'text' && (
          <motion.div
            key="text"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            style={{ overflow: 'hidden', marginTop: '14px', display: 'flex', flexDirection: 'column', gap: '8px' }}
          >
            <input
              type="text"
              placeholder="Titel (optional)"
              value={textTitle}
              onChange={(e) => setTextTitle(e.target.value)}
              className="input-field"
            />
            <textarea
              placeholder="Text einfügen (10–100.000 Zeichen)…"
              value={textBody}
              onChange={(e) => setTextBody(e.target.value)}
              className="input-field"
              style={{ minHeight: '120px', resize: 'vertical', fontFamily: 'var(--font-body)', lineHeight: 1.5 }}
            />
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                {textBody.length.toLocaleString()} Zeichen
              </span>
              <button className="btn btn-primary" onClick={handleText} disabled={!textBody.trim() || busy}>
                {busy ? 'Fügt hinzu…' : 'Text hinzufügen'}
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div style={{ marginTop: sources.length > 0 || mode !== 'none' ? '14px' : '10px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {loading ? (
          <div style={{ fontSize: '12px', color: 'var(--color-text-dim)', padding: '8px 0' }}>Lädt…</div>
        ) : sources.length === 0 ? (
          <div style={{ fontSize: '12.5px', color: 'var(--color-text-dim)', padding: '8px 0' }}>
            Mindestens eine Quelle hinzufügen, damit ein Skript entstehen kann.
          </div>
        ) : (
          sources.map((s) => (
            <div
              key={s.id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                padding: '10px 12px',
                borderRadius: '10px',
                background: 'rgba(255,255,255,0.02)',
                border: '1px solid rgba(255,255,255,0.06)',
              }}
            >
              <SourceIcon type={s.type} />
              <div style={{ minWidth: 0, flex: 1 }}>
                <div
                  style={{
                    fontSize: '13px',
                    color: 'var(--color-text)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {s.title || s.url || '(ohne Titel)'}
                </div>
                <div
                  style={{
                    fontSize: '10.5px',
                    color: 'var(--color-text-dim)',
                    fontFamily: 'var(--font-mono)',
                    display: 'flex',
                    gap: '8px',
                    marginTop: '2px',
                  }}
                >
                  <span>{s.type}</span>
                  <span style={{ opacity: 0.4 }}>·</span>
                  <span style={{ color: statusColor(s.status) }}>{statusLabel(s.status)}</span>
                  {s.chunk_count > 0 && (
                    <>
                      <span style={{ opacity: 0.4 }}>·</span>
                      <span>{s.chunk_count} Abschnitte</span>
                    </>
                  )}
                </div>
                {/* Der Fehler entsteht erst in der Hintergrundverarbeitung —
                    ohne ihn hier steht nur "fehlgeschlagen" ohne Grund. */}
                {s.status === 'failed' && s.error_message && (
                  <div
                    style={{
                      fontSize: '11px',
                      color: 'var(--color-danger)',
                      marginTop: '4px',
                      lineHeight: 1.45,
                      wordBreak: 'break-word',
                    }}
                  >
                    {s.error_message}
                  </div>
                )}
              </div>
              {s.status === 'failed' && (
                <button
                  className="btn-ghost"
                  onClick={() => handleReprocess(s.id)}
                  style={{ padding: '4px 8px', fontSize: '11.5px', color: 'var(--color-text-dim)' }}
                  title="Quelle erneut verarbeiten"
                >
                  Erneut
                </button>
              )}
              <button
                className="btn-ghost"
                onClick={() => handleDelete(s.id)}
                style={{ padding: '4px 6px', color: 'var(--color-text-faint)' }}
                onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--color-danger)')}
                onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--color-text-faint)')}
                title="Löschen"
              >
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                  <path d="M3 4h10M6 4V2h4v2M5 4l1 10h4l1-10" />
                </svg>
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function ModeButton({ children, active, onClick }: { children: React.ReactNode; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '6px 12px',
        borderRadius: '8px',
        fontSize: '12px',
        fontWeight: 500,
        border: `1px solid ${active ? 'rgba(123,97,255,0.32)' : 'rgba(255,255,255,0.08)'}`,
        background: active ? 'var(--color-accent-dim)' : 'rgba(255,255,255,0.03)',
        color: active ? 'var(--color-accent-hover)' : 'var(--color-text-secondary)',
        transition: 'all 0.15s ease',
      }}
    >
      {children}
    </button>
  );
}

function SourceIcon({ type }: { type: PodcastSource['type'] }) {
  const color = 'var(--color-text-dim)';
  if (type === 'url') {
    return (
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round">
        <circle cx="8" cy="8" r="6" />
        <path d="M2 8h12M8 2c2 2 3 4 3 6s-1 4-3 6M8 2c-2 2-3 4-3 6s1 4 3 6" />
      </svg>
    );
  }
  if (type === 'text') {
    return (
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round">
        <path d="M3 4h10M3 8h10M3 12h8" />
      </svg>
    );
  }
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round">
      <path d="M3 2h7l3 3v9H3z" />
      <path d="M10 2v3h3" />
    </svg>
  );
}

function statusLabel(status: string) {
  if (status === 'processed') return 'verarbeitet';
  if (status === 'failed') return 'fehlgeschlagen';
  return 'wird verarbeitet…';
}

function statusColor(status: string) {
  if (status === 'processed') return 'var(--color-success)';
  if (status === 'failed') return 'var(--color-danger)';
  return 'var(--color-warning)';
}

/* ────────────────── Script card ────────────────── */


function ScriptCard({
  podcast,
  jobs,
  onReload,
  onError,
}: {
  podcast: Podcast;
  jobs: PodcastJobs;
  onReload: () => void;
  onError: (msg: string) => void;
}) {
  const scriptSegments = podcast.script?.segments;
  const segments = useMemo(() => scriptSegments || [], [scriptSegments]);
  const [query, setQuery] = useState('');
  const [speakerFilter, setSpeakerFilter] = useState<string>('');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const speakers = useMemo(() => {
    const seen = new Set<string>();
    for (const s of segments) if (s.speaker) seen.add(s.speaker);
    return Array.from(seen).sort();
  }, [segments]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return segments.filter((s) => {
      if (speakerFilter && s.speaker !== speakerFilter) return false;
      if (!q) return true;
      return (s.text || '').toLowerCase().includes(q) || (s.prompt || '').toLowerCase().includes(q);
    });
  }, [segments, query, speakerFilter]);

  if (segments.length === 0) {
    return (
      <div className="card" style={{ padding: '44px 28px', textAlign: 'center' }}>
        <div style={{ fontSize: '15px', fontWeight: 500, color: 'var(--color-text)' }}>Noch kein Skript</div>
        <div style={{ fontSize: '13px', color: 'var(--color-text-dim)', marginTop: '8px', lineHeight: 1.55 }}>
          Lade zuerst Quellen hoch, dann schreibt das Sprachmodell daraus einen mehrstimmigen Dialog.
        </div>
        <button
          className="btn btn-primary"
          onClick={jobs.generateScript}
          disabled={jobs.scriptBusy || jobs.audioBusy || jobs.hasDrafts}
          style={{ marginTop: '18px' }}
        >
          Skript erzeugen
        </button>
      </div>
    );
  }

  return (
    <div className="card" style={{ padding: '18px 20px' }}>
      <div className="podcast-script-toolbar">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Skriptsuche"
          placeholder="Im Skript suchen…"
          className="input-field podcast-script-toolbar-search"
        />
        <select
          aria-label="Sprecherfilter"
          value={speakerFilter}
          onChange={(e) => setSpeakerFilter(e.target.value)}
          className="input-field"
          style={{ ...selectStyle, width: 'auto' }}
        >
          <option value="">Alle Sprecher</option>
          {speakers.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <span className="podcast-script-count">
          {visible.length}/{segments.length}
        </span>
        <button
          className="btn btn-ghost"
          onClick={jobs.generateScript}
          disabled={jobs.scriptBusy || jobs.audioBusy || jobs.hasDrafts}
          style={{ height: '32px', fontSize: '12px', flexShrink: 0 }}
          title="Skript komplett neu schreiben lassen"
        >
          Neu erzeugen
        </button>
      </div>

      <div style={{ marginTop: '14px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {visible.map((seg) => (
          <SegmentRow
            key={seg.id}
            podcastId={podcast.id}
            hosts={podcast.hosts}
            disabled={jobs.audioBusy || jobs.scriptBusy}
            segment={seg}
            editing={expandedId === seg.id}
            onEdit={(on) => setExpandedId(on ? seg.id : null)}
            onReload={onReload}
            onError={onError}
          />
        ))}
        {visible.length === 0 && (
          <div style={{ padding: '24px 6px', fontSize: '12.5px', color: 'var(--color-text-dim)', textAlign: 'center' }}>
            Kein Segment passt zum Filter.
          </div>
        )}
        {!query && !speakerFilter && (
          <AddTrackBar
            podcastId={podcast.id}
            position={segments.length}
            onReload={onReload}
            onError={onError}
          />
        )}
      </div>
    </div>
  );
}

function AddTrackBar({
  podcastId,
  position,
  onReload,
  onError,
}: {
  podcastId: string;
  position: number;
  onReload: () => void;
  onError: (msg: string) => void;
}) {
  const [pickerOpen, setPickerOpen] = useState<'music' | null>(null);
  const [prompt, setPrompt] = useState('');
  const [duration, setDuration] = useState('30');
  const [busy, setBusy] = useState(false);

  const handleAdd = async () => {
    if (!pickerOpen || !prompt.trim()) return;
    const durSec = parseFloat(duration) || 30;
    setBusy(true);
    try {
      await addSegment(podcastId, {
        type: pickerOpen,
        prompt: prompt.trim(),
        duration_ms: Math.round(durSec * 1000),
        position,
        volume_db: pickerOpen === 'music' ? -14 : 0,
      });
      setPickerOpen(null);
      setPrompt('');
      setDuration('30');
      onReload();
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Hinzufügen fehlgeschlagen');
    } finally {
      setBusy(false);
    }
  };

  if (pickerOpen) {
    return (
      <div
        style={{
          padding: '14px 16px',
          borderRadius: '12px',
          background: 'rgba(123,97,255,0.06)',
          border: '1px solid rgba(123,97,255,0.20)',
          display: 'flex',
          flexDirection: 'column',
          gap: '8px',
        }}
      >
        <div style={{ fontSize: '11px', color: 'var(--color-text-faint)', textTransform: 'uppercase', letterSpacing: 0 }}>
          Hintergrundmusik
        </div>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder={pickerOpen === 'music'
            ? 'z. B. ambient lo-fi piano, soft pad, contemplative'
            : 'z. B. rain on window, thunder distant'}
          className="input-field"
          style={{ minHeight: '60px', padding: '10px 12px', fontSize: '13px', lineHeight: 1.55 }}
        />
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <label style={{ fontSize: '12px', color: 'var(--color-text-faint)' }}>Dauer (s):</label>
          <input
            type="number"
            value={duration}
            onChange={(e) => setDuration(e.target.value)}
            min="1"
            max="300"
            className="input-field"
            style={{ padding: '6px 10px', width: '80px', fontSize: '12.5px' }}
          />
          <div style={{ flex: 1 }} />
          <button className="btn btn-primary" onClick={handleAdd} disabled={busy || !prompt.trim()} style={{ padding: '6px 12px', fontSize: '12px', height: '32px' }}>
            Einfügen
          </button>
          <button
            className="btn-ghost"
            onClick={() => { setPickerOpen(null); setPrompt(''); }}
            style={{ padding: '6px 12px', fontSize: '12px' }}
          >
            Abbrechen
          </button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', gap: '8px', justifyContent: 'center', padding: '4px 0' }}>
      <button
        className="btn-ghost"
        onClick={() => setPickerOpen('music')}
        style={{ padding: '6px 14px', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}
        title="Musikbett, das unter dem Dialog läuft"
      >
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M9 3v8.5" />
          <circle cx="7" cy="11.5" r="2" />
          <path d="M9 3l5 1v3l-5-1" />
        </svg>
        Musik
      </button>
    </div>
  );
}

const TYPE_STYLES: Record<string, { color: string; bg: string; border: string; label: string }> = {
  speech:   { color: 'var(--color-text-faint)',  bg: 'rgba(255,255,255,0.04)',     border: 'rgba(255,255,255,0.06)',  label: 'Sprache' },
  reaction: { color: 'var(--color-text-faint)',  bg: 'rgba(255,255,255,0.04)',     border: 'rgba(255,255,255,0.06)',  label: 'Reaktion' },
  pause:    { color: 'var(--color-text-faint)',  bg: 'rgba(255,255,255,0.04)',     border: 'rgba(255,255,255,0.06)',  label: 'Pause' },
  music:    { color: '#a888ff',                  bg: 'rgba(123,97,255,0.10)',      border: 'rgba(123,97,255,0.30)',  label: 'Musik' },
};

function progressPercent(value: number): number {
  const normalized = value <= 1 ? value * 100 : value;
  return Math.max(0, Math.min(100, normalized));
}


/** OmniVoice rendert diese Klammern als echten Laut, statt sie vorzulesen. */
const TAG_PATTERN = /\[([A-Za-z][A-Za-z-]{1,23})\]/g;

let tagCatalogPromise: Promise<SegmentTag[]> | null = null;

function useSegmentTags(): SegmentTag[] {
  const [tags, setTags] = useState<SegmentTag[]>([]);
  useEffect(() => {
    if (!tagCatalogPromise) tagCatalogPromise = getSegmentTags().catch(() => []);
    let alive = true;
    tagCatalogPromise.then((t) => alive && setTags(t));
    return () => {
      alive = false;
    };
  }, []);
  return tags;
}

/** Text mit hervorgehobenen Tags — der Rest bleibt normaler Fließtext. */
function renderTaggedText(text: string, known: Set<string>): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  TAG_PATTERN.lastIndex = 0;
  while ((m = TAG_PATTERN.exec(text)) !== null) {
    if (!known.has(m[1])) continue;
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(
      <span key={`${m.index}-${m[1]}`} className="podcast-tag-chip" title={m[1]}>
        {m[1]}
      </span>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out.length > 0 ? out : [text];
}

function SegmentRow({
  podcastId,
  segment,
  hosts,
  disabled,
  editing,
  onEdit,
  onReload,
  onError,
}: {
  podcastId: string;
  segment: ScriptSegment;
  hosts: Host[];
  disabled: boolean;
  editing: boolean;
  onEdit: (on: boolean) => void;
  onReload: () => void;
  onError: (msg: string) => void;
}) {
  const savedSpeaker = segment.speaker_id || hosts.find(h=>h.name === segment.speaker)?.id || '';
  const draftKey = `podcast:${podcastId}:${segment.id}`;
  const [text, setText, clearText] = useDraftField(`${draftKey}:text`, segment.text);
  const [speaker, setSpeaker, clearSpeaker] = useDraftField(`${draftKey}:speaker`, savedSpeaker);
  const [prompt, setPrompt, clearPrompt] = useDraftField(`${draftKey}:prompt`, segment.prompt || '');
  const [duration, setDuration, clearDuration] = useDraftField(`${draftKey}:duration`, segment.duration_ms ? String(segment.duration_ms / 1000) : '');
  const [overlap, setOverlap, clearOverlap] = useDraftField(`${draftKey}:overlap`, segment.overlap_ms ? String(segment.overlap_ms) : '');
  const [volume, setVolume, clearVolume] = useDraftField(`${draftKey}:volume`, segment.volume_db ? String(segment.volume_db) : '');
  const dirty = text !== segment.text || speaker !== savedSpeaker || prompt !== (segment.prompt || '') ||
    duration !== (segment.duration_ms ? String(segment.duration_ms / 1000) : '') ||
    overlap !== (segment.overlap_ms ? String(segment.overlap_ms) : '') || volume !== (segment.volume_db ? String(segment.volume_db) : '');
  const clearDraft = () => { clearText(); clearSpeaker(); clearPrompt(); clearDuration(); clearOverlap(); clearVolume(); };
  const [busy, setBusy] = useState(false);
  const textRef = useRef<HTMLTextAreaElement>(null);

  const isMedia = segment.type === 'music';
  const styleSet = TYPE_STYLES[segment.type] || TYPE_STYLES.speech;
  const tags = useSegmentTags();
  const knownTags = useMemo(() => new Set(tags.map((t) => t.id)), [tags]);

  const reset = clearDraft;

  // Tag an der Cursorposition einsetzen, nicht stumpf hinten anhängen — die
  // Wirkung hängt daran, wo im Satz der Laut fällt.
  const insertTag = (tag: string) => {
    const el = textRef.current;
    const token = `[${tag}]`;
    if (!el) {
      setText((t) => (t ? `${t} ${token}` : token));
      return;
    }
    const start = el.selectionStart ?? text.length;
    const end = el.selectionEnd ?? start;
    const before = text.slice(0, start);
    const after = text.slice(end);
    const spaced = `${before}${before && !/\s$/.test(before) ? ' ' : ''}${token}${after && !/^\s/.test(after) ? ' ' : ''}${after}`;
    setText(spaced);
    requestAnimationFrame(() => {
      const pos = before.length + (before && !/\s$/.test(before) ? 1 : 0) + token.length;
      el.focus();
      el.setSelectionRange(pos, pos);
    });
  };

  const handleSave = async () => {
    if (disabled || busy) return;
    setBusy(true);
    try {
      const payload: Parameters<typeof updateSegment>[2] = {
        text: text.trim(),
        ...(!isMedia ? { speaker_id:speaker, speaker:hosts.find(h=>h.id === speaker)?.name || segment.speaker } : {}),
      };
      if (isMedia) {
        payload.prompt = prompt.trim() || null;
        payload.duration_ms = duration ? Math.round(parseFloat(duration) * 1000) : 0;
        payload.volume_db = volume ? parseFloat(volume) : 0;
      }
      payload.overlap_ms = overlap ? parseInt(overlap, 10) || 0 : 0;
      await updateSegment(podcastId, segment.id, payload);
      clearDraft();
      onEdit(false);
      onReload();
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Speichern fehlgeschlagen');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm('Dieses Segment löschen?')) return;
    setBusy(true);
    try {
      await deleteSegment(podcastId, segment.id);
      clearDraft();
      onReload();
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Löschen fehlgeschlagen');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className={`podcast-segment${editing ? ' podcast-segment-editing' : ''}`}
      style={{
        background: isMedia ? styleSet.bg : undefined,
        borderColor: isMedia ? styleSet.border : undefined,
      }}
    >
      <div className="podcast-segment-gutter">#{segment.position + 1}{dirty && <span className="draft-indicator" title="Ungespeicherter Entwurf">●</span>}</div>

      <div className="podcast-segment-body">
        {dirty && <div role="status" className="draft-status">Ungespeicherter Entwurf · zum Fortsetzen bearbeiten</div>}
        {editing ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {!isMedia && (
              <label>Sprecher dieser Episode<select aria-label="Sprecher" className="input-field" value={speaker} onChange={e=>setSpeaker(e.target.value)}><option value="">Sprecher zuordnen</option>{hosts.map(h=><option key={h.id} value={h.id}>{h.name}</option>)}</select></label>
            )}
            {isMedia ? (
              <>
                <textarea
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  placeholder="Musik-Prompt (z. B. ambient lo-fi piano, contemplative)"
                  className="input-field"
                  style={{ minHeight: '60px', padding: '10px 12px', fontSize: '13px', lineHeight: 1.55 }}
                />
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
                  <label style={{ fontSize: '11px', color: 'var(--color-text-faint)' }}>Dauer (s):</label>
                  <input
                    type="number"
                    value={duration}
                    onChange={(e) => setDuration(e.target.value)}
                    min="1"
                    max="300"
                    className="input-field"
                    style={{ padding: '4px 8px', width: '70px', fontSize: '12px' }}
                  />
                  <label style={{ fontSize: '11px', color: 'var(--color-text-faint)' }}>Lautst. (dB):</label>
                  <input
                    type="number"
                    value={volume}
                    onChange={(e) => setVolume(e.target.value)}
                    min="-40"
                    max="6"
                    step="0.5"
                    className="input-field"
                    style={{ padding: '4px 8px', width: '70px', fontSize: '12px' }}
                  />
                  <label style={{ fontSize: '11px', color: 'var(--color-text-faint)' }}>Versatz (ms):</label>
                  <input
                    type="number"
                    value={overlap}
                    onChange={(e) => setOverlap(e.target.value)}
                    step="100"
                    className="input-field"
                    style={{ padding: '4px 8px', width: '80px', fontSize: '12px' }}
                  />
                </div>
              </>
            ) : (
              <>
                <textarea
                  aria-label="Gesprochener Text"
                  ref={textRef}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  className="input-field"
                  style={{ minHeight: '84px', padding: '10px 12px', fontSize: '13px', lineHeight: 1.55 }}
                />
                {tags.length > 0 && (
                  <div className="podcast-tag-palette">
                    <span className="podcast-tag-palette-label">Laute</span>
                    {tags.map((t) => (
                      <button
                        key={t.id}
                        type="button"
                        className="podcast-tag-button"
                        title={`${t.label} — ${t.hint}`}
                        onClick={() => insertTag(t.id)}
                      >
                        {t.label}
                      </button>
                    ))}
                  </div>
                )}
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                  <label style={{ fontSize: '11px', color: 'var(--color-text-faint)' }}>
                    Versatz (ms, &lt;0 = ins Wort fallen):
                  </label>
                  <input
                    type="number"
                    value={overlap}
                    onChange={(e) => setOverlap(e.target.value)}
                    step="50"
                    className="input-field"
                    style={{ padding: '4px 8px', width: '90px', fontSize: '12px' }}
                  />
                </div>
              </>
            )}
            <div style={{ display: 'flex', gap: '6px' }}>
              <button className="btn btn-primary" onClick={handleSave} disabled={busy || disabled} style={{ padding: '6px 12px', fontSize: '12px', height: '32px' }}>
                Speichern
              </button>
              <button
                className="btn-ghost"
                onClick={() => {
                  onEdit(false);
                  reset();
                }}
                style={{ padding: '6px 12px', fontSize: '12px' }}
              >
                Abbrechen
              </button>
            </div>
          </div>
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '5px', flexWrap: 'wrap' }}>
              {!isMedia && (
                <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--color-accent-hover)', textTransform: 'uppercase' }}>
                  {segment.speaker}
                </span>
              )}
              {segment.type !== 'speech' && (
                <span
                  style={{
                    fontSize: '9.5px',
                    color: styleSet.color,
                    fontFamily: 'var(--font-mono)',
                    padding: '1px 6px',
                    borderRadius: '4px',
                    background: styleSet.bg,
                    border: `1px solid ${styleSet.border}`,
                    textTransform: 'uppercase',
                  }}
                >
                  {styleSet.label}
                </span>
              )}
              {!!segment.overlap_ms && segment.overlap_ms !== 0 && (
                <span
                  style={{
                    fontSize: '9.5px',
                    color: segment.overlap_ms < 0 ? '#ff7a8a' : '#7adfff',
                    fontFamily: 'var(--font-mono)',
                    padding: '1px 6px',
                    borderRadius: '4px',
                    background: segment.overlap_ms < 0 ? 'rgba(255,90,101,0.10)' : 'rgba(122,223,255,0.10)',
                    border: `1px solid ${segment.overlap_ms < 0 ? 'rgba(255,90,101,0.25)' : 'rgba(122,223,255,0.25)'}`,
                  }}
                  title={segment.overlap_ms < 0 ? 'Beginnt, bevor das vorige Segment endet' : 'Erzwungene Pause davor'}
                >
                  {segment.overlap_ms > 0 ? `+${segment.overlap_ms}ms` : `${segment.overlap_ms}ms`}
                </span>
              )}
              <span style={{ fontSize: '10px', color: 'var(--color-text-faint)', fontFamily: 'var(--font-mono)', marginLeft: 'auto' }}>
                {isMedia
                  ? `~${(segment.duration_ms ? segment.duration_ms / 1000 : segment.estimated_duration).toFixed(1)}s${typeof segment.volume_db === 'number' && segment.volume_db !== 0 ? ` · ${segment.volume_db}dB` : ''}`
                  : `~${segment.estimated_duration.toFixed(1)}s · ${segment.word_count}W`}
              </span>
            </div>
            <p
              style={{
                fontSize: isMedia ? '12.5px' : '13.5px',
                color: isMedia ? 'var(--color-text-faint)' : 'var(--color-text)',
                fontStyle: isMedia ? 'italic' : 'normal',
                lineHeight: 1.6,
                margin: 0,
              }}
            >
              {isMedia
                ? segment.prompt || segment.text || '(kein Prompt)'
                : renderTaggedText(segment.text, knownTags)}
            </p>
            {segment.notes && (
              <div className="podcast-segment-note" title="Regie-Notiz — steuert die Stimme nicht">
                {segment.notes}
              </div>
            )}
          </>
        )}
      </div>

      {!editing && (
        <div style={{ display: 'flex', gap: '4px', flexShrink: 0 }}>
          <button
            className="btn-ghost"
            disabled={disabled}
            onClick={() => onEdit(true)}
            style={{ padding: '4px 6px', color: 'var(--color-text-faint)' }}
            title="Bearbeiten"
          >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M2 14l3-1L14 4.5a1.5 1.5 0 00-2-2L3 12l-1 2z" />
            </svg>
          </button>
          <button
            className="btn-ghost"
            onClick={handleDelete}
            disabled={busy || disabled}
            style={{ padding: '4px 6px', color: 'var(--color-text-faint)' }}
            onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--color-danger)')}
            onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--color-text-faint)')}
            title="Löschen"
          >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M3 4h10M6 4V2h4v2M5 4l1 10h4l1-10" />
            </svg>
          </button>
        </div>
      )}
    </div>
  );
}

/* ────────────────── Audio card ────────────────── */


function AudioCard({ podcast, jobs }: { podcast: Podcast; jobs: PodcastJobs }) {
  const hasAudio = !!podcast.audio_path;
  const hasScript = (podcast.script?.segments?.length ?? 0) > 0;

  if (!hasScript && !hasAudio) {
    return (
      <div className="card" style={{ padding: '44px 28px', textAlign: 'center' }}>
        <div style={{ fontSize: '15px', fontWeight: 500, color: 'var(--color-text)' }}>Noch kein Audio</div>
        <div style={{ fontSize: '13px', color: 'var(--color-text-dim)', marginTop: '8px' }}>
          Erst das Skript, dann der Ton.
        </div>
      </div>
    );
  }

  return (
    <div className="card" style={{ padding: '20px 22px' }}>
      <p className="hs-item-note">Geschätzte Sprechdauer: {formatClock(podcast.script?.estimated_duration || (podcast.script?.total_words || 0) / 150 * 60)} · tatsächliche Dauer hängt von Stimme, Pausen und Musik ab.</p>
      {hasAudio && <p role="status" className={podcast.audio_stale ? 'draft-status':'hs-item-note'}>{podcast.audio_stale ? 'Audio ist veraltet: Text oder Besetzung wurden geändert.' : podcast.audio_revision ? 'Audio entspricht dem gespeicherten Skript und der Besetzung.' : 'Vorhandenes Audio: Der zugehörige Skriptstand ist noch nicht belegt.'}</p>}
      {podcast.script_revision && <details><summary>Fassungsnachweis</summary><p className="hs-meta">Skript {podcast.script_revision.slice(0,12)} · Audio aus {podcast.audio_revision?.slice(0,12) || 'unbekanntem Stand'}</p></details>}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' }}>
        <div>
          <div className="label-eyebrow" style={{ fontSize: '10px' }}>Mischung</div>
          <div style={{ fontSize: '14px', color: 'var(--color-text)', marginTop: '4px' }}>
            {hasAudio
              ? `${formatClock(podcast.audio_duration)} · ${podcast.audio_format.toUpperCase()} · ${(podcast.audio_size / 1048576).toFixed(1)} MB`
              : 'Noch nicht gerendert'}
          </div>
        </div>
        <div style={{ display: 'flex', gap: '6px' }}>
          {hasAudio && !jobs.audioBusy && (
            <button className="btn btn-ghost" onClick={jobs.downloadAudio} style={{ height: '34px', fontSize: '12.5px' }}>
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M8 2v9M4 8l4 4 4-4M2 14h12" />
              </svg>
              Herunterladen
            </button>
          )}
          {hasAudio && !jobs.audioBusy && (
            <SingleOfflineButton kind="podcast" id={podcast.id} title={podcast.topic} url={getPodcastAudioStreamUrl(podcast.id, podcast.audio_sha256)}
              extraJson={[`/api/podcasts/${podcast.id}`]} className="btn btn-ghost" />
          )}
          <button
            className="btn btn-secondary"
            onClick={() => jobs.generateAudio(hasAudio)}
            disabled={jobs.audioBusy || jobs.scriptBusy || jobs.hasDrafts}
            style={{ height: '34px', fontSize: '12.5px' }}
          >
            {hasAudio ? 'Neu rendern' : 'Audio rendern'}
          </button>
        </div>
      </div>

      {hasAudio && <PlayEdition track={{ key:`podcast:${podcast.id}:${podcast.audio_sha256 || podcast.audio_path}`, title:podcast.topic, subtitle:'Podcast', url:getPodcastAudioStreamUrl(podcast.id, podcast.audio_sha256), href:'/podcast' }} />}
    </div>
  );
}

/* ────────────────── Hosts preview ────────────────── */

function EpisodeCast({podcast,allHosts,engineVoices,disabled,onReload,onError}:{podcast:Podcast;allHosts:Host[];engineVoices:Voice[];disabled:boolean;onReload:()=>void;onError:(message:string)=>void}) {
  const [ids,setIds] = useState(podcast.hosts.map(h=>h.id));
  const [busy,setBusy] = useState(false);
  const available=[...allHosts,...podcast.hosts.filter(h=>!allHosts.some(a=>a.id===h.id))];
  async function save() {
    if (busy || disabled) return; setBusy(true);
    try { await updatePodcastCast(podcast.id,ids); onReload(); } catch(e){onError((e as Error).message);} finally{setBusy(false);}
  }
  return <section className="card studio-section"><h2>Besetzung dieser Episode</h2><p>Hier speicherst du eine eigene Momentaufnahme der ausgewählten Sprecher. Änderungen an der globalen Vorlage werden erst mit erneutem Speichern übernommen.</p>
    <fieldset className="studio-field"><legend>Sprecher auswählen</legend>{available.map(h=><label key={h.id} className="cast-choice"><input type="checkbox" checked={ids.includes(h.id)} disabled={disabled || busy || !h.voice_id} onChange={e=>setIds(prev=>e.target.checked?[...prev,h.id]:prev.filter(id=>id!==h.id))} />{h.name} · {engineVoices.find(v=>v.id === h.voice_id)?.name || 'Stimme nicht verfügbar'}</label>)}</fieldset>
    <button className="btn btn-primary" disabled={disabled || busy || !ids.length} onClick={()=>void save()}>{busy?'Wird gespeichert…':'Episodenbesetzung speichern'}</button>
    <p className="hs-item-note">Vor dem Entfernen eines Sprechers dessen Skriptsegmente neu zuordnen.</p><HostsPreview hosts={podcast.hosts} engineVoices={engineVoices} allHosts={allHosts} />
  </section>;
}

function HostsPreview({ hosts, engineVoices, allHosts }: { hosts: Host[]; engineVoices: Voice[]; allHosts: Host[] }) {
  if (hosts.length === 0) {
    return (
      <div className="card-subtle" style={{ padding: '14px 18px', fontSize: '12.5px', color: 'var(--color-text-dim)' }}>
        Diesem Podcast ist niemand zugewiesen — oben über „Sprecher“ die Besetzung verwalten.
      </div>
    );
  }
  const voiceName = (id: string | null) => {
    if (!id) return 'keine Stimme';
    const v = engineVoices.find((x) => x.id === id);
    return v ? v.name : 'unbekannt';
  };
  void allHosts;
  return (
    <div className="card" style={{ padding: '18px 22px' }}>
      <SectionHeader eyebrow="Besetzung" title={`${hosts.length} Sprecher`} />
      <div style={{ marginTop: '12px', display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '10px' }}>
        {hosts.map((h) => (
          <div
            key={h.id}
            style={{
              padding: '10px 12px',
              borderRadius: '10px',
              background: 'rgba(255,255,255,0.02)',
              border: '1px solid rgba(255,255,255,0.06)',
            }}
          >
            <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--color-text)' }}>{h.name}</div>
            <div style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)', marginTop: '2px' }}>
              {h.role} · {voiceName(h.voice_id)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ────────────────── Create podcast modal ────────────────── */

function CreatePodcastModal({
  hosts,
  languages,
  onCreate,
  onClose,
}: {
  hosts: Host[];
  languages: string[];
  onCreate: (data: Parameters<typeof createPodcast>[0] & { source_text?: string }) => Promise<void>;
  onClose: () => void;
}) {
  const [topic, setTopic] = useState('');
  const [sourceText, setSourceText] = useState('');
  const [format, setFormat] = useState<PodcastFormat>('dialog');
  const [duration, setDuration] = useState<PodcastDuration>('medium');
  const [language, setLanguage] = useState('German');
  const [disfluency, setDisfluency] = useState(1);
  const [hostIds, setHostIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [submitError,setSubmitError] = useState('');
  const castValid = format === 'dialog' ? hostIds.length >= 2 : format === 'monolog' ? hostIds.length === 1 : hostIds.length > 0;

  const canSubmit = topic.trim().length > 0 && castValid && !busy;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setSubmitError('');
    try { await onCreate({
      topic: topic.trim(),
      source_text:sourceText,
      format,
      duration,
      language,
      disfluency_level: disfluency,
      host_ids: hostIds,
    }); } catch(e) { setSubmitError((e as Error).message); } finally { setBusy(false); }
  };

  return (
    <Modal title="Neuer Podcast" onClose={onClose}>
      {submitError && <p role="alert" className="hs-error">{submitError}</p>}
      {!castValid && <p className="hs-item-note">{format === 'dialog' ? 'Für einen Dialog mindestens zwei Sprecher auswählen.' : 'Für einen Monolog genau einen Sprecher auswählen.'}</p>}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
        <LabeledField label="Erste Textquelle (optional)"><textarea className="input-field" rows={4} value={sourceText} onChange={e=>setSourceText(e.target.value)} placeholder="Quelltext hier einfügen" /></LabeledField>
        <p className="hs-item-note">Dateien und Webadressen kannst du anschließend im Reiter Quellen hinzufügen. Die Erstellung startet noch keine Produktion.</p>
        <LabeledField label="Thema">
          <input
            type="text"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="z. B. Die Zukunft synthetischer Stimmen"
            className="input-field"
            autoFocus
          />
        </LabeledField>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <LabeledField label="Format">
            <select value={format} onChange={(e) => setFormat(e.target.value as PodcastFormat)} className="input-field" style={selectStyle}>
              <option value="dialog">Dialog (2+ Sprecher)</option>
              <option value="monolog">Monolog (einer)</option>
              <option value="custom">Frei</option>
            </select>
          </LabeledField>

          <LabeledField label="Länge">
            <select value={duration} onChange={(e) => setDuration(e.target.value as PodcastDuration)} className="input-field" style={selectStyle}>
              <option value="short">Kurz (~3 Min)</option>
              <option value="medium">Mittel (~7 Min)</option>
              <option value="long">Lang (~15 Min)</option>
            </select>
          </LabeledField>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <LabeledField label="Sprache">
            <select value={language} onChange={(e) => setLanguage(e.target.value)} className="input-field" style={selectStyle}>
              {languages.map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
            </select>
          </LabeledField>

          <LabeledField label={`Sprechfluss-Störer ${disfluency}/3`}>
            <input
              type="range"
              min={0}
              max={3}
              step={1}
              value={disfluency}
              onChange={(e) => setDisfluency(Number(e.target.value))}
              style={{ width: '100%', accentColor: 'var(--color-accent)' }}
            />
          </LabeledField>
        </div>

        <LabeledField label={`Besetzung (${hostIds.length} ausgewählt)`}>
          {hosts.length === 0 ? (
            <div
              style={{
                padding: '12px',
                fontSize: '12.5px',
                borderRadius: '10px',
                background: 'var(--color-warning-dim)',
                border: '1px solid rgba(244,193,82,0.25)',
                color: 'var(--color-text-secondary)',
              }}
            >
              Noch keine Sprecher. Im{' '}
              <Link to="/podcast/hosts" style={{ color: 'var(--color-accent)' }}>Host-Hub</Link>{' '}
              eine der 40 Persönlichkeiten übernehmen.
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', maxHeight: '220px', overflowY: 'auto' }}>
              {hosts.map((h) => {
                const selected = hostIds.includes(h.id);
                // Eine gesetzte voice_id heißt nicht, dass die Engine sie noch
                // kennt. Solche Sprecher lassen `POST /podcasts` mit 400
                // scheitern — also hier gar nicht erst auswählbar.
                const unusable = !h.voice_id || h.voice_available === false;
                return (
                  <label
                    key={h.id}
                    title={unusable ? 'Diesem Sprecher fehlt eine gültige Stimme — im Host-Hub zuweisen.' : undefined}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '10px',
                      padding: '10px 12px',
                      borderRadius: '10px',
                      border: `1px solid ${selected ? 'rgba(123,97,255,0.32)' : 'rgba(255,255,255,0.08)'}`,
                      background: selected ? 'var(--color-accent-dim)' : 'rgba(255,255,255,0.02)',
                      cursor: unusable ? 'not-allowed' : 'pointer',
                      opacity: unusable ? 0.55 : 1,
                      transition: 'all 0.15s ease',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={selected}
                      disabled={unusable}
                      onChange={() => {
                        setHostIds((prev) => (selected ? prev.filter((id) => id !== h.id) : [...prev, h.id]));
                      }}
                      style={{ accentColor: 'var(--color-accent)' }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: '13px', color: 'var(--color-text)' }}>{h.name}</div>
                      <div
                        style={{
                          fontSize: '11px',
                          color: unusable ? 'var(--color-warning)' : 'var(--color-text-dim)',
                          fontFamily: 'var(--font-mono)',
                        }}
                      >
                        {h.role}{' '}
                        {!h.voice_id
                          ? '· keine Stimme'
                          : h.voice_available === false
                            ? '· Stimme fehlt'
                            : `· ${h.voice_id}`}
                      </div>
                    </div>
                  </label>
                );
              })}
            </div>
          )}
        </LabeledField>

        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end', marginTop: '4px' }}>
          <button className="btn btn-ghost" onClick={onClose}>Abbrechen</button>
          <button className="btn btn-primary" onClick={handleSubmit} disabled={!canSubmit}>
            {busy ? 'Legt an…' : 'Anlegen'}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/* ────────────────── Host manager modal ────────────────── */

/* ────────────────── Shared primitives ────────────────── */

function LabeledField({ label, children }: { label: string; children: React.ReactNode }) {
  if (isValidElement(children) && typeof children.type === 'string' && ['input','textarea','select'].includes(children.type)) return <label className="studio-field"><span>{label}</span>{children}</label>;
  return <fieldset className="studio-field"><legend>{label}</legend>{children}</fieldset>;
}

function SectionHeader({
  eyebrow,
  title,
  trailing,
}: {
  eyebrow: string;
  title: string;
  trailing?: React.ReactNode;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: '12px' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', minWidth: 0 }}>
        <span className="label-eyebrow" style={{ fontSize: '10px' }}>{eyebrow}</span>
        <div
          style={{
            fontSize: '15px',
            fontWeight: 500,
            color: 'var(--color-text)',
            letterSpacing: 0,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {title}
        </div>
      </div>
      {trailing}
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  height: '42px',
  padding: '0 12px',
  fontSize: '13.5px',
  cursor: 'pointer',
  appearance: 'none',
  WebkitAppearance: 'none',
  backgroundImage:
    "url(\"data:image/svg+xml;charset=UTF-8,%3csvg width='10' height='6' viewBox='0 0 10 6' xmlns='http://www.w3.org/2000/svg'%3e%3cpath d='M1 1l4 4 4-4' stroke='white' stroke-opacity='0.5' stroke-width='1.4' stroke-linecap='round' stroke-linejoin='round' fill='none'/%3e%3c/svg%3e\")",
  backgroundRepeat: 'no-repeat',
  backgroundPosition: 'right 12px center',
  paddingRight: '30px',
};
