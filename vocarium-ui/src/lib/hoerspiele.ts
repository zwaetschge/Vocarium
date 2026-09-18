/**
 * Datentypen und Hilfsfunktionen für den Bereich Hörspiele.
 *
 * Die Typen stammen aus dem Standalone-Projekt Szenenklang und beschreiben
 * unverändert dieselben Serverobjekte — portiert wurde nur die Basis-URL
 * (`/api/v1` → `/api/hoerspiele`) und die Herkunftsbezeichnung der
 * Wortzeitmarken (Qwen3 → Whisper), weil Vocarium ausschließlich
 * whisper-stt als ASR betreibt.
 */

export const HS_BASE = '/api/hoerspiele';

export type NarrationDensity = 'compact' | 'balanced' | 'detailed' | 'audio_drama';

export type NarrationNameEntry = { canonical: string; aliases: string[]; evidence: string; confidence: number };

export type NarrationRules = {
  preserve_existing_narrator: boolean;
  max_anchor_distance_ms: number;
  min_forced_insert_spacing_ms: number;
  name_source: string;
  narration_max_gap_ms?: number;
  narration_coverage_target_ms?: number;
};

export type EpisodeContextResearch = {
  series_title?: string;
  episode_contexts: {
    episode_id: string;
    synopsis: string;
    continuity_before: string;
    major_beats: string[];
    character_introductions: {
      name: string; role: string; distinguishing_traits: string;
      first_appearance: string; introduction_required: boolean;
    }[];
    sources: { url: string; title: string; claim: string }[];
  }[];
};

export type IntroDetection = {
  status: 'detected' | 'not_detected' | 'not_applicable';
  detector: string;
  episode_count: number;
  required_occurrences: number;
  occurrence_count: number;
  removed_duration_ms: number;
  confidence?: number;
  reason?: string;
  occurrences: { episode_id: string; start_ms: number; end_ms: number; duration_ms: number; confidence: number }[];
};

export type Chapter = { id: string; order: number; title: string; characters: number; preview: string };

export type Series = {
  id?: string; series_id?: string; title: string; year?: number;
  seasons?: number; episodes?: number; summary?: string;
  audio_language?: string; narrator_voice?: string;
};

export type Voice = { id: string; name: string; language: string; source: string };

export type Mapping = {
  episode_id?: string; season?: number;
  id: string; chapter_title: string; episode: number; episode_title: string;
  confidence: number; evidence: string; review_state: string;
};

export type Transcript = {
  id: string; chapter_title?: string; episode_id: string; episode?: number; episode_title?: string;
  episode_start_ms?: number; episode_end_ms?: number; start_ms: number; end_ms: number;
  text: string; reconciled_text?: string; subtitle_text?: string; asr_text?: string;
  asr_word_count?: number; subtitle_match_score?: number; subtitle_match_status?: string;
  subtitle_text_status?: string; language?: string; timestamp_source?: string; confidence: number;
};

export type Cue = {
  revision?: number; episode_id?: string; anchor_episode_id?: string;
  id: string; chapter_title: string; text: string; estimated_duration_ms: number;
  measured_duration_ms?: number; target_duration_ms?: number; confidence: number;
  placement_policy: string; narrative_purpose?: string; beat_type?: string; audio_strategy?: string;
  introduced_characters?: string[]; information_gain?: string; alignment_placement?: string;
  anchor_episode_start_ms?: number; anchor_insert_ms?: number; anchor_text?: string;
  anchor_match_score?: number; timeline_start_sample?: number;
};

export type Timeline = {
  revision: number; duration_samples: number; clips: unknown[];
  validation: {
    status: string; note: string; automatic_gate_status?: string; automatic_release_ready?: boolean;
    removed_intro_count?: number; removed_intro_duration_ms?: number; book_boundary_status?: string;
    book_boundary_exclusion_count?: number; book_boundary_exclusion_duration_ms?: number;
    book_boundary_detail?: string;
  };
};

export type Artifact = { id: string; filename: string; bytes: number; sha256: string; role: string };

export type Run = {
  id: string; project_id?: string; status: string; stage: string; message: string;
  completed_units: number; total_units: number; created_at?: string; updated_at?: string; error?: string;
};

export type QualityCheck = { id: string; label: string; status: 'passed' | 'failed'; detail: string; actual?: unknown; expected?: unknown };

export type QualityReport = {
  status: 'passed' | 'blocked'; release_ready: boolean; score: number;
  passed_checks: number; total_checks: number; summary: string;
  checks: QualityCheck[]; timeline_revision: number;
};

export type ListenerRound = {
  id: string; created_at: string; timeline_revision: number;
  verdict: 'passed' | 'changes_requested'; score: number; notes: string; findings: string[];
};

export type NarrationReview = {
  mode: 'calibration'; required: false; status: string; automatic_status: string;
  score: number | null; one_shot_ready: boolean; calibration_mismatch: boolean;
  rounds: ListenerRound[]; one_shot_guidance: string[];
};

export type Project = {
  chapter_count?: number; cue_count?: number;
  id: string; title: string; status: string; stage: number; updated_at: string;
  audio_language: string;
  source: { filename: string; characters: number; sha256: string };
  chapters: Chapter[]; binding: Series | null; mapping: Mapping[];
  transcript: Transcript[]; reconciled_transcript?: Transcript[];
  cues: Cue[]; timeline: Timeline | null; artifacts: Artifact[]; warnings: string[];
  audio_stale?: boolean;
  audio_history?: Artifact[];
  narration_density?: NarrationDensity;
  narration_name_lexicon?: NarrationNameEntry[];
  narration_rules?: NarrationRules;
  episode_context_research?: EpisodeContextResearch | null;
  intro_detection?: IntroDetection | null;
  commercial_bumper_detection?: IntroDetection | null;
  quality_report?: QualityReport | null;
  narration_review?: NarrationReview;
  subtitle_reconciliation?: {
    episode_count: number; subtitle_episode_count: number; subtitle_cue_count: number;
    segment_count: number; matched_segment_count: number; consistent_segment_count: number;
    review_segment_count: number; missing_segment_count: number; source: string;
  };
};

export type AgentProvider = 'codex' | 'claude' | 'zai';
export type AgentCandidate = {
  provider: AgentProvider; model: string;
  reasoning_effort: 'automatic' | 'low' | 'medium' | 'high' | 'xhigh';
  timeout_seconds: number;
};
export type AgentProfile = AgentCandidate & { fallbacks: AgentCandidate[] };
export type AgentSettings = {
  research: AgentProfile;
  scripting: AgentProfile;
  provider_options: { value: AgentProvider; label: string }[];
  model_options: Record<AgentProvider, { value: string; label: string }[]>;
  reasoning_options: { value: AgentProfile['reasoning_effort']; label: string }[];
  fixed_runtime: {
    mapping_web_search: string; episode_context_web_search: string; scene_alignment_web_search: string;
    sandbox: string; approval_policy: string; output_schema: string; user_config: string;
  };
  updated_at: string;
};
export type ProviderStatus = { status: string; authenticated: boolean; label: string; base_url?: string };
export type AgentProviderStatus = { status: string; label: string; providers: Partial<Record<AgentProvider, ProviderStatus>> };
export type CliLoginSession = {
  id: string; provider: 'codex' | 'claude';
  status: 'starting' | 'awaiting_code' | 'completed' | 'error';
  login_url?: string | null; verification_code?: string | null;
  error?: string | null; expires_in_seconds: number;
};

export const steps = ['Quelle', 'Plex-Serie', 'Zuordnung', 'Transkript', 'Skript', 'Timeline', 'Export'];

export const stageLabels: Record<string, string> = {
  source_ready: 'Quelle importiert',
  series_ready: 'Serie gewählt',
  mapping_review: 'Zuordnung prüfen',
  aligning: 'Textabgleich',
  script_review: 'Skript prüfen',
  completed: 'Export bereit',
};

export const runStageLabels: Record<string, string> = {
  mapping: 'Folgen werden zugeordnet',
  transcribing: 'Audio wird transkribiert',
  reconciling_subtitles: 'Untertitel werden abgeglichen',
  researching_context: 'Episodenkontext wird recherchiert',
  writing: 'Erzählpassagen werden ausgerichtet',
  detecting_intros: 'Wiederkehrende Introspuren werden erkannt',
  synthesizing: 'Erzählstimme wird gerendert',
  completed: 'Verarbeitung abgeschlossen',
};

export const listenerFindingOptions = [
  'Erzähltext', 'Szenenzeitpunkt', 'Dialogkollision',
  'Stimme/Aussprache', 'Mischung/Lautstärke', 'Sonstiges',
];

/** Herkunft der Wortzeitmarken. In Vocarium liefert whisper-stt die Timings. */
export const ALIGNED_SOURCE = 'whisper_segments';

export function projectStatusLabel(project: Project) {
  if (project.quality_report && !project.quality_report.release_ready) return 'Qualitätsgate offen';
  return stageLabels[project.status] || project.status;
}

export function isActiveRun(run: Run) {
  return run.status === 'queued' || run.status === 'running';
}

export function runProgress(run: Run) {
  if (!run.total_units) return isActiveRun(run) ? 5 : 0;
  return Math.min(100, Math.max(0, (run.completed_units / run.total_units) * 100));
}

export function runForProject(runs: Run[], projectId: string, activeOnly = false) {
  const ordered = runs
    .filter((run) => run.project_id === projectId)
    .sort((l, r) => String(r.created_at || '').localeCompare(String(l.created_at || '')));
  return (activeOnly ? ordered.find(isActiveRun) : ordered.find(isActiveRun) || ordered[0]) || null;
}

export function mergeRuns(current: Run[], updates: Run[]) {
  const byId = new Map(current.map((run) => [run.id, run]));
  updates.forEach((run) => byId.set(run.id, run));
  return Array.from(byId.values()).sort((l, r) => String(r.created_at || '').localeCompare(String(l.created_at || '')));
}

export function runStatusLabel(run: Run) {
  if (run.status === 'running') return 'läuft';
  if (run.status === 'queued') return 'wartet';
  if (run.status === 'succeeded') return 'fertig';
  if (run.status === 'failed') return 'fehlgeschlagen';
  return run.status;
}

export async function hs<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${HS_BASE}${path}`, init);
  if (!response.ok) {
    const problem = await response.json().catch(() => ({} as { detail?: string }));
    throw new Error(problem.detail || `Anfrage fehlgeschlagen (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export function jsonBody(payload: unknown): RequestInit {
  return { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
}

export function artifactUrl(artifactId: string) {
  return `${HS_BASE}/artifacts/${artifactId}/content`;
}

/** Poster der gebundenen Plex-Serie; 404 ohne Bindung, dann bleibt das Monogramm. */
export function projectCoverUrl(projectId: string) {
  return `${HS_BASE}/projects/${encodeURIComponent(projectId)}/cover`;
}

export function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

export function formatTime(ms: number) {
  const seconds = Math.floor(ms / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

export function formatPreciseTime(ms: number) {
  const total = Math.max(0, ms) / 1000;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = (total % 60).toFixed(2).padStart(5, '0');
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

export function cuePolicyLabel(policy: string) {
  if (policy === 'replace_existing_narration') return 'ersetzt vorhandenen Kurz-Erzähler';
  if (policy === 'overlay_intro_music') return 'über der Intro-Musik';
  if (policy === 'overlay_opening_gap') return 'in früher sprachfreier Passage';
  if (policy === 'overlay_speech_free') return 'in dialogfreier Musik-/Bildpassage';
  if (policy === 'insert_for_dense_action') return 'Zeiteinschub für dichte Handlung';
  if (policy === 'insert_at_aligned_scene_boundary') return 'samplegenau an belegter Szenengrenze';
  if (policy === 'after_aligned_speech_gap') return 'nach der Szene · bestätigte Sprechpause';
  if (policy === 'before_aligned_dialog') return 'vor ausgerichtetem Dialog';
  return 'Kapitelgrenze';
}

export function cuePurposeLabel(purpose?: string) {
  const labels: Record<string, string> = {
    character_introduction: 'Figureneinführung',
    scene_transition: 'Szenenübergang',
    visual_action: 'akustisch fehlende Handlung',
    internal_motivation: 'Motivation',
    offscreen_context: 'Kontext außerhalb des Dialogs',
    continuity_bridge: 'Kontinuitätsbrücke',
    foreshadowing: 'Vorausdeutung',
    book_boundary: 'Buchgrenze',
  };
  return labels[purpose || ''] || 'redaktioneller Kontext';
}

export function cueBeatLabel(beat?: string) {
  const labels: Record<string, string> = {
    scene_setup: 'Orientierung',
    pre_action: 'Aktionsvorbereitung',
    action_sync: 'Aktionsbeat',
    reaction: 'lautlose Reaktion',
    dialogue_bridge: 'Dialogbrücke',
    scene_close: 'Szenenabschluss',
  };
  return labels[beat || ''] || '';
}
