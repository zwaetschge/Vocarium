# Audio Generation API Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add practical SFX/music generation options, first-load visibility, and benchmark tooling for Vocarium's German-focused audio stack.

**Architecture:** Keep ACE-Step as the only music engine and MMAudio as the only SFX engine for now, but make engine choice explicit in the API so future engines can be added without reshaping clients. Add non-destructive request options and clearer health/status metadata instead of auto-triggering multi-gigabyte model downloads during health checks.

**Tech Stack:** FastAPI, Pydantic, aiohttp, Docker Compose, ACE-Step, MMAudio, Python unittest source-hardening tests.

## Global Constraints

- Use TDD: write failing tests before production code.
- Do not re-add Chatterbox or NeuTTS after prior removal decisions.
- Do not trigger first-run model downloads in automated tests.
- Keep SFX/music engines GPU-aware and transparent about first-load model weights.
- Preserve existing default API behavior for clients that do not send new fields.

---

### Task 1: Production-Hardening Tests

**Files:**
- Modify: `vocarium-api/tests/test_production_hardening.py`

**Interfaces:**
- Produces tests that require `engine`, negative prompt helpers, first-load health metadata, and `scripts/benchmark-audio-generation.py`.

- [ ] **Step 1: Write failing tests**

Add tests asserting:
- `/api/music/generate` request model exposes `engine`, `negative_prompt`, `instrumental`, `loopable`, `normalize_lufs`, and `fade_ms`.
- `/api/sfx/generate` handles explicit `engine`, `no_speech`, `no_music`, `normalize_lufs`, and `fade_ms`.
- ACE-Step and MMAudio health responses expose first-load/download metadata.
- A benchmark script exists with `--kind`, `--preload`, `--json`, and model weight hints.

- [ ] **Step 2: Run tests to verify RED**

Run: `PYTHONPYCACHEPREFIX=/tmp/vocarium-pycache-check python3 vocarium-api/tests/test_production_hardening.py`

Expected: FAIL because new fields and script are not implemented.

### Task 2: API Request Options

**Files:**
- Modify: `vocarium-api/main.py`

**Interfaces:**
- Consumes tests from Task 1.
- Produces explicit engine validation and option parsing for music/SFX routes.

- [ ] **Step 1: Implement minimal API changes**

Add supported engine constants, validation helpers, and negative-prompt merging. Preserve current defaults: `engine="acestep"` for music and `engine="mmaudio"` for SFX.

- [ ] **Step 2: Run tests to verify GREEN for API assertions**

Run: `PYTHONPYCACHEPREFIX=/tmp/vocarium-pycache-check python3 vocarium-api/tests/test_production_hardening.py`

Expected: API-related tests pass or move to the next missing production file.

### Task 3: First-Load Health Metadata

**Files:**
- Modify: `acestep/proxy.py`
- Modify: `mmaudio/server.py`

**Interfaces:**
- Produces health JSON fields used by WebUI, CLI, and benchmark tooling: loading/starting state, last error, timeout, and model weight hints.

- [ ] **Step 1: Add metadata without changing health side effects**

Health endpoints must report whether the backend/model is already running or loading, but must not start downloads.

- [ ] **Step 2: Run tests and syntax checks**

Run: `python3 -m py_compile acestep/proxy.py mmaudio/server.py vocarium-api/main.py`

Expected: PASS.

### Task 4: Benchmark Tool

**Files:**
- Create: `scripts/benchmark-audio-generation.py`

**Interfaces:**
- Produces CLI flags `--kind`, `--preload`, `--json`, `--api-url`, `--duration`, and `--force-generate`.

- [ ] **Step 1: Add script**

The script should read health first, print first-load weight hints, and only start generation when explicitly requested with `--force-generate`.

- [ ] **Step 2: Verify script**

Run: `python3 -m py_compile scripts/benchmark-audio-generation.py`

Expected: PASS.

### Task 5: Container Verification

**Files:**
- No code changes.

**Interfaces:**
- Verifies volume-mounted Python changes are active in containers.

- [ ] **Step 1: Restart services**

Run: `docker compose restart vocarium-api mmaudio acestep`

- [ ] **Step 2: Probe health endpoints**

Run:
```bash
curl -s http://localhost:8280/api/music/health
curl -s http://localhost:8280/api/sfx/health
```

Expected: JSON includes first-load metadata and does not trigger generation.
