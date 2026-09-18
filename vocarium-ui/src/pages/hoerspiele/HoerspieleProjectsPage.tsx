import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  hs, mergeRuns, isActiveRun, runForProject, runProgress, runStageLabels,
  projectStatusLabel,
} from '../../lib/hoerspiele';
import type { Project, Run } from '../../lib/hoerspiele';
import { Modal, PlusIcon, ProgressBar, TrashIcon, ProjectCover } from './shared';
import { Mediathek } from './Mediathek';

const LANGUAGES = [
  { value: 'de', label: 'Deutsch' },
  { value: 'en', label: 'Englisch' },
  { value: 'ja', label: 'Japanisch' },
];


function NewProjectDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (project: Project) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const project = await hs<Project>('/projects', { method: 'POST', body: new FormData(event.currentTarget) });
      onCreated(project);
    } catch (problem) {
      setError((problem as Error).message);
      setBusy(false);
    }
  }

  return (
    <Modal title="Neues Hörspielprojekt" onClose={onClose} busy={busy}>
      <div>
        <span className="section-kicker">Neues Projekt</span>
        <h2 style={{ marginTop: 8 }}>Romantext importieren</h2>
      </div>
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div className="hs-field">
          <label htmlFor="hs-title">Titel</label>
          <input id="hs-title" name="title" className="input-field" required placeholder="z. B. Dragon Ball – Band 1" />
        </div>
        <div className="hs-field">
          <label htmlFor="hs-source">Textquelle</label>
          <input id="hs-source" name="source" type="file" className="input-field" required accept=".txt,.md,.pdf,.docx,.epub" />
          <span className="hs-field-hint">TXT, Markdown, PDF, DOCX oder EPUB · max. 50 MB</span>
        </div>
        <div className="hs-row">
          <div className="hs-field">
            <label htmlFor="hs-source-language">Sprache der Vorlage</label>
            <select id="hs-source-language" name="source_language" className="input-field" defaultValue="de">
              {LANGUAGES.map((language) => <option key={language.value} value={language.value}>{language.label}</option>)}
            </select>
          </div>
          <div className="hs-field">
            <label htmlFor="hs-audio-language">Sprache der Serie</label>
            <select id="hs-audio-language" name="audio_language" className="input-field" defaultValue="de">
              {LANGUAGES.map((language) => <option key={language.value} value={language.value}>{language.label}</option>)}
            </select>
          </div>
        </div>
        <label className="hs-field-hint" style={{ display: 'flex', gap: 10, alignItems: 'flex-start', cursor: 'pointer' }}>
          <input type="checkbox" name="rights_confirmed" value="true" required style={{ marginTop: 2, accentColor: 'var(--color-accent)' }} />
          Ich darf Text und Serienmaterial für diese private Adaption verwenden.
        </label>
        {error && <div className="hs-error">{error}</div>}
        <div className="hs-modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>Abbrechen</button>
          <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? 'Text wird importiert …' : 'Projekt anlegen'}</button>
        </div>
      </form>
    </Modal>
  );
}

export function DeleteProjectDialog({
  project, onClose, onDeleted,
}: { project: Project; onClose: () => void; onDeleted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function remove() {
    setBusy(true);
    setError('');
    try {
      await hs<void>(`/projects/${project.id}`, { method: 'DELETE' });
      onDeleted();
    } catch (problem) {
      setError((problem as Error).message);
      setBusy(false);
    }
  }

  return (
    <Modal title="Projekt löschen" onClose={onClose} busy={busy}>
      <h2>„{project.title}" löschen?</h2>
      <p style={{ fontSize: 13, lineHeight: 1.6, color: 'var(--color-text-secondary)' }}>
        Das Projekt, seine Transkripte, Erzähler-Skripte und erzeugten Audiodateien werden dauerhaft
        gelöscht. Diese Aktion kann nicht rückgängig gemacht werden.
      </p>
      {error && <div className="hs-error">{error}</div>}
      <div className="hs-modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy} autoFocus>Abbrechen</button>
        <button type="button" className="btn btn-danger" onClick={remove} disabled={busy}>
          {busy ? 'Projekt wird gelöscht …' : 'Projekt endgültig löschen'}
        </button>
      </div>
    </Modal>
  );
}

export default function HoerspieleProjectsPage() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Project | null>(null);

  const reload = useCallback(async () => {
    try {
      const [projectList, runList] = await Promise.all([
        hs<Project[]>('/projects'),
        hs<Run[]>('/pipeline-runs?limit=100'),
      ]);
      setProjects(projectList);
      setRuns(runList);
      setError('');
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  // Aktive Läufe werden im Sekundentakt nachgezogen; sobald einer endet,
  // holen wir die Projektliste neu, weil sich Status und Stufe ändern.
  useEffect(() => {
    const active = runs.filter(isActiveRun).map((run) => run.id);
    if (!active.length) return;
    const timer = window.setInterval(async () => {
      try {
        const updates = await Promise.all(active.map((id) => hs<Run>(`/pipeline-runs/${id}`)));
        setRuns((current) => mergeRuns(current, updates));
        if (updates.some((run) => !isActiveRun(run))) {
          setProjects(await hs<Project[]>('/projects'));
        }
      } catch { /* Ein verpasster Tick ist harmlos — der nächste holt es nach. */ }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [runs]);

  if (loading) {
    return (
      <div className="hs-page">
        <div className="hs-grid">
          {[0, 1, 2].map((index) => <div key={index} className="card hs-project skeleton-shimmer" style={{ height: 150 }} />)}
        </div>
      </div>
    );
  }

  if (!projects.length) {
    return (
      <div className="hs-page">
        {error && <div className="hs-error">{error}</div>}
        <div className="card hs-empty">
          <span className="section-kicker">Dein privates Hörspielstudio</span>
          <h1>Geschichten bekommen<br /><em>eine neue Stimme.</em></h1>
          <p>
            Verbinde Romantext mit deiner Plex-Serie. Vocarium ordnet Kapitel den Folgen zu,
            transkribiert den Originalton und legt samplegenau Erzählpassagen dazwischen.
          </p>
          <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
            <PlusIcon />Erstes Projekt anlegen
          </button>
          <div className="hs-flowline">
            <span><b>01</b>Text importieren</span>
            <span><b>02</b>Serie verbinden</span>
            <span><b>03</b>Hörspiel prüfen</span>
          </div>
        </div>
        {creating && <NewProjectDialog onClose={() => setCreating(false)} onCreated={(project) => navigate(`/hoerspiele/${project.id}`)} />}
      </div>
    );
  }

  return (
    <div className="hs-page">
      <header className="hs-head">
        <div className="hs-head-text">
          <span className="section-kicker">Arbeitsbereich</span>
          <h1>Deine Projekte</h1>
          <p>Von der Textquelle bis zum fertigen Hörspiel.</p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
          <PlusIcon />Neues Projekt
        </button>
      </header>

      {error && <div className="hs-error">{error}</div>}

      <Mediathek projects={projects} />

      <div className="hs-grid">
        {projects.map((project) => {
          const run = runForProject(runs, project.id);
          const active = run ? isActiveRun(run) : false;
          const percent = run && active ? runProgress(run) : (project.stage / 7) * 100;
          return (
            <motion.div
              key={project.id}
              className="card hs-project"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2 }}
              role="button"
              tabIndex={0}
              onClick={() => navigate(`/hoerspiele/${project.id}`)}
              onKeyDown={(event) => { if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); navigate(`/hoerspiele/${project.id}`); } }}
            >
              <div className="hs-project-top">
                <ProjectCover project={project} />
                <div className="hs-project-body">
                  <span className="hs-project-title">{project.title}</span>
                  <span className="hs-project-sub">
                    {project.chapter_count ?? project.chapters.length} Kapitel · {project.binding?.title || 'Plex-Serie offen'}
                  </span>
                </div>
                <button
                  type="button"
                  className="icon-button"
                  aria-label="Projekt löschen"
                  disabled={active}
                  onClick={(event) => { event.stopPropagation(); setPendingDelete(project); }}
                >
                  <TrashIcon />
                </button>
              </div>

              <ProgressBar
                value={percent}
                tone={run?.status === 'failed' ? 'failed' : project.status === 'completed' ? 'done' : undefined}
              />

              <div className="hs-project-foot">
                <span className="hs-meta">
                  {run && active
                    ? `${run.message || runStageLabels[run.stage] || run.stage} · ${run.completed_units}/${run.total_units} · ${Math.round(runProgress(run))} %`
                    : `Schritt ${project.stage} von 7`}
                </span>
                <span className="status-chip">
                  {run && active ? (runStageLabels[run.stage] || run.stage) : projectStatusLabel(project)}
                </span>
              </div>
            </motion.div>
          );
        })}
      </div>

      {creating && <NewProjectDialog onClose={() => setCreating(false)} onCreated={(project) => navigate(`/hoerspiele/${project.id}`)} />}
      {pendingDelete && (
        <DeleteProjectDialog
          project={pendingDelete}
          onClose={() => setPendingDelete(null)}
          onDeleted={() => { setPendingDelete(null); void reload(); }}
        />
      )}
    </div>
  );
}
