import { expect, test } from '@playwright/test';

import { mockApi } from './mock-api';

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test('loads core routes without a blank screen', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));

  const routes = [
    { path: '/', heading: 'Speech' },
    { path: '/voices', heading: 'Stimmen' },
    { path: '/podcast', heading: 'Podcast Studio' },
    { path: '/settings', heading: 'Einstellungen' },
  ];

  for (const item of routes) {
    await page.goto(item.path);
    await expect(page.getByRole('heading', { name: item.heading, exact: true })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: 'Speech' })).toBeVisible();
  await page.goto('/podcast');
  await expect(page.getByRole('heading', { name: 'Podcast Studio' })).toBeVisible();

  expect(scriptRequests.some((url) => /PodcastPage|podcast/i.test(url))).toBeTruthy();
  expect(scriptRequests.length).toBeGreaterThanOrEqual(2);
});

test('voice cloning accepts an audio reference', async ({ page }) => {
  await page.goto('/clone');

  await expect(page.getByRole('heading', { name: 'Stimme klonen' })).toBeVisible();
  await expect(page.locator('input[type="file"]').first()).toHaveAttribute('accept', /audio/);
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
  await page.getByRole('button', { name: 'Link' }).click();
  await page.getByPlaceholder('https://www.youtube.com/watch?v=…').fill('https://www.youtube.com/watch?v=test');
  await page.getByRole('button', { name: 'Transkribieren', exact: true }).click();

  await expect(page.getByRole('button',{name:'0:00 Guten Morgen.'})).toBeVisible();
  await expect(page.getByRole('button',{name:'0:02 Willkommen bei Vocarium.'})).toBeVisible();
  await expect(page.getByText('Willkommen bei Vocarium.')).toBeVisible();
});


test('reduced motion keeps the shared Aurora static in all studios', async ({ page }) => {
  await page.emulateMedia({reducedMotion:'reduce'});
  const scripts: string[] = [];
  page.on('request', request => { if (request.resourceType() === 'script') scripts.push(request.url()); });
  for (const route of ['/podcast', '/audiobooks', '/hoerspiele']) {
    await page.goto(route);
    await expect(page.locator('#root')).not.toBeEmpty();
  }
  expect(scripts.some(url => /ThreeAurora/.test(url))).toBeFalsy();
});
