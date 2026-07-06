# F5-TTS German Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in F5-TTS German worker and route Vocarium requests to it for German voice-clone benchmarking.

**Architecture:** Add a separate `f5-tts` FastAPI worker with Qwen-compatible endpoints and wire it into `vocarium-api` through explicit engine/model selection. Keep Qwen as the default TTS engine and make F5 opt-in through Docker Compose profile/configuration.

**Tech Stack:** FastAPI, aiohttp, Docker Compose, shared `/app/voices` volume, optional `f5-tts` Python package, PyTorch/CUDA.

## Global Constraints

- Qwen remains the default engine for all existing requests.
- F5 must be opt-in and must not load a model during `/health`.
- No production code is written before a failing test.
- Voice data remains user-scoped in the API database; worker filesystem paths must validate `voice_id`.
- Python syntax checks and targeted tests must pass before completion.

---

### Task 1: Gateway Engine Routing Tests

**Files:**
- Modify: `vocarium-api/tests/test_production_hardening.py`
- Modify: `vocarium-api/main.py`

**Interfaces:**
- Produces: `_select_tts_backend(req_model: str | None, req_engine: str | None) -> tuple[str, str]`
- Consumes: `F5_TTS_URL`

- [ ] **Step 1: Write failing tests**

Add tests that reload `vocarium-api/main.py` with `F5_TTS_URL` configured, assert `engine=f5` and `model=f5-german` select the F5 URL, and assert F5 is rejected when disabled.

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPYCACHEPREFIX=/tmp/vocarium-pycache-check python3 vocarium-api/tests/test_production_hardening.py`

Expected: FAIL because `_select_tts_backend` does not exist.

- [ ] **Step 3: Implement minimal gateway helper**

Add `F5_TTS_URL`, `SUPPORTED_TTS_ENGINES`, `_f5_enabled()`, and `_select_tts_backend()`.

- [ ] **Step 4: Run tests to verify pass**

Run the same test file. Expected: all tests pass.

### Task 2: F5 Worker Skeleton Tests

**Files:**
- Create: `f5-tts/server.py`
- Create: `f5-tts/Dockerfile`
- Modify: `vocarium-api/tests/test_production_hardening.py`

**Interfaces:**
- Produces: `GET /health`, `GET /v1/models`, `GET /v1/voices`, `POST /v1/audio/speech`

- [ ] **Step 1: Write failing worker tests**

Add tests that import `f5-tts/server.py` by path and assert health metadata, model listing, and a dependency-missing 503 for inference when `f5_tts` is unavailable.

- [ ] **Step 2: Run tests to verify failure**

Run production hardening tests. Expected: FAIL because `f5-tts/server.py` is missing.

- [ ] **Step 3: Implement worker skeleton**

Create a FastAPI server with lazy model import, health metadata, safe voice scanning, audio format validation, and clear 503 on missing dependency.

- [ ] **Step 4: Run tests to verify pass**

Run production hardening tests. Expected: all tests pass.

### Task 3: Gateway Proxy Integration

**Files:**
- Modify: `vocarium-api/main.py`
- Modify: `vocarium-api/tests/test_production_hardening.py`

**Interfaces:**
- Consumes: `_select_tts_backend()`
- Produces: F5-aware `/v1/audio/speech`, `/api/models`, `/api/models/current`, clone registration forwarding.

- [ ] **Step 1: Write failing proxy tests**

Add tests verifying OpenAI request payloads route to F5 URL when `engine=f5` or `model=f5-german`, and clone registration sends multipart registration to all configured TTS services including F5.

- [ ] **Step 2: Run tests to verify failure**

Run production hardening tests. Expected: FAIL because existing proxy always uses Qwen.

- [ ] **Step 3: Implement minimal proxy routing**

Use `_select_tts_backend()` in OpenAI speech proxy and include F5 in registration URL list only when enabled.

- [ ] **Step 4: Run tests to verify pass**

Run production hardening tests. Expected: all tests pass.

### Task 4: Docker and Benchmark Wiring

**Files:**
- Modify: `docker-compose.yml`
- Modify: `scripts/benchmark-audio-generation.py`

**Interfaces:**
- Produces: `f5-tts` profile service and benchmark `--kind tts --engine qwen|f5|both`

- [ ] **Step 1: Write failing config/script tests**

Add tests that assert Compose contains `f5-tts` profile service and benchmark script documents F5/Qwen TTS comparison flags.

- [ ] **Step 2: Run tests to verify failure**

Run production hardening tests. Expected: FAIL because Compose/script do not contain F5 wiring.

- [ ] **Step 3: Implement Compose and benchmark updates**

Add profile-gated service and extend benchmark script for TTS comparison without forcing generation by default.

- [ ] **Step 4: Run tests to verify pass**

Run production hardening tests. Expected: all tests pass.

### Task 5: Verification and Publish

**Files:**
- All modified files

- [ ] **Step 1: Syntax check**

Run: `PYTHONPYCACHEPREFIX=/tmp/vocarium-pycache-check python3 -m py_compile vocarium-api/main.py f5-tts/server.py scripts/benchmark-audio-generation.py`

- [ ] **Step 2: Test suite**

Run: `PYTHONPYCACHEPREFIX=/tmp/vocarium-pycache-check python3 vocarium-api/tests/test_production_hardening.py`

- [ ] **Step 3: Docker config check**

Run: `docker compose config --profiles f5`

- [ ] **Step 4: Optional build/preflight**

Run F5 build and GPU benchmark only if dependency install and GPU preflight are acceptable. Report if skipped due first-load/model-download cost.

- [ ] **Step 5: Commit and push**

Stage only files for this feature, commit, and push the branch.
