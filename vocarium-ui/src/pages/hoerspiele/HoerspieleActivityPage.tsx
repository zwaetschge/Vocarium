import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  hs, isActiveRun, mergeRuns, runProgress, runStageLabels, runStatusLabel,
} from '../../lib/hoerspiele';
import type { Project, Run } from '../../lib/hoerspiele';
import { ProgressBar } from './shared';

export default function HoerspieleActivityPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      const [runList, projectList] = await Promise.all([
        hs<Run[]>('/pipeline-runs?limit=100'),
        hs<Project[]>('/projects'),
      ]);
      setRuns(runList);
      setProjects(projectList);
      setError('');
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  // Laufende Jobs werden im Sekundentakt nachgezogen, ruhende gar nicht.
  useEffect(() => {
    const active = runs.filter(isActiveRun).map((run) => run.id);
    if (!active.length) return;
    const timer = window.setInterval(async () => {
      try {
        const updates = await Promise.all(active.map((id) => hs<Run>(`/pipeline-runs/${id}`)));
        setRuns((current) => mergeRuns(current, updates));
      } catch {
        /* Ein verpasster Tick ist harmlos. */
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [runs]);

  const titleOf = (projectId?: string) =>
    projects.find((project) => project.id === projectId)?.title || 'Unbekanntes Projekt';

  return (
    <div className="app-content hs-page">
      <header className="hs-head">
        <div className="hs-head-text">
          <span className="label-eyebrow">Hörspiele</span>
          <h1 className="font-display">Aktivität</h1>
          <p>Analyse-, Ausrichtungs- und Renderläufe aller Projekte.</p>
        </div>
      </header>

      {error && <div className="hs-error">{error}</div>}

      {loading && <div className="skeleton-shimmer" style={{ height: 160, borderRadius: 'var(--radius-panel)' }} />}

      {!loading && !runs.length && !error && (
        <section className="card hs-panel hs-panel-wide">
          <h2>Noch keine Läufe</h2>
          <p className="hs-panel-lead">Starte in einem Projekt die Analyse, dann erscheint der Fortschritt hier.</p>
          <div className="hs-actions">
            <Link className="btn btn-sm btn-outline" to="/hoerspiele">Zu den Projekten</Link>
          </div>
        </section>
      )}

      {runs.length > 0 && (
        <ul className="hs-list">
          {runs.map((run) => (
            <li key={run.id} className="hs-item">
              <div className="hs-item-head">
                <Link className="hs-item-title" to={`/hoerspiele/${run.project_id}`}>{titleOf(run.project_id)}</Link>
                <span className="status-chip">
                  <span className={`status-dot ${isActiveRun(run) ? 'status-dot-loading' : run.status === 'failed' ? 'status-dot-offline' : 'status-dot-online'}`} />
                  {runStatusLabel(run)}
                </span>
              </div>
              <span className="hs-item-note">{run.message}</span>
              <span className="hs-meta">
                {runStageLabels[run.stage] || run.stage} · {run.completed_units}/{run.total_units} Einheiten · {Math.round(runProgress(run))} %
              </span>
              {isActiveRun(run) && <ProgressBar value={runProgress(run)} />}
              {run.error && <div className="hs-error">{run.error}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
