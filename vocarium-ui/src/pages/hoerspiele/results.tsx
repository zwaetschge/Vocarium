import {useRouteField} from '../../hooks/useRouteField';
import { PlayEdition } from '../../components/StudioPlayer';
import { playFile } from '../../state/player';
import { useDraftField } from '../../state/EditorDrafts';
import { useEffect, useState } from 'react';
import { SingleOfflineButton } from '../../components/OfflineControls';
import {
  hs, jsonBody, artifactUrl, formatBytes, formatTime, formatPreciseTime,
  cueBeatLabel, cuePolicyLabel, cuePurposeLabel, listenerFindingOptions,
} from '../../lib/hoerspiele';
import type {
  Cue, EpisodeContextResearch, NarrationDensity, NarrationNameEntry, NarrationReview,
  NarrationRules, Project, QualityReport, Run, Voice,
} from '../../lib/hoerspiele';
import { CheckIcon, CrossIcon } from './shared';
import { DetectionPanel, TranscriptPanel } from './panels';

type Actions = {
  refresh: () => Promise<void>;
  rerender: () => Promise<void>;
  realign: () => Promise<void>;
  reconcileSubtitles: () => Promise<void>;
  changeVoice: (voiceId: string) => Promise<void>;
  updateNameLexicon: (entries: NarrationNameEntry[]) => Promise<void>;
  regenerateNarration: (density: NarrationDensity) => Promise<void>;
  submitListenerReview: (payload: { verdict: string; score: number; notes: string; findings: string[] }) => Promise<void>;
  repairFromQuality: () => Promise<void>;
};

/* --- Erzaehlregeln ---------------------------------------------------- */

function NarrationRulesSummary({ rules }: { rules?: NarrationRules }) {
  if (!rules) return null;
  const coverage = rules.narration_coverage_target_ms ? Math.round(rules.narration_coverage_target_ms / 60000) : null;
  const gap = rules.narration_max_gap_ms ? Math.round(rules.narration_max_gap_ms / 60000) : null;
  return (
    <ul className="hs-list">
      <li className="hs-item-note">Namen stammen aus: {rules.name_source}.</li>
      <li className="hs-item-note">
        {rules.preserve_existing_narrator
          ? 'Vorhandene Erzählerstellen der Serie bleiben erhalten.'
          : 'Vorhandene Kurz-Erzählerstellen dürfen ersetzt werden.'}
      </li>
      {coverage != null && <li className="hs-item-note">Zielabdeckung: etwa alle {coverage} Minuten eine Erzählpassage.</li>}
      {gap != null && <li className="hs-item-note">Maximale Lücke ohne Erzählung: {gap} Minuten.</li>}
      <li className="hs-item-note">
        Ein Einschub wird höchstens {Math.round(rules.max_anchor_distance_ms / 1000)} Sekunden vom belegten Anker entfernt gesetzt.
      </li>
      <li className="hs-item-note">
        Erzwungene Einschübe halten mindestens {Math.round(rules.min_forced_insert_spacing_ms / 1000)} Sekunden Abstand.
      </li>
      <li className="hs-item-note">Jede Passage sitzt samplegenau an einer im Transkript belegten Stelle.</li>
    </ul>
  );
}

function EpisodeContextPanel({ research }: { research?: EpisodeContextResearch | null }) {
  if (!research || !research.episode_contexts.length) return null;
  return (
    <details className="hs-transcript-group">
      <summary>Episodenkontext {research.series_title ? `· ${research.series_title}` : ''}</summary>
      <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 12 }}>
        {research.episode_contexts.map((context) => (
          <div key={context.episode_id} className="hs-item">
            <span className="hs-item-title">{context.episode_id}</span>
            <span className="hs-item-note">{context.synopsis}</span>
            {context.continuity_before && <span className="hs-item-note">Vorher: {context.continuity_before}</span>}
            {context.character_introductions.filter((entry) => entry.introduction_required).length > 0 && (
              <span className="hs-item-note">
                Einzuführen: {context.character_introductions.filter((entry) => entry.introduction_required).map((entry) => entry.name).join(', ')}
              </span>
            )}
            {context.sources.length > 0 && (
              <div className="hs-actions">
                {context.sources.slice(0, 6).map((source) => (
                  <a key={source.url} className="status-chip" href={source.url} target="_blank" rel="noreferrer">{source.title}</a>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}

function NarrationNameLexicon({
  entries, disabled, update,
}: { entries: NarrationNameEntry[]; disabled: boolean; update: Actions['updateNameLexicon'] }) {
  const asText = entries.map((entry) => `${entry.canonical} = ${entry.aliases.join(', ')}`).join('\n');
  const [draft, setDraft] = useState(asText);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => { setDraft(asText); }, [asText]);

  async function submit() {
    setBusy(true);
    setError('');
    try {
      const parsed: NarrationNameEntry[] = draft
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const [canonical, aliases] = line.split('=');
          if (!canonical || !canonical.trim()) throw new Error(`Ungültige Zeile: ${line}`);
          return {
            canonical: canonical.trim(),
            aliases: (aliases || '').split(',').map((alias) => alias.trim()).filter(Boolean),
            evidence: 'manuell',
            confidence: 1,
          };
        });
      await update(parsed);
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="hs-field">
      <label htmlFor="hs-lexicon">Namenslexikon</label>
      <textarea
        id="hs-lexicon"
        className="input-field hs-textarea"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        placeholder="Son-Goku = Goku, Kakarott"
      />
      <span className="hs-field-hint">Eine Zeile je Figur: Kanonischer Name = Alias, Alias.</span>
      {error && <div className="hs-error">{error}</div>}
      <div className="hs-actions">
        <button type="button" className="btn btn-sm btn-outline" onClick={() => void submit()} disabled={disabled || busy}>
          Namen speichern
        </button>
      </div>
    </div>
  );
}

const DENSITY_OPTIONS: { value: NarrationDensity; label: string }[] = [
  { value: 'audio_drama', label: 'Hörspiel · durchgehend begleitete Szenen' },
  { value: 'compact', label: 'Kompakt · etwa alle 12 Minuten' },
  { value: 'balanced', label: 'Ausgewogen · etwa alle 8 Minuten' },
  { value: 'detailed', label: 'Romanfassung · alle belegten Passagen' },
];

function NarrationDensityPicker({
  current, disabled, regenerate,
}: { current?: NarrationDensity; disabled: boolean; regenerate: Actions['regenerateNarration'] }) {
  const [density, setDensity] = useState<NarrationDensity>(current || 'audio_drama');
  const [busy, setBusy] = useState(false);

  return (
    <div className="hs-field">
      <label htmlFor="hs-density">Erzähldichte</label>
      <select id="hs-density" className="input-field" value={density} onChange={(event) => setDensity(event.target.value as NarrationDensity)}>
        {DENSITY_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      <div className="hs-actions">
        <button
          type="button"
          className="btn btn-sm btn-outline"
          disabled={disabled || busy}
          onClick={async () => { setBusy(true); try { await regenerate(density); } finally { setBusy(false); } }}
        >
          Skript neu aufbauen und rendern
        </button>
      </div>
    </div>
  );
}

function NarratorVoicePicker({
  current, disabled, change,
}: { current?: string; disabled: boolean; change: Actions['changeVoice'] }) {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voice, setVoice] = useState(current || '');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    hs<{ items: Voice[] }>('/tts/voices')
      .then((payload) => {
        const items = payload.items;
        // Eine inzwischen entfernte Stimme bleibt sichtbar, sonst springt die
        // Auswahl still auf einen anderen Sprecher.
        if (current && !items.some((entry) => entry.id === current)) {
          items.unshift({ id: current, name: `${current} (nicht mehr verfügbar)`, language: '—', source: '—' });
        }
        setVoices(items);
      })
      .catch(() => setVoices([]));
  }, [current]);

  return (
    <div className="hs-field">
      <label htmlFor="hs-narrator">Erzählstimme</label>
      <select id="hs-narrator" className="input-field" value={voice} onChange={(event) => setVoice(event.target.value)}>
        {voices.map((entry) => (
          <option key={entry.id} value={entry.id}>{entry.name} · {entry.language} · {entry.source}</option>
        ))}
      </select>
      <div className="hs-actions">
        <button
          type="button"
          className="btn btn-sm btn-outline"
          disabled={disabled || busy || !voice || voice === current}
          onClick={async () => { setBusy(true); try { await change(voice); } finally { setBusy(false); } }}
        >
          Stimme speichern
        </button>
      </div>
    </div>
  );
}

/* --- Erzaehlpassagen -------------------------------------------------- */

function CueEditor({
  cue, projectId, refresh, disabled,
}: { cue: Cue; projectId: string; refresh: () => Promise<void>; disabled: boolean }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft, clearDraft] = useDraftField(`hoerspiel:${projectId}:${cue.id}`, cue.text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function save() {
    setBusy(true);
    setError('');
    try {
      await hs(`/projects/${projectId}/cues/${cue.id}`, { method: 'PATCH', ...jsonBody({ text: draft }) });
      await refresh();
      clearDraft();
      setEditing(false);
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const beat = cueBeatLabel(cue.beat_type);

  return (
    <li className="hs-item" id={`cue-${cue.id}`} tabIndex={-1}>
      <div className="hs-item-head">
        <span className="hs-item-title">{cue.chapter_title}</span>
        <span className="hs-meta">
          {formatTime(cue.measured_duration_ms ?? cue.estimated_duration_ms)}
          {cue.target_duration_ms ? ` / Ziel ${formatTime(cue.target_duration_ms)}` : ''}
          {' · Sicherheit '}{Math.round(cue.confidence * 100)} %
        </span>
      </div>

      {disabled && <span className="status-chip">Während der Produktion gesperrt</span>}
      {draft !== cue.text && <p className="draft-status">Ungespeicherter Entwurf</p>}
      {(cue.revision || 1) > 1 && <span className="hs-meta">Bearbeitet · Textstand {cue.revision}</span>}
      {editing ? (
        <>
          <textarea aria-label="Erzählpassage" className="input-field hs-textarea" value={draft} onChange={(event) => setDraft(event.target.value)} />
          {error && <div className="hs-error">{error}</div>}
          <div className="hs-actions">
            <button type="button" className="btn btn-sm btn-primary" onClick={() => void save()} disabled={busy}>
              {busy ? 'Wird gespeichert …' : 'Entwurf speichern'}
            </button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => { setDraft(cue.text); setEditing(false); }} disabled={busy}>
              Abbrechen
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="hs-item-text">{cue.text}</p>
          <span className="hs-meta">
            {cuePurposeLabel(cue.narrative_purpose)}
            {beat ? ` · ${beat}` : ''}
            {' · '}{cuePolicyLabel(cue.placement_policy)}
            {cue.anchor_episode_start_ms != null && ` · Anker ${formatPreciseTime(cue.anchor_episode_start_ms)}`}
            {cue.anchor_match_score != null && ` · Trefferwert ${Math.round(cue.anchor_match_score * 100)} %`}
          </span>
          {cue.information_gain && <span className="hs-item-note">Mehrwert: {cue.information_gain}</span>}
          {cue.anchor_text && <blockquote className="hs-quote">{cue.anchor_text}</blockquote>}
          <div className="hs-actions">
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => setEditing(true)} disabled={disabled}>
              Text bearbeiten
            </button>
          </div>
        </>
      )}
    </li>
  );
}

/* --- Timeline --------------------------------------------------------- */

function TimelinePanel({ timeline, cues }: { timeline: Project['timeline']; cues: Cue[] }) {
  if (!timeline) return null;
  const total = timeline.duration_samples || 1;
  return (
    <section className="card hs-panel hs-panel-wide">
      <div className="hs-panel-head">
        <h2>Timeline</h2>
        <span className="hs-meta">Revision {timeline.revision} · {formatPreciseTime((total / 48000) * 1000)}</span>
      </div>
      <div className="hs-legend">
        <span><i className="hs-swatch" style={{ background: 'rgba(255,255,255,0.18)' }} />Erhaltener Originalton</span>
        <span><i className="hs-swatch" style={{ background: 'var(--color-accent)' }} />Hinzugefügte Bucherzählung</span>
      </div>
      <div className="hs-wave">
        {Array.from({ length: 96 }, (_, index) => (
          <span key={index} className="hs-wave-bar" style={{ height: `${28 + ((index * 37) % 61)}%` }} />
        ))}
        {cues
          .filter((cue) => cue.timeline_start_sample != null)
          .map((cue) => (
            <span
              key={cue.id}
              className="hs-wave-marker"
              style={{ left: `${Math.min(99.6, ((cue.timeline_start_sample as number) / total) * 100)}%` }}
              title={cue.text.slice(0, 90)}
            />
          ))}
      </div>
      {timeline.validation.book_boundary_status && (
        <span className="hs-meta">
          Buchgrenze: {timeline.validation.book_boundary_status}
          {timeline.validation.book_boundary_exclusion_count != null && ` · ${timeline.validation.book_boundary_exclusion_count} Ausschlüsse`}
          {timeline.validation.book_boundary_exclusion_duration_ms != null && ` · ${formatPreciseTime(timeline.validation.book_boundary_exclusion_duration_ms)}`}
        </span>
      )}
      <span className="hs-item-note">{timeline.validation.note}</span>
    </section>
  );
}

/* --- Qualitaetsgate --------------------------------------------------- */

function AutomaticQualityPanel({
  report, review, disabled, submit, repair, project, onCue,
}: {
  report: QualityReport;
  project: Project;
  onCue: (id: string) => void;
  review?: NarrationReview;
  disabled: boolean;
  repair?: () => Promise<void>;
  submit: Actions['submitListenerReview'];
}) {
  const [problemFilter, setProblemFilter] = useState('');
  const [episodeFilter, setEpisodeFilter] = useState('');
  const [scope, setScope] = useState<{ cue_ids: string[]; locked_count: number; adds_coverage: boolean; available: boolean; sample_rate: number } | null>(null);
  const [scopeError, setScopeError] = useState('');
  useEffect(() => { hs<typeof scope>(`/projects/${project.id}/quality-repair-scope`).then(setScope).catch(() => setScopeError('Reparaturumfang ist nicht verfügbar. Projekt erneut laden.')); }, [project.id, report]);
  const relatedCues = (detail: string) => project.cues.filter(c => detail.includes(c.id));
  const episodeOf = (cue: Cue) => cue.anchor_episode_id || cue.episode_id || '';
  const episodes = [...new Map(project.mapping.map(m=>[String(m.episode_id || m.episode),`Folge ${m.episode}${m.episode_title ? ` · ${m.episode_title}` : ''}`])).entries()];
  const failed = report.checks.filter(c=>c.status !== 'passed');
  const passed = report.checks.filter(c=>c.status === 'passed');
  const artifact = project.artifacts.find(a=>a.role === 'delivery');
  const checks = failed.filter(c=>(!problemFilter || c.id === problemFilter) && (!episodeFilter || relatedCues(c.detail + JSON.stringify(c.actual || '')).some(cue=>episodeOf(cue) === episodeFilter)));
  const [verdict, setVerdict] = useState('passed');
  const [score, setScore] = useState(100);
  const [notes, setNotes] = useState('');
  const [findings, setFindings] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function send() {
    setBusy(true);
    setError('');
    try {
      await submit({ verdict, score: score / 100, notes, findings });
      setNotes('');
      setFindings([]);
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card hs-panel hs-panel-wide">
      <div className="hs-panel-head">
        <h2>Automatisches Qualitätsgate</h2>
        <span className="status-chip">
          {report.release_ready ? 'freigegeben' : 'blockiert'} · {report.passed_checks}/{report.total_checks} · {Math.round(report.score * 100)} %
        </span>
      </div>
      <p className="hs-panel-lead">{report.summary}</p>
      {!report.release_ready && repair && (
        <div className="hs-actions">
          <button type="button" className="btn btn-sm btn-primary" onClick={() => void repair()} disabled={disabled || !scope?.available}>
            Beanstandete Cues reparieren
          </button>
          <span className="hs-meta">{scope ? `${scope.cue_ids.length} Passagen neu schreiben · ${scope.locked_count} erhalten${scope.adds_coverage ? ' · zusätzliche Passagen für Lücken' : ''}. Anschließend rendern.` : scopeError || 'Reparaturumfang wird geladen…'}</span>
        </div>
      )}

      <div className="library-toolbar">
        <label>Problem<select className="input-field" value={problemFilter} onChange={e=>setProblemFilter(e.target.value)}><option value="">Alle offenen Kriterien</option>{failed.map(c=><option key={c.id} value={c.id}>{c.label}</option>)}</select></label>
        <label>Folge<select className="input-field" value={episodeFilter} onChange={e=>setEpisodeFilter(e.target.value)}><option value="">Alle Folgen / übergreifend</option>{episodes.map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
      </div>
      <ul className="hs-list">
        {checks.map(check=><li key={check.id} className="hs-check hs-check-fail"><CrossIcon /><div><strong>{check.label}</strong><p className="hs-item-note">{check.detail}</p><div className="hs-actions">{relatedCues(check.detail + JSON.stringify(check.actual || '')).map(cue=><div key={cue.id} className="hs-actions"><button className="btn btn-secondary btn-sm" onClick={()=>onCue(cue.id)}>Passage öffnen · {cue.chapter_title}</button>{artifact && !project.audio_stale && project.timeline && cue.timeline_start_sample != null && scope?.sample_rate ? <button className="btn btn-ghost btn-sm" onClick={()=>playFile({key:`hoerspiel:${project.id}:${artifact.id}`,title:project.title,subtitle:cue.chapter_title,href:`/hoerspiele/${project.id}`,url:artifactUrl(artifact.id)},cue.timeline_start_sample! / scope.sample_rate)}>Stelle anhören</button> : <span className="hs-meta">Keine belegte Zeit in dieser Audiofassung</span>}</div>)}</div>{relatedCues(check.detail + JSON.stringify(check.actual || '')).length === 0 && <span className="hs-meta">Übergreifender Befund; keine einzelne Passage zugeordnet.</span>}</div></li>)}
      </ul>
      {!checks.length && <p role="status">Keine offenen Befunde für diese Auswahl.</p>}
      <details><summary>{passed.length} bestandene Prüfungen</summary><ul className="hs-list">{passed.map(check=><li key={check.id} className="hs-check hs-check-pass"><CheckIcon /><span>{check.label}<p className="hs-item-note">{check.detail}</p></span></li>)}</ul></details>

      {review && (
        <>
          <h3>Optionales Hörfeedback</h3>
          <span className="hs-item-note">
            Status {review.automatic_status}
            {review.score != null && ` · Kalibrierwert ${Math.round(review.score * 100)} %`}
            {review.calibration_mismatch && ' · weicht vom Automatikurteil ab'}
          </span>
          {review.one_shot_guidance.length > 0 && (
            <ul className="hs-list">
              {review.one_shot_guidance.map((hint) => <li key={hint} className="hs-item-note">{hint}</li>)}
            </ul>
          )}
          <div className="hs-row">
            <div className="hs-field">
              <label htmlFor="hs-verdict">Urteil</label>
              <select id="hs-verdict" className="input-field" value={verdict} onChange={(event) => setVerdict(event.target.value)}>
                <option value="passed">Kann so veröffentlicht werden</option>
                <option value="changes_requested">Workflow muss nachbessern</option>
              </select>
            </div>
            <div className="hs-field">
              <label htmlFor="hs-score">Bewertung</label>
              <input
                id="hs-score"
                type="number"
                className="input-field"
                min={0}
                max={100}
                value={score}
                onChange={(event) => setScore(Number(event.target.value))}
              />
              <span className="hs-field-hint">Kalibrierziel mindestens 92 %</span>
            </div>
          </div>
          <div className="hs-actions">
            {listenerFindingOptions.map((option) => {
              const on = findings.includes(option);
              return (
                <button
                  key={option}
                  type="button"
                  className={`btn btn-sm ${on ? 'btn-outline' : 'btn-ghost'}`}
                  onClick={() => setFindings((current) => (on ? current.filter((entry) => entry !== option) : [...current, option]))}
                >
                  {option}
                </button>
              );
            })}
          </div>
          <textarea
            className="input-field hs-textarea"
            maxLength={4000}
            placeholder="Was ist beim Hören aufgefallen?"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
          />
          {error && <div className="hs-error">{error}</div>}
          <div className="hs-actions">
            <button type="button" className="btn btn-sm btn-outline" onClick={() => void send()} disabled={disabled || busy}>
              Optionales Feedback speichern
            </button>
          </div>
          {review.rounds.length > 0 && (
            <ul className="hs-list">
              {review.rounds.map((round) => (
                <li key={round.id} className="hs-item">
                  <div className="hs-item-head">
                    <span className="hs-item-title">
                      {round.verdict === 'passed' ? 'Freigegeben' : 'Nachbessern'} · {Math.round(round.score * 100)} %
                    </span>
                    <span className="hs-meta">Revision {round.timeline_revision}</span>
                  </div>
                  {round.notes && <span className="hs-item-note">{round.notes}</span>}
                  {round.findings.length > 0 && <span className="hs-meta">{round.findings.join(' · ')}</span>}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

/* --- Ergebnisansicht -------------------------------------------------- */

export function ResultsPanel({
  project, run, runs, actions,
}: { project: Project; run: Run | null; runs: Run[]; actions: Actions }) {
  const [rawView, setView] = useRouteField('view', 'overview');
  const view = ['overview','quality','script','sources','history'].includes(rawView) ? rawView : 'overview';
  const [query, setQuery] = useState('');
  const [selectedCue, setSelectedCue] = useState('');
  useEffect(() => { if (view === 'script' && selectedCue) document.getElementById(`cue-${selectedCue}`)?.focus(); }, [view, selectedCue]);
  const openCue = (id: string) => { setQuery(''); setSelectedCue(id); setView('script'); };
  const busy = run ? run.status === 'running' || run.status === 'queued' : false;
  const transcript = project.reconciled_transcript?.length ? project.reconciled_transcript : project.transcript;
  const resolvedQualityFailure = Boolean(
    project.quality_report?.release_ready && !project.audio_stale
    && run?.error?.startsWith('Quality repair did not pass:')
  );


  return (
    <div className="hs-panels">      {project.artifacts.length > 0 && (
        <section className="card hs-panel hs-panel-wide">
          <div className="hs-panel-head">
            <h2>Aktuelle Audiofassung</h2>
            <div className="hs-actions">
              <button type="button" className="btn btn-sm btn-outline" onClick={() => void actions.rerender()} disabled={busy}>
                Neu rendern
              </button>
            </div>
          </div>
          <ul className="hs-list">
            {project.artifacts.map((artifact) => (
              <li key={artifact.id} className="hs-item">
                <div className="hs-item-head">
                  <span className="hs-item-title">{artifact.filename}</span>
                  <span className="hs-meta">{artifact.role} · {formatBytes(artifact.bytes)}</span>
                </div>
                <PlayEdition track={{key:`hoerspiel:${project.id}:${artifact.id}`,title:project.title,subtitle:"Hörspiel",href:`/hoerspiele/${project.id}`,url:artifactUrl(artifact.id)}} />
                <div className="hs-actions">
                  <a className="btn btn-sm btn-ghost" href={artifactUrl(artifact.id)} download={artifact.filename}>Herunterladen</a>
                  <SingleOfflineButton kind="hoerspiel" id={project.id} title={project.title} url={artifactUrl(artifact.id)} extraJson={[`/api/hoerspiele/projects/${project.id}`]} />
                  <span className="hs-meta">{artifact.sha256.slice(0, 12)}…</span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
      {project.audio_stale && <p role="status" className="hs-notice hs-panel-wide">Der gespeicherte Text wurde geändert. Die vorhandene Audiofassung bleibt hörbar; mit „Neu rendern“ übernimmst du die Änderungen.</p>}
      <nav className="hs-actions hs-panel-wide" aria-label="Projektansichten">{[['overview','Übersicht'],['quality','Prüfen'],['script','Skript'],['sources','Quellen'],['history','Verlauf']].map(([key,label])=><button key={key} className={`btn ${view === key ? 'btn-primary':'btn-secondary'}`} aria-current={view === key ? 'page':undefined} onClick={()=>setView(key)}>{label}{key === 'quality' && project.quality_report ? ` (${project.quality_report.total_checks-project.quality_report.passed_checks})` : ''}</button>)}</nav>
      {view === 'overview' && <section className="card studio-section hs-panel-wide"><h2>Produktionsstand</h2><p>{project.cues.length} Erzählpassagen · {project.quality_report ? `${project.quality_report.total_checks-project.quality_report.passed_checks} offene Kriterien`:'Qualitätsprüfung nach dem Rendern'}</p><button className="btn btn-secondary" onClick={()=>setView('quality')}>Offene Kriterien prüfen</button></section>}
      {view === 'history' && <section className="card studio-section hs-panel-wide"><h2>Produktionsverlauf</h2>{runs.map(r=><div className="edition-link" key={r.id}><span>{r.created_at ? new Date(r.created_at).toLocaleString('de') : ''} · {r.status}</span><p>{r.error || r.message}</p></div>)}</section>}

      {run?.status === 'failed' && !resolvedQualityFailure && <div role="alert" className="hs-error hs-panel-wide">{run.error || run.message}</div>}
      {view === 'quality' && !project.quality_report && <p role="status" className="hs-notice hs-panel-wide">Für den aktuellen Textstand liegt noch keine Qualitätsprüfung vor. Nach dem Rendern erscheinen hier die Ergebnisse.</p>}
      {busy && (
        <div className="hs-notice hs-panel-wide">
          Ein Lauf ist aktiv: {run?.message}. Änderungen am Skript sind währenddessen gesperrt.
        </div>
      )}

      {view === 'overview' && project.warnings.length > 0 && (
        <div className="hs-notice hs-panel-wide">
          {project.warnings.map((warning) => <div key={warning}>{warning}</div>)}
        </div>
      )}

      {view === 'sources' && <>
      {project.intro_detection && <DetectionPanel title="Introspur" detection={project.intro_detection} />}
      {project.commercial_bumper_detection && <DetectionPanel title="Werbetrenner" detection={project.commercial_bumper_detection} />}

      <section className="card hs-panel hs-panel-wide">
        <div className="hs-panel-head">
          <h2>Zuordnung</h2>
          <div className="hs-actions">
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => void actions.reconcileSubtitles()} disabled={busy}>
              Untertitel neu abgleichen
            </button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => void actions.realign()} disabled={busy}>
              Neu ausrichten
            </button>
          </div>
        </div>
        <table className="hs-table">
          <thead>
            <tr><th>Kapitel</th><th>Folge</th><th>Sicherheit</th><th>Beleg</th></tr>
          </thead>
          <tbody>
            {project.mapping.map((entry) => (
              <tr key={entry.id}>
                <td data-label="Kapitel" style={{ color: 'var(--color-text)' }}>{entry.chapter_title}</td>
                <td data-label="Folge">{entry.episode}{entry.episode_title ? ` · ${entry.episode_title}` : ''}</td>
                <td data-label="Sicherheit" className="hs-meta">{Math.round(entry.confidence * 100)} %</td>
                <td data-label="Beleg" className="hs-item-note">{entry.evidence}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {transcript.length > 0 && <TranscriptPanel transcript={transcript} summary={project.subtitle_reconciliation} />}

      </>}
      {view === 'script' && <>
      <section className="card hs-panel hs-panel-wide">
        <div className="hs-panel-head">
          <h2>Erzähler-Skript</h2>
          <span className="hs-meta">{project.cues.length} Passagen</span>
        </div>

        <NarrationRulesSummary rules={project.narration_rules} />
        <EpisodeContextPanel research={project.episode_context_research} />

        <div className="hs-row">
          <NarrationDensityPicker current={project.narration_density} disabled={busy} regenerate={actions.regenerateNarration} />
          <NarratorVoicePicker current={project.binding?.narrator_voice} disabled={busy} change={actions.changeVoice} />
        </div>

        <NarrationNameLexicon
          entries={project.narration_name_lexicon || []}
          disabled={busy}
          update={actions.updateNameLexicon}
        />

        <label>Passagen suchen<input className="input-field" type="search" value={query} onChange={e=>setQuery(e.target.value)} /></label>
        <ul className="hs-list">
          {project.cues.filter(c=>`${c.text} ${c.chapter_title}`.toLowerCase().includes(query.toLowerCase())).map((cue) => (
            <CueEditor key={cue.id} cue={cue} projectId={project.id} refresh={actions.refresh} disabled={busy} />
          ))}
        </ul>
      </section>

      </>}
      {view === 'overview' && <TimelinePanel timeline={project.timeline} cues={project.cues} />}

      {view === 'quality' && project.quality_report && (
        <AutomaticQualityPanel
          project={project}
          onCue={openCue}
          report={project.quality_report}
          review={project.narration_review}
          disabled={busy}
          submit={actions.submitListenerReview}
          repair={actions.repairFromQuality}
        />
      )}


    </div>
  );
}
