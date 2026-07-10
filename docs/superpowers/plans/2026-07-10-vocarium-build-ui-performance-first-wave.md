# Vocarium Build and UI Performance First-Wave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove multi-gigabyte Docker build-context waste, enable compressed/cacheable UI assets, give long audio jobs safe proxy headroom, and release browser microphone/AudioContext resources on every exit path.

**Architecture:** Use per-context `.dockerignore` files, static Nginx policies for hashed assets, and component-owned cleanup refs in the Clone page. Verify browser lifecycle behavior with Playwright using deterministic media stubs rather than real microphone access.

**Tech Stack:** Docker BuildKit, Nginx Alpine, React 18, TypeScript, Vite, Playwright Chromium

## Global Constraints

- No generated asset work is needed; `imagegen` remains out of scope.
- Do not remove `qwen3-tts/ref_audio/` from the build context because operators may intentionally bake a licensed default sample into the image.
- Do not rebuild Qwen merely to verify `.dockerignore`; use BuildKit's check/metadata path so no GPU image dependency layer is recreated.
- UI behavior and styling remain unchanged except for correct media cleanup/error state.
- Proxy timeouts must exceed the longest configured backend operation.
- Existing `AGENTS.md` and `CLAUDE.md` modifications remain untouched.

---

### Task 1: Exclude runtime artifacts from every Docker build context

**Files:**
- Create: `qwen3-tts/.dockerignore`
- Create: `vocarium-ui/.dockerignore`
- Create: `vocarium-api/.dockerignore`
- Create: `acestep/.dockerignore`
- Create: `mmaudio/.dockerignore`
- Create: `vocarium-api/tests/test_build_and_ui_hardening.py`

**Interfaces:**
- Produces: deterministic, minimal Docker contexts without changing Dockerfile inputs

- [ ] **Step 1: Write failing context-policy tests**

```python
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class DockerContextTest(unittest.TestCase):
    def _patterns(self, directory: str) -> set[str]:
        path = REPO_ROOT / directory / ".dockerignore"
        return {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    def test_qwen_model_cache_is_not_sent_to_docker(self):
        patterns = self._patterns("qwen3-tts")
        self.assertIn("models/", patterns)
        self.assertNotIn("ref_audio/", patterns)

    def test_ui_dependencies_and_build_outputs_are_excluded(self):
        patterns = self._patterns("vocarium-ui")
        self.assertTrue({"node_modules/", "dist/", "test-results/"} <= patterns)

    def test_python_contexts_exclude_bytecode_and_runtime_data(self):
        for directory in ("vocarium-api", "acestep", "mmaudio"):
            patterns = self._patterns(directory)
            self.assertIn("__pycache__/", patterns)
            self.assertIn("*.pyc", patterns)
```

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-build-ui-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_build_and_ui_hardening.DockerContextTest -v
```

Expected: ERROR because the `.dockerignore` files do not exist.

- [ ] **Step 3: Add the exact ignore policies**

`qwen3-tts/.dockerignore`:

```dockerignore
models/
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.venv/
venv/
```

`vocarium-ui/.dockerignore`:

```dockerignore
node_modules/
dist/
.vite/
test-results/
playwright-report/
npm-debug.log*
```

`vocarium-api/.dockerignore`:

```dockerignore
__pycache__/
*.pyc
*.pyo
.pytest_cache/
data/
*.db
*.db-shm
*.db-wal
```

`acestep/.dockerignore` and `mmaudio/.dockerignore`:

```dockerignore
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.venv/
venv/
.cache/
```

- [ ] **Step 4: Run tests and inspect BuildKit context transfer**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-build-ui-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_build_and_ui_hardening.DockerContextTest -v
docker build --check qwen3-tts 2>&1 | tee /tmp/qwen-build-check.log
docker build --check vocarium-ui 2>&1 | tee /tmp/ui-build-check.log
rg -n 'transferring context|Check complete|No warnings' \
  /tmp/qwen-build-check.log /tmp/ui-build-check.log
```

Expected: tests pass and context transfer is measured in KB/low MB, not the
previous 15 GB Qwen directory or 125 MB UI dependency tree. If this Docker
version lacks `--check`, use `docker buildx build --call=check <context>`; do not
run a full Qwen rebuild for this measurement.

- [ ] **Step 5: Commit context policies**

```bash
git add qwen3-tts/.dockerignore vocarium-ui/.dockerignore \
  vocarium-api/.dockerignore acestep/.dockerignore mmaudio/.dockerignore \
  vocarium-api/tests/test_build_and_ui_hardening.py
git commit -m "perf: shrink Docker build contexts"
```

### Task 2: Compress and cache UI assets with safe long-job timeouts

**Files:**
- Modify: `vocarium-ui/nginx.conf:3-31`
- Modify: `vocarium-api/tests/test_build_and_ui_hardening.py`

**Interfaces:**
- Hashed `/assets/` responses receive one-year browser cache lifetime
- Compressible responses negotiate gzip
- API proxy read/send timeouts become 3600 seconds

- [ ] **Step 1: Add failing Nginx policy tests**

Append:

```python
class NginxPerformancePolicyTest(unittest.TestCase):
    def test_assets_are_compressed_and_long_lived(self):
        nginx = (REPO_ROOT / "vocarium-ui" / "nginx.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("gzip on;", nginx)
        self.assertIn("location /assets/", nginx)
        self.assertIn("expires 1y;", nginx)

    def test_proxy_timeout_exceeds_ten_minute_backend_poll(self):
        nginx = (REPO_ROOT / "vocarium-ui" / "nginx.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("proxy_read_timeout 3600s;", nginx)
        self.assertIn("proxy_send_timeout 3600s;", nginx)
```

- [ ] **Step 2: Run and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-build-ui-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_build_and_ui_hardening.NginxPerformancePolicyTest -v
```

Expected: FAIL for gzip, asset expiry, and 3600-second timeouts.

- [ ] **Step 3: Add compression, immutable-path caching, and timeout headroom**

Inside the `server` block before locations, add:

```nginx
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_comp_level 5;
    gzip_types text/css application/javascript application/json image/svg+xml;
```

Add before `location /`:

```nginx
    location /assets/ {
        try_files $uri =404;
        expires 1y;
    }
```

Change in `location /api/`:

```nginx
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
```

- [ ] **Step 4: Run tests, build the UI, and validate Nginx syntax**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-build-ui-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_build_and_ui_hardening.NginxPerformancePolicyTest -v
npm run build --prefix vocarium-ui
docker compose build vocarium-ui
docker compose up -d --no-deps --force-recreate vocarium-ui
docker exec vocarium-ui nginx -t
```

Expected: all commands pass.

- [ ] **Step 5: Measure live compression and cache headers**

```bash
docker exec vocarium-ui sh -c \
  'asset=$(find /usr/share/nginx/html/assets -name "*.js" | head -n 1); wget --header="Accept-Encoding: gzip" -S -O /dev/null "http://127.0.0.1:3000/assets/$(basename "$asset")" 2>&1'
```

Expected headers include `Content-Encoding: gzip`, `Vary: Accept-Encoding`,
`Expires`, and `Cache-Control: max-age=31536000`.

- [ ] **Step 6: Commit Nginx performance policy**

```bash
git add vocarium-ui/nginx.conf \
  vocarium-api/tests/test_build_and_ui_hardening.py
git commit -m "perf: compress and cache UI assets"
```

### Task 3: Release microphone and AudioContext resources on Clone-page exit

**Files:**
- Modify: `vocarium-ui/e2e/runtime.spec.ts`
- Modify: `vocarium-ui/src/pages/ClonePage.tsx:80-130,193-215`

**Interfaces:**
- Produces: component-owned `mediaStreamRef`
- Preserves all visible Clone page controls and generated file behavior

- [ ] **Step 1: Add failing Playwright media-cleanup tests**

Append to `runtime.spec.ts` outside the mocked core-route tests:

```typescript
test('clone page stops microphone tracks when unmounted', async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { __tracksStopped: number }).__tracksStopped = 0;
    const track = {
      stop: () => {
        (window as unknown as { __tracksStopped: number }).__tracksStopped += 1;
      },
    };
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: async () => ({ getTracks: () => [track] }) },
    });
    class FakeMediaRecorder {
      state = 'inactive';
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      start() { this.state = 'recording'; }
      stop() { this.state = 'inactive'; this.onstop?.(); }
    }
    Object.defineProperty(window, 'MediaRecorder', {
      configurable: true,
      value: FakeMediaRecorder,
    });
  });

  await page.goto('/clone');
  await page.getByRole('button', { name: 'Record audio' }).click();
  await expect(page.getByRole('button', { name: 'Stop recording' })).toBeVisible();
  await page.goto('/');
  await expect.poll(() => page.evaluate(
    () => (window as unknown as { __tracksStopped: number }).__tracksStopped,
  )).toBe(1);
});

test('clone page closes AudioContext when decoding fails', async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { __contextsClosed: number }).__contextsClosed = 0;
    class FakeAudioContext {
      state = 'running';
      decodeAudioData() { return Promise.reject(new Error('bad audio')); }
      close() {
        this.state = 'closed';
        (window as unknown as { __contextsClosed: number }).__contextsClosed += 1;
        return Promise.resolve();
      }
    }
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    HTMLMediaElement.prototype.play = () => Promise.resolve();
  });

  await page.goto('/clone');
  await page.locator('input[type="file"]').setInputFiles({
    name: 'broken.wav',
    mimeType: 'audio/wav',
    buffer: Buffer.from('not a wav'),
  });
  await expect.poll(() => page.evaluate(
    () => (window as unknown as { __contextsClosed: number }).__contextsClosed,
  )).toBe(1);
});
```

- [ ] **Step 2: Run the two tests and verify RED**

```bash
npx playwright test e2e/runtime.spec.ts --project=chromium-desktop \
  --grep='stops microphone|closes AudioContext'
```

Expected: both tests fail because current unmount/error paths do not release
the resources.

- [ ] **Step 3: Track and stop the active media stream**

Add beside `mediaRecorderRef`:

```typescript
  const mediaStreamRef = useRef<MediaStream | null>(null);
```

After acquiring the stream:

```typescript
      mediaStreamRef.current = stream;
```

Replace the existing track-stop line at the end of the normal
`recorder.onstop` callback with:

```typescript
        stream.getTracks().forEach((track) => track.stop());
        if (mediaStreamRef.current === stream) mediaStreamRef.current = null;
        mediaRecorderRef.current = null;
```

Add a component cleanup effect:

```typescript
  useEffect(() => {
    return () => {
      const recorder = mediaRecorderRef.current;
      if (recorder && recorder.state !== 'inactive') {
        recorder.ondataavailable = null;
        recorder.onstop = null;
        recorder.stop();
      }
      mediaRecorderRef.current = null;
      mediaStreamRef.current?.getTracks().forEach((track) => track.stop());
      mediaStreamRef.current = null;
    };
  }, []);
```

- [ ] **Step 4: Close stale or failed decode contexts deterministically**

Replace the file-decode effect with:

```typescript
  useEffect(() => {
    if (!file) {
      setAudioBuffer(null);
      setPeaks([]);
      setTotalDuration(0);
      setClipStart(0);
      return;
    }

    let cancelled = false;
    const context = new AudioContext();
    const decode = async () => {
      try {
        const arrayBuffer = await file.arrayBuffer();
        const buffer = await context.decodeAudioData(arrayBuffer);
        if (cancelled) return;
        setAudioBuffer(buffer);
        setTotalDuration(buffer.duration);
        setPeaks(getPeaks(buffer, 200));
        setClipStart(0);
      } catch {
        if (!cancelled) {
          setAudioBuffer(null);
          setPeaks([]);
          setTotalDuration(0);
        }
      } finally {
        if (context.state !== 'closed') await context.close();
      }
    };
    void decode();
    return () => {
      cancelled = true;
      if (context.state !== 'closed') void context.close();
    };
  }, [file]);
```

The `cancelled` flag also prevents an older file's decode result from replacing
the currently selected file.

- [ ] **Step 5: Run E2E, TypeScript, and production build**

```bash
npx playwright test e2e/runtime.spec.ts --project=chromium-desktop \
  --grep='stops microphone|closes AudioContext'
npm run build --prefix vocarium-ui
npm run test:e2e --prefix vocarium-ui
```

Expected: both new tests and the existing desktop/mobile runtime suite pass.

- [ ] **Step 6: Commit media cleanup**

```bash
git add vocarium-ui/src/pages/ClonePage.tsx vocarium-ui/e2e/runtime.spec.ts
git commit -m "fix: release clone page media resources"
```

### Task 4: Run the combined build/UI verification gate

**Files:**
- Verify only; no planned source edits

**Interfaces:**
- Verifies build context, browser tests, static asset headers, API proxy health,
  and clean worktree separation

- [ ] **Step 1: Run host-side checks**

```bash
git diff --check
PYTHONPYCACHEPREFIX=/tmp/vocarium-build-ui-pyc \
  python3 -m unittest discover -s vocarium-api/tests -v
npm audit --prefix vocarium-ui
npm run build --prefix vocarium-ui
npm run test:e2e --prefix vocarium-ui
docker compose config --quiet
```

Expected: all pass and npm reports zero known vulnerabilities.

- [ ] **Step 2: Rebuild/recreate only UI and probe Nginx internally**

```bash
docker compose build vocarium-ui
docker compose up -d --no-deps --force-recreate vocarium-ui
docker exec vocarium-ui nginx -t
docker exec vocarium-ui wget -qO- http://127.0.0.1:3000/ \
  | rg -n '<div id="root"></div>'
```

- [ ] **Step 3: Confirm no new runtime errors**

```bash
docker compose logs --since=15m --no-color vocarium-ui vocarium-api \
  | rg -n -i '(traceback|uncaught|exception|error)' || true
git status --short
```

Expected: no new application error; user-owned `AGENTS.md` and `CLAUDE.md`
changes remain uncommitted and untouched.
