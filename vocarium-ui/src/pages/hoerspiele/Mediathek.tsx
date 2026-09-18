import { PlayEdition } from '../../components/StudioPlayer';
import { Link } from 'react-router-dom';
import { artifactUrl, formatBytes } from '../../lib/hoerspiele';
import type { Project } from '../../lib/hoerspiele';
import { OfflineBadge, SingleOfflineButton } from '../../components/OfflineControls';
import { ProjectCover } from './shared';

/**
 * Mediathek: fertige Hoerspiele zum Hoeren. Auf dem Handy ist das der
 * eigentliche Nutzen des Bereichs -- die Pipeline-Schritte bleiben in der
 * Projektkarte, hier gibt es nur Abspielen, Offline-Kopie und Download.
 */
export function Mediathek({ projects }: { projects: Project[] }) {
  const finished = projects
    .map((project) => ({ project, artifact: project.artifacts.find((a) => a.role === 'delivery') }))
    .filter((item): item is { project: Project; artifact: NonNullable<Project['artifacts'][number]> } => Boolean(item.artifact));
  if (!finished.length) return null;

  return (
    <section className="hs-mediathek">
      <div className="hs-panel-head">
        <span className="label-eyebrow">Mediathek</span>
        <h2>Fertige Hörspiele</h2>
      </div>
      <ul className="hs-list">
        {finished.map(({ project, artifact }) => {
          const url = artifactUrl(artifact.id);
          return (
            <li key={project.id} className="hs-item hs-media-item">
              <div className="hs-item-head">
                <ProjectCover project={project} className="hs-cover-media" />
                <PlayEdition track={{ key:`hoerspiel:${project.id}:${artifact.id}`, title:project.title, subtitle:'Hörspiel', url, href:`/hoerspiele/${project.id}` }} />
                <div className="hs-media-text">
                  <Link className="hs-item-title" to={`/hoerspiele/${project.id}`}>{project.title}</Link>
                  <span className="hs-meta">
                    {project.binding?.title || 'Hörspiel'} · {formatBytes(artifact.bytes)} <OfflineBadge kind="hoerspiel" id={project.id} />
                  </span>
                </div>
              </div>
              <div className="hs-actions">
                <SingleOfflineButton kind="hoerspiel" id={project.id} title={project.title} url={url} extraJson={[`/api/hoerspiele/projects/${project.id}`]} />
                <a className="btn btn-sm btn-ghost" href={url} download={artifact.filename}>Herunterladen</a>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
