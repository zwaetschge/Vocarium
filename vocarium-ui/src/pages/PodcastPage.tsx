import { useEffect, useMemo, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  getHosts,
  createHost,
  deleteHost,
  getPodcasts,
  getPodcast,
  createPodcast,
  deletePodcast,
  getPodcastSources,
  uploadPodcastSource,
  addUrlSource,
  addTextSource,
  deletePodcastSource,
  generatePodcastScript,
  updateSegment,
  deleteSegment,
  generatePodcastAudio,
  getPodcastAudioStreamUrl,
  downloadPodcastAudio,
  getVoices,
  getLanguages,
} from '../api';
import type {
  Host,
  HostRole,
  Podcast,
  PodcastFormat,
  PodcastDuration,
  PodcastSource,
  ScriptSegment,
  PodcastProgressEvent,
  Voice,
} from '../types';

export default function PodcastPage() {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [podcasts, setPodcasts] = useState<Podcast[]>([]);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [languages, setLanguages] = useState<string[]>([]);
  const [selectedPodcastId, setSelectedPodcastId] = useState<string | null>(null);
  const [hostManagerOpen, setHostManagerOpen] = useState(false);
  const [createPodcastOpen, setCreatePodcastOpen] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const customVoices = useMemo(
    () => voices.filter((v) => v.source === 'custom'),
    [voices],
  );
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
      setError(e instanceof Error ? e.message : 'Failed to load podcasts');
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

  const handleCreate = async (payload: Parameters<typeof createPodcast>[0]) => {
    try {
      const pod = await createPodcast(payload);
      setPodcasts((prev) => [pod, ...prev]);
      setSelectedPodcastId(pod.id);
      setCreatePodcastOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Create failed');
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this podcast? This cannot be undone.')) return;
    try {
      await deletePodcast(id);
      setPodcasts((prev) => prev.filter((p) => p.id !== id));
      if (selectedPodcastId === id) {
        const remaining = podcasts.filter((p) => p.id !== id);
        setSelectedPodcastId(remaining.length > 0 ? remaining[0].id : null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed');
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
            Turn sources into scripted, multi-voice podcasts. Uses your saved custom voices.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '10px' }}>
          <button
            className="btn btn-secondary"
            onClick={() => setHostManagerOpen(true)}
            style={{ height: '40px' }}
          >
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <circle cx="10" cy="6" r="3" />
              <path d="M3 18v-1a5 5 0 015-5h4a5 5 0 015 5v1" />
            </svg>
            Hosts ({hosts.length})
          </button>
          <motion.button
            whileTap={{ scale: 0.97 }}
            className="btn btn-primary"
            onClick={() => setCreatePodcastOpen(true)}
            style={{ height: '40px' }}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M8 2v12M2 8h12" />
            </svg>
            New podcast
          </motion.button>
        </div>
      </div>

      {/* Custom voice gate hint */}
      {customVoices.length === 0 && (
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
            You have no custom voices yet. Podcasts require custom voices — use <em>Custom</em> to create one.
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
      <div style={{ display: 'grid', gridTemplateColumns: '300px 1fr', gap: '18px', alignItems: 'start' }}>
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
            customVoices={customVoices}
            onReload={() => reloadPodcast(selectedPodcast.id)}
            onReloadList={reloadPodcastList}
            onError={setError}
          />
        ) : (
          <EmptyPane onNew={() => setCreatePodcastOpen(true)} canCreate={customVoices.length >= 1 && hosts.length >= 1} />
        )}
      </div>

      <AnimatePresence>
        {hostManagerOpen && (
          <HostManagerModal
            hosts={hosts}
            customVoices={customVoices}
            onClose={() => {
              setHostManagerOpen(false);
              loadAll();
            }}
            onError={setError}
          />
        )}
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
    <div className="card" style={{ padding: '12px', display: 'flex', flexDirection: 'column', gap: '4px', minHeight: '220px' }}>
      <div style={{ padding: '6px 10px 10px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span className="label-eyebrow" style={{ fontSize: '10px' }}>Library</span>
        <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>{podcasts.length}</span>
      </div>
      {loading ? (
        <div style={{ padding: '20px 12px', fontSize: '12px', color: 'var(--color-text-dim)' }}>Loading…</div>
      ) : podcasts.length === 0 ? (
        <div style={{ padding: '18px 12px', fontSize: '12.5px', color: 'var(--color-text-dim)', lineHeight: 1.5 }}>
          No podcasts yet. Create one to start.
        </div>
      ) : (
        podcasts.map((p) => {
          const active = p.id === selectedId;
          return (
            <motion.div
              key={p.id}
              whileTap={{ scale: 0.99 }}
              onClick={() => onSelect(p.id)}
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
              <div style={{ minWidth: 0, flex: 1 }}>
                <div
                  style={{
                    fontSize: '13.5px',
                    fontWeight: 500,
                    color: 'var(--color-text)',
                    letterSpacing: '-0.01em',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {p.topic || 'Untitled podcast'}
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
                  <span>{p.format}</span>
                  <span style={{ opacity: 0.4 }}>·</span>
                  <span>{p.duration}</span>
                  <span style={{ opacity: 0.4 }}>·</span>
                  <StatusPill status={p.status} />
                </div>
              </div>
              <button
                className="btn-ghost"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(p.id);
                }}
                title="Delete"
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
    draft: { label: 'draft', color: 'var(--color-text-dim)', bg: 'rgba(255,255,255,0.05)' },
    generating_script: { label: 'script…', color: 'var(--color-warning)', bg: 'var(--color-warning-dim)' },
    script_ready: { label: 'script ready', color: 'var(--color-accent-hover)', bg: 'var(--color-accent-dim)' },
    generating_audio: { label: 'audio…', color: 'var(--color-warning)', bg: 'var(--color-warning-dim)' },
    ready: { label: 'ready', color: 'var(--color-success)', bg: 'var(--color-success-dim)' },
    error: { label: 'error', color: 'var(--color-danger)', bg: 'var(--color-danger-dim)' },
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
        letterSpacing: '0.08em',
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
      <div style={{ fontSize: '15px', fontWeight: 500, color: 'var(--color-text)', letterSpacing: '-0.01em' }}>
        No podcast selected
      </div>
      <div style={{ fontSize: '13px', color: 'var(--color-text-dim)', maxWidth: '360px', lineHeight: 1.5 }}>
        Create a podcast to add sources, generate a script, and render multi-voice audio.
      </div>
      {canCreate && (
        <button className="btn btn-primary" style={{ marginTop: '6px' }} onClick={onNew}>
          Start new podcast
        </button>
      )}
      {!canCreate && (
        <div style={{ fontSize: '11.5px', color: 'var(--color-warning)', marginTop: '6px' }}>
          You need at least one custom voice and one host first.
        </div>
      )}
    </div>
  );
}

/* ────────────────── Podcast detail (right column) ────────────────── */

function PodcastDetail({
  podcast,
  hosts,
  customVoices,
  onReload,
  onReloadList,
  onError,
}: {
  podcast: Podcast;
  hosts: Host[];
  customVoices: Voice[];
  onReload: () => void;
  onReloadList: () => void;
  onError: (msg: string) => void;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
      <ConfigCard podcast={podcast} />
      <SourcesCard podcast={podcast} onError={onError} />
      <ScriptCard
        podcast={podcast}
        onReload={onReload}
        onReloadList={onReloadList}
        onError={onError}
      />
      <AudioCard
        podcast={podcast}
        onReload={onReload}
        onReloadList={onReloadList}
        onError={onError}
      />
      {/* Hosts preview */}
      <HostsPreview hosts={podcast.hosts} customVoices={customVoices} allHosts={hosts} />
    </div>
  );
}

function ConfigCard({ podcast }: { podcast: Podcast }) {
  return (
    <div className="card" style={{ padding: '22px 24px' }}>
      <SectionHeader
        eyebrow="Overview"
        title={podcast.topic || 'Untitled'}
      />
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', marginTop: '14px' }}>
        <MetaChip label="Format" value={podcast.format} />
        <MetaChip label="Duration" value={podcast.duration} />
        <MetaChip label="Language" value={podcast.language || 'auto'} />
        <MetaChip label="Disfluency" value={`${podcast.disfluency_level}/3`} />
        <MetaChip label="Hosts" value={String(podcast.hosts.length)} />
        {podcast.total_words > 0 && <MetaChip label="Words" value={String(podcast.total_words)} emphasis />}
      </div>
      {podcast.error_message && (
        <div
          style={{
            marginTop: '14px',
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

/* ────────────────── Sources card ────────────────── */

function SourcesCard({ podcast, onError }: { podcast: Podcast; onError: (msg: string) => void }) {
  const [sources, setSources] = useState<PodcastSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<'none' | 'file' | 'url' | 'text'>('none');
  const [url, setUrl] = useState('');
  const [urlTitle, setUrlTitle] = useState('');
  const [textBody, setTextBody] = useState('');
  const [textTitle, setTextTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadSources = async () => {
    setLoading(true);
    try {
      const s = await getPodcastSources(podcast.id);
      setSources(s);
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to load sources');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [podcast.id]);

  const handleFile = async (file: File) => {
    setBusy(true);
    try {
      await uploadPodcastSource(podcast.id, file);
      await loadSources();
      setMode('none');
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Upload failed');
    } finally {
      setBusy(false);
    }
  };

  const handleUrl = async () => {
    if (!url.trim()) return;
    setBusy(true);
    try {
      await addUrlSource(podcast.id, url.trim(), urlTitle.trim() || undefined);
      await loadSources();
      setUrl('');
      setUrlTitle('');
      setMode('none');
    } catch (e) {
      onError(e instanceof Error ? e.message : 'URL source failed');
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
      onError(e instanceof Error ? e.message : 'Text source failed');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (sourceId: string) => {
    if (!confirm('Delete this source?')) return;
    try {
      await deletePodcastSource(podcast.id, sourceId);
      setSources((prev) => prev.filter((s) => s.id !== sourceId));
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Delete failed');
    }
  };

  return (
    <div className="card" style={{ padding: '22px 24px' }}>
      <SectionHeader
        eyebrow="Sources"
        title={`${sources.length} ${sources.length === 1 ? 'source' : 'sources'}`}
        trailing={
          <div style={{ display: 'flex', gap: '6px' }}>
            <ModeButton active={mode === 'file'} onClick={() => setMode(mode === 'file' ? 'none' : 'file')}>
              File
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
              {busy ? 'Uploading…' : 'Choose file (PDF, DOCX, MD, TXT, HTML, EPUB…)'}
            </button>
            <div style={{ fontSize: '11px', color: 'var(--color-text-faint)', marginTop: '6px' }}>
              Max 50MB · parsed via Docling
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
              placeholder="Title (optional)"
              value={urlTitle}
              onChange={(e) => setUrlTitle(e.target.value)}
              className="input-field"
            />
            <button className="btn btn-primary" onClick={handleUrl} disabled={!url.trim() || busy}>
              {busy ? 'Fetching…' : 'Add URL'}
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
              placeholder="Title (optional)"
              value={textTitle}
              onChange={(e) => setTextTitle(e.target.value)}
              className="input-field"
            />
            <textarea
              placeholder="Paste raw text content (10–100,000 characters)…"
              value={textBody}
              onChange={(e) => setTextBody(e.target.value)}
              className="input-field"
              style={{ minHeight: '120px', resize: 'vertical', fontFamily: 'var(--font-body)', lineHeight: 1.5 }}
            />
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                {textBody.length.toLocaleString()} chars
              </span>
              <button className="btn btn-primary" onClick={handleText} disabled={!textBody.trim() || busy}>
                {busy ? 'Adding…' : 'Add text'}
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div style={{ marginTop: sources.length > 0 || mode !== 'none' ? '14px' : '10px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {loading ? (
          <div style={{ fontSize: '12px', color: 'var(--color-text-dim)', padding: '8px 0' }}>Loading…</div>
        ) : sources.length === 0 ? (
          <div style={{ fontSize: '12.5px', color: 'var(--color-text-dim)', padding: '8px 0' }}>
            Add at least one source to generate a script.
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
                  {s.title || s.url || '(untitled)'}
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
                  <span style={{ color: statusColor(s.status) }}>{s.status}</span>
                  {s.chunk_count > 0 && (
                    <>
                      <span style={{ opacity: 0.4 }}>·</span>
                      <span>{s.chunk_count} chunks</span>
                    </>
                  )}
                </div>
              </div>
              <button
                className="btn-ghost"
                onClick={() => handleDelete(s.id)}
                style={{ padding: '4px 6px', color: 'var(--color-text-faint)' }}
                onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--color-danger)')}
                onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--color-text-faint)')}
                title="Delete"
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

function statusColor(status: string) {
  if (status === 'processed') return 'var(--color-success)';
  if (status === 'failed') return 'var(--color-danger)';
  return 'var(--color-warning)';
}

/* ────────────────── Script card ────────────────── */

function ScriptCard({
  podcast,
  onReload,
  onReloadList,
  onError,
}: {
  podcast: Podcast;
  onReload: () => void;
  onReloadList: () => void;
  onError: (msg: string) => void;
}) {
  const [generating, setGenerating] = useState(false);
  const [progress, setProgress] = useState<PodcastProgressEvent | null>(null);
  const segments = podcast.script?.segments || [];

  const handleGenerate = async () => {
    setGenerating(true);
    setProgress(null);
    try {
      await generatePodcastScript(
        podcast.id,
        (p) => setProgress(p),
        () => {
          setGenerating(false);
          setProgress(null);
          onReload();
          onReloadList();
        },
        (err) => {
          setGenerating(false);
          setProgress(null);
          onError(err);
          onReload();
          onReloadList();
        },
      );
    } catch (e) {
      setGenerating(false);
      setProgress(null);
      onError(e instanceof Error ? e.message : 'Script generation failed');
    }
  };

  return (
    <div className="card" style={{ padding: '22px 24px' }}>
      <SectionHeader
        eyebrow="Script"
        title={segments.length > 0 ? `${segments.length} segments · ${podcast.script?.total_words || 0} words` : 'No script yet'}
        trailing={
          <button
            className="btn btn-primary"
            onClick={handleGenerate}
            disabled={generating}
            style={{ height: '36px' }}
          >
            {generating ? (
              <>
                <svg style={{ width: 13, height: 13, animation: 'spin 0.9s linear infinite' }} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <circle cx="12" cy="12" r="10" strokeDasharray="60" strokeDashoffset="20" strokeLinecap="round" opacity="0.8" />
                </svg>
                {progress ? progress.stage : 'Generating'}
              </>
            ) : segments.length > 0 ? (
              <>Regenerate</>
            ) : (
              <>
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
                  <path d="M2 8l5 5 7-10" />
                </svg>
                Generate script
              </>
            )}
          </button>
        }
      />

      <AnimatePresence>
        {generating && progress && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
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
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '12.5px' }}>
                <span style={{ color: 'var(--color-text)' }}>{progress.message}</span>
                <span style={{ color: 'var(--color-accent-hover)', fontFamily: 'var(--font-mono)', fontSize: '11px' }}>
                  {Math.round(progress.progress * 100)}%
                </span>
              </div>
              <div style={{ height: '3px', borderRadius: '999px', background: 'rgba(255,255,255,0.08)', overflow: 'hidden' }}>
                <motion.div
                  animate={{ width: `${progress.progress * 100}%` }}
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
        )}
      </AnimatePresence>

      {segments.length > 0 && (
        <div style={{ marginTop: '16px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {segments.map((seg) => (
            <SegmentRow
              key={seg.id}
              podcastId={podcast.id}
              segment={seg}
              onReload={onReload}
              onError={onError}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function SegmentRow({
  podcastId,
  segment,
  onReload,
  onError,
}: {
  podcastId: string;
  segment: ScriptSegment;
  onReload: () => void;
  onError: (msg: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(segment.text);
  const [speaker, setSpeaker] = useState(segment.speaker);
  const [busy, setBusy] = useState(false);

  const handleSave = async () => {
    setBusy(true);
    try {
      await updateSegment(podcastId, segment.id, { text: text.trim(), speaker: speaker.trim() });
      setEditing(false);
      onReload();
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm('Delete this segment?')) return;
    setBusy(true);
    try {
      await deleteSegment(podcastId, segment.id);
      onReload();
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Delete failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      style={{
        padding: '14px 16px',
        borderRadius: '12px',
        background: 'rgba(255,255,255,0.02)',
        border: '1px solid rgba(255,255,255,0.06)',
        display: 'flex',
        gap: '14px',
        alignItems: 'flex-start',
      }}
    >
      <div style={{ minWidth: '48px', flexShrink: 0 }}>
        <div
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '10px',
            color: 'var(--color-text-faint)',
            letterSpacing: '0.06em',
            textTransform: 'uppercase',
          }}
        >
          #{segment.position + 1}
        </div>
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {editing ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <input
              type="text"
              value={speaker}
              onChange={(e) => setSpeaker(e.target.value)}
              className="input-field"
              style={{ padding: '8px 12px', fontSize: '12.5px' }}
            />
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              className="input-field"
              style={{ minHeight: '80px', padding: '10px 12px', fontSize: '13px', lineHeight: 1.55 }}
            />
            <div style={{ display: 'flex', gap: '6px' }}>
              <button className="btn btn-primary" onClick={handleSave} disabled={busy} style={{ padding: '6px 12px', fontSize: '12px', height: '32px' }}>
                Save
              </button>
              <button
                className="btn-ghost"
                onClick={() => {
                  setEditing(false);
                  setText(segment.text);
                  setSpeaker(segment.speaker);
                }}
                style={{ padding: '6px 12px', fontSize: '12px' }}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
              <span
                style={{
                  fontSize: '11px',
                  fontWeight: 600,
                  color: 'var(--color-accent-hover)',
                  textTransform: 'uppercase',
                  letterSpacing: '0.08em',
                }}
              >
                {segment.speaker}
              </span>
              {segment.type !== 'dialogue' && (
                <span
                  style={{
                    fontSize: '9.5px',
                    color: 'var(--color-text-faint)',
                    fontFamily: 'var(--font-mono)',
                    padding: '1px 6px',
                    borderRadius: '4px',
                    background: 'rgba(255,255,255,0.04)',
                  }}
                >
                  {segment.type}
                </span>
              )}
              <span style={{ fontSize: '10px', color: 'var(--color-text-faint)', fontFamily: 'var(--font-mono)', marginLeft: 'auto' }}>
                ~{segment.estimated_duration.toFixed(1)}s · {segment.word_count}w
              </span>
            </div>
            <p style={{ fontSize: '13.5px', color: 'var(--color-text)', lineHeight: 1.6, margin: 0 }}>{segment.text}</p>
          </>
        )}
      </div>
      {!editing && (
        <div style={{ display: 'flex', gap: '4px', flexShrink: 0 }}>
          <button
            className="btn-ghost"
            onClick={() => setEditing(true)}
            style={{ padding: '4px 6px', color: 'var(--color-text-faint)' }}
            title="Edit"
          >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M2 14l3-1L14 4.5a1.5 1.5 0 00-2-2L3 12l-1 2z" />
            </svg>
          </button>
          <button
            className="btn-ghost"
            onClick={handleDelete}
            disabled={busy}
            style={{ padding: '4px 6px', color: 'var(--color-text-faint)' }}
            onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--color-danger)')}
            onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--color-text-faint)')}
            title="Delete"
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

function AudioCard({
  podcast,
  onReload,
  onReloadList,
  onError,
}: {
  podcast: Podcast;
  onReload: () => void;
  onReloadList: () => void;
  onError: (msg: string) => void;
}) {
  const [generating, setGenerating] = useState(false);
  const [progress, setProgress] = useState<PodcastProgressEvent | null>(null);
  const hasAudio = podcast.status === 'ready' && !!podcast.audio_path;
  const canGenerate = podcast.status === 'script_ready' || podcast.status === 'ready' || podcast.status === 'error';
  const audioRef = useRef<HTMLAudioElement>(null);

  const handleGenerate = async (force = false) => {
    setGenerating(true);
    setProgress(null);
    try {
      await generatePodcastAudio(
        podcast.id,
        (p) => setProgress(p),
        () => {
          setGenerating(false);
          setProgress(null);
          onReload();
          onReloadList();
        },
        (err) => {
          setGenerating(false);
          setProgress(null);
          onError(err);
          onReload();
          onReloadList();
        },
        force,
      );
    } catch (e) {
      setGenerating(false);
      setProgress(null);
      onError(e instanceof Error ? e.message : 'Audio generation failed');
    }
  };

  const handleDownload = async () => {
    try {
      const blob = await downloadPodcastAudio(podcast.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${(podcast.topic || 'podcast').replace(/[^a-z0-9]+/gi, '-').toLowerCase()}.${podcast.audio_format || 'wav'}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Download failed');
    }
  };

  return (
    <div className="card" style={{ padding: '22px 24px' }}>
      <SectionHeader
        eyebrow="Audio"
        title={
          hasAudio
            ? `${podcast.audio_duration.toFixed(1)}s · ${podcast.audio_format.toUpperCase()}`
            : 'Not rendered yet'
        }
        trailing={
          <div style={{ display: 'flex', gap: '6px' }}>
            {hasAudio && !generating && (
              <button className="btn btn-ghost" onClick={handleDownload} style={{ height: '36px', fontSize: '12.5px' }}>
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                  <path d="M8 2v9M4 8l4 4 4-4M2 14h12" />
                </svg>
                Download
              </button>
            )}
            <button
              className="btn btn-primary"
              onClick={() => handleGenerate(hasAudio)}
              disabled={generating || !canGenerate}
              style={{ height: '36px' }}
            >
              {generating ? (
                <>
                  <svg style={{ width: 13, height: 13, animation: 'spin 0.9s linear infinite' }} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <circle cx="12" cy="12" r="10" strokeDasharray="60" strokeDashoffset="20" strokeLinecap="round" opacity="0.8" />
                  </svg>
                  {progress ? progress.stage : 'Rendering'}
                </>
              ) : hasAudio ? (
                <>Re-render</>
              ) : (
                <>
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M3 2v12l10-6z" />
                  </svg>
                  Render audio
                </>
              )}
            </button>
          </div>
        }
      />

      <AnimatePresence>
        {generating && progress && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
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
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '12.5px' }}>
                <span style={{ color: 'var(--color-text)' }}>{progress.message}</span>
                <span style={{ color: 'var(--color-accent-hover)', fontFamily: 'var(--font-mono)', fontSize: '11px' }}>
                  {Math.round(progress.progress * 100)}%
                  {progress.segment_position != null && <> · seg {progress.segment_position + 1}</>}
                </span>
              </div>
              <div style={{ height: '3px', borderRadius: '999px', background: 'rgba(255,255,255,0.08)', overflow: 'hidden' }}>
                <motion.div
                  animate={{ width: `${progress.progress * 100}%` }}
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
        )}
      </AnimatePresence>

      {hasAudio && !generating && (
        <div style={{ marginTop: '14px' }}>
          <audio
            ref={audioRef}
            src={getPodcastAudioStreamUrl(podcast.id)}
            controls
            preload="metadata"
            style={{ width: '100%', height: '40px' }}
          />
        </div>
      )}

      {!hasAudio && !generating && podcast.status !== 'script_ready' && podcast.status !== 'ready' && (
        <div style={{ marginTop: '12px', fontSize: '12.5px', color: 'var(--color-text-dim)' }}>
          Generate a script first, then render audio here.
        </div>
      )}
    </div>
  );
}

/* ────────────────── Hosts preview ────────────────── */

function HostsPreview({ hosts, customVoices, allHosts }: { hosts: Host[]; customVoices: Voice[]; allHosts: Host[] }) {
  if (hosts.length === 0) {
    return (
      <div className="card-subtle" style={{ padding: '14px 18px', fontSize: '12.5px', color: 'var(--color-text-dim)' }}>
        No hosts assigned. Manage hosts to add cast.
      </div>
    );
  }
  const voiceName = (id: string | null) => {
    if (!id) return 'no voice';
    const v = customVoices.find((x) => x.id === id);
    return v ? v.name : 'unknown';
  };
  void allHosts;
  return (
    <div className="card" style={{ padding: '18px 22px' }}>
      <SectionHeader eyebrow="Cast" title={`${hosts.length} ${hosts.length === 1 ? 'host' : 'hosts'}`} />
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
  onCreate: (data: Parameters<typeof createPodcast>[0]) => void;
  onClose: () => void;
}) {
  const [topic, setTopic] = useState('');
  const [format, setFormat] = useState<PodcastFormat>('dialog');
  const [duration, setDuration] = useState<PodcastDuration>('medium');
  const [language, setLanguage] = useState('German');
  const [disfluency, setDisfluency] = useState(1);
  const [hostIds, setHostIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const canSubmit = topic.trim().length > 0 && hostIds.length > 0 && !busy;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    await onCreate({
      topic: topic.trim(),
      format,
      duration,
      language,
      disfluency_level: disfluency,
      host_ids: hostIds,
    });
    setBusy(false);
  };

  return (
    <Modal title="New podcast" onClose={onClose}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
        <LabeledField label="Topic">
          <input
            type="text"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="e.g. The future of synthetic voices"
            className="input-field"
            autoFocus
          />
        </LabeledField>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <LabeledField label="Format">
            <select value={format} onChange={(e) => setFormat(e.target.value as PodcastFormat)} className="input-field" style={selectStyle}>
              <option value="dialog">Dialog (2+ hosts)</option>
              <option value="monolog">Monolog (single)</option>
              <option value="custom">Custom</option>
            </select>
          </LabeledField>

          <LabeledField label="Duration">
            <select value={duration} onChange={(e) => setDuration(e.target.value as PodcastDuration)} className="input-field" style={selectStyle}>
              <option value="short">Short (~3 min)</option>
              <option value="medium">Medium (~7 min)</option>
              <option value="long">Long (~15 min)</option>
            </select>
          </LabeledField>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <LabeledField label="Language">
            <select value={language} onChange={(e) => setLanguage(e.target.value)} className="input-field" style={selectStyle}>
              {languages.map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
            </select>
          </LabeledField>

          <LabeledField label={`Disfluency ${disfluency}/3`}>
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

        <LabeledField label={`Hosts (${hostIds.length} selected)`}>
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
              No hosts yet. Use <strong style={{ color: 'var(--color-text)' }}>Manage hosts</strong> first.
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', maxHeight: '220px', overflowY: 'auto' }}>
              {hosts.map((h) => {
                const selected = hostIds.includes(h.id);
                return (
                  <label
                    key={h.id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '10px',
                      padding: '10px 12px',
                      borderRadius: '10px',
                      border: `1px solid ${selected ? 'rgba(123,97,255,0.32)' : 'rgba(255,255,255,0.08)'}`,
                      background: selected ? 'var(--color-accent-dim)' : 'rgba(255,255,255,0.02)',
                      cursor: 'pointer',
                      transition: 'all 0.15s ease',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={() => {
                        setHostIds((prev) => (selected ? prev.filter((id) => id !== h.id) : [...prev, h.id]));
                      }}
                      style={{ accentColor: 'var(--color-accent)' }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: '13px', color: 'var(--color-text)' }}>{h.name}</div>
                      <div style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                        {h.role} {h.voice_id ? '· voice set' : '· no voice'}
                      </div>
                    </div>
                  </label>
                );
              })}
            </div>
          )}
        </LabeledField>

        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end', marginTop: '4px' }}>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={handleSubmit} disabled={!canSubmit}>
            {busy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/* ────────────────── Host manager modal ────────────────── */

function HostManagerModal({
  hosts,
  customVoices,
  onClose,
  onError,
}: {
  hosts: Host[];
  customVoices: Voice[];
  onClose: () => void;
  onError: (msg: string) => void;
}) {
  const [localHosts, setLocalHosts] = useState<Host[]>(hosts);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [personality, setPersonality] = useState('');
  const [speakingStyle, setSpeakingStyle] = useState('');
  const [voiceId, setVoiceId] = useState('');
  const [role, setRole] = useState<HostRole>('host');
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setName('');
    setPersonality('');
    setSpeakingStyle('');
    setVoiceId('');
    setRole('host');
    setCreating(false);
  };

  const handleCreate = async () => {
    if (!name.trim() || !voiceId) return;
    setBusy(true);
    try {
      const h = await createHost({
        name: name.trim(),
        personality: personality.trim() || undefined,
        speaking_style: speakingStyle.trim() || undefined,
        voice_id: voiceId,
        role,
      });
      setLocalHosts((prev) => [...prev, h]);
      reset();
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Create host failed');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this host?')) return;
    try {
      await deleteHost(id);
      setLocalHosts((prev) => prev.filter((h) => h.id !== id));
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Delete failed');
    }
  };

  return (
    <Modal title="Manage hosts" onClose={onClose} wide>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <div style={{ fontSize: '12.5px', color: 'var(--color-text-dim)', lineHeight: 1.5 }}>
          Hosts are podcast personas. Each host must use one of your custom voices.
        </div>

        {/* Host list */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '280px', overflowY: 'auto' }}>
          {localHosts.length === 0 ? (
            <div style={{ padding: '16px', fontSize: '12.5px', color: 'var(--color-text-dim)', textAlign: 'center' }}>
              No hosts yet.
            </div>
          ) : (
            localHosts.map((h) => {
              const v = customVoices.find((x) => x.id === h.voice_id);
              return (
                <div
                  key={h.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '12px',
                    padding: '12px 14px',
                    borderRadius: '10px',
                    background: 'rgba(255,255,255,0.02)',
                    border: '1px solid rgba(255,255,255,0.06)',
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: '13.5px', color: 'var(--color-text)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                      {h.name}
                      <span
                        style={{
                          fontSize: '9.5px',
                          textTransform: 'uppercase',
                          letterSpacing: '0.08em',
                          color: 'var(--color-accent-hover)',
                          background: 'var(--color-accent-dim)',
                          padding: '1px 6px',
                          borderRadius: '4px',
                        }}
                      >
                        {h.role}
                      </span>
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)', marginTop: '3px' }}>
                      Voice: {v ? v.name : '—'}
                    </div>
                    {h.personality && (
                      <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginTop: '6px', lineHeight: 1.45 }}>
                        {h.personality}
                      </div>
                    )}
                  </div>
                  <button
                    className="btn-ghost"
                    onClick={() => handleDelete(h.id)}
                    style={{ padding: '4px 6px', color: 'var(--color-text-faint)' }}
                    onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--color-danger)')}
                    onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--color-text-faint)')}
                  >
                    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                      <path d="M3 4h10M6 4V2h4v2M5 4l1 10h4l1-10" />
                    </svg>
                  </button>
                </div>
              );
            })
          )}
        </div>

        {/* Create form */}
        {creating ? (
          <div
            style={{
              padding: '16px',
              borderRadius: '12px',
              background: 'rgba(255,255,255,0.02)',
              border: '1px solid rgba(255,255,255,0.06)',
              display: 'flex',
              flexDirection: 'column',
              gap: '10px',
            }}
          >
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
              <LabeledField label="Name">
                <input type="text" value={name} onChange={(e) => setName(e.target.value)} className="input-field" placeholder="e.g. Alex" />
              </LabeledField>
              <LabeledField label="Role">
                <select value={role} onChange={(e) => setRole(e.target.value as HostRole)} className="input-field" style={selectStyle}>
                  <option value="host">Host</option>
                  <option value="expert">Expert</option>
                </select>
              </LabeledField>
            </div>
            <LabeledField label="Custom voice (required)">
              {customVoices.length === 0 ? (
                <div
                  style={{
                    padding: '12px',
                    fontSize: '12.5px',
                    borderRadius: '10px',
                    background: 'var(--color-danger-dim)',
                    border: '1px solid rgba(255,90,101,0.28)',
                    color: 'var(--color-danger)',
                  }}
                >
                  No custom voices available. Create one via <strong>Custom</strong> first.
                </div>
              ) : (
                <select value={voiceId} onChange={(e) => setVoiceId(e.target.value)} className="input-field" style={selectStyle}>
                  <option value="">Select a custom voice…</option>
                  {customVoices.map((v) => (
                    <option key={v.id} value={v.id}>{v.name}</option>
                  ))}
                </select>
              )}
            </LabeledField>
            <LabeledField label="Personality (optional)">
              <textarea
                value={personality}
                onChange={(e) => setPersonality(e.target.value)}
                className="input-field"
                placeholder="e.g. Curious, skeptical, asks probing questions"
                style={{ minHeight: '64px', resize: 'vertical' }}
              />
            </LabeledField>
            <LabeledField label="Speaking style (optional)">
              <input
                type="text"
                value={speakingStyle}
                onChange={(e) => setSpeakingStyle(e.target.value)}
                className="input-field"
                placeholder="e.g. Warm, conversational, occasional humor"
              />
            </LabeledField>
            <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
              <button className="btn btn-ghost" onClick={reset}>Cancel</button>
              <button className="btn btn-primary" onClick={handleCreate} disabled={!name.trim() || !voiceId || busy}>
                {busy ? 'Adding…' : 'Add host'}
              </button>
            </div>
          </div>
        ) : (
          <button className="btn btn-secondary" onClick={() => setCreating(true)} style={{ alignSelf: 'flex-start' }}>
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
              <path d="M8 3v10M3 8h10" />
            </svg>
            New host
          </button>
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '4px' }}>
          <button className="btn btn-primary" onClick={onClose}>Done</button>
        </div>
      </div>
    </Modal>
  );
}

/* ────────────────── Shared primitives ────────────────── */

function Modal({
  title,
  children,
  onClose,
  wide,
}: {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(10, 10, 15, 0.72)',
        backdropFilter: 'blur(8px)',
        WebkitBackdropFilter: 'blur(8px)',
        zIndex: 100,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '32px',
      }}
      onClick={onClose}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.98 }}
        transition={{ duration: 0.2, ease: [0.25, 0.46, 0.45, 0.94] }}
        onClick={(e) => e.stopPropagation()}
        className="glass-strong"
        style={{
          width: '100%',
          maxWidth: wide ? '640px' : '480px',
          maxHeight: '88vh',
          overflowY: 'auto',
          borderRadius: '18px',
          padding: '22px 24px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '18px' }}>
          <h2 style={{ fontSize: '18px', color: 'var(--color-text)' }}>{title}</h2>
          <button
            className="btn-ghost"
            onClick={onClose}
            style={{ padding: '4px 8px', color: 'var(--color-text-dim)' }}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
              <path d="M3 3l10 10M13 3L3 13" />
            </svg>
          </button>
        </div>
        {children}
      </motion.div>
    </motion.div>
  );
}

function LabeledField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
      <span className="label-eyebrow" style={{ fontSize: '10px' }}>{label}</span>
      {children}
    </div>
  );
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
            letterSpacing: '-0.015em',
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

function MetaChip({ label, value, emphasis }: { label: string; value: string; emphasis?: boolean }) {
  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '8px',
        padding: '5px 11px',
        borderRadius: '999px',
        background: emphasis ? 'var(--color-accent-dim)' : 'rgba(255,255,255,0.04)',
        border: `1px solid ${emphasis ? 'rgba(123,97,255,0.28)' : 'rgba(255,255,255,0.06)'}`,
        fontSize: '11px',
      }}
    >
      <span
        style={{
          color: emphasis ? 'var(--color-accent-hover)' : 'var(--color-text-dim)',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
          fontWeight: 500,
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          color: emphasis ? 'var(--color-text)' : 'var(--color-text-secondary)',
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {value}
      </span>
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
