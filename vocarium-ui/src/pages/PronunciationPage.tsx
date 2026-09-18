import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  addPronunciationRule,
  deletePronunciationRule,
  listPronunciationRules,
  patchPronunciationRule,
  previewPronunciation,
} from '../api';
import type { AbPronunciationRule } from '../api';

const LANGUAGES = ['German', 'English', 'French', 'Italian'];
const LANG_LABEL: Record<string, string> = { German: 'Deutsch', English: 'Englisch', French: 'Französisch', Italian: 'Italienisch' };

export default function PronunciationPage() {
  const [rules, setRules] = useState<AbPronunciationRule[]>([]);
  const [original, setOriginal] = useState('');
  const [replacement, setReplacement] = useState('');
  const [language, setLanguage] = useState('German');
  const [query, setQuery] = useState('');
  const [langFilter, setLangFilter] = useState('');
  const [editId, setEditId] = useState('');
  const [editForm, setEditForm] = useState({ original: '', replacement: '', language: 'German' });
  const [previewText, setPreviewText] = useState('');
  const [previewResult, setPreviewResult] = useState('');
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try {
      setRules((await listPronunciationRules()).rules);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Regeln konnten nicht geladen werden');
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const usedLanguages = useMemo(() => [...new Set(rules.map((r) => r.language))].sort(), [rules]);
  const visible = useMemo(() => {
    let list = rules;
    if (langFilter) list = list.filter((r) => r.language === langFilter);
    const q = query.trim().toLowerCase();
    if (q) list = list.filter((r) => r.original.toLowerCase().includes(q) || r.replacement.toLowerCase().includes(q));
    return list;
  }, [rules, langFilter, query]);

  const add = useCallback(async () => {
    if (!original.trim() || !replacement.trim()) return;
    try {
      await addPronunciationRule(original.trim(), replacement.trim(), language);
      setOriginal('');
      setReplacement('');
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Regel konnte nicht angelegt werden');
    }
  }, [original, replacement, language, refresh]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', maxWidth: '820px' }}>
      <div>
        <div className="section-kicker" style={{ marginBottom: '8px' }}>Vocarium · Aussprache</div>
        <h1 className="display-serif" style={{ fontSize: '38px', margin: 0 }}>Aussprache</h1>
        <p style={{ margin: '6px 0 0', fontSize: '13px', opacity: 0.65, lineHeight: 1.6 }}>
          Wortersetzungen für die Vertonung — der angezeigte Text bleibt unverändert, nur die
          Stimme spricht die Ersetzung. Achtung: Regeländerungen machen vorhandenes Audio
          ungültig; betroffene Bücher müssen neu generiert werden.
        </p>
      </div>

      {error && <p style={{ color: '#f87171', fontSize: '13px', margin: 0 }}>{error}</p>}

      <div className="card" style={{ padding: '16px 18px', display: 'flex', gap: '10px', alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: '140px' }}>
          <label className="label-eyebrow">Original</label>
          <input className="input-field" value={original} placeholder="z. B. Hermione"
            onChange={(e) => setOriginal(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void add()} />
        </div>
        <span style={{ paddingBottom: '10px', opacity: 0.5 }}>→</span>
        <div style={{ flex: 1, minWidth: '140px' }}>
          <label className="label-eyebrow">Gesprochen als</label>
          <input className="input-field" value={replacement} placeholder="z. B. Hermine"
            onChange={(e) => setReplacement(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void add()} />
        </div>
        <div style={{ width: '130px' }}>
          <label className="label-eyebrow">Sprache</label>
          <select className="input-field" value={language} onChange={(e) => setLanguage(e.target.value)}>
            {LANGUAGES.map((l) => <option key={l} value={l}>{LANG_LABEL[l]}</option>)}
          </select>
        </div>
        <button className="btn btn-primary" onClick={() => void add()} disabled={!original.trim() || !replacement.trim()}>
          Hinzufügen
        </button>
      </div>

      <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
        <input className="input-field" placeholder="Regeln durchsuchen…" value={query}
          onChange={(e) => setQuery(e.target.value)} style={{ width: '220px' }} />
        <button className={langFilter === '' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
          onClick={() => setLangFilter('')}>Alle</button>
        {usedLanguages.map((l) => (
          <button key={l} className={langFilter === l ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
            onClick={() => setLangFilter(langFilter === l ? '' : l)}>{LANG_LABEL[l] ?? l}</button>
        ))}
        <span style={{ fontSize: '11.5px', opacity: 0.55, marginLeft: 'auto' }}>
          {visible.length}/{rules.length} Regeln
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {visible.map((r) => (
          editId === r.id ? (
            <div key={r.id} className="card" style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 14px', flexWrap: 'wrap' }}>
              <input className="input-field" value={editForm.original} style={{ flex: 1, minWidth: '110px' }}
                onChange={(e) => setEditForm({ ...editForm, original: e.target.value })} />
              <span style={{ opacity: 0.45 }}>→</span>
              <input className="input-field" value={editForm.replacement} style={{ flex: 1, minWidth: '110px' }}
                onChange={(e) => setEditForm({ ...editForm, replacement: e.target.value })} />
              <select className="input-field" value={editForm.language} style={{ width: '120px' }}
                onChange={(e) => setEditForm({ ...editForm, language: e.target.value })}>
                {LANGUAGES.map((l) => <option key={l} value={l}>{LANG_LABEL[l]}</option>)}
              </select>
              <button className="btn btn-primary btn-sm" disabled={!editForm.original.trim() || !editForm.replacement.trim()}
                onClick={async () => {
                  try {
                    await patchPronunciationRule(r.id, editForm);
                    setEditId('');
                    await refresh();
                  } catch (exc) { setError(exc instanceof Error ? exc.message : 'Speichern fehlgeschlagen'); }
                }}>✓</button>
              <button className="btn btn-ghost btn-sm" onClick={() => setEditId('')}>✕</button>
            </div>
          ) : (
            <div key={r.id} className="card-subtle" style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '10px 16px' }}>
              <span style={{ flex: 1, fontSize: '14px' }}>{r.original}</span>
              <span style={{ opacity: 0.45 }}>→</span>
              <span style={{ flex: 1, fontSize: '14px', color: 'var(--color-accent, #7b61ff)' }}>{r.replacement}</span>
              <span style={{ fontSize: '10.5px', fontFamily: 'var(--font-mono)', opacity: 0.5, width: '76px' }}>
                {LANG_LABEL[r.language] ?? r.language}
              </span>
              <button className="btn btn-ghost btn-sm" aria-label={`Regel ${r.original} bearbeiten`}
                onClick={() => { setEditId(r.id); setEditForm({ original: r.original, replacement: r.replacement, language: r.language }); }}>✎</button>
              <button className="btn btn-ghost btn-sm" aria-label={`Regel ${r.original} löschen`}
                onClick={async () => { await deletePronunciationRule(r.id); await refresh(); }}>✕</button>
            </div>
          )
        ))}
        {!visible.length && (
          <p style={{ fontSize: '13px', opacity: 0.55, margin: 0 }}>
            {rules.length ? 'Keine Treffer.' : 'Noch keine Regeln — Namen, Abkürzungen oder Fremdwörter, die falsch gesprochen werden, hier eintragen.'}
          </p>
        )}
      </div>

      <div className="card" style={{ padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
        <label className="label-eyebrow">Vorschau</label>
        <textarea
          className="input-field" rows={3} value={previewText}
          placeholder="Testtext eingeben, um die Regeln anzuwenden…"
          onChange={(e) => setPreviewText(e.target.value)}
        />
        <div style={{ display: 'flex', gap: '10px', alignItems: 'flex-start' }}>
          <button className="btn btn-secondary btn-sm" disabled={!previewText.trim()}
            onClick={async () => setPreviewResult((await previewPronunciation(previewText)).result)}>
            Anwenden
          </button>
          {previewResult && <p style={{ margin: 0, fontSize: '13.5px', opacity: 0.85, lineHeight: 1.6 }}>{previewResult}</p>}
        </div>
      </div>
    </div>
  );
}
