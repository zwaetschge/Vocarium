import { useEffect, useState } from 'react';
import {
  hs, jsonBody, formatPreciseTime, runProgress, runStageLabels, ALIGNED_SOURCE,
} from '../../lib/hoerspiele';
import type { IntroDetection, Project, Run, Series, Transcript, Voice } from '../../lib/hoerspiele';
import { ProgressBar } from './shared';

/* --- Schritt 1: Quelle ------------------------------------------------ */

export function SourcePanel({ project }: { project: Project }) {
  return (
    <div className="hs-panels">
      <section className="card hs-panel">
        <h2>Textquelle</h2>
        <dl className="hs-dl">
          <dt>Datei</dt><dd>{project.source.filename}</dd>
          <dt>Zeichen</dt><dd>{project.source.characters.toLocaleString('de-DE')}</dd>
          <dt>Kapitel</dt><dd>{project.chapters.length}</dd>
          <dt>Quellhash</dt><dd>{project.source.sha256.slice(0, 12)}…</dd>
        </dl>
      </section>
      <section className="card hs-panel">
        <h2>Erkannte Kapitel</h2>
        <ul className="hs-list">
          {project.chapters.map((chapter) => (
            <li key={chapter.id} className="hs-item">
              <div className="hs-item-head">
                <span className="hs-item-title">
                  <span className="hs-meta" style={{ marginRight: 8 }}>{String(chapter.order).padStart(2, '0')}</span>
                  {chapter.title}
                </span>
                <span className="hs-meta">{chapter.characters.toLocaleString('de-DE')} Zeichen</span>
              </div>
              <span className="hs-item-note">{chapter.preview}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

/* --- Schritt 2: Plex-Serie und Erzaehlstimme -------------------------- */

export function SeriesPanel({ project, refresh }: { project: Project; refresh: () => Promise<void> }) {
  const [query, setQuery] = useState('');
  const [series, setSeries] = useState<Series[]>([]);
  const [seriesMode, setSeriesMode] = useState('');
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voiceMode, setVoiceMode] = useState('');
  const [voice, setVoice] = useState(project.binding?.narrator_voice || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const payload = await hs<{ mode: string; items: Series[] }>(`/plex/series?query=${encodeURIComponent(query)}`);
        if (cancelled) return;
        setSeries(payload.items);
        setSeriesMode(payload.mode);
      } catch (problem) {
        if (!cancelled) setError((problem as Error).message);
      }
    }, query ? 250 : 0);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [query]);

  useEffect(() => {
    hs<{ mode: string; items: Voice[] }>('/tts/voices')
      .then((payload) => {
        setVoices(payload.items);
        setVoiceMode(payload.mode);
        setVoice((current) => current || payload.items[0]?.id || '');
      })
      .catch((problem) => setError((problem as Error).message));
  }, []);

  async function bind(entry: Series) {
    setBusy(true);
    setError('');
    try {
      await hs(`/projects/${project.id}/plex-binding`, {
        method: 'PUT',
        ...jsonBody({
          series_id: entry.id ?? entry.series_id,
          title: entry.title,
          year: entry.year,
          seasons: entry.seasons,
          episodes: entry.episodes,
          audio_language: project.audio_language,
          narrator_voice: voice,
        }),
      });
      await refresh();
    } catch (problem) {
      setError((problem as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const bound = project.binding?.series_id ?? project.binding?.id;

  return (
    <div className="hs-panels">
      <section className="card hs-panel">
        <div className="hs-panel-head">
          <h2>Plex-Serie</h2>
          <span className="status-chip">{seriesMode === 'live' ? 'Live-Plex' : 'Demo-Bibliothek'}</span>
        </div>
        <input
          className="input-field"
          placeholder="Serie suchen …"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <ul className="hs-list" style={{ maxHeight: 420, overflow: 'auto' }}>
          {series.map((entry) => {
            const id = entry.id ?? entry.series_id;
            const selected = bound && String(bound) === String(id);
            return (
              <li key={String(id)} className="hs-item">
                <div className="hs-item-head">
                  <span className="hs-item-title">{entry.title}{entry.year ? ` (${entry.year})` : ''}</span>
                  <span className="hs-meta">{entry.seasons ?? '?'} St. · {entry.episodes ?? '?'} Folgen</span>
                </div>
                {entry.summary && <span className="hs-item-note">{entry.summary.slice(0, 180)}…</span>}
                <div className="hs-actions" style={{ marginTop: 4 }}>
                  <button
                    type="button"
                    className={`btn btn-sm ${selected ? 'btn-outline' : 'btn-primary'}`}
                    disabled={busy}
                    onClick={() => void bind(entry)}
                  >
                    {selected ? 'Verknüpft · erneut übernehmen' : 'Serie verknüpfen'}
                  </button>
                </div>
              </li>
            );
          })}
          {!series.length && <li className="hs-item-note">Keine Treffer.</li>}
        </ul>
      </section>

      <section className="card hs-panel">
        <div className="hs-panel-head">
          <h2>Erzählstimme</h2>
          <span className="status-chip">{voiceMode === 'live' ? 'Live-TTS' : 'Demo'}</span>
        </div>
        <p className="hs-panel-lead">
          Die Erzählstimme spricht alle eingefügten Buchpassagen. Sie wird zusammen mit der Serie
          gespeichert und lässt sich im Skript-Schritt jederzeit tauschen.
        </p>
        <div className="hs-field">
          <label htmlFor="hs-voice">Stimme</label>
          <select id="hs-voice" className="input-field" value={voice} onChange={(event) => setVoice(event.target.value)}>
            {voices.map((entry) => (
              <option key={entry.id} value={entry.id}>{entry.name} · {entry.language} · {entry.source}</option>
            ))}
          </select>
        </div>
        {project.binding && (
          <dl className="hs-dl">
            <dt>Gewählt</dt><dd>{project.binding.title}</dd>
            <dt>Stimme</dt><dd>{project.binding.narrator_voice || '—'}</dd>
          </dl>
        )}
        {error && <div className="hs-error">{error}</div>}
      </section>
    </div>
  );
}

/* --- Schritt 3 und folgende: Verarbeitungslauf ------------------------ */

export function PipelinePanel({
  project, run, start, disabled,
}: { project: Project; run: Run | null; start: () => void; disabled: boolean }) {
  const running = run ? run.status === 'running' || run.status === 'queued' : false;
  const headline = running
    ? run?.message || runStageLabels[run?.stage || ''] || 'Verarbeitung läuft'
    : project.status === 'completed'
      ? 'Hörspiel ist bereit'
      : 'Roman und Serie zusammenführen';

  return (
    <section className="card hs-panel">
      <div className="hs-panel-head">
        <h2>{headline}</h2>
        {run && <span className="status-chip">{runStageLabels[run.stage] || run.stage}</span>}
      </div>

      {run && (
        <>
          <ProgressBar value={runProgress(run)} tone={run.status === 'failed' ? 'failed' : run.status === 'succeeded' ? 'done' : undefined} />
          <span className="hs-meta">{run.completed_units}/{run.total_units} Einheiten · {Math.round(runProgress(run))} %</span>
        </>
      )}

      {run?.status === 'failed' && <div className="hs-error">{run.error || run.message}</div>}

      <p className="hs-panel-lead">
        Der Lauf ordnet Kapitel den Folgen zu, transkribiert den Originalton, gleicht vorhandene
        Untertitel ab, richtet die Erzählpassagen samplegenau aus und rendert sie mit der gewählten
        Stimme.
      </p>

      <ul className="hs-list">
        <li className="hs-item-note">Zeitmarken stammen aus Whisper-Segmenten des Originaltons.</li>
        <li className="hs-item-note">Wiederkehrende Intro- und Werbetrenner werden per Chromaprint erkannt und ausgespart.</li>
        <li className="hs-item-note">Vor dem Export läuft ein automatisches Qualitätsgate über die fertige Timeline.</li>
      </ul>

      <div className="hs-actions">
        <button type="button" className="btn btn-primary" onClick={start} disabled={disabled || running}>
          {running ? 'Lauf aktiv …' : 'Analyse und Qualitätslauf starten'}
        </button>
      </div>
    </section>
  );
}

/* --- Transkript ------------------------------------------------------- */

export function TranscriptPanel({
  transcript, summary,
}: { transcript: Transcript[]; summary?: Project['subtitle_reconciliation'] }) {
  const groups = new Map<string, Transcript[]>();
  transcript.forEach((line) => {
    const key = `${line.chapter_title || ''}|${line.episode_id}`;
    const bucket = groups.get(key);
    if (bucket) bucket.push(line);
    else groups.set(key, [line]);
  });

  const aligned = transcript.filter((line) => line.timestamp_source === ALIGNED_SOURCE).length;

  return (
    <section className="card hs-panel hs-panel-wide">
      <div className="hs-panel-head">
        <h2>Transkript</h2>
        <span className="hs-meta">{transcript.length} Segmente · {aligned} mit Whisper-Zeitmarken</span>
      </div>

      {summary && (
        <span className="hs-meta">
          Untertitelabgleich · {summary.matched_segment_count}/{summary.segment_count} zugeordnet ·{' '}
          {summary.consistent_segment_count} deckungsgleich · {summary.review_segment_count} zur Prüfung ·{' '}
          {summary.missing_segment_count} ohne Untertitel · Quelle {summary.source}
        </span>
      )}

      {Array.from(groups.entries()).map(([key, lines], index) => {
        const [chapterTitle, episodeId] = key.split('|');
        const head = lines[0];
        return (
          <details key={key} className="hs-transcript-group" open={index === 0}>
            <summary>
              {chapterTitle || 'Kapitel'} · Folge {head.episode ?? episodeId}
              {head.episode_title ? ` · ${head.episode_title}` : ''}
              <span className="hs-meta" style={{ marginLeft: 10 }}>{lines.length} Segmente</span>
            </summary>
            <div style={{ marginTop: 10 }}>
              {lines.map((line) => (
                <div key={line.id} className="hs-transcript-line">
                  <time>
                    {formatPreciseTime(line.episode_start_ms ?? line.start_ms)}
                    {' bis '}
                    {formatPreciseTime(line.episode_end_ms ?? line.end_ms)}
                  </time>
                  <p>{line.reconciled_text || line.text}</p>
                  {(line.asr_text || line.subtitle_match_status) && (
                    <small>
                      Whisper · {line.asr_word_count ?? 0} Wörter
                      {line.subtitle_match_score != null && ` · Untertitelabgleich ${Math.round(line.subtitle_match_score * 100)} %`}
                      {line.subtitle_match_status && ` · ${line.subtitle_match_status}`}
                    </small>
                  )}
                </div>
              ))}
            </div>
          </details>
        );
      })}
    </section>
  );
}

/* --- Chromaprint-Erkennung -------------------------------------------- */

export function DetectionPanel({ title, detection }: { title: string; detection: IntroDetection }) {
  return (
    <section className="card hs-panel">
      <div className="hs-panel-head">
        <h2>{title}</h2>
        <span className="status-chip">
          {detection.status === 'detected' ? 'erkannt' : detection.status === 'not_detected' ? 'nicht gefunden' : 'nicht anwendbar'}
        </span>
      </div>
      <dl className="hs-dl">
        <dt>Detektor</dt><dd>{detection.detector}</dd>
        <dt>Folgen</dt><dd>{detection.episode_count}</dd>
        <dt>Fundstellen</dt><dd>{detection.occurrence_count} / mind. {detection.required_occurrences}</dd>
        <dt>Entfernt</dt><dd>{formatPreciseTime(detection.removed_duration_ms)}</dd>
      </dl>
      {detection.reason && <span className="hs-item-note">{detection.reason}</span>}
      {detection.occurrences.length > 0 && (
        <div className="hs-actions">
          {detection.occurrences.slice(0, 12).map((occurrence, index) => (
            <span key={`${occurrence.episode_id}-${index}`} className="status-chip">
              {occurrence.episode_id} · {formatPreciseTime(occurrence.start_ms)}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
