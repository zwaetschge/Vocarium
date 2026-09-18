export interface User {
  id: number;
  username: string;
  display_name: string;
  created_at: string;
}

export interface Voice {
  id: string;
  name: string;
  language: string;
  source: string;
  design_prompt?: string;
  ref_text?: string;
  speaker?: string;
  instruct?: string;
  created_at: string;
  has_audio: boolean;
  engine?: string;
  /**
   * Kikiri only: `kikiri` for the fine-tunes, `fallback` for the Piper voice
   * bank that exists purely for the case where OmniVoice cannot run.
   */
  group?: string;
  gender?: string;
  backend?: string;
  notes?: string;
}

export type TtsEngine = 'auto' | 'kikiri' | 'vibevoice' | 'omnivoice' | 'qwen';

export interface Speaker {
  id: string;
  name: string;
  gender: string;
  language_hint: string;
}

export interface Model {
  id: string;
  path: string;
  type: string;
  params: string;
  loaded: boolean;
}

export interface HealthStatus {
  api: string;
  tts: {
    status: string;
    current_model: string;
    voices_loaded: number;
    [key: string]: unknown;
  };
  gpu_resources?: {
    enabled?: boolean;
    available?: boolean;
    error?: string;
    [key: string]: unknown;
  };
}

export interface BenchmarkResult {
  model_id: string;
  voice_id: string;
  voice_name?: string;
  run: number;
  audio_duration: number;
  generation_time: number;
  rtf: number;
  load_time?: number;
}

export interface BenchmarkConfig {
  text: string;
  voice_ids: string[];
  model_ids: string[];
  runs_per_combo: number;
}

export interface GenerationMeta {
  audioDuration: string;
  generationTime: string;
  rtf: string;
  model: string;
  voice: string;
}

// --- Podcast ---

export type HostRole = 'host' | 'expert';

export interface Host {
  id: string;
  name: string;
  personality: string;
  speaking_style: string;
  voice_id: string | null;
  role: HostRole;
  created_at: string;
  updated_at: string;
  tagline?: string;
  /** Nur bei "aus Preset übernehmen": Wunschstimme war nicht verfügbar. */
  voice_missing?: boolean;
  /** Kennt OmniVoice/Kikiri die gesetzte Stimme gerade? Fehlt bei alten Antworten. */
  voice_available?: boolean;
}

export interface HostPresetCategory {
  id: string;
  label: string;
  description: string;
}

export interface HostPreset {
  id: string;
  name: string;
  tagline: string;
  category: string;
  role: HostRole;
  voice: string;
  gender: 'male' | 'female';
  personality: string;
  speaking_style: string;
  voice_available: boolean;
  already_added: boolean;
}

export type SettingsNamespace = 'podcast' | 'lab' | 'general';

export interface PodcastPrefs {
  format: PodcastFormat;
  duration: PodcastDuration;
  audio_format: string;
  disfluency_level: number;
  language: string;
}

export interface LabPrefs {
  speech_voice: string;
  speech_format: string;
  speech_speed: number;
}

export interface GeneralPrefs {
  area_default: string;
}

export type PodcastFormat = 'dialog' | 'monolog' | 'custom';
export type PodcastDuration = 'short' | 'medium' | 'long';
export type PodcastStatus =
  | 'draft'
  | 'generating_script'
  | 'script_ready'
  | 'generating_audio'
  | 'ready'
  | 'cancelled'
  | 'error';

export interface ScriptSegment {
  speaker_id?: string;
  id: string;
  script_id?: string | null;
  speaker: string;
  text: string;
  type: string;
  voice?: string | null;
  notes?: string | null;
  position: number;
  word_count: number;
  estimated_duration: number;
  regenerated_from?: string | null;
  created_at: string;
  updated_at: string;
  /** Mixing offset relative to the previous segment's end (ms).
   *  0 = use the contextual gap; <0 = overlap (interruption); >0 = forced gap. */
  overlap_ms?: number;
  /** Music prompt. */
  prompt?: string | null;
  /** Target duration for music segments. */
  duration_ms?: number;
  /** Per-segment volume in dB (typically negative for music ducked under speech). */
  volume_db?: number;
}

export interface ScriptPayload {
  segments: ScriptSegment[];
  total_words: number;
  estimated_duration: number;
}

export interface Podcast {
  script_revision?: string;
  audio_revision?: string;
  audio_stale?: boolean;
  id: string;
  topic: string;
  format: PodcastFormat;
  disfluency_level: number;
  duration: PodcastDuration;
  language: string;
  status: PodcastStatus;
  error_message?: string | null;
  hosts: Host[];
  sources: unknown[];
  script: ScriptPayload | null;
  audio_path?: string | null;
  audio_duration: number;
  audio_format: string;
  audio_size: number;
  audio_sha256: string;
  total_words: number;
  created_at: string;
  updated_at: string;
}

export type PodcastSourceKind = 'file' | 'url' | 'text';
export type PodcastSourceStatus = 'pending' | 'processed' | 'failed';

export interface PodcastSource {
  id: string;
  podcast_id: string;
  type: PodcastSourceKind;
  title: string;
  content?: string | null;
  url?: string | null;
  status: PodcastSourceStatus;
  error_message?: string | null;
  chunk_count: number;
  created_at: string;
  processed_at?: string | null;
}

export interface PodcastProgressEvent {
  stage: string;
  progress: number;
  message: string;
  segment_id?: string | null;
  segment_position?: number | null;
  job_id?: string | null;
  request_id?: string | null;
}

export interface PodcastAudioResult {
  audio_path: string;
  duration: number;
  file_size: number;
  audio_sha256?: string;
  audio_format: string;
}

export interface QueueJob {
  job_id: string;
  user_id?: number | null;
  request_id?: string | null;
  service_type: string;
  description: string;
  status: string;
  position: number;
  created_at: number;
  started_at?: number | null;
  finished_at?: number | null;
  heartbeat_at?: number | null;
  error?: string | null;
  last_error?: string | null;
  attempt_count?: number;
  max_attempts?: number;
  cancel_requested?: boolean;
}

// --- LLM Providers ---

export interface LLMProviderConfig {
  id?: string;
  name: string;
  base_url: string;
  model: string;
  temperature: string;
  max_tokens: number;
}

export interface LLMProvider {
  id: string;
  name: string;
  base_url: string;
  model: string;
  temperature: number;
  max_tokens: number;
  is_active: boolean;
  provider_type: string;
  created_at: string;
}

export interface AbChapter {
  index: number;
  title: string;
  totalSegments: number;
  cachedSegments: number;
  complete: boolean;
}

export interface AbBook {
  id: string;
  title: string;
  author: string;
  format: string;
  voice_id: string | null;
  total_chapters: number;
  created_at: string;
  is_hidden?: boolean;
  has_cover?: boolean;
  collectionIds?: string[];
  progress?: { chapterIndex: number; segmentIndex: number; completed?: boolean; updatedAt?: string } | null;
}

export interface AbCollection {
  id: string;
  name: string;
  color: string;
}

export interface AbGeneration {
  status: 'running' | 'completed' | 'failed' | 'cancelled';
  voice_id: string;
  chapter: number | null;
  done: number;
  total: number;
  error: string;
}

export interface AbBookDetail extends AbBook {
  chapters: AbChapter[];
  generation: AbGeneration | null;
}

export interface AbSegment {
  index: number;
  text: string;
  chapterIndex: number;
  paragraphBreak: boolean;
  heading?: boolean;
}
