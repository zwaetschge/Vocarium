# Vocarium Staged Audit and Optimization Design

**Approved:** 2026-07-10

## Goal

Audit the complete Vocarium stack for correctness, security, reliability, and
performance defects, then remediate confirmed findings in small, measurable,
low-risk batches without disrupting unrelated services or user-owned work.

## Starting Baseline

The following baseline was captured before any implementation changes:

- `python3 -m unittest vocarium-api.tests.test_production_hardening` passes
  all 57 tests.
- `npm run build` in `vocarium-ui/` completes successfully.
- Python compilation succeeds for the API, TTS, ASR, ACE-Step, and MMAudio
  sources when a separate pycache directory is used.
- `npm audit --json` reports no known vulnerabilities in the locked UI
  dependency graph.
- All six default Compose services are running.
- GPU 0 is the preferred Vocarium GPU. GPU 1 is protected while ComfyUI or
  other significant non-Vocarium workloads are active.
- The existing uncommitted edits to `AGENTS.md` and `CLAUDE.md` are user-owned
  and must not be modified, staged, or committed by this work.

These checks establish a clean build and test baseline; they do not prove the
absence of race conditions, tenant leaks, runtime integration defects, or
performance bottlenecks.

## Scope

The audit covers the maintained Vocarium Compose stack:

1. `vocarium-api/`: FastAPI routes, SQLite access, authentication, artifact
   handling, queueing, metrics, and Podcast Studio.
2. `qwen3-tts/`, `asr_proxy.py`, `acestep/`, and `mmaudio/`: model lifecycle,
   concurrency, timeouts, memory release, subprocess handling, and inference
   request validation.
3. `vocarium-ui/`: API contracts, asynchronous state, audio object lifetime,
   error handling, accessibility, runtime behavior, and bundle performance.
4. Docker Compose, Dockerfiles, Nginx, environment defaults, health checks,
   mounts, network exposure, privileges, and resource policy.
5. Test coverage, operational scripts, dependency posture, and documentation
   where they directly affect safe operation.

The independent `vibevoice-api/` stack is excluded unless a shared interface
or root-level configuration creates a directly reproducible Vocarium defect.
Model quality tuning that is purely subjective is excluded; deterministic
quality regressions, malformed audio, endpoint mismatches, and wasteful model
lifecycle behavior remain in scope.

## Audit Workstreams

### 1. Correctness and data integrity

- Trace every public route through validation, persistence, worker dispatch,
  artifact creation, and response delivery.
- Check state transitions, cancellation, retry, timeout, and cleanup behavior.
- Inspect SQLite transactions, concurrent writers, migrations, foreign keys,
  stale reads, and file/database consistency.
- Compare API response shapes with UI assumptions and documentation.
- Reproduce each suspected defect before proposing a change.

### 2. Security and tenant isolation

- Map trust boundaries from reverse-proxy identity headers through API routes,
  database queries, generated files, and worker services.
- Verify that every tenant-owned read, mutation, and download is scoped by
  `user_id` and cannot be bypassed through identifiers or paths.
- Check upload/download limits, content validation, path traversal, SSRF,
  subprocess arguments, unsafe parsing, secret exposure, CORS, proxy-header
  trust, and production defaults.
- Review container privileges, writable mounts, published ports, and service
  access according to least privilege.

### 3. GPU, concurrency, and reliability

- Trace queue admission, GPU conflict checks, worker load/unload, inference,
  cancellation, and post-job cleanup across every audio kind.
- Look for blocking work on event loops, lock contention, duplicate model
  loads, unbounded queues, lost wakeups, stale counters, and unsafe shared
  state.
- Verify SSE emits an early event and remains live during slow operations.
- Exercise cold-start, warm-start, worker-unavailable, timeout, cancellation,
  and malformed-response paths with the smallest useful requests.
- Keep GPU 1 protected unless the user explicitly approves its use.

### 4. Measured performance

- Establish stable baselines before optimization: queue wait, model-load time,
  inference time, total response time, peak and idle VRAM, output duration,
  CPU/RAM, API latency, UI bundle size, and repeated I/O counts as applicable.
- Use fixed inputs and compare like-for-like cold and warm runs.
- Profile or instrument a path before changing it.
- Prioritize redundant model loads, blocking I/O, avoidable serialization,
  repeated database/file work, oversized client bundles, and leaked browser
  resources.
- Accept an optimization only when the relevant metric improves without
  correctness, quality, memory, or concurrency regression.

### 5. Test and operations resilience

- Identify critical behaviors currently covered only by mocks or not covered
  at all.
- Check that health endpoints reflect actual readiness rather than process
  existence alone.
- Review startup, shutdown, restart, stale-pycache, disk-full, partial-file,
  and dependency-unavailable behavior.
- Confirm that operational instructions match the current Compose topology.

## Evidence Standard

A finding is actionable only when it includes:

- exact affected component and location;
- observed behavior and expected behavior;
- reproducible command, request, test, trace, or static data-flow proof;
- root cause rather than a symptom-only guess;
- impact and realistic trigger conditions;
- proposed smallest safe remediation;
- verification method and regression-test location.

Potential concerns without sufficient evidence are recorded separately as
investigation notes, not presented as confirmed bugs.

## Severity and Ordering

- **P0:** active tenant escape, remote code execution, unrecoverable data loss,
  or service-wide outage. Stop normal work and contain immediately.
- **P1:** reproducible authorization bypass, corruption, common crash, deadlock,
  or severe resource leak. Fix in the first remediation wave.
- **P2:** bounded correctness error, race, timeout, poor recovery, or material
  latency/VRAM waste. Fix after P0/P1 with a focused test and benchmark.
- **P3:** low-impact edge case, maintainability risk with a concrete failure
  mechanism, or modest measured optimization. Batch only when low risk.

Security and data integrity outrank performance. Performance work never
bypasses GPU guards, removes validation, weakens tenant isolation, or makes
timeouts shorter than realistic cold-start behavior.

## Remediation Method

Confirmed findings are fixed one root cause at a time:

1. Write the smallest regression test and run it to observe the expected
   failure.
2. Implement only the change required to address that root cause.
3. Run the focused test, then the relevant component suite.
4. For performance changes, repeat the same benchmark and record before/after
   metrics.
5. Restart or recreate only the affected service according to `AGENTS.md`.
6. Verify mounted Python source inside the container when applicable.
7. Run a direct container-level request for changed inference endpoints.
8. Inspect logs and resource state after the test, including VRAM release.
9. Commit the isolated fix without staging unrelated files.

Large architectural refactors are not bundled into the first wave. If three
focused fix attempts expose failures across multiple shared boundaries, work
stops for a separate architecture decision instead of adding a fourth patch.

## Runtime Safety

- No destructive database commands or production-data rewrites.
- Schema changes require migration coverage and a database backup before a
  live restart.
- No use of GPU 1 while it is protected by the active resource policy.
- Use the smallest audio input that proves an inference behavior.
- Avoid whole-stack restarts when one service restart or recreate is enough.
- Preserve current public API behavior unless a confirmed security or
  correctness defect requires a documented compatibility change.
- Do not expose secrets, model tokens, private audio, or user content in test
  logs or commits.

## Deliverables

The work produces:

1. A versioned audit ledger containing confirmed findings, evidence, severity,
   status, and measured baselines.
2. Small remediation commits grouped by root cause and priority.
3. Regression tests for every behavioral fix.
4. Before/after measurements for every accepted performance optimization.
5. Updated operational documentation only where verified behavior changed.

Because the audit spans independent subsystems, implementation is decomposed
into testable packages. The first execution plan covers discovery, evidence,
and the first safe P0/P1 triage wave. Subsequent P2/P3 or architectural work
receives a separate scoped plan after the relevant findings are confirmed.

## Completion Criteria

The first staged audit and remediation wave is complete when:

- every in-scope subsystem has been inspected using the workstreams above;
- confirmed findings are recorded with evidence and severity;
- all P0 findings are contained and fixed;
- all safely actionable P1 findings are fixed or explicitly documented as
  blocked with the missing authority or external dependency;
- each fix has demonstrated red/green regression coverage;
- Python tests, UI build, relevant E2E checks, Compose validation, syntax
  checks, and targeted container smokes pass freshly;
- changed inference endpoints load and return valid media directly at the
  container level;
- performance claims include reproducible before/after metrics;
- runtime logs show no new errors and GPU/resource state is left safe;
- unrelated user changes remain untouched.

The audit may discover additional P2/P3 work. Those findings remain explicit
follow-up items rather than being hidden inside a premature completion claim.
