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
}

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
}

export type PodcastFormat = 'dialog' | 'monolog' | 'custom';
export type PodcastDuration = 'short' | 'medium' | 'long';
export type PodcastStatus =
  | 'draft'
  | 'generating_script'
  | 'script_ready'
  | 'generating_audio'
  | 'ready'
  | 'error';

export interface ScriptSegment {
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
}

export interface ScriptPayload {
  segments: ScriptSegment[];
  total_words: number;
  estimated_duration: number;
}

export interface Podcast {
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
}

export interface PodcastAudioResult {
  audio_path: string;
  duration: number;
  file_size: number;
  audio_format: string;
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
