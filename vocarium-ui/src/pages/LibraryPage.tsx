import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { listAudiobooks, audiobookCoverUrl } from '../api';
import { hs, projectStatusLabel, type Project } from '../lib/hoerspiele';
import type { AbBook } from '../types';

type EditionLink = { book_id: string; project_id: string };
async function linksRequest(method = 'GET', body?: EditionLink): Promise<{ links: EditionLink[] }> {
  const response = await fetch('/api/library/links', { method, headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) });
  if (!response.ok) throw new Error('Verknüpfungen konnten nicht geladen oder gespeichert werden.');
  return response.json();
}
export default function LibraryPage() {
  const { kind, id } = useParams();
  const [books, setBooks] = useState<AbBook[]>([]);
  const [plays, setPlays] = useState<Project[]>([]);
  const [links, setLinks] = useState<EditionLink[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [sort, setSort] = useState('recent');
  const [candidate, setCandidate] = useState('');
  const [saving, setSaving] = useState(false);
  const refresh = useCallback(async () => {
    setLoading(true); setError('');
    const results = await Promise.allSettled([listAudiobooks(), hs<Project[]>('/projects'), linksRequest()]);
    const [b,p,l] = results;
    if (b.status === 'fulfilled') setBooks(b.value.books);
    if (p.status === 'fulfilled') setPlays(p.value);
    if (l.status === 'fulfilled') setLinks(l.value.links);
    if (results.some(r => r.status === 'rejected')) setError('Ein Teil der Bibliothek ist nicht erreichbar. Erneut laden, bevor du Verknüpfungen bearbeitest.');
    setLoading(false);
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const entries = useMemo(() => [
    ...books.filter(b => !b.is_hidden).map(b => ({ id:b.id, kind:'book', title:b.title, subtitle:b.author || 'Buchquelle', date:b.progress?.updatedAt || b.created_at, cover:b.has_cover ? audiobookCoverUrl(b.id):'', state:b.progress?.completed ? 'Gehört' : b.progress ? 'Angefangen' : 'Noch nicht gehört' })),
    ...plays.map(p => ({ id:p.id, kind:'play', title:p.title, subtitle:p.binding?.title || 'Hörspielprojekt', date:p.updated_at, cover:'', state:projectStatusLabel(p) })),
  ], [books, plays]);
  const entry = entries.find(e => e.id === id && e.kind === kind);
  const selectedLinks = links.filter(l => kind === 'book' ? l.book_id === id : l.project_id === id);
  const related = entries.filter(e => selectedLinks.some(l => kind === 'book' ? e.kind === 'play' && e.id === l.project_id : e.kind === 'book' && e.id === l.book_id));
  const candidates = entries.filter(e => e.kind !== kind && !related.some(r => r.id === e.id));
  async function changeLink(method: string, otherId: string) {
    if (!id || saving) return;
    setSaving(true);
    try { await linksRequest(method, {book_id: kind === 'book' ? id : otherId, project_id: kind === 'play' ? id : otherId}); setCandidate(''); await refresh(); }
    catch(e) { setError((e as Error).message); }
    finally { setSaving(false); }
  }
  const visible = entries.filter(e => (filter === 'all' || e.kind === filter) && `${e.title} ${e.subtitle}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())).sort((a,b)=> sort === 'title' ? a.title.localeCompare(b.title,'de') : b.date.localeCompare(a.date));
  return <div className="library-page">
    <header className="studio-page-heading"><div><p className="label-eyebrow">Bücher & Hörspiele</p><h1>{entry?.title || 'Deine Bibliothek'}</h1><p>Eine Quelle, mehrere Hörfassungen. Gemeinsam finden und gezielt weiterarbeiten.</p></div>
      <div className="hs-actions"><Link className="btn btn-secondary" to="/audiobooks">Buch importieren</Link><Link className="btn btn-secondary" to="/hoerspiele">Hörspiel erstellen</Link></div></header>
    {error && <div role="alert" className="hs-error">{error} <button className="btn btn-secondary" onClick={()=>void refresh()}>Erneut laden</button></div>}
    {loading && <p role="status">Bibliothek wird geladen…</p>}
    {id ? entry ? <>
      <Link to="/library">← Zum gemeinsamen Regal</Link>
      <section className="card edition-detail">
        {entry.cover && <img src={entry.cover} alt="" />}
        <div><span className="status-chip">{kind === 'book' ? 'Buch / Hörbuch' : 'Hörspiel'}</span><h2>{entry.title}</h2><p>{entry.subtitle}</p><p>{entry.state}</p>
          <Link className="btn btn-primary" to={kind === 'book' ? `/audiobooks/${id}` : `/hoerspiele/${id}`}>{kind === 'book' ? 'Lesen / weiterhören' : 'Hörspiel öffnen'}</Link>
          <p className="hs-item-note">Quelle: {kind === 'book' ? `${books.find(b=>b.id===id)?.format.toUpperCase()} · ${books.find(b=>b.id===id)?.total_chapters} Kapitel` : plays.find(p=>p.id===id)?.source?.filename || 'Importierte Textquelle'}</p>
        </div>
      </section>
      <section className="card studio-section"><h2>Verknüpfte Fassungen</h2><p>Verknüpfe passende Einträge ausdrücklich. Ihre Hörpositionen bleiben unabhängig.</p>
        {related.length === 0 && <p>Noch keine Fassung verknüpft.</p>}
        {related.map(r=><div className="edition-link" key={r.id}><Link to={`/library/${r.kind}/${r.id}`}>{r.title} · {r.kind === 'book' ? 'Buch' : 'Hörspiel'}</Link><button className="btn btn-ghost" disabled={saving || !!error} onClick={()=>void changeLink('DELETE',r.id)}>Verknüpfung lösen</button></div>)}
        <div className="hs-actions"><label>Passende Fassung<select className="input-field" value={candidate} onChange={e=>setCandidate(e.target.value)}><option value="">Fassung auswählen</option>{candidates.map(c=><option key={c.id} value={c.id}>{c.title}</option>)}</select></label><button className="btn btn-secondary" disabled={!candidate || saving || !!error} onClick={()=>void changeLink('POST',candidate)}>Verknüpfen</button></div>
      </section>
    </> : !loading && <p role="status">Dieses Werk ist nicht verfügbar.</p> : <>
      <div className="library-toolbar"><label>Suche<input className="input-field" type="search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="Titel oder Autor" /></label><label>Fassung<select className="input-field" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">Alle Fassungen</option><option value="book">Bücher / Hörbücher</option><option value="play">Hörspiele</option></select></label><label>Sortieren<select className="input-field" value={sort} onChange={e=>setSort(e.target.value)}><option value="recent">Zuletzt verwendet</option><option value="title">Titel A–Z</option></select></label></div>
      <p role="status">{visible.length} Fassungen</p><div className="edition-grid">{visible.map(e=><Link key={`${e.kind}:${e.id}`} className="card edition-card" to={`/library/${e.kind}/${e.id}`}>
        <div className="edition-cover">{e.cover ? <img loading="lazy" src={e.cover} alt="" /> : <span aria-hidden="true">{e.title.slice(0,2)}</span>}</div><div><span className="status-chip">{e.kind === 'book' ? 'Buch / Hörbuch' : 'Hörspiel'}</span><h2>{e.title}</h2><p>{e.subtitle}</p><p>{e.state}</p></div></Link>)}</div>
      {!loading && !visible.length && <p>Keine passenden Fassungen. Ändere deine Suche oder importiere ein Buch.</p>}
    </>}
  </div>;
}
