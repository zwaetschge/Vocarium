import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { hs, type Project } from '../../lib/hoerspiele';

/** Navigation only: switching books never saves drafts or starts production. */
export function BookNavigation({ project }: { project: Project }) {
  const navigate = useNavigate();
  const [books, setBooks] = useState<Project[]>([]);
  useEffect(() => {
    let cancelled = false;
    hs<Project[]>('/projects').then((items) => {
      if (!cancelled) setBooks(items.sort((a, b) => a.title.localeCompare(b.title, 'de', { numeric: true }) || a.id.localeCompare(b.id)));
    }).catch(() => { /* The overview link remains available if discovery fails. */ });
    return () => { cancelled = true; };
  }, []);
  const index = books.findIndex((book) => book.id === project.id);
  const previous = books[index - 1];
  const next = index >= 0 ? books[index + 1] : undefined;
  return (
    <nav className="hs-book-navigation" aria-label="Hörspiel wechseln">
      <Link className="btn btn-ghost btn-sm" to="/hoerspiele">← Alle Hörspiele</Link>
      <label className="hs-book-picker">
        <span>Buch wechseln</span>
        <select aria-label="Buch wechseln" value={project.id} onChange={(event) => navigate(`/hoerspiele/${event.target.value}`)}>
          {index < 0 && <option value={project.id}>{project.title}</option>}
          {books.map((book) => <option key={book.id} value={book.id}>{book.title}</option>)}
        </select>
      </label>
      <div className="hs-book-neighbours">
        {previous ? <Link className="btn btn-ghost btn-sm" to={`/hoerspiele/${previous.id}`} aria-label={`Vorheriges Hörspiel: ${previous.title}`} title={previous.title}>← Zurück</Link> : <button className="btn btn-ghost btn-sm" disabled>← Zurück</button>}
        {next ? <Link className="btn btn-ghost btn-sm" to={`/hoerspiele/${next.id}`} aria-label={`Nächstes Hörspiel: ${next.title}`} title={next.title}>Weiter →</Link> : <button className="btn btn-ghost btn-sm" disabled>Weiter →</button>}
      </div>
    </nav>
  );
}
