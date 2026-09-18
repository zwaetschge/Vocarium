import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  addStoreBookToLibrary,
  audiobookStoreCoverUrl,
  deleteStoreBook,
  listAudiobookStore,
  patchStoreBook,
  rateStoreBook,
} from '../api';
import type { AbStoreBook } from '../api';

function Stars({ book, onRate }: { book: AbStoreBook; onRate: (n: number) => void }) {
  const [hover, setHover] = useState(0);
  const shown = hover || book.userRating || Math.round(book.avgRating);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
      <div onMouseLeave={() => setHover(0)} style={{ display: 'flex', cursor: 'pointer' }}>
        {[1, 2, 3, 4, 5].map((n) => (
          <span key={n} role="button" aria-label={`${n} Sterne`}
            onMouseEnter={() => setHover(n)} onClick={() => onRate(n)}
            style={{ fontSize: '13px', color: n <= shown ? '#f5c451' : 'rgba(255,255,255,0.22)', transition: 'color 120ms' }}>
            ★
          </span>
        ))}
      </div>
      {book.ratingCount > 0 && (
        <span style={{ fontSize: '10.5px', fontFamily: 'var(--font-mono)', opacity: 0.6 }}>
          Ø{book.avgRating} · {book.ratingCount}
        </span>
      )}
    </div>
  );
}

function coverGradient(id: string): string {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `linear-gradient(160deg, hsl(${h} 45% 28%) 0%, hsl(${(h + 40) % 360} 55% 16%) 100%)`;
}

export default function AudiobookStorePage() {
  const navigate = useNavigate();
  const [books, setBooks] = useState<AbStoreBook[]>([]);
  const [genre, setGenre] = useState('');
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [editing, setEditing] = useState<AbStoreBook | null>(null);
  const [editForm, setEditForm] = useState({ title: '', author: '', genre: '' });

  const refresh = useCallback(async () => {
    try {
      setBooks((await listAudiobookStore()).books);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Store konnte nicht geladen werden');
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  const genres = useMemo(
    () => [...new Set(books.map((b) => b.genre).filter(Boolean))].sort(),
    [books],
  );
  const visible = useMemo(() => {
    let list = books;
    if (genre) list = list.filter((b) => b.genre === genre);
    const q = search.trim().toLowerCase();
    if (q) list = list.filter((b) => `${b.title} ${b.author} ${b.genre}`.toLowerCase().includes(q));
    return list;
  }, [books, genre, search]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '22px', maxWidth: '1080px' }}>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: '16px', flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: '220px' }}>
          <div className="section-kicker" style={{ marginBottom: '8px' }}>Vocarium · Store</div>
        <h1 className="display-serif" style={{ fontSize: '38px', margin: 0 }}>Geteiltes Regal</h1>
          <p style={{ margin: '4px 0 0', fontSize: '13px', opacity: 0.65 }}>
            Geteilte Hörbücher — bereits generiertes Audio wird mitgeteilt. Teilen über das ⋯-Menü in der Bibliothek.
          </p>
        </div>
        <input
          className="input-field" placeholder="Suchen…" value={search}
          onChange={(e) => setSearch(e.target.value)} style={{ width: '200px' }}
        />
      </div>

      {genres.length > 0 && (
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <button className={genre === '' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'} onClick={() => setGenre('')}>Alle</button>
          {genres.map((g) => (
            <button key={g} className={genre === g ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
              onClick={() => setGenre(genre === g ? '' : g)}>{g}</button>
          ))}
        </div>
      )}

      {error && <p style={{ color: '#f87171', fontSize: '13px', margin: 0 }}>{error}</p>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: '18px' }}>
        {visible.map((b) => (
          <motion.div key={b.id} layout whileHover={{ y: -3 }}>
            <div style={{
              aspectRatio: '2 / 3', borderRadius: '10px', background: coverGradient(b.id),
              boxShadow: '0 8px 24px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.12)',
              padding: '12px', overflow: 'hidden', position: 'relative',
              display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
            }}>
              {b.has_cover && (
                <img src={audiobookStoreCoverUrl(b.id)} alt=""
                  style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
              )}
              {!b.has_cover && (
                <>
                  <span style={{
                    fontSize: '14px', fontWeight: 700, lineHeight: 1.3, color: 'rgba(255,255,255,0.92)',
                    textShadow: '0 1px 3px rgba(0,0,0,0.4)', display: '-webkit-box',
                    WebkitLineClamp: 4, WebkitBoxOrient: 'vertical', overflow: 'hidden',
                  }}>{b.title}</span>
                  <span style={{ fontSize: '11px', color: 'rgba(255,255,255,0.7)' }}>{b.author}</span>
                </>
              )}
              {b.genre && (
                <span style={{
                  position: 'absolute', bottom: '8px', left: '8px', zIndex: 2,
                  fontSize: '10px', padding: '3px 8px', borderRadius: '999px',
                  background: 'rgba(0,0,0,0.55)', color: 'rgba(255,255,255,0.85)', backdropFilter: 'blur(6px)',
                }}>{b.genre}</span>
              )}
            </div>
            <div style={{ marginTop: '7px', display: 'flex', flexDirection: 'column', gap: '5px' }}>
              <span style={{ fontSize: '11px', opacity: 0.6, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {b.title} · von {b.added_by}
              </span>
              <Stars book={b} onRate={async (n) => {
                try {
                  const r = await rateStoreBook(b.id, n);
                  setBooks((list) => list.map((x) => x.id === b.id ? { ...x, ...r } : x));
                } catch (exc) { setError(exc instanceof Error ? exc.message : 'Bewertung fehlgeschlagen'); }
              }} />
              <div style={{ display: 'flex', gap: '5px' }}>
                {b.inLibrary ? (
                  <span className="btn btn-secondary btn-sm" style={{ flex: 1, opacity: 0.6, pointerEvents: 'none' }}>✓ In Bibliothek</span>
                ) : (
                  <button className="btn btn-primary btn-sm" style={{ flex: 1 }} disabled={busy === b.id}
                    onClick={async () => {
                      setBusy(b.id);
                      try {
                        await addStoreBookToLibrary(b.id);
                        navigate('/audiobooks');
                      } catch (exc) {
                        setError(exc instanceof Error ? exc.message : 'Konnte nicht hinzugefügt werden');
                      } finally { setBusy(''); }
                    }}>
                    {busy === b.id ? '…' : '+ Holen'}
                  </button>
                )}
                {b.mine && (
                  <button className="btn btn-ghost btn-sm" aria-label={`${b.title} bearbeiten`}
                    onClick={() => { setEditing(b); setEditForm({ title: b.title, author: b.author, genre: b.genre }); }}>✎</button>
                )}
                {b.mine && (
                  <button className="btn btn-ghost btn-sm" aria-label={`${b.title} aus dem Store entfernen`}
                    onClick={async () => {
                      if (!window.confirm(`„${b.title}" aus dem Store entfernen?`)) return;
                      try { await deleteStoreBook(b.id); await refresh(); }
                      catch (exc) { setError(exc instanceof Error ? exc.message : 'Löschen fehlgeschlagen'); }
                    }}>✕</button>
                )}
              </div>
            </div>
          </motion.div>
        ))}
        {!visible.length && !error && (
          <p style={{ gridColumn: '1 / -1', fontSize: '13px', opacity: 0.6 }}>
            Noch nichts im Store. Teile ein Buch über das ⋯-Menü in deiner Bibliothek.
          </p>
        )}
      </div>

      {editing && (
        <div role="dialog" aria-modal="true" aria-label="Store-Buch bearbeiten"
          onClick={() => setEditing(null)}
          style={{
            position: 'fixed', inset: 0, zIndex: 70, background: 'rgba(0,0,0,0.55)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '20px',
          }}>
          <div className="card" onClick={(e) => e.stopPropagation()}
            style={{ width: '360px', padding: '22px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <strong>„{editing.title}" bearbeiten</strong>
            <div>
              <label className="label-eyebrow">Titel</label>
              <input className="input-field" value={editForm.title}
                onChange={(e) => setEditForm({ ...editForm, title: e.target.value })} />
            </div>
            <div>
              <label className="label-eyebrow">Autor</label>
              <input className="input-field" value={editForm.author}
                onChange={(e) => setEditForm({ ...editForm, author: e.target.value })} />
            </div>
            <div>
              <label className="label-eyebrow">Genre</label>
              <input className="input-field" value={editForm.genre} placeholder="z. B. Fantasy, Krimi, Sachbuch"
                onChange={(e) => setEditForm({ ...editForm, genre: e.target.value })} />
            </div>
            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
              <button className="btn btn-ghost btn-sm" onClick={() => setEditing(null)}>Abbrechen</button>
              <button className="btn btn-primary btn-sm" disabled={!editForm.title.trim()}
                onClick={async () => {
                  try {
                    await patchStoreBook(editing.id, editForm);
                    setEditing(null);
                    await refresh();
                  } catch (exc) { setError(exc instanceof Error ? exc.message : 'Speichern fehlgeschlagen'); }
                }}>Speichern</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
