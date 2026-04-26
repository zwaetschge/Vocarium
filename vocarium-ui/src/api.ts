import type {
  Voice,
  Speaker,
  Model,
  HealthStatus,
  BenchmarkResult,
  BenchmarkConfig,
  User,
  Host,
  HostRole,
  Podcast,
  PodcastFormat,
  PodcastDuration,
  PodcastSource,
  ScriptPayload,
  PodcastProgressEvent,
  PodcastAudioResult,
  LLMProvider,
} from './types';

const BASE = '/api';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `Request failed: ${res.status}`);
  }
  return res.json();
}

async function requestBlob(path: string, init?: RequestInit): Promise<{ blob: Blob; headers: Headers }> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `Request failed: ${res.status}`);
  }
  const blob = await res.blob();
  return { blob, headers: res.headers };
}

// Auth
export async function getMe(): Promise<User> {
  const data = await request<{ user: User }>('/auth/me');
  return data.user;
}

// Health
export async function getHealth(): Promise<HealthStatus> {
  return request('/health');
}

// Voices
export async function getVoices(): Promise<Voice[]> {
  const data = await request<{ voices: Voice[] }>('/voices');
  return data.voices;
}

export async function getVoice(id: string): Promise<Voice> {
  return request(`/voices/${id}`);
}

export async function deleteVoice(id: string): Promise<void> {
  await request(`/voices/${id}`, { method: 'DELETE' });
}

export async function getVoiceAudio(id: string): Promise<Blob> {
  const { blob } = await requestBlob(`/voices/${id}/audio`);
  return blob;
}

export async function cloneVoice(formData: FormData): Promise<{ voice_id: string; name: string }> {
  const res = await fetch(`${BASE}/voices/clone`, { method: 'POST', body: formData });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `Clone failed: ${res.status}`);
  }
  return res.json();
}

export async function designPreview(data: { text: string; description: string; language: string }): Promise<Blob> {
  const { blob } = await requestBlob('/voices/design/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return blob;
}

export async function designSave(data: { name: string; description: string; text: string; language: string }): Promise<{ voice_id: string; name: string }> {
  return request('/voices/design/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

// Custom Voices (prebuilt speakers + optional steering)
export async function getSpeakers(): Promise<Speaker[]> {
  const data = await request<{ speakers: Speaker[] }>('/speakers');
  return data.speakers;
}

export async function previewCustomVoice(data: {
  text: string;
  speaker: string;
  language?: string;
  instruct?: string | null;
}): Promise<Blob> {
  const { blob } = await requestBlob('/voices/custom/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return blob;
}

export async function saveCustomVoice(data: {
  name: string;
  speaker: string;
  instruct?: string | null;
  language?: string;
}): Promise<{ voice_id: string; name: string; speaker: string; instruct: string; language: string }> {
  return request('/voices/custom/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

// Generate
export async function generate(data: {
  text: string;
  voice_id: string;
  model_id?: string;
  language?: string;
  response_format?: string;
}): Promise<{ blob: Blob; meta: { audioDuration: string; generationTime: string; rtf: string; model: string; voice: string } }> {
  const { blob, headers } = await requestBlob('/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return {
    blob,
    meta: {
      audioDuration: headers.get('X-Audio-Duration') || '',
      generationTime: headers.get('X-Generation-Time') || '',
      rtf: headers.get('X-RTF') || '',
      model: headers.get('X-Model') || '',
      voice: headers.get('X-Voice') || '',
    },
  };
}

// Streaming Generate (SSE, chunk-by-chunk)
export interface StreamChunk {
  index: number;
  total: number;
  audio: string; // base64 WAV
  duration: number;
  text: string;
}

export interface StreamDone {
  total_duration: number;
  generation_time: number;
  rtf: number;
  model: string;
  voice: string;
  chunks: number;
}

export async function generateStream(
  data: { text: string; voice_id: string; model_id?: string; language?: string },
  onChunk: (chunk: StreamChunk) => void,
  onDone: (meta: StreamDone) => void,
  onError: (error: string) => void,
): Promise<void> {
  const res = await fetch(`${BASE}/generate/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

  if (!res.ok) {
    const body = await res.text();
    onError(body || `Stream failed: ${res.status}`);
    return;
  }

  const reader = res.body?.getReader();
  if (!reader) { onError('No readable stream'); return; }

  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Parse SSE events from buffer
    const lines = buffer.split('\n');
    buffer = lines.pop() || ''; // keep incomplete line

    let eventType = '';
    let eventData = '';

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith('data: ')) {
        eventData = line.slice(6);
      } else if (line === '' && eventType && eventData) {
        try {
          const parsed = JSON.parse(eventData);
          if (eventType === 'chunk') onChunk(parsed as StreamChunk);
          else if (eventType === 'done') onDone(parsed as StreamDone);
          else if (eventType === 'error') onError(parsed.error || 'Unknown error');
        } catch (e) {
          onError(`Parse error: ${eventData}`);
        }
        eventType = '';
        eventData = '';
      }
    }
  }
}

// Models
export async function getModels(): Promise<Model[]> {
  const data = await request<{ models: Model[] }>('/models');
  return data.models;
}

export async function getCurrentModel(): Promise<Model & { loaded: boolean }> {
  return request('/models/current');
}

export async function switchModel(modelId: string): Promise<{ status: string; model_id: string; load_time_s: number; voices_loaded: number }> {
  return request('/models/switch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_id: modelId }),
  });
}

// Benchmark
export async function runBenchmark(config: BenchmarkConfig): Promise<{ results: BenchmarkResult[] }> {
  return request('/benchmark/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
}

export async function getBenchmarkResults(): Promise<{ results: BenchmarkResult[] }> {
  return request('/benchmark/results');
}

// Transcription (STT)
export async function transcribe(data: { file?: File; url?: string }): Promise<{ text: string }> {
  const formData = new FormData();
  if (data.file) formData.append('file', data.file);
  if (data.url) formData.append('url', data.url);
  const res = await fetch(`${BASE}/transcribe`, { method: 'POST', body: formData });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `Transcription failed: ${res.status}`);
  }
  return res.json();
}

// Languages
export async function getLanguages(): Promise<string[]> {
  const data = await request<{ languages: string[] }>('/languages');
  return data.languages;
}

// Music Generation (ACE-Step)
export interface MusicTask {
  task_id: string;
  status: string;
  queue_position?: number;
}

export interface MusicResult {
  task_id: string;
  status: number; // 0=running, 1=success, 2=failed
  result?: string; // JSON string with file URLs
}

export async function generateMusic(data: {
  prompt: string;
  lyrics?: string;
  audio_duration?: number;
  bpm?: number;
  key_scale?: string;
  time_signature?: string;
  thinking?: boolean;
  audio_format?: string;
  seed?: number;
}): Promise<{ data: MusicTask; code: number }> {
  return request('/music/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function getMusicStatus(taskIds: string[]): Promise<{ data: MusicResult[]; code: number }> {
  return request('/music/status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_ids: taskIds }),
  });
}

export async function getMusicAudio(path: string): Promise<Blob> {
  const { blob } = await requestBlob(`/music/audio?path=${encodeURIComponent(path)}`);
  return blob;
}

export async function enhanceMusic(data: { prompt?: string; lyrics?: string }): Promise<unknown> {
  return request('/music/enhance', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function getMusicHealth(): Promise<{ status: string; backend_running: boolean }> {
  return request('/music/health');
}

// GPU Queue
export async function getQueueStatus(): Promise<{
  current: { job_id: string; service_type: string; description: string; started_at: number } | null;
  queue: Array<{ job_id: string; position: number; service_type: string; description: string; created_at: number }>;
  queue_length: number;
}> {
  return request('/queue/status');
}

// Sound Effects (MMAudio)
export async function generateSfx(data: {
  prompt: string;
  negative_prompt?: string;
  duration?: number;
  cfg_strength?: number;
  num_steps?: number;
  seed?: number;
}): Promise<Blob> {
  const { blob } = await requestBlob('/sfx/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return blob;
}

export async function getSfxHealth(): Promise<{ status: string; model_loaded: boolean }> {
  return request('/sfx/health');
}

// --- Podcast: Hosts ---
export async function getHosts(): Promise<Host[]> {
  const data = await request<{ hosts: Host[] }>('/hosts');
  return data.hosts;
}

export async function createHost(data: {
  name: string;
  personality?: string;
  speaking_style?: string;
  voice_id?: string | null;
  role?: HostRole;
}): Promise<Host> {
  return request('/hosts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function updateHost(hostId: string, data: Partial<Omit<Host, 'id' | 'created_at' | 'updated_at'>>): Promise<Host> {
  return request(`/hosts/${hostId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function deleteHost(hostId: string): Promise<void> {
  await request(`/hosts/${hostId}`, { method: 'DELETE' });
}

// --- Podcast: Podcasts ---
export async function getPodcasts(): Promise<Podcast[]> {
  const data = await request<{ podcasts: Podcast[] }>('/podcasts');
  return data.podcasts;
}

export async function getPodcast(podcastId: string): Promise<Podcast> {
  return request(`/podcasts/${podcastId}`);
}

export async function createPodcast(data: {
  topic?: string;
  format?: PodcastFormat;
  disfluency_level?: number;
  duration?: PodcastDuration;
  language?: string;
  audio_format?: string;
  host_ids?: string[];
}): Promise<Podcast> {
  return request('/podcasts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function updatePodcast(
  podcastId: string,
  data: {
    topic?: string;
    format?: PodcastFormat;
    disfluency_level?: number;
    duration?: PodcastDuration;
    language?: string;
    audio_format?: string;
    host_ids?: string[];
  },
): Promise<Podcast> {
  return request(`/podcasts/${podcastId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function deletePodcast(podcastId: string): Promise<void> {
  await request(`/podcasts/${podcastId}`, { method: 'DELETE' });
}

// --- Podcast: Sources ---
export async function getPodcastSources(podcastId: string): Promise<PodcastSource[]> {
  const data = await request<{ sources: PodcastSource[] }>(`/podcasts/${podcastId}/sources`);
  return data.sources;
}

export async function uploadPodcastSource(podcastId: string, file: File): Promise<PodcastSource> {
  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch(`${BASE}/podcasts/${podcastId}/sources/upload`, {
    method: 'POST',
    body: fd,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `Upload failed: ${res.status}`);
  }
  return res.json();
}

export async function addUrlSource(podcastId: string, url: string, title?: string): Promise<PodcastSource> {
  return request(`/podcasts/${podcastId}/sources/url`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, title }),
  });
}

export async function addTextSource(
  podcastId: string,
  content: string,
  title?: string,
): Promise<PodcastSource> {
  return request(`/podcasts/${podcastId}/sources/text`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, title }),
  });
}

export async function deletePodcastSource(podcastId: string, sourceId: string): Promise<void> {
  await request(`/podcasts/${podcastId}/sources/${sourceId}`, { method: 'DELETE' });
}

// --- Podcast: Script SSE + Segment Edit ---
async function streamPodcastSSE(
  path: string,
  body: unknown,
  onProgress: (p: PodcastProgressEvent) => void,
  onComplete: (payload: ScriptPayload | PodcastAudioResult) => void,
  onError: (error: string) => void,
  method: 'POST' = 'POST',
): Promise<void> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const txt = await res.text();
    onError(txt || `Stream failed: ${res.status}`);
    return;
  }
  const reader = res.body?.getReader();
  if (!reader) {
    onError('No readable stream');
    return;
  }
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    let eventType = '';
    let eventData = '';
    for (const line of lines) {
      if (line.startsWith('event: ')) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith('data: ')) {
        eventData = line.slice(6);
      } else if (line === '' && eventType && eventData) {
        try {
          const parsed = JSON.parse(eventData);
          if (eventType === 'progress') onProgress(parsed as PodcastProgressEvent);
          else if (eventType === 'complete') onComplete(parsed);
          else if (eventType === 'error') onError(parsed.error || 'Unknown error');
        } catch {
          onError(`Parse error: ${eventData}`);
        }
        eventType = '';
        eventData = '';
      }
    }
  }
}

export async function generatePodcastScript(
  podcastId: string,
  onProgress: (p: PodcastProgressEvent) => void,
  onComplete: (payload: ScriptPayload) => void,
  onError: (error: string) => void,
): Promise<void> {
  await streamPodcastSSE(
    `/podcasts/${podcastId}/script/generate`,
    null,
    onProgress,
    (payload) => onComplete(payload as ScriptPayload),
    onError,
  );
}

export async function updateSegment(
  podcastId: string,
  segmentId: string,
  data: { speaker?: string; text?: string; type?: string; voice?: string | null; notes?: string | null },
): Promise<Podcast> {
  return request(`/podcasts/${podcastId}/script/segments/${segmentId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function deleteSegment(podcastId: string, segmentId: string): Promise<void> {
  await request(`/podcasts/${podcastId}/script/segments/${segmentId}`, { method: 'DELETE' });
}

export async function generatePodcastAudio(
  podcastId: string,
  onProgress: (p: PodcastProgressEvent) => void,
  onComplete: (result: PodcastAudioResult) => void,
  onError: (error: string) => void,
  force = false,
): Promise<void> {
  const suffix = force ? '?force=true' : '';
  await streamPodcastSSE(
    `/podcasts/${podcastId}/audio/generate${suffix}`,
    null,
    onProgress,
    (payload) => onComplete(payload as PodcastAudioResult),
    onError,
  );
}

export function getPodcastAudioStreamUrl(podcastId: string): string {
  return `${BASE}/podcasts/${podcastId}/audio/stream`;
}

export async function downloadPodcastAudio(podcastId: string): Promise<Blob> {
  const { blob } = await requestBlob(`/podcasts/${podcastId}/audio/download`);
  return blob;
}

// --- LLM Providers ---
export async function getLLMProviders(): Promise<LLMProvider[]> {
  const data = await request<{ providers: LLMProvider[] }>('/llm/providers');
  return data.providers;
}

export async function getActiveLLMProvider(): Promise<LLMProvider> {
  return request('/llm/providers/active');
}

export async function createLLMProvider(data: {
  name: string;
  base_url: string;
  api_key?: string;
  model: string;
  temperature: number;
  max_tokens: number;
  provider_type?: string;
}): Promise<{ id: string; name: string; status: string }> {
  return request('/llm/providers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function updateLLMProvider(
  providerId: string,
  data: Partial<{
    name: string;
    base_url: string;
    api_key: string;
    model: string;
    temperature: number;
    max_tokens: number;
    is_active: boolean;
    provider_type: string;
  }>,
): Promise<{ status: string; provider_id: string }> {
  return request(`/llm/providers/${providerId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function deleteLLMProvider(providerId: string): Promise<{ status: string; provider_id: string }> {
  return request(`/llm/providers/${providerId}`, { method: 'DELETE' });
}

export async function setActiveLLMProvider(providerId: string): Promise<{ status: string; provider_id: string }> {
  return request(`/llm/providers/${providerId}/set-active`, { method: 'POST' });
}

export async function testLLMProvider(data: {
  base_url: string;
  api_key?: string;
  model: string;
}): Promise<{ status: string; response?: string; code?: number; detail?: string }> {
  return request('/llm/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}
