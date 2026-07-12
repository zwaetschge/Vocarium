# Vocarium API Security First-Wave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove confirmed API information leaks and global-operation authorization gaps, bound metrics memory growth, make user creation race-safe, and record the complete first-pass audit ledger.

**Architecture:** Keep request authentication in `main.py`, but move public-health shaping and admin-name parsing into dependency-free helpers that can be tested without importing FastAPI. Preserve the existing SQLite schema and public endpoint paths; change only returned health detail and authorization for global operations.

**Tech Stack:** Python 3.11, FastAPI, SQLite WAL, `unittest`, Docker Compose

## Global Constraints

- Existing uncommitted edits to `AGENTS.md` and `CLAUDE.md` are user-owned and must remain untouched.
- GPU 1 remains protected; this plan does not require any GPU generation.
- Every tenant-owned query must remain scoped by `user_id`.
- Every behavioral fix follows red-green-refactor and receives a regression test.
- Do not expose secrets, private audio, usernames, voice identifiers, PIDs, or raw process commands in test output or commits.
- Restart or recreate only `vocarium-api` when runtime verification requires it.
- Commit only files explicitly listed by the active task.

---

### Task 1: Create the versioned audit ledger

**Files:**
- Create: `docs/superpowers/audits/2026-07-10-vocarium-first-pass.md`

**Interfaces:**
- Consumes: the approved design in `docs/superpowers/specs/2026-07-10-vocarium-staged-audit-optimization-design.md`
- Produces: stable finding IDs referenced by this and the other first-wave plans

- [ ] **Step 1: Write the initial evidence ledger**

Create the file with this content:

```markdown
# Vocarium First-Pass Audit Ledger

**Date:** 2026-07-10
**Baseline:** 57 Python hardening tests pass; UI production build passes;
Python syntax compilation passes; npm audit reports zero known findings.

## Confirmed findings

| ID | Priority | Component | Root cause | Planned wave |
|---|---|---|---|---|
| API-SEC-001 | P1 | `GET /api/health` | The unauthenticated response forwards worker voice IDs/cache entries and host GPU process details. | API security |
| API-AUTHZ-001 | P1 | artifact cleanup | Every authenticated tenant can invoke a global destructive cleanup, including `max_age_hours=0`. | API security |
| API-AUTHZ-002 | P1 | model switch | Every authenticated tenant can switch the process-global TTS model. | API security |
| API-METRICS-001 | P2 | request metrics | Unmatched request paths become unbounded Prometheus label values and grow process memory. | API security |
| DB-RACE-001 | P1 | user creation | Two API replicas can both observe a missing user and race on the unique username insert. | API security |
| LLM-DATA-001 | P1 | LLM provider lookup | A database exception is silently treated as “no provider” and can route private podcast text to the global fallback LLM. | Podcast reliability |
| TTS-LIFE-001 | P1 | Qwen worker | Model load/unload executes synchronously inside async endpoints and blocks health and request handling. | Worker lifecycle |
| TTS-LIFE-002 | P1 | Qwen worker | Client cancellation decrements `active_requests` although executor inference continues, permitting unload/model switch during CUDA work. | Worker lifecycle |
| TTS-CONTRACT-001 | P2 | Qwen worker | Response headers read mutable `current_model_id` after inference, so a later switch can report the wrong model. | Worker lifecycle |
| TTS-META-001 | P2 | Qwen worker/API startup | One malformed voice metadata JSON file can abort voice indexing or listing. | Follow-up |
| ASR-LIFE-001 | P1 | ASR proxy | Cold start runs synchronously on the event loop for up to 240 seconds. | Worker lifecycle |
| ASR-LIFE-002 | P1 | ASR proxy | Idle unload uses request-start time and has no active-request count, so a long transcription can be killed in flight. | Worker lifecycle |
| ASR-LIFE-003 | P1 | ASR proxy | Startup timeout leaves the child process referenced and later requests may proxy before readiness. | Worker lifecycle |
| ACE-LIFE-001 | P1 | ACE-Step proxy | Idle/manual unload can terminate an active long `query_result` request. | Worker lifecycle |
| ACE-PERF-001 | P2 | ACE-Step proxy | A new `aiohttp.ClientSession` is created for each poll/proxy request. | Worker lifecycle |
| MMA-LIFE-001 | P1 | MMAudio worker | The generation lock is released before the CUDA tensor is copied to CPU, allowing concurrent unload/cache cleanup. | Worker lifecycle |
| MMA-CONTRACT-001 | P2 | MMAudio worker | `GenerateRequest.num_steps` is validated by the gateway but never applied to `FlowMatching`. | Worker lifecycle |
| POD-AUDIO-001 | P1 | Podcast assembler | Required speech failures are caught and discarded; a partial podcast can still be marked `ready`. | Podcast reliability |
| POD-STREAM-001 | P2 | Podcast range endpoint | A requested byte range is read fully into RAM before one yield, so it is not streaming. | Podcast reliability |
| POD-FILES-001 | P2 | Podcast deletion | Uploaded source files are not removed when the parent podcast is deleted. | Follow-up |
| POD-TASK-001 | P2 | Source processing | Fire-and-forget source tasks are not tracked or recovered after restart, leaving `pending` rows indefinitely. | Follow-up |
| LLM-RETRY-001 | P2 | LLM/embedding clients | Non-retriable 4xx responses are retried three times, tripling avoidable latency and traffic. | Follow-up |
| UI-MEDIA-001 | P1 | Clone page | Recording tracks are not stopped when the component unmounts during capture. | Build/UI follow-up |
| UI-MEDIA-002 | P2 | Clone page | Failed/stale decode paths leak `AudioContext` objects and may apply an older file result. | Build/UI follow-up |
| UI-AUDIO-001 | P2 | audio hook | Rejected `HTMLMediaElement.play()` promises are unhandled while UI state remains “playing”. | Build/UI follow-up |
| OPS-CONTEXT-001 | P1 | Docker builds | No `.dockerignore` exists; the Qwen build context includes 15 GB of model weights and the UI context includes 125 MB of `node_modules`. | Build performance |
| OPS-WEB-001 | P2 | Nginx | Hashed assets have no long-lived cache policy and gzip is disabled. | Build performance |
| OPS-TIMEOUT-001 | P1 | Nginx/API | Proxy timeout is 600 seconds while music polling alone may consume 600 seconds after cold-start work. | Build performance |
| OPS-SUPPLY-001 | P1 | images/dependencies | `latest`, unpinned pip packages, and unpinned shallow Git clones make rebuilds non-reproducible. | Follow-up |
| ARCH-QUEUE-001 | P1 | dual API replicas | Each replica owns an independent in-memory GPU queue; the shared DB is observational, not a distributed execution lock. | Architecture decision |
| ARCH-QUEUE-002 | P2 | dual GPU scheduling | One queue worker serializes jobs even when their configured GPUs differ. | Architecture decision |

## Investigation notes

- `SEC-TRUST-001`: identity headers are forwarded by Nginx and are safe only
  if the external authentication proxy overwrites untrusted client values.
- `SSRF-DNS-001`: outbound hosts are validated before use, but DNS rebinding
  remains possible because the later client/Docling resolution is not pinned.
- `API-PCM-001`: `response_format=pcm` currently returns a WAV container rather
  than raw PCM; changing this requires a compatibility decision.
- `OPS-CONTAINER-001`: all containers run as root with writable root filesystems
  and no Compose health checks, PID limits, or memory limits.

## Evidence rules

Each status change in this ledger must link to a failing test, reproduction
command, source trace, benchmark, or runtime log. Concerns without that proof
remain under Investigation notes.
```

- [ ] **Step 2: Check the ledger for accidental sensitive values and placeholders**

Run:

```bash
rg -n -i '(api[_-]?key|bearer |token=|password|secret=|T[B]D|T[O]DO)' \
  docs/superpowers/audits/2026-07-10-vocarium-first-pass.md
```

Expected: no output.

- [ ] **Step 3: Commit the ledger**

```bash
git add docs/superpowers/audits/2026-07-10-vocarium-first-pass.md
git commit -m "docs: record Vocarium first-pass audit"
```

### Task 2: Redact the public health response

**Files:**
- Create: `vocarium-api/health_public.py`
- Create: `vocarium-api/tests/test_api_security.py`
- Modify: `vocarium-api/main.py:30-57,1788-1795`
- Modify: `docker-compose.yml:214-229,280-295`

**Interfaces:**
- Consumes: raw dictionaries from Qwen `/health` and `get_resource_status()`
- Produces: `build_public_health(tts_health: dict, gpu_resources: dict) -> dict`

- [ ] **Step 1: Write the failing health-redaction test**

```python
# vocarium-api/tests/test_api_security.py
import json
import sys
import unittest
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


class PublicHealthTest(unittest.TestCase):
    def test_public_health_redacts_voice_ids_and_gpu_processes(self):
        from health_public import build_public_health

        payload = build_public_health(
            {
                "status": "ok",
                "current_model": "1.7b-base",
                "model_loaded": True,
                "active_requests": 1,
                "voices_loaded": ["tenant-private-voice"],
                "voice_clone_prompt_cache": [
                    {"model": "1.7b-base", "voice": "tenant-private-voice"}
                ],
            },
            {
                "enabled": True,
                "available": True,
                "service_gpus": {"tts": "0"},
                "decisions": {
                    "tts": {
                        "service_type": "tts",
                        "target_gpu": "0",
                        "estimated_need_mib": 8500,
                        "allowed": True,
                        "selected": {
                            "reasons": [],
                            "processes": [
                                {"pid": 123, "container": "private-container"}
                            ],
                        },
                    }
                },
            },
        )

        serialized = json.dumps(payload)
        self.assertNotIn("tenant-private-voice", serialized)
        self.assertNotIn("private-container", serialized)
        self.assertNotIn('"pid"', serialized)
        self.assertEqual(payload["tts"]["voices_loaded"], 1)
        self.assertTrue(payload["gpu_resources"]["decisions"]["tts"]["allowed"])
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_api_security.PublicHealthTest -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'health_public'`.

- [ ] **Step 3: Implement the dependency-free public-health builder**

```python
# vocarium-api/health_public.py
"""Redacted public health payloads for the unauthenticated readiness route."""

from __future__ import annotations

from typing import Any


def _voice_count(value: object) -> int:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    if isinstance(value, int):
        return max(0, value)
    return 0


def _decision_summary(decision: dict[str, Any]) -> dict[str, Any]:
    selected = decision.get("selected") or {}
    reasons = decision.get("reasons") or selected.get("reasons") or []
    return {
        "service_type": decision.get("service_type"),
        "target_gpu": decision.get("target_gpu"),
        "estimated_need_mib": decision.get("estimated_need_mib"),
        "allowed": bool(decision.get("allowed", False)),
        "reasons": [str(reason) for reason in reasons],
    }


def build_public_health(
    tts_health: dict[str, Any],
    gpu_resources: dict[str, Any],
) -> dict[str, Any]:
    tts = {
        "status": tts_health.get("status", "unreachable"),
        "current_model": tts_health.get("current_model"),
        "model_loaded": bool(tts_health.get("model_loaded", False)),
        "active_requests": int(tts_health.get("active_requests") or 0),
        "voices_loaded": _voice_count(tts_health.get("voices_loaded")),
    }
    gpu: dict[str, Any] = {
        "enabled": bool(gpu_resources.get("enabled", False)),
        "available": bool(gpu_resources.get("available", False)),
        "service_gpus": dict(gpu_resources.get("service_gpus") or {}),
    }
    decisions = gpu_resources.get("decisions") or {}
    if isinstance(decisions, dict):
        gpu["decisions"] = {
            str(name): _decision_summary(value)
            for name, value in decisions.items()
            if isinstance(value, dict)
        }
    if gpu_resources.get("error"):
        gpu["error"] = "resource monitor unavailable"
    return {"api": "ok", "tts": tts, "gpu_resources": gpu}
```

- [ ] **Step 4: Wire the builder into the health endpoint and Compose mounts**

Add to `vocarium-api/main.py` imports:

```python
from health_public import build_public_health
```

Replace the return in `health()` with:

```python
    return build_public_health(tts_health, gpu_resources)
```

Add this read-only mount to both API replicas beside `metrics.py`:

```yaml
      - ./vocarium-api/health_public.py:/app/health_public.py:ro
```

- [ ] **Step 5: Run the focused and hardening tests**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_api_security.PublicHealthTest -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_production_hardening
docker compose config --quiet
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit the health fix**

```bash
git add vocarium-api/health_public.py vocarium-api/tests/test_api_security.py \
  vocarium-api/main.py docker-compose.yml
git commit -m "fix: redact public health diagnostics"
```

### Task 3: Restrict process-global admin operations

**Files:**
- Create: `vocarium-api/access_control.py`
- Modify: `vocarium-api/tests/test_api_security.py`
- Modify: `vocarium-api/main.py:98-105,216-242,634-652,765-773`
- Modify: `docker-compose.yml:178-211,244-277,214-229,280-295`
- Modify: `docker-compose.prod.yml:25-34`
- Modify: `.env.example`

**Interfaces:**
- Produces: `is_admin_username(username: str, configured: str) -> bool`
- Produces: `_require_admin(request: Request) -> dict` in `main.py`

- [ ] **Step 1: Add failing allowlist tests**

Append to `vocarium-api/tests/test_api_security.py`:

```python
class AdminAllowlistTest(unittest.TestCase):
    def test_admin_names_are_exact_casefolded_matches(self):
        from access_control import is_admin_username

        self.assertTrue(is_admin_username("API", "api, ops@example.test"))
        self.assertTrue(
            is_admin_username("ops@example.test", "api, ops@example.test")
        )
        self.assertFalse(is_admin_username("tenant", "api, ops@example.test"))
        self.assertFalse(is_admin_username("", "api"))
        self.assertFalse(is_admin_username("api", ""))
```

- [ ] **Step 2: Run the test and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_api_security.AdminAllowlistTest -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'access_control'`.

- [ ] **Step 3: Implement exact admin-name matching**

```python
# vocarium-api/access_control.py
"""Small authorization helpers without FastAPI or database dependencies."""

from __future__ import annotations


def is_admin_username(username: str, configured: str) -> bool:
    allowed = {
        item.strip().casefold()
        for item in (configured or "").split(",")
        if item.strip()
    }
    value = (username or "").strip().casefold()
    return bool(value) and value in allowed
```

- [ ] **Step 4: Enforce the allowlist in `main.py`**

Add imports and configuration:

```python
from access_control import is_admin_username

VOCARIUM_ADMIN_USERS = os.environ.get("VOCARIUM_ADMIN_USERS", "")
```

Add immediately after `get_current_user`:

```python
def _require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if not is_admin_username(user["username"], VOCARIUM_ADMIN_USERS):
        raise HTTPException(403, "Administrator access required")
    return user
```

Replace `get_current_user(request)` with `_require_admin(request)` inside the
`switch_model` and `admin_artifact_cleanup` route functions.

- [ ] **Step 5: Configure safe local and production defaults**

Add to both API services in `docker-compose.yml`:

```yaml
      VOCARIUM_ADMIN_USERS: ${VOCARIUM_ADMIN_USERS:-api}
```

Add the helper mount to both API services:

```yaml
      - ./vocarium-api/access_control.py:/app/access_control.py:ro
```

Add to both API services in `docker-compose.prod.yml`:

```yaml
      VOCARIUM_ADMIN_USERS: ${VOCARIUM_ADMIN_USERS:-}
```

Document in `.env.example`:

```dotenv
# Comma-separated authenticated usernames allowed to run process-global admin
# actions such as model switching and destructive artifact cleanup.
VOCARIUM_ADMIN_USERS=api
```

- [ ] **Step 6: Run tests and validate the merged Compose configuration**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_api_security -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_production_hardening
docker compose config --quiet
CORS_ORIGINS=https://vocarium.example.test \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml config \
  | rg -n 'VOCARIUM_ADMIN_USERS|ALLOW_ANONYMOUS|CORS_ORIGINS'
```

Expected: tests and Compose validation pass; the production merge shows an
empty admin allowlist unless explicitly configured.

- [ ] **Step 7: Commit the authorization boundary**

```bash
git add vocarium-api/access_control.py vocarium-api/tests/test_api_security.py \
  vocarium-api/main.py docker-compose.yml docker-compose.prod.yml .env.example
git commit -m "fix: restrict global API operations"
```

### Task 4: Bound unmatched-route metric labels

**Files:**
- Modify: `vocarium-api/metrics.py:18-31`
- Modify: `vocarium-api/main.py:178-200`
- Modify: `vocarium-api/tests/test_api_security.py`

**Interfaces:**
- Produces: `route_path_label(route: object | None) -> str`

- [ ] **Step 1: Add the failing cardinality test**

Append:

```python
class MetricsCardinalityTest(unittest.TestCase):
    def test_unmatched_routes_share_one_bounded_label(self):
        from metrics import route_path_label

        class Matched:
            path = "/api/voices/{voice_id}"

        self.assertEqual(route_path_label(Matched()), "/api/voices/{voice_id}")
        self.assertEqual(route_path_label(None), "__unmatched__")
        self.assertEqual(route_path_label(object()), "__unmatched__")
```

- [ ] **Step 2: Run the test and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_api_security.MetricsCardinalityTest -v
```

Expected: FAIL because `route_path_label` is absent.

- [ ] **Step 3: Implement and use the bounded label**

Add to `metrics.py`:

```python
def route_path_label(route: object | None) -> str:
    value = getattr(route, "path", None)
    return str(value) if value else "__unmatched__"
```

Import it in `main.py`:

```python
from metrics import inc, observe, render_prometheus, route_path_label
```

Replace the fallback that uses `request.url.path`:

```python
        route_path = route_path_label(request.scope.get("route"))
```

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_api_security -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_production_hardening
git add vocarium-api/metrics.py vocarium-api/main.py \
  vocarium-api/tests/test_api_security.py
git commit -m "fix: bound unmatched route metrics"
```

### Task 5: Make first-login user creation race-safe

**Files:**
- Modify: `vocarium-api/database.py:699-712`
- Modify: `vocarium-api/tests/test_api_security.py`

**Interfaces:**
- Preserves: `get_or_create_user(username: str) -> dict`
- Changes: missing-user insertion becomes idempotent across API processes

- [ ] **Step 1: Add a deterministic uniqueness-race test**

Append:

```python
class UserCreationRaceTest(unittest.TestCase):
    def test_unique_insert_loser_reads_the_winning_user(self):
        import database

        class Cursor:
            def __init__(self, row=None, rowcount=-1):
                self._row = row
                self.rowcount = rowcount

            def fetchone(self):
                return self._row

        class RaceConnection:
            def __init__(self):
                self.selects = 0

            def execute(self, sql, params=()):
                if sql.startswith("SELECT id, username"):
                    self.selects += 1
                    if self.selects == 1:
                        return Cursor(None)
                    return Cursor((7, "race-user", "race-user", "2026-07-10"))
                if sql.startswith("INSERT OR IGNORE INTO users"):
                    return Cursor(rowcount=0)
                if sql.startswith("INSERT INTO users"):
                    raise database.sqlite3.IntegrityError("UNIQUE constraint failed")
                raise AssertionError(sql)

            def commit(self):
                return None

        original_get_db = database.get_db
        original_voices = database.seed_prebuilt_custom_voices
        original_hosts = database.seed_prebuilt_hosts
        try:
            database.get_db = lambda: RaceConnection()
            database.seed_prebuilt_custom_voices = lambda user_id: 0
            database.seed_prebuilt_hosts = lambda user_id: 0
            user = database.get_or_create_user("race-user")
        finally:
            database.get_db = original_get_db
            database.seed_prebuilt_custom_voices = original_voices
            database.seed_prebuilt_hosts = original_hosts

        self.assertEqual(user["id"], 7)
```

- [ ] **Step 2: Run the test and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_api_security.UserCreationRaceTest -v
```

Expected: FAIL with the simulated unique-constraint `IntegrityError`.

- [ ] **Step 3: Replace check-then-insert with an idempotent insert**

Replace the missing-user branch in `get_or_create_user` with:

```python
    cursor = db.execute(
        "INSERT OR IGNORE INTO users (username, display_name) VALUES (?, ?)",
        (username, username),
    )
    created = cursor.rowcount == 1
    db.commit()
    row = db.execute(
        "SELECT id, username, display_name, created_at FROM users WHERE username=?",
        (username,),
    ).fetchone()
    if row is None:
        raise RuntimeError("User insert completed without a readable row")
    if created:
        seed_prebuilt_custom_voices(row[0])
        seed_prebuilt_hosts(row[0])
    return {
        "id": row[0],
        "username": row[1],
        "display_name": row[2] or row[1],
        "created_at": row[3],
    }
```

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_api_security -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest vocarium-api.tests.test_production_hardening
git add vocarium-api/database.py vocarium-api/tests/test_api_security.py
git commit -m "fix: make user creation race safe"
```

### Task 6: Recreate and verify the API security wave

**Files:**
- Verify only; no planned source edits

**Interfaces:**
- Verifies: public health response, admin authorization, loaded source mounts,
  tests, Compose, and preservation of user-owned changes

- [ ] **Step 1: Run fresh static verification**

```bash
git diff --check
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m compileall -q vocarium-api
PYTHONPYCACHEPREFIX=/tmp/vocarium-api-security-pyc \
  python3 -m unittest discover -s vocarium-api/tests -v
docker compose config --quiet
```

Expected: all commands exit 0.

- [ ] **Step 2: Recreate only the API so the new mounts exist**

```bash
docker compose up -d --no-deps --force-recreate vocarium-api
```

Expected: only `vocarium-api` is recreated.

- [ ] **Step 3: Confirm the running source and redaction behavior**

```bash
docker exec vocarium-api python -c \
  "import inspect, health_public, access_control; print(inspect.getsourcefile(health_public)); print(inspect.getsourcefile(access_control))"
docker exec vocarium-api python -c \
  "import json, urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8280/api/health')); s=json.dumps(d); assert 'processes' not in s and 'voice_clone_prompt_cache' not in s; print(d['api'], d['tts']['voices_loaded'])"
```

Expected: both helper paths are under `/app`, the assertion passes, and no
voice identifiers or process details are printed.

- [ ] **Step 4: Confirm non-admin global operations fail closed**

Use an already-existing non-admin test identity without printing its value:

```bash
docker exec vocarium-api python -c \
  "import urllib.request, urllib.error; req=urllib.request.Request('http://127.0.0.1:8280/api/admin/artifacts/cleanup?dry_run=true', method='POST', headers={'Remote-User':'non-admin-security-smoke'});\
try: urllib.request.urlopen(req); raise SystemExit('unexpected allow')\
except urllib.error.HTTPError as e: print(e.code); assert e.code == 403"
```

Expected: `403`. This creates only the normal auto-provisioned smoke identity;
it performs no cleanup and generates no GPU work.

- [ ] **Step 5: Inspect API logs and final worktree**

```bash
docker compose logs --since=10m --no-color vocarium-api \
  | rg -n -i '(traceback|exception|error)' || true
git status --short
```

Expected: no new traceback; only the pre-existing user-owned `AGENTS.md` and
`CLAUDE.md` modifications remain outside this plan's commits.
