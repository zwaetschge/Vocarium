import type {
  Voice,
  Model,
  HealthStatus,
  User,
  Host,
  HostPreset,
  HostPresetCategory,
  SettingsNamespace,
  HostRole,
  Podcast,
  PodcastFormat,
  PodcastDuration,
  PodcastSource,
  ScriptPayload,
  PodcastProgressEvent,
  PodcastAudioResult,
  LLMProvider,
  AbBook,
  AbBookDetail,
  AbCollection,
  AbSegment,
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

async function readSSE(
  res: Response,
  onEvent: (eventType: string, eventData: string) => void,
): Promise<void> {
  const reader = res.body?.getReader();
  if (!reader) throw new Error('No readable stream');

  const decoder = new TextDecoder();
  let buffer = '';
  let eventType = '';
  let eventDataLines: string[] = [];

  const dispatch = () => {
    if (!eventType && eventDataLines.length === 0) return;
    onEvent(eventType || 'message', eventDataLines.join('\n'));
    eventType = '';
    eventDataLines = [];
  };

  const processLine = (rawLine: string) => {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
    if (line === '') {
      dispatch();
    } else if (line.startsWith('event:')) {
      eventType = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      eventDataLines.push(line.slice(5).replace(/^ /, ''));
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let newlineIndex = buffer.indexOf('\n');
    while (newlineIndex >= 0) {
      processLine(buffer.slice(0, newlineIndex));
      buffer = buffer.slice(newlineIndex + 1);
      newlineIndex = buffer.indexOf('\n');
    }
  }

  buffer += decoder.decode();
  if (buffer) processLine(buffer);
  dispatch();
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

// Generate
export async function generate(data: {
  text: string;
  voice_id: string;
  model_id?: string;
  engine?: string;
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
  data: { text: string; voice_id: string; model_id?: string; engine?: string; language?: string },
  onChunk: (chunk: StreamChunk) => void,
  onDone: (meta: StreamDone) => void,
  onError: (error: string) => void,
): Promise<void> {
  let completed = false;
  try {
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

    await readSSE(res, (eventType, eventData) => {
      try {
        const parsed = JSON.parse(eventData);
        if (eventType === 'chunk') onChunk(parsed as StreamChunk);
        else if (eventType === 'done') {
          completed = true;
          onDone(parsed as StreamDone);
        } else if (eventType === 'error') {
          completed = true;
          onError(parsed.error || 'Unknown error');
        }
      } catch {
        completed = true;
        onError(`Parse error: ${eventData}`);
      }
    });
  } catch (err) {
    onError(err instanceof Error ? err.message : 'Stream failed');
    return;
  }

  if (!completed) onError('Stream ended before completion');
}

// Models
export async function getModels(): Promise<Model[]> {
  const data = await request<{ models: Model[] }>('/models');
  return data.models;
}

// Transcription (STT)
export interface TranscriptionWord {
  text: string;
  start: number;
  end: number;
}

export interface TranscriptionResult {
  text: string;
  language: string;
  /** Whisper-Profil, das gelaufen ist: `german` oder `swiss`. */
  model: string;
  words: TranscriptionWord[];
  segments: TranscriptionWord[];
}

export async function transcribe(data: {
  file?: File;
  url?: string;
  /** `german` (large-v3) oder `swiss` (Flix-Finetune). Leer = Servervorgabe. */
  model?: string;
  /** ISO-Code oder `auto`. Leer = Servervorgabe (Deutsch). */
  language?: string;
}): Promise<TranscriptionResult> {
  const formData = new FormData();
  if (data.file) formData.append('file', data.file);
  if (data.url) formData.append('url', data.url);
  if (data.model) formData.append('model', data.model);
  if (data.language) formData.append('language', data.language);
  const res = await fetch(`${BASE}/transcribe`, { method: 'POST', body: formData });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `Transcription failed: ${res.status}`);
  }
  const result = await res.json() as Partial<TranscriptionResult>;
  return {
    text: result.text ?? '',
    language: result.language ?? 'Unknown',
    model: result.model ?? 'german',
    words: result.words ?? [],
    segments: result.segments ?? [],
  };
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

export interface MusicGenerateResponse {
  submit?: { data?: MusicTask; code?: number };
  result?: { data?: MusicResult[]; code?: number };
  data?: MusicTask;
  code?: number;
}

// --- Podcast: Hosts ---
export async function getHosts(): Promise<Host[]> {
  const data = await request<{ hosts: Host[] }>('/hosts');
  return data.hosts;
}

export async function createHost(data: {
  name: string;
  tagline?: string;
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

// --- Podcast: Host-Hub ---
export async function getHostPresets(): Promise<{
  categories: HostPresetCategory[];
  presets: HostPreset[];
}> {
  return request('/hosts/presets');
}

export async function createHostFromPreset(
  presetId: string,
  overrides: { name?: string; voice_id?: string } = {},
): Promise<Host & { voice_missing?: boolean }> {
  return request(`/hosts/presets/${presetId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(overrides),
  });
}

// --- Bereichs-Voreinstellungen ---
export async function getSettingsPrefs<T>(namespace: SettingsNamespace): Promise<T> {
  const data = await request<{ prefs: T }>(`/settings/prefs/${namespace}`);
  return data.prefs;
}

export async function saveSettingsPrefs<T>(
  namespace: SettingsNamespace,
  prefs: Partial<T>,
): Promise<T> {
  const data = await request<{ prefs: T }>(`/settings/prefs/${namespace}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(prefs),
  });
  return data.prefs;
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

export async function deletePodcast(podcastId: string): Promise<void> {
  await request(`/podcasts/${podcastId}`, { method: 'DELETE' });
}

// --- Podcast: Nonverbale Tags ---
export interface SegmentTag {
  id: string;
  label: string;
  hint: string;
}

export async function getSegmentTags(): Promise<SegmentTag[]> {
  const data = await request<{ tags: SegmentTag[] }>('/podcasts/tags');
  return data.tags;
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

export async function reprocessPodcastSource(
  podcastId: string,
  sourceId: string,
): Promise<PodcastSource> {
  return request(`/podcasts/${podcastId}/sources/${sourceId}/reprocess`, { method: 'POST' });
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
  signal?: AbortSignal,
): Promise<void> {
  let completed = false;
  try {
    const res = await fetch(`${BASE}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal,
    });
    if (!res.ok) {
      const txt = await res.text();
      onError(txt || `Stream failed: ${res.status}`);
      return;
    }

    await readSSE(res, (eventType, eventData) => {
      try {
        const parsed = JSON.parse(eventData);
        if (eventType === 'progress') onProgress(parsed as PodcastProgressEvent);
        else if (eventType === 'complete') {
          completed = true;
          onComplete(parsed);
        } else if (eventType === 'error') {
          completed = true;
          onError(parsed.error || 'Unknown error');
        }
      } catch {
        completed = true;
        onError(`Parse error: ${eventData}`);
      }
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      onError('cancelled');
      return;
    }
    onError(err instanceof Error ? err.message : 'Stream failed');
    return;
  }

  if (!completed) onError('Stream ended before completion');
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
  data: {
    speaker_id?: string;
    speaker?: string;
    text?: string;
    type?: string;
    voice?: string | null;
    notes?: string | null;
    prompt?: string | null;
    overlap_ms?: number;
    duration_ms?: number;
    volume_db?: number;
  },
): Promise<Podcast> {
  return request(`/podcasts/${podcastId}/script/segments/${segmentId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function addSegment(
  podcastId: string,
  data: {
    type: 'speech' | 'reaction' | 'pause' | 'sfx' | 'music';
    speaker_id?: string;
    speaker?: string;
    text?: string;
    prompt?: string;
    duration_ms?: number;
    overlap_ms?: number;
    volume_db?: number;
    voice?: string | null;
    notes?: string | null;
    position?: number;
  },
): Promise<Podcast> {
  return request(`/podcasts/${podcastId}/script/segments`, {
    method: 'POST',
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
  signal?: AbortSignal,
): Promise<void> {
  const suffix = force ? '?force=true' : '';
  await streamPodcastSSE(
    `/podcasts/${podcastId}/audio/generate${suffix}`,
    null,
    onProgress,
    (payload) => onComplete(payload as PodcastAudioResult),
    onError,
    'POST',
    signal,
  );
}

export function getPodcastAudioStreamUrl(podcastId: string, revision?: string): string {
  return `${BASE}/podcasts/${podcastId}/audio/stream${revision ? `?revision=${encodeURIComponent(revision)}` : ''}`;
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

// Audiobooks
export async function listAudiobooks(): Promise<{ books: AbBook[] }> {
  return request('/audiobooks');
}

export async function uploadAudiobook(data: {
  file: File; title?: string; voice_id?: string;
}): Promise<AbBook & { segments: number }> {
  const form = new FormData();
  form.append('file', data.file);
  if (data.title) form.append('title', data.title);
  if (data.voice_id) form.append('voice_id', data.voice_id);
  return request('/audiobooks', { method: 'POST', body: form });
}

export async function getAudiobook(id: string, voice?: string): Promise<AbBookDetail> {
  const q = voice ? `?voice=${encodeURIComponent(voice)}` : '';
  return request(`/audiobooks/${id}${q}`);
}

export async function deleteAudiobook(id: string): Promise<{ ok: boolean }> {
  return request(`/audiobooks/${id}`, { method: 'DELETE' });
}

export async function patchAudiobook(id: string, body: { title?: string; voice_id?: string | null; is_hidden?: boolean; author?: string }): Promise<AbBook> {
  return request(`/audiobooks/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
}

export async function getAudiobookContent(id: string, chapter: number): Promise<{
  chapter: { index: number; title: string }; segments: AbSegment[]; totalChapters: number;
}> {
  return request(`/audiobooks/${id}/content?chapter=${chapter}`);
}

export async function generateAudiobook(id: string, body: { voice_id?: string; chapter?: number }): Promise<unknown> {
  return request(`/audiobooks/${id}/generate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
}

export async function saveAudiobookProgress(id: string, chapterIndex: number, segmentIndex: number, completed = false): Promise<void> {
  await request(`/audiobooks/${id}/progress`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chapterIndex, segmentIndex, completed }),
  });
}

/** Live-Variante: fehlende Segmente werden serverseitig on-demand generiert
 *  und landen dabei im regulären Cache. */
export function audiobookSegmentLiveUrl(id: string, voice: string, chapter: number, segment: number): string {
  return `${BASE}/audiobooks/${id}/audio-live/${encodeURIComponent(voice)}/${chapter}/${segment}`;
}

export interface AbBookmark {
  id: string;
  chapterIndex: number;
  segmentIndex: number;
  note: string;
  created_at: string;
}

export async function listAudiobookBookmarks(id: string): Promise<{ bookmarks: AbBookmark[] }> {
  return request(`/audiobooks/${id}/bookmarks`);
}

export async function addAudiobookBookmark(id: string, chapterIndex: number, segmentIndex: number, note = ''): Promise<{ id: string }> {
  return request(`/audiobooks/${id}/bookmarks`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chapterIndex, segmentIndex, note }),
  });
}

export async function deleteAudiobookBookmark(id: string, bmId: string): Promise<void> {
  await request(`/audiobooks/${id}/bookmarks/${bmId}`, { method: 'DELETE' });
}

export function audiobookCoverUrl(id: string): string {
  return `${BASE}/audiobooks/${id}/cover`;
}

export async function uploadAudiobookCover(id: string, file: File): Promise<void> {
  const form = new FormData();
  form.append('cover', file);
  await request(`/audiobooks/${id}/cover`, { method: 'POST', body: form });
}

export async function listAbCollections(): Promise<{ collections: AbCollection[] }> {
  return request('/audiobooks/collections');
}

export async function createAbCollection(name: string, color: string): Promise<AbCollection> {
  return request('/audiobooks/collections', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, color }),
  });
}

export async function deleteAbCollection(cid: string): Promise<void> {
  await request(`/audiobooks/collections/${cid}`, { method: 'DELETE' });
}

export async function addBookToCollection(cid: string, bookId: string): Promise<void> {
  await request(`/audiobooks/collections/${cid}/books`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bookId }),
  });
}

export async function removeBookFromCollection(cid: string, bookId: string): Promise<void> {
  await request(`/audiobooks/collections/${cid}/books/${bookId}`, { method: 'DELETE' });
}

export interface AbExportStatus {
  job: { status: string; done: number; total: number; error: string } | null;
  ready: boolean;
  size: number;
}

export async function startAudiobookExport(id: string, voiceId: string, format: 'm4b' | 'mp3'): Promise<void> {
  await request(`/audiobooks/${id}/export`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ voice_id: voiceId, format }),
  });
}

export async function audiobookExportStatus(id: string, voiceId: string, format: 'm4b' | 'mp3'): Promise<AbExportStatus> {
  return request(`/audiobooks/${id}/export/status?voice=${encodeURIComponent(voiceId)}&format=${format}`);
}

export function audiobookExportDownloadUrl(id: string, voiceId: string, format: 'm4b' | 'mp3'): string {
  return `${BASE}/audiobooks/${id}/export/download?voice=${encodeURIComponent(voiceId)}&format=${format}`;
}

export interface AbQueueJob {
  id: string;
  book_id: string;
  voice_id: string;
  chapter: number | null;
  priority: number;
  status: 'pending' | 'processing' | 'complete' | 'error' | 'cancelled';
  done: number;
  total: number;
  error: string;
  title: string;
  created_at: string;
}

export async function enqueueAbGeneration(bookId: string, voiceId: string, opts: { chapter?: number; priority?: number } = {}): Promise<{ id: string }> {
  return request('/audiobooks/queue', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_id: bookId, voice_id: voiceId, ...opts }),
  });
}

export async function cancelAbQueueJob(qid: string): Promise<void> {
  await request(`/audiobooks/queue/${qid}`, { method: 'DELETE' });
}

export function abQueueEventsUrl(): string {
  return `${BASE}/audiobooks/queue/events`;
}

export interface AbPronunciationRule {
  id: string;
  original: string;
  replacement: string;
  language: string;
  created_at: string;
}

export async function listPronunciationRules(): Promise<{ rules: AbPronunciationRule[] }> {
  return request('/audiobooks/pronunciation');
}

export async function addPronunciationRule(original: string, replacement: string, language = 'German'): Promise<{ id: string }> {
  return request('/audiobooks/pronunciation', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ original, replacement, language }),
  });
}

export async function deletePronunciationRule(rid: string): Promise<void> {
  await request(`/audiobooks/pronunciation/${rid}`, { method: 'DELETE' });
}

export interface AbStoreBook {
  id: string;
  title: string;
  author: string;
  genre: string;
  format: string;
  total_chapters: number;
  created_at: string;
  mine: boolean;
  added_by: string;
  inLibrary: boolean;
  has_cover: boolean;
  avgRating: number;
  ratingCount: number;
  userRating: number | null;
}

export async function rateStoreBook(sid: string, rating: number): Promise<{ avgRating: number; ratingCount: number; userRating: number }> {
  return request(`/audiobooks/store/${sid}/rating`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rating }),
  });
}

export async function patchStoreBook(sid: string, body: { title?: string; author?: string; genre?: string }): Promise<void> {
  await request(`/audiobooks/store/${sid}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function getReaderPrefs(): Promise<Record<string, unknown>> {
  return request('/audiobooks/prefs');
}

export async function putReaderPrefs(prefs: Record<string, unknown>): Promise<void> {
  await request('/audiobooks/prefs', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(prefs),
  });
}

export interface AbSearchHit {
  chapterIndex: number;
  index: number;
  text: string;
  score: number;
}

export async function searchAudiobook(id: string, q: string): Promise<{ results: AbSearchHit[]; ready: boolean }> {
  return request(`/audiobooks/${id}/search?q=${encodeURIComponent(q)}`);
}

export interface AbOfflineVoice {
  voice_id: string;
  cachedSegments: number;
  totalSegments: number;
  complete: boolean;
}

export async function getOfflineVoices(bookId: string): Promise<{ voices: AbOfflineVoice[]; totalSegments: number }> {
  return request(`/audiobooks/${bookId}/offline-voices`);
}

export async function patchPronunciationRule(rid: string, body: { original?: string; replacement?: string; language?: string }): Promise<void> {
  await request(`/audiobooks/pronunciation/${rid}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function listAudiobookStore(): Promise<{ books: AbStoreBook[] }> {
  return request('/audiobooks/store');
}

export async function publishBookToStore(bookId: string, genre: string): Promise<{ id: string }> {
  return request('/audiobooks/store/publish', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_id: bookId, genre }),
  });
}

export async function addStoreBookToLibrary(sid: string): Promise<{ id: string }> {
  return request(`/audiobooks/store/${sid}/add`, { method: 'POST' });
}

export async function deleteStoreBook(sid: string): Promise<void> {
  await request(`/audiobooks/store/${sid}`, { method: 'DELETE' });
}

export function audiobookStoreCoverUrl(sid: string): string {
  return `${BASE}/audiobooks/store/${sid}/cover`;
}

export interface AbAmbience {
  id: string;
  name: string;
  filename: string;
  created_at: string;
}

export async function listAmbience(): Promise<{ sounds: AbAmbience[] }> {
  return request('/audiobooks/ambience');
}

export async function uploadAmbience(file: File, name = ''): Promise<{ id: string }> {
  const form = new FormData();
  form.append('file', file);
  if (name) form.append('name', name);
  return request('/audiobooks/ambience', { method: 'POST', body: form });
}

export async function deleteAmbience(aid: string): Promise<void> {
  await request(`/audiobooks/ambience/${aid}`, { method: 'DELETE' });
}

export function ambienceAudioUrl(aid: string): string {
  return `${BASE}/audiobooks/ambience/${aid}/audio`;
}

export async function startListeningSession(bookId: string): Promise<{ id: string }> {
  return request('/audiobooks/listening-sessions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bookId }),
  });
}

export async function updateListeningSession(sessionId: string, durationMs: number, segmentsPlayed: number): Promise<void> {
  await request('/audiobooks/listening-sessions', {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId, durationMs, segmentsPlayed }),
  });
}

export interface AbStats {
  stats: {
    totalBooks: number; booksStarted: number; booksCompleted: number;
    totalAudioSegments: number; totalListeningMs: number; totalBookmarks: number;
    sessions: number; segmentsPlayed: number; streak: number;
    formats: { format: string; count: number }[];
    dailyBreakdown: { date: string; ms: number }[];
  };
  achievements: { id: string; title: string; description: string; icon: string; unlocked: boolean }[];
}

export async function getAudiobookStats(): Promise<AbStats> {
  return request('/audiobooks/stats');
}

export async function previewPronunciation(text: string): Promise<{ result: string }> {
  return request('/audiobooks/pronunciation/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
}

export async function updatePodcastCast(id: string, host_ids: string[]): Promise<Podcast> {
  return request(`/podcasts/${id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({host_ids}) });
}
