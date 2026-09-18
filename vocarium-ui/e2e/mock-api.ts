import type { Page } from '@playwright/test';
const now = '2026-06-07T00:00:00Z';

export async function mockApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    const json = (body: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });

    if (path === '/api/auth/me') {
      return json({ user: { id: 1, username: 'playwright', display_name: 'Playwright', created_at: now } });
    }
    if (path === '/api/podcasts/tags') return json({tags:[]});
    if (path.startsWith('/api/settings/prefs/')) return json({prefs:{}});
    if (path === '/api/health') {
      return json({
        api: 'ok',
        tts: { status: 'healthy', current_model: '1.7b-custom', voices_loaded: 2 },
        gpu_resources: { enabled: true, available: true },
      });
    }
    if (path === '/api/models') {
      return json({
        models: [
          { id: '1.7b-base', path: 'Qwen/Qwen3-TTS-12Hz-1.7B-Base', type: 'base', params: '1.7B', loaded: false },
          { id: '1.7b-custom', path: 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice', type: 'custom', params: '1.7B', loaded: true },
        ],
      });
    }
    if (path === '/api/models/current') {
      return json({ id: '1.7b-custom', path: 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice', type: 'custom', params: '1.7B', loaded: true });
    }
    if (path === '/api/voices') {
      return json({
        voices: [
          { id: 'voice-vivian', name: 'Vivian', language: 'German', source: 'omnivoice', speaker: 'Vivian', instruct: '', created_at: now, has_audio: false },
          { id: 'voice-ryan', name: 'Ryan', language: 'German', source: 'omnivoice', speaker: 'Ryan', instruct: '', created_at: now, has_audio: false },
        ],
      });
    }
    if (path === '/api/languages') {
      return json({ languages: ['German', 'English', 'Spanish'] });
    }
    if (path === '/api/speakers') {
      return json({
        speakers: [
          { id: 'Vivian', name: 'Vivian', gender: 'female', language_hint: 'German' },
          { id: 'Ryan', name: 'Ryan', gender: 'male', language_hint: 'German' },
        ],
      });
    }
    if (path === '/api/hosts') {
      return json({
        hosts: [
          { id: 'host-vivian', name: 'Vivian', personality: 'Warm host', speaking_style: 'Clear', voice_id: 'voice-vivian', role: 'host', created_at: now, updated_at: now },
        ],
      });
    }
    if (path === '/api/podcasts') {
      return json({
        podcasts: [
          {
            id: 'pod-smoke',
            topic: 'Runtime Check',
            format: 'dialog',
            disfluency_level: 1,
            duration: 'short',
            language: 'de',
            status: 'draft',
            error_message: null,
            hosts: [],
            sources: [],
            script: null,
            audio_path: null,
            audio_duration: 0,
            audio_format: 'mp3',
            total_words: 0,
            created_at: now,
            updated_at: now,
          },
        ],
      });
    }
    if (path === '/api/podcasts/pod-smoke') {
      return json({
        id: 'pod-smoke',
        topic: 'Runtime Check',
        format: 'dialog',
        disfluency_level: 1,
        duration: 'short',
        language: 'de',
        status: 'draft',
        error_message: null,
        hosts: [],
        sources: [],
        script: null,
        audio_path: null,
        audio_duration: 0,
        audio_format: 'mp3',
        total_words: 0,
        created_at: now,
        updated_at: now,
      });
    }
    if (path === '/api/podcasts/pod-smoke/sources') {
      return json({ sources: [] });
    }
    if (path === '/api/llm/providers') {
      return json({ providers: [] });
    }
    if (path === '/api/llm/providers/active') {
      return json({ detail: 'No active LLM provider configured' }, 404);
    }
    if (path === '/api/queue/status') {
      return json({ current: null, queue: [], queue_length: 0 });
    }

    return json({});
  });
}

