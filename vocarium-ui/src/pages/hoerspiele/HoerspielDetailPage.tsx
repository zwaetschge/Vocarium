import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  hs, jsonBody, isActiveRun, mergeRuns, projectStatusLabel, runForProject,
} from '../../lib/hoerspiele';
import type { NarrationDensity, NarrationNameEntry, Project, Run } from '../../lib/hoerspiele';
import { Stepper, TrashIcon } from './shared';
import { BookNavigation } from './BookNavigation';
import { PipelinePanel, SeriesPanel, SourcePanel } from './panels';
import { ResultsPanel } from './results';
import { DeleteProjectDialog } from './HoerspieleProjectsPage';

/** Fehlgeschlagene Aktionen erscheinen als lokaler Lauf, damit die
 *  Fortschrittsanzeige die einzige Fehlerstelle der Seite bleibt. */
function preflightFailure(message: string): Run {
  return {
    id: 'preflight',
    status: 'failed',
    stage: 'preflight',
    message,
    completed_units: 0,
    total_units: 0,
    created_at: new Date().toISOString(),
    error: message,
  };
}

export default function HoerspielDetailPage() {
  const { id = '' } = useParams();
  return <BookDetail key={id} id={id} />;
}

function BookDetail({ id }: { id: string }) {
  const navigate = useNavigate();
  const [project, setProject] = useState<Project | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [localRun, setLocalRun] = useState<Run | null>(null);
  const [viewStep, setViewStep] = useState(0);
  const [error, setError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);

  const refresh = useCallback(async () => {
    const [loaded, runList] = await Promise.all([
      hs<Project>(`/projects/${id}`),
      hs<Run[]>(`/pipeline-runs?project_id=${id}&limit=20`),
    ]);
    setProject(loaded);
    setRuns(runList);
    setError('');
  }, [id]);

  useEffect(() => {
    refresh().catch((problem) => setError((problem as Error).message));
  }, [refresh]);

  // Der aktive Lauf bestimmt die Ansicht: Solange er läuft, ist das Skript
  // gesperrt; sobald er endet, muss das Projekt neu geladen werden.
  const serverRun = useMemo(() => runForProject(runs, id), [runs, id]);
  const run = localRun ?? serverRun;
  const busy = run ? isActiveRun(run) : false;

  useEffect(() => {
    if (!serverRun || !isActiveRun(serverRun)) return;
    const timer = window.setInterval(async () => {
      try {
        const update = await hs<Run>(`/pipeline-runs/${serverRun.id}`);
        setRuns((current) => mergeRuns(current, [update]));
        if (!isActiveRun(update)) setProject(await hs<Project>(`/projects/${id}`));
      } catch {
        /* Ein verpasster Tick ist harmlos — der nächste holt es nach. */
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [serverRun, id]);

  // Auch nach einem Fehler können Reparaturen in einem anderen Fenster oder
  // über die API starten. Nur bekannte aktive Läufe zu pollen übersieht sie.
  useEffect(() => {
    if (!project || (serverRun && isActiveRun(serverRun))) return;
    let cancelled = false;
    let pending = false;
    async function discoverRuns() {
      if (pending || document.visibilityState === 'hidden') return;
      pending = true;
      try {
        const latest = await hs<Run[]>(`/pipeline-runs?project_id=${id}&limit=20`);
        if (cancelled || JSON.stringify(latest) === JSON.stringify(runs)) return;
        // Das umfangreiche Projekt nur bei geändertem Laufstand nachladen.
        const loaded = await hs<Project>(`/projects/${id}`);
        if (cancelled) return;
        setProject(loaded);
        setRuns(latest);
        setLocalRun(null);
        setError('');
      } catch {
        // Bestehende Ansicht bei einem Verbindungsfehler erhalten und erneut versuchen.
      } finally {
        pending = false;
      }
    }
    const check = () => { void discoverRuns(); };
    const timer = window.setInterval(check, 15_000);
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', check);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener('focus', check);
      document.removeEventListener('visibilitychange', check);
    };
  }, [id, project, runs, serverRun]);

  // Die Ansicht folgt dem Projektfortschritt, solange der Nutzer nicht selbst
  // zurückgeblättert hat.
  const derivedStep = useMemo(() => {
    if (!project) return 1;
    if (!project.binding) return 2;
    if (project.cues.length) return 5;
    return 3;
  }, [project]);
  const step = viewStep || derivedStep;

  async function guarded(label: string, action: () => Promise<unknown>) {
    setLocalRun(null);
    try {
      await action();
      await refresh();
    } catch (problem) {
      setLocalRun(preflightFailure(`${label}: ${(problem as Error).message}`));
    }
  }

  const actions = {
    refresh,
    rerender: () => guarded('Rendern fehlgeschlagen', () => hs(`/projects/${id}/renders`, { method: 'POST' })),
    realign: () => guarded('Ausrichten fehlgeschlagen', () => hs(`/projects/${id}/alignments`, { method: 'POST' })),
    reconcileSubtitles: () =>
      guarded('Untertitelabgleich fehlgeschlagen', () =>
        hs(`/projects/${id}/subtitle-reconciliations`, {
          method: 'POST',
          ...jsonBody({ language: project?.audio_language || 'de' }),
        })),
    changeVoice: (voiceId: string) =>
      guarded('Stimmwechsel fehlgeschlagen', async () => {
        await hs(`/projects/${id}/narrator-voice`, { method: 'PATCH', ...jsonBody({ voice_id: voiceId }) });
      }),
    updateNameLexicon: (entries: NarrationNameEntry[]) =>
      guarded('Namenslexikon fehlgeschlagen', async () => {
        const result = await hs<{ render_required?: boolean }>(`/projects/${id}/narration-name-lexicon`, {
          method: 'PATCH',
          ...jsonBody({ entries }),
        });
        void result;
      }),
    regenerateNarration: (density: NarrationDensity) =>
      guarded('Skriptaufbau fehlgeschlagen', () =>
        hs(`/projects/${id}/narration-plans`, { method: 'POST', ...jsonBody({ density }) })),
    submitListenerReview: (payload: { verdict: string; score: number; notes: string; findings: string[] }) =>
      guarded('Feedback fehlgeschlagen', () =>
        hs(`/projects/${id}/listener-reviews`, { method: 'POST', ...jsonBody(payload) })),
    repairFromQuality: () =>
      guarded('Reparaturlauf fehlgeschlagen', async () => {
        const created = await hs<Run>(`/projects/${id}/quality-repairs`, { method: 'POST', ...jsonBody({ cue_ids: [] }) });
        setRuns((current) => mergeRuns(current, [created]));
      }),
  };

  function start() {
    void guarded('Start fehlgeschlagen', async () => {
      const created = await hs<Run>(`/projects/${id}/pipeline-runs`, { method: 'POST' });
      setRuns((current) => mergeRuns(current, [created]));
    });
  }

  if (error && !project) {
    return (
      <div className="app-content">
        <div className="hs-error">{error}</div>
        <Link className="btn btn-sm btn-ghost" to="/hoerspiele">Zurück zur Übersicht</Link>
      </div>
    );
  }

  if (!project) {
    return (
      <div className="app-content">
        <div className="skeleton-shimmer" style={{ height: 220, borderRadius: 'var(--radius-panel)' }} />
      </div>
    );
  }

  return (
    <div className="app-content hs-detail">
      <BookNavigation project={project} />
      <header className="hs-detail-head">
        <div>
          <span className="label-eyebrow">Hörspiel</span>
          <h1 className="font-display">{project.title}</h1>
          <span className="hs-meta">
            {projectStatusLabel(project)} · {project.chapters.length} Kapitel
            {project.binding ? ` · ${project.binding.title}` : ' · Plex-Serie offen'}
          </span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Projekt löschen"
          onClick={() => setConfirmDelete(true)}
          disabled={busy}
        >
          <TrashIcon />
        </button>
      </header>

      <Link className="btn btn-ghost btn-sm" to={`/library/play/${id}`}>Werk & verknüpfte Bücher</Link>
      <Stepper stage={step} reachable={Math.max(derivedStep, step)} onSelect={setViewStep} />

      {step === 1 && (
        <>
          <SourcePanel project={project} />
          <div className="hs-nextbar">
            <button type="button" className="btn btn-primary btn-sm" onClick={() => setViewStep(2)}>
              Weiter zur Serie
            </button>
          </div>
        </>
      )}

      {step === 2 && (
        <>
          <SeriesPanel project={project} refresh={refresh} />
          <div className="hs-nextbar">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => setViewStep(3)}
              disabled={!project.binding}
            >
              Weiter zur Analyse
            </button>
          </div>
        </>
      )}

      {step >= 3 && !project.cues.length && (
        <PipelinePanel project={project} run={run} start={start} disabled={busy || !project.binding} />
      )}

      {step >= 3 && project.cues.length > 0 && (
        <ResultsPanel project={project} run={run} runs={runs} actions={actions} />
      )}

      {confirmDelete && (
        <DeleteProjectDialog
          project={project}
          onClose={() => setConfirmDelete(false)}
          onDeleted={() => navigate('/hoerspiele')}
        />
      )}
    </div>
  );
}
