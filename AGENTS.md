# AGENTS.md — Coding-agent conventions for Vocarium

## Scope

This file guides AI coding agents working on the Vocarium stack. It complements `CLAUDE.md` with operative conventions, pitfalls, and workflow rules.

## GPU ownership and client access

- **RTX 3060 / GPU 0 is dedicated to Vocarium inference.** TTS, STT and music share it through Vocarium's queue and load/unload coordination.
- **Subwave, Hermes, and other applications are normal Vocarium clients.** Their RTX 3060 use through the Vocarium API is intended traffic, not foreign contention.
- Clients must use the Vocarium gateway/API, not workers such as `qwen3-tts:8880`; direct worker access bypasses queueing, resource guards, user scoping, and model eviction.
- **RTX 5060 Ti / GPU 1 is not normal Vocarium capacity.** Use it only as an explicitly enabled emergency fallback after the RTX 3060 path genuinely fails.

## Testing philosophy

- **Test before, not after:** When changing inference endpoints (`qwen3-tts/server.py`, `whisper-stt/server.py`, API routes), run a direct container-level request to verify the model loads and responds. CUDA/VRAM failures can be silent.
- **Verify changes were picked up:** After `docker restart` of a volume-mounted service, confirm the running code matches edits; see the `__pycache__` gotcha in `CLAUDE.md`.

## Common gotchas

### 1. Pycache poisoning (silent failure mode)

When a Python file is `:ro` mounted into a container, Python may load stale `.pyc` instead of newer `.py` with no warning.

**Always verify after restart:**
```bash
docker exec vocarium-api python -c "import sys, inspect, podcast.routes; print(sys.dont_write_bytecode); print(inspect.getsourcefile(podcast.routes))"
```

**Prevention:** `PYTHONDONTWRITEBYTECODE=1` and a `tmpfs` mount over `__pycache__` are configured in `docker-compose.yml`.

### 2. Runtime Python refactor traps

Python resolves methods at runtime. If `VocariumTTSGenerator` inherits a `default_voice_for_speaker` from a Mixin that does not accept `user_id`, that shadowed version wins silently. Remove the shadowing method or unify signatures. When adding a parameter such as `user_id` to a protocol method, grep all implementations, including Mixins, base classes, and test mocks.

### 3. SSE buffering → Gateway timeout

If a FastAPI `StreamingResponse` collects all events before yielding, clients receive zero bytes until completion and can hit 30-60s browser/proxy timeouts.

**Correct pattern:** Use `asyncio.Queue` (or `asyncio.Event` + queue) to stream events as they happen:
```python
event_queue: asyncio.Queue[str | None] = asyncio.Queue()

async def worker():
    event_queue.put_nowait(_sse_event("progress", {...}))
    result = await slow_work()
    event_queue.put_nowait(_sse_event("complete", result))
    await event_queue.put(None)  # sentinel

async def event_stream():
    task = asyncio.create_task(worker())
    while True:
        ev = await event_queue.get()
        if ev is None: break
        yield ev
    if task.done() and task.exception():
        raise task.exception()

return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### 4. TTS event-loop blocking and model locking

Qwen3-TTS inference is CPU-heavy and blocks the asyncio event loop. Wrap `model.generate_custom_voice()` in `loop.run_in_executor(None, _blocking_inference)` so concurrent requests do not queue behind blocked endpoints while their timeout clocks run:
```python
def _gen():
    return model.model.generate_custom_voice(**kwargs)
wavs, sr = await loop.run_in_executor(None, _gen)
```

This is already applied to `/v1/audio/speech`, `/v1/audio/speech/custom`, and `/v1/audio/speech/stream` in `server.py`.

Use `threading.Lock` only for model load/unload state changes and counter manipulation. Release it before inference, and use `active_requests` to prevent unload while inference runs. In `qwen3-tts/server.py`, `with model_lock:` wraps `active_requests += 1`, then inference runs outside the lock.

### 5. Timeout math and idle unload

With 2 TTS GPUs and ~70s average inference:
- `Semaphore(8)` × 70s = queue depth per GPU of ~4 → 4th request waits ~210s + 70s = 280s → hits `total=300s` timeout on long segments
- `Semaphore(3)` keeps queue depth to ~1 per GPU → safe margin
- aiohttp `total=` must include wait time and inference time. Use 3600s (1 hour) for long segments.

`IDLE_TIMEOUT=0` unloads the model immediately when `active_requests` reaches zero, causing every sequential segment to reload the model (~14-20s). Use `IDLE_TIMEOUT=120` or higher and watch for repeated `Loading model: ...` in TTS logs.

### 6. `FasterQwen3TTS.from_pretrained()` crashes on kwargs

This wrapper does not accept arbitrary kwargs like `attn_impl`, `trust_remote_code`, or `eos_token_id`. In `server.py`, pass only `device` and `dtype` to `from_pretrained`.

### 7. Script re-generation race condition

"Regenerate Script" runs asynchronously in the GPU Queue. If "Render Audio" starts before the script is committed, the audio worker may load old script JSON. Fresh-query `script_json` at the start of the audio generation endpoint:
```python
# Re-load script fresh from DB in case it was just regenerated
row = db.execute("SELECT script_json FROM podcasts WHERE id=?", (podcast_id,)).fetchone()
if row and row[0]:
    fresh_script = json.loads(row[0])
    if fresh_script and fresh_script.get("segments"):
        script = fresh_script
```

### 8. Every DB query must filter by `user_id`

The `voices` and `hosts` tables are multi-tenant. Omitting `user_id=?` in `SELECT` or `UPDATE` leaks data between users. Search for `"WHERE.*user_id"` or `"AND.*user_id"` when reviewing queries.

## Database schema conventions

### Adding a new entity

1. Add CREATE TABLE in `database.py` `init_db()` with `ON DELETE CASCADE` where appropriate
2. Add migration logic in `database.py` for existing DBs (`sqlite_master` → ALTER TABLE)
3. Add CRUD endpoints in the appropriate router
4. Filter every query by `user_id`

### Seeding prebuilt entities per-user

Custom voices and preset hosts seed automatically on first API request (`seed_prebuilt_custom_voices()` and `seed_prebuilt_hosts()`) using `INSERT OR IGNORE` with user-scoped IDs. If seed data changes, existing users need a migration or new seeding trigger.

## Code style

- **Python:** Strict type annotations on public methods. Use `str | None` (Python 3.10+), not `Optional[str]`.
- **Error messages:** User-facing errors are in English. Internal logs can be English or German.
- **Logging:** Use `logger = logging.getLogger(__name__)` per module. Never `print()` in `routes.py` or production paths.
- **File I/O:** Use `asyncio.to_thread()` or `aiofiles` for blocking disk I/O in async paths.

## When to restart vs rebuild

| Change | Action |
|--------|--------|
| `*.py` in volume-mounted paths | `docker compose restart <service>` |
| `*.ts`, `*.tsx` in UI | `npm run build` then `docker compose build vocarium-ui` |
| `Dockerfile`, `requirements.txt`, `package.json` | `docker compose build <service>` |
| `docker-compose.yml` structure | `docker compose down && docker compose up -d` |
| Database schema change | `docker compose restart vocarium-api` (migrations run on startup) |

## Health checks for debugging

```bash
# Full-stack health in one command
echo "API: $(curl -s http://localhost:8280/api/health | head -c 5)" \
  && echo "TTS-0: $(curl -s http://localhost:8201/health | head -c 5)" \
  && echo "TTS-1: $(curl -s http://localhost:8202/health | head -c 5)" \
  && echo "STT: $(curl -s http://localhost:8210/health | head -c 5)"

# Queue status
curl -s http://localhost:8280/api/queue/status | python3 -m json.tool

# GPU memory usage (inside TTS containers)
docker exec qwen3-tts nvidia-smi
```

## Pull request / edit checklist

Before finishing any change, verify:

1. [ ] `py_compile` syntax check passed on modified `.py` files
2. [ ] If modifying SQL queries, `user_id=?` filter is present
3. [ ] If modifying `server.py`, run a direct container-level TTS request to verify model loads
4. [ ] If modifying SSE/streaming response, verify events are yielded before work completes
5. [ ] If modifying Docker compose, verify containers still communicate (`docker exec X curl http://Y:PORT/endpoint`)
6. [ ] If modifying timeout values, they fit the longest realistic operation
7. [ ] `CLAUDE.md` or `AGENTS.md` updated if architecture or conventions changed

<!-- webui-managed: project-context:start -->
# Project: voxtral

Vocarium

## Tech Stack
Docker Compose

## Key Directories
scripts/
<!-- webui-managed: project-context:end -->

## Android client and adaptive layout

- The Vocarium Android client package is `ch.zwaetschge.vocarium`, builder project `242819cb-6367-4c34-bb0e-75fa70fc0b7f`; source workspace: `/mnt/user/AI/plum-code/android-app-creator/projects/242819cb-6367-4c34-bb0e-75fa70fc0b7f`. Canto rebuild specifications are a separate client.
- Build/install/device checks use the Android-builder MCP and the session-selected serial. Do not silently substitute another connected device.
- Fold layouts use viewport width, not model names. Navigation is bottom-mounted below 720 px, rail at 720–1024 px, and existing sidebar above. The reader uses the full window; chapter/preferences panels replace text below 720 px and sit beside it on wider windows.
- Preserve the WebView and React reader during resize where the wrapper is still in use. Native system/cutout/IME insets are applied once by the wrapper, and Android font scale is passed to WebView text zoom.
- Verification and 1.2.0 APK: `docs/verification/fold-layout-2026-09-05/`. Real folding, IME and playback continuity still require a reachable physical device.

## Studio frontend, drafts and editions (September 2026)

- `/library` combines audiobook and audio-drama adapters. Relationships are explicit and user-owned through `vocarium-api/library.py`; never merge works by title or start production when linking. `main.py` registers this router. Startup migrates `library_links`, `ab_progress.completed` and `podcasts.audio_revision`.
- `vocarium-ui/src/state/EditorDrafts.tsx` owns memory-only unsaved editor fields above routes. Warn on full unload and never silently start production while segment drafts remain.
- `state/player.ts` owns the single audio transport. ReaderWorkspace in Layout retains viewed/current playback reader during navigation. Do not remount it on resize or allow page audio elements to compete. Keep media-bridge actions and positions per edition/artifact.
- Saving cues/cast/script is separate from rendering. Preserve playable artifacts during edits and jobs, record stale state, and reject conflicting edits during active runs. Podcast revision hashes represent audible script plus cast; timestamps alone do not prove current audio. Archive render files by artifact ID/content hash so old player URLs remain valid.
- Frontend evidence and delivery status: `docs/implementation/frontend-20-tage/`. Browser fixtures block service workers so API interception cannot escape to production. API regressions run via `bash scripts/run-api-tests.sh tests --color=no --tb=short`.
- Release API and UI together after confirming no active audio-drama/podcast/audiobook jobs. Read authenticated user state through the UI gateway; direct API reads can fall back to the `api` user and hide real productions. Also check global database production counts. No API restart while a production is active.

## Narration source distribution (September 2026)

- A chapter mapped to multiple episodes does not imply equal paragraph boundaries. `source_fragment_groups_for_episodes` proposes monotone cuts using vocabulary that distinguishes retained subtitle corpora; insufficient evidence keeps the balanced fallback. Each fragment remains assigned exactly once, in source order. These are source-selection hints, not measured scene times.
- Scene alignment reports the currently processed package and accepted count through its progress callback; do not carry 100% from context research into unfinished scripting. Band 5 regression and ongoing delivery status: `docs/operations/dragonball-band-5-repair/`.
- Hörspiel-Overlays: Endliche FFmpeg-Zweige können den Sidechain-/Mixgraphen vorzeitig beenden. Zugeschnittene Eingänge mit `apad` offen halten und den Mix explizit auf die geplante Samplezahl trimmen; keine Stille an einen schon abgeschnittenen Mix hängen. Regression: `tests/test_overlay_duration.py`, reale Prüfnachweise unter `docs/operations/dragonball-band-5-repair/`.

## Four workspaces and Android 2.0

- `src/lib/navigation.tsx` defines the four modes and tools. `WorkspacesProvider` retains in-memory route positions per mode; shared `/library` and general settings retain origin context. Direct `/library/book/...` and `/library/play/...` links choose their explicit edition mode.
- Global mode navigation is separate from local workflow tools. Keep all four modes visible on mobile/rail; the full-width reader uses its mode dialog. Measured bottom-navigation height positions the shared player during Android font scaling.
- Podcast episode/view and drama view use URL query fields. Preserve unrelated query fields when changing one. Never interpret a mode switch as a save, render, or link command.
- `SpeechWorkspace` retains speech generation, settings and clips above routes. Completed speech clips share `state/player.ts`; live Web Audio previews stop on leaving the workspace while generation may finish. Keep reader and speech workspaces outside keyed route content.
- Android 2.0 keeps one WebView. The origin-restricted `VocariumNative` message listener supplies the compatible media bridge. App links reuse `__vocariumNavigate`; hardware Back first calls `__vocariumBack`. Native insets and text zoom remain authoritative.
- Browser-created blob downloads use Storage Access Framework and acknowledged chunks on a file worker; retain the Blob before its temporary URL is revoked. Never replace this with a full-file base64 string on the UI thread.
- Evidence, APK and remaining physical-device checks: `docs/implementation/four-workflows/`.

## CLI maintenance and model catalogs

- Settings are reachable from WorkflowBar and the reader. `/settings/ki` exposes shared runner CLI maintenance, model profiles and logins; `/settings/hoerspiele` contains integrations. The old drama settings route remains compatible.
- Admin-only `/api/hoerspiele/settings/agents/maintenance` proxies allowlisted actions to the Hörspiele agent. `vocarium-agent/maintenance.py` shares `RUN_LOCK`, installs exact registry versions into the persistent provider-secrets volume's `cli-runtime/releases`, checks the executable, then atomically switches its per-CLI symlink. Never replace this with arbitrary user-supplied commands or in-place global installation. Prior release files remain available.
- Codex app-server `model/list`, Claude initialization metadata and Z.AI `/v1/models` supply catalogs without inference. Catalog failures preserve previous data; retained model IDs stay valid in API and runner. The API stores catalogs inside existing `agent_settings`; refreshing the UI must not replace edited profiles.
- Podcast provider catalogs use saved, user-scoped credentials via `/api/llm/providers/{id}/models`; do not return credentials or follow authenticated redirects. Refreshing never selects or saves a model automatically.
- Verification and rollout: `docs/implementation/settings-maintenance/`. CLI updates are user-triggered; isolated installation tests do not mean production CLIs were upgraded.

## Shared typography and Aurora

- All studio UI uses local variable Geist (`public/fonts`, SIL OFL). `--font-display` and legacy book-UI `--font-serif` alias `--font-body`; Geist Mono remains for technical values. Do not reintroduce per-workflow heading families or external Google Fonts. Reader-selected serif/mono preferences remain independent.
- Layout mounts one shared Aurora across all four workflows. It must survive mode changes; reduced motion and unavailable WebGL retain the static fallback. Verification: `docs/implementation/type-aurora/`; the headless test browser has no WebGL support, so those screenshots show the fallback.

## Android widget design collection

- Twelve planned homescreen widgets (three per workflow) and the interactive gallery are in `docs/design/android-widgets/`. `PLAN.md` records individual actions, verified routes, native prerequisites, Fold sizing, delivery order and acceptance checks; `designs.json` is the structured inventory. This is a completed design study, not implemented native widgets. Preserve that distinction when resuming implementation.

## Hörspiel quality repairs (September 10)

- Original-picture evidence can supplement anime-only scenes without modifying the novel. `POST /projects/{id}/scene-evidence` saves owner-scoped reviewed frames and source-digest anchor corrections only while no project run is active; saving never starts production. `scene_evidence.py` appends narrowly timed picture candidates after existing candidates to preserve unrelated checkpoint aliases. Picture descriptions are not native dialogue and must be excluded from nearby subtitles and raw novel-anchor selection. Evidence and delivery status: `docs/operations/dragonball-band-10-TODO.md`.
- API source binds use `/mnt/user/AI/plum-code/voxtral` so Unraid mover activity cannot remove their sources. Include `library.py`: the September-12 container had it only in its writable layer, so recreating the older image without that module breaks startup.
- Normalize character identities across the whole book before episode batching; only the earliest required episode introduces an identity. Use explicit lexicon aliases, parenthetical names and spelling separators, never fuzzy name guesses. Final gates use the same identities.
- Episode-context research carries the prior batches' cast (canonical names, explicit aliases, visual traits, first introduced episode) into later batches. A name-only lexicon entry or a mere mention does not establish an introduction. Explicitly unverified required appearance descriptions are not reusable research; timing/research boilerplate does not count as a visual trait.
- Repair selection shares editorial cue predicates with the gate and supports missing introduction candidates. Existing-to-raw cue mapping is one-to-one; preserve unmatched cues and locked source evidence/anchors. Structured `actual.cue_ids` links findings in the existing UI.
- Explicit quality repairs cannot use exhausted-error or early optional-cue salvage to waive the requested contract. A blocked final gate means a failed repair, with artifact/report retained. Unit tests or deployment alone do not prove stored audio passes the quality gate. Technical/live verification and Band-8 status: `docs/operations/quality-gate-repair/`.
- A failed dedicated introduction must put its required candidate in `AlignmentValidationError.required_cue_ids`, even when another existing cue was incorrectly mapped. Otherwise retry filtering hides the candidate and repeats the same failure. Regression covers the validator and the complete two-attempt repair chain.
- Coverage repairs may need to reposition an already selected cue: use actual locked anchors for planning, release exactly the chosen relocation locks, and retain the source windows in both runner and final coverage checks. Anchor feedback must prefer existing subtitles in a feasible gap-closing interval; duration feedback includes the exact word limit.
- Character timing compares the intro and the book boundary after the same speech-safe adjustment. Research-based introduction cues use the same visual-evidence contract in the runner and final editorial gate; they are not required to reproduce the action fragment's vocabulary.
- Scene-alignment packets may only return formal introductions required in that packet. In retry payloads, lock formal introduction rows only when their cue is actually locked; a rejected cue's stale mapping must stay removable. Scene checkpoints v9 invalidate earlier results that allowed extra introductions.
- Coverage candidate timestamps for synthetic character introductions are search hints, not novel-action evidence. Exclude them from coverage-home checks while retaining their actual placements in comment counts and required-introduction validation. Anchor-only retries preserve prior content and metadata; content failures release that preservation.
- The Hörspiel project-list endpoint is a cheap, owner-scoped read. Never run `ensure_project_quality_state` or persist state while listing; detail reads and production completion refresh reports. Regression: `test_project_list_does_not_revalidate_quality_and_is_owner_scoped`.
- Final comment-gap validation measures the rendered timeline from each narration clip's end to the next clip's start, merging overlaps and including retained book edges and episode transitions. Missing source candidates never exempt audible gaps. `_planned_narration_coverage_result` is only a pre-render candidate check; the release gate must receive the new render's timeline explicitly. Evaluator v22 supersedes the insufficient v21 gap claim; Band 8's September-10 artifact has 24 real pauses over 150 seconds (maximum 625238 ms).
- Actual-gap repairs generate source-backed candidates at retained subtitle positions, with mandatory `coverage_repair_window` bounds propagated through retries. They cannot be omitted or moved out of the window. Reviewed silent-picture anchors are marked `visual_scene_anchor` and excluded from native spoken-audio context; retain their `visual_evidence` when rebuilding reconciled segments. Reviewed book-end anchors survive raw-cue regeneration. Checkpoints v10 include this repair contract.
- Retry prompts include accepted narration text and purpose alongside positions. Chronology uses explicit cue `order`, never numeric cue IDs; added gap candidates may have high IDs in the middle of an episode.
- Local duplicate-detail feedback releases both editable cue texts so retries can redistribute information; engine-locked texts stay protected. If the later cue is locked, report the editable earlier cue instead of silently filtering out the duplication.
- Explicit book-opening/closing markers define narrative edge order even when reviewed boundary anchors cross stale cue timing proposals. Normalize those edges first/last before scene-alignment prompts and validation; ordinary cues keep their explicit relative order.
- Minimum cue-count failures must carry the valid partial result and reopen unused candidates from the affected episode. A plain ValueError preserves the previous single-cue repair scope, so extra model selections are filtered out and the count error repeats. Keep existing valid cues locked and let the agent choose enough additional candidates within the unchanged minimum/maximum.
- Required introduction candidates must survive collisions in the small first-subtitle hint window. Broaden to other existing subtitle hints rather than silently dropping the figure; only final alignment proves timing. Coverage candidates may cite book-edge paragraphs as context but must have `book_edge=none`; the agent rejects contradictory input.
- Der redaktionelle Release-Befund übernimmt das vollständige Ergebnis von `_character_introduction_timing_result` für dieselbe gerenderte Timeline. Keine zweite verkürzte Namens-/Timingprüfung: belegte Synchronnamen wie Krillin gegenüber Forschungsname Kuririn und speech-safe Grenzkorrekturen müssen konsistent bleiben. Evaluator v23; Band-9-Nachweise unter `docs/operations/dragonball-band-9/`.
- Band-10 continuation: missing introductions without a named carrier reopen editable candidates in that episode, not an arbitrary first cue; engine locks stay intact. Role names do not inherit substring aliases (Panputto is not Panputtos Manager).
- Evaluator v24 aligns the runner's short-overlay allowance with the actual rendered placement (at least eight words and 3.6 s target); inserted scene pauses retain their minimum. Native speech checks exclude reviewed picture anchors. Actual-gap repairs prefer available reviewed picture tasks, preserve their independent source and stable cue IDs, and avoid duplicate candidates.
- Reviewed picture provenance is independent of the selected timing subtitle. Resolve stable picture cue IDs to their stored reviewed markers; retain matching source text/frame provenance, require a reconciled visual marker and a real timing segment in the same episode within the original ±1-second review window. Evaluator v25; Band-10 evidence in `docs/operations/dragonball-band-10-TODO.md`.
- Episode audio extraction uses unique temporary files and publishes them atomically only after FFmpeg succeeds and the measured duration matches Plex (2 s tolerance). Existing FLAC/WAV caches require a valid duration; never accept file size alone after a timeout. Source reads on the array can be slow: extraction allows 1800 s. Band-11 incident evidence: `docs/operations/dragonball-band-11-timeout.md`.

## Native Android design preview (September 14)

- The Android builder project `242819cb-6367-4c34-bb0e-75fa70fc0b7f` now launches `NativeActivity` and `NativePlaybackService`; `NativeApi` calls the authenticated API directly. The old WebView classes remain in source but are not the launcher. WebView is currently used only for sign-in. This supersedes the wrapper architecture for this development branch, not the existing production delivery.
- `NativeDesign.kt` and the native Activity implement the cover library, search/status filters, chapter/reader surfaces, mini-player and adaptive navigation. Native app parity, offline, production editors and real device validation remain incomplete. Never describe a compiled preview as the completed full client.
- Plan, gaps, contrast checks and preview build hashes: `docs/implementation/android-native-client/`. The explicitly labelled September-14 preview APK is on the Zwischenspeicher share. Device verification must use the session-selected serial; the old 35023 endpoint was unavailable and no alternative serial was silently substituted.
- Reviewed narration anchors store their subtitle start/end times and exact text digest alongside the generated row ID. Reconciliation may replace row IDs; rebind only to one identical subtitle in the same episode. Changed timing/text or ambiguous matches require renewed review, never nearest-timestamp guessing. Legacy anchors with missing IDs must be re-reviewed. Regression: `test_scene_evidence.py`; Band-11 continuation notes track live verification.
