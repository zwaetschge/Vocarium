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
