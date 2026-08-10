import { expect, test, type Page } from '@playwright/test';

const now = '2026-06-07T00:00:00Z';

async function mockApi(page: Page) {
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
          { id: 'voice-clone-de', name: 'German Clone', language: 'German', source: 'clone', ref_text: 'Guten Morgen.', created_at: now, has_audio: true },
          { id: 'voice-vivian', name: 'Vivian', language: 'German', source: 'custom', speaker: 'Vivian', instruct: '', created_at: now, has_audio: false },
          { id: 'voice-ryan', name: 'Ryan', language: 'German', source: 'custom', speaker: 'Ryan', instruct: '', created_at: now, has_audio: false },
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
    if (path === '/api/music/health') {
      return json({ status: 'healthy', backend_running: false });
    }
    if (path === '/api/sfx/health') {
      return json({ status: 'healthy', model_loaded: false });
    }
    if (path === '/api/benchmark/results') {
      return json({ results: [] });
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

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test('loads core routes without a blank screen', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));

  const routes = [
    { path: '/', heading: 'Text to Speech' },
    { path: '/voices', heading: 'Voice Library' },
    { path: '/music', heading: 'Music Studio' },
    { path: '/podcast', heading: 'Podcast' },
    { path: '/settings', heading: 'Settings' },
  ];

  for (const item of routes) {
    await page.goto(item.path);
    await expect(page.getByRole('heading', { name: item.heading })).toBeVisible();
    await expect(page.locator('#root')).not.toBeEmpty();
  }

  expect(errors).toEqual([]);
});

test('lazy route chunks are requested after navigation', async ({ page }) => {
  const scriptRequests: string[] = [];
  page.on('request', (request) => {
    const url = request.url();
    if (request.resourceType() === 'script' || /\/src\/pages\//.test(url) || /\/assets\/.+\.js/.test(url)) {
      scriptRequests.push(url);
    }
  });

  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Text to Speech' })).toBeVisible();
  await page.goto('/podcast');
  await expect(page.getByRole('heading', { name: 'Podcast' })).toBeVisible();

  expect(scriptRequests.some((url) => /PodcastPage|podcast/i.test(url))).toBeTruthy();
  expect(scriptRequests.length).toBeGreaterThanOrEqual(2);
});

test('voice cloning defaults to German', async ({ page }) => {
  await page.goto('/clone');

  await expect(page.getByRole('heading', { name: 'Voice Cloning' })).toBeVisible();
  await expect(page.getByRole('combobox')).toHaveValue('German');
});

test('speech page compares Qwen and dots with the same clone voice', async ({ page }) => {
  let requestBody: Record<string, unknown> | null = null;
  await page.route('**/api/generate', async (route) => {
    requestBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'audio/wav',
      headers: { 'X-TTS-Engine': 'dots', 'X-Voice': 'voice-clone-de' },
      body: Buffer.from('RIFF....WAVE'),
    });
  });

  await page.goto('/');
  await expect(page.getByRole('button', { name: /Qwen3-TTS/ })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: /dots\.tts/ }).click();
  await expect(page.getByRole('button', { name: /dots\.tts/ })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('combobox').first()).toHaveValue('voice-clone-de');
  await expect(page.getByRole('option', { name: /Vivian/ })).toHaveCount(0);
  await page.getByPlaceholder('Write or paste what you want your voice to say…').fill('Guten Morgen aus Vocarium.');
  await page.getByRole('button', { name: 'Generate' }).click();
  await expect.poll(() => requestBody).not.toBeNull();
  expect(requestBody).toMatchObject({
    engine: 'dots',
    voice_id: 'voice-clone-de',
  });
});

test('transcription renders model-backed timestamps', async ({ page }) => {
  await page.route('**/api/transcribe', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      text: 'Guten Morgen. Willkommen bei Vocarium.',
      language: 'German',
      words: [],
      segments: [
        { start: 0, end: 2.2, text: 'Guten Morgen.' },
        { start: 2.5, end: 5.2, text: 'Willkommen bei Vocarium.' },
      ],
    }),
  }));
  await page.goto('/transcribe');
  await page.getByRole('button', { name: 'Media URL' }).click();
  await page.getByPlaceholder('https://www.youtube.com/watch?v=...').fill('https://www.youtube.com/watch?v=test');
  await page.getByRole('button', { name: 'Transcribe', exact: true }).click();

  await expect(page.getByText('0:00 – 0:02')).toBeVisible();
  await expect(page.getByText('0:02 – 0:05')).toBeVisible();
  await expect(page.getByText('Willkommen bei Vocarium.')).toBeVisible();
});
