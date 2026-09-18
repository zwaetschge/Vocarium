# CLAUDE.md

This file guides Claude Code (Claude.ai/code) when working in this repository.

## What this repo is

Self-hosted, ElevenLabs-compatible voice platform named **Vocarium**: a Docker Compose stack with a central FastAPI gateway (`vocarium-api`), React/Vite UI, GPU inference workers, Podcast Studio (`/podcasts`), audiobooks, and Hörspiele workflows.

## Architecture

```text
UI (3100) ──► vocarium-api (8280) ──► gpu_queue ──┬─► whisper-stt   (8000 internal, 8210 host)
                   │                               │
                   ├─► kikiri-tts    (8881 internal, 8205 host) CPU fallback, no queue
                   ├─► omnivoice-tts (8880 internal, 8208 host) GPU 0 resident, zero-shot clones
                   │
                   └── SQLite at /app/data/vocarium.db
                       /app/voices shared voice metadata + reference audio
```

Inter-container URLs use service names on `voice-network`: `http://whisper-stt:8000` and `http://kikiri-tts:8881`.

### GPU coordination

GPU placement is env-driven (`GPU_TTS_1`, `GPU_TTS_2`, `GPU_ASR`, `GPU_MUSIC`). Defaults target GPU 0; idle unloading lets workers share it.

- **Single-GPU default:** OmniVoice stays resident (~2.1 GB). `whisper-stt` is lazy and unloads after `IDLE_TIMEOUT=300s`.
- **GPU queue:** `vocarium-api/gpu_queue.py` is an env-aware FIFO coordinator. It reads `GPU_TTS_PRIMARY`, `GPU_TTS_EXTRA`, `GPU_ASR`, and `GPU_MUSIC`; before a job it unloads services assigned to the same GPU. Never evict the incoming service type.
- **Podcast rendering:** speech/reaction rendering runs as one TTS queue job rather than one job per segment.

### Podcast data flow

1. User uploads PDF/TXT source documents, chunked through Docling API.
2. User creates a podcast and assigns preset or custom hosts.
3. `POST /api/podcasts/{id}/script/generate` creates an SSE-streamed multi-speaker script. Segments have `speaker`, `text`, `type` (`speech`|`reaction`|`pause`|`music`), `voice`, optional `notes`, and optional timeline fields (`overlap_ms`, `prompt`, `duration_ms`, `volume_db`).
4. `POST /api/podcasts/{id}/audio/generate` synthesizes speech/reactions and mixes files with ffmpeg `filter_complex`/`amix` plus EBU R128 loudness normalisation.
5. Audio is served through `/api/podcasts/{id}/audio/download` or `/audio/stream` with HTTP Range support.

### Multi-tenancy

EVERY DB query filters by `user_id`. Custom voices, hosts, and LLM providers are per-user. The UI's `Remote-User` header, set by Authelia, identifies the caller.

### Volume-mounted source

These files are mounted read-only into containers and picked up on `restart`; no rebuild is needed:

- `vocarium-api/{main.py,database.py,gpu_queue.py,access_control.py,...}` and packages `podcast/`, `audiobooks/`, `hoerspiele/`
- `whisper-stt/server.py` and `omnivoice-api/server.py` mounted at `/srv/server.py`

Rebuild when Dockerfiles, `package.json`, or `requirements.txt` change, or for the UI, which is built statically into the Nginx image.

#### CRITICAL: read-only mounts and Python `__pycache__`

A `:ro` mount can cause Python to silently load stale cached bytecode even when source is newer.

Prevention:

- `PYTHONDONTWRITEBYTECODE=1` is set in `docker-compose.yml` for every Python service.
- `tmpfs` mounts cover `/app/__pycache__` and `/app/podcast/__pycache__`.

Verify the running code after restart:

```bash
docker exec vocarium-api python -c "
import sys, inspect, podcast.routes
print('dont_write_bytecode:', sys.dont_write_bytecode)
source = inspect.getsourcefile(podcast.routes)
print('Loaded from:', source)
# For a specific function:
src = inspect.getsource(podcast.routes.VocariumTTSGenerator.synthesize_to_file)
print('First line:', src.split('\n')[0])
"
```

## Kikiri: CPU voices — fallback

`kikiri-tts` (port 8205) serves German Kokoro/StyleTTS2 fine-tunes and Piper presets on CPU. It is selected in `vocarium-api/main.py:_select_tts_backend`, can be forced with `engine="kikiri"`, serves WAV only, has no cloned/custom voices, and bypasses `gpu_queue` through `_run_tts_job(..., engine="kikiri")`.

### Model layout

One fine-tune directory under `kikiri-tts/models/`:

```text
kikiri-tts/models/<model-id>/
  model.pth      # five serving submodules, ~330 MB
  default.pt     # voicepack, shape (510, 1, 256)
  config.json    # Kokoro architecture config
  meta.json      # optional display name
```

The server rescans on `/v1/models`, so newly published voices appear without restart. It keeps `KIKIRI_MAX_RESIDENT_MODELS` (default 2) loaded and evicts least-recently-used models.

### Two engines

There are exactly two voice-engine stages:

```text
OmniVoice — cloned voices, primary GPU engine                16
Kikiri — CPU fallback when OmniVoice fails                   44
```

The 44 CPU voices are Kokoro fine-tunes plus 42 German Piper presets. Piper versus Kokoro is an implementation detail within the fallback; fine-tunes only sort first.

`_kikiri_models()` in `main.py` exposes `group` (`kikiri` | `fallback`), `gender`, `backend` (`kokoro` | `piper`), and `notes`. The UI must group voices through `voiceUtils.groupEngineVoices` and `components/VoiceOptions.tsx`; use `<VoiceOptions>` rather than custom `voices.map(...)`.

`DEFAULT_TTS_ENGINE` defaults to `omnivoice`; it decides genuine ties and the built-in `default` voice. Setting it to `kikiri` reverses the priority.

### Fine-tuning removed

`kikiri-trainer`, `/api/finetunes/*`, and the UI Finetunes page are removed. Existing models in `kikiri-tts/models/` continue serving, including the built-in `default` mapping. `kikiri-trainer/` remains available for manual pipeline use.

`omnivoice-tts` and `whisper-stt` build from the `gpu-base/` image, a build-only Compose service (`profiles: ["build"]`):

```bash
# Build order matters — the consumers build FROM this image.
docker compose build gpu-base omnivoice-tts whisper-stt
```

Its venv path is contractual: `whisper-stt` extends `LD_LIBRARY_PATH` with `/app/venv/lib/python3.12/site-packages/nvidia/{cublas,cudnn}/lib` for CUDA-12 CTranslate2 wheels on the CUDA-13 base.

## OmniVoice: zero-shot clones

`omnivoice-tts` (port 8208, GPU 0, resident, no queue) serves k2-fsa/OmniVoice through `omnivoice-api/`. It is a regular Compose service built from `gpu-base`.

Voices are reference pairs in `omnivoice-api/voices/`:

```text
<Name>.wav  # 3–10 seconds of clean 24 kHz speech
<Name>.txt  # exact transcript
```

Restart the service after adding a pair; it appears with `source="omnivoice"`.

The resident footprint is ~2.1 GB. Prevent allocator bloat with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` and `torch.cuda.empty_cache()` after `load_voices()` and each synthesis in `server.py`.

Qwen TTS is retired. `QWEN_TTS_ENABLED=false` hides its DB voices; rows and reference audio remain. The built-in `default` voice maps to the first Kikiri fine-tune. Legacy Qwen host presets were remapped to OmniVoice voices:

```text
Vivian→Podcast-Female, Serena→Vanessa, Uncle Fu→David-Nathan,
Dylan→Podcast-Male, Eric→Simon, Ryan→Marc-Uwe-Kling,
Aiden→Rufus-Beck, Ono Anna→Ygritte, Sohee→Bluetooth-Lady
```

Auto-routing uses exact voice IDs. OmniVoice serves WAV only. Legacy 8-char Qwen clone IDs (e.g. `83b59aca` = Michael Scott, still used by Sub-Wave personas) are aliased by `_legacy_clone_alias()` in `main.py` to the OmniVoice voice with the same normalised name; the OpenAI proxy selects the backend by the resolved voice.

## Hörspiele agent (vocarium-agent/)

The research/dramaturgy agent is the `hoerspiele-agent` Compose service, built from `vocarium-agent/`. It listens on `hoerspiele-agent:8090` on `voice-network`; `CODEX_AGENT_BASE_URL` points there. Its provider home lives under `VOCARIUM_AGENT_HOST_ROOT` (default `/mnt/user/appdata/vocarium/agent`, subdirs `codex-home`, `claude-home`, `provider-secrets`, owned by 1000:1000). The z.ai key is stored in `provider-secrets/zai.json` via `PUT /api/hoerspiele/settings/agents/zai-credentials` (Hörspiele → Einstellungen → Zugänge). `SUPPORTED_MODELS` is compiled into the image: a new model needs an entry in `runner.py` plus `docker compose build hoerspiele-agent && docker compose up -d hoerspiele-agent`. The Plex token lives in `/mnt/user/appdata/vocarium/secrets/plex-token` (`PLEX_TOKEN_HOST_FILE`). Nothing in Vocarium may reference `/mnt/user/appdata/szenenklang` or the `szenenklang_backend` network anymore.

Codex 0.145 inside the agent container needs `seccomp=unconfined` and a `requirements.toml` deny list naming only secrets inside `CODEX_HOME` (`auth.json`, `config.toml`, `history.jsonl`, `sessions/`). Denying the whole `.codex` directory makes Codex refuse to start because its own `tmp/arg0` lives there.

## Podcast Studio on OmniVoice

`hosts.voice_id` no longer has a foreign key to `voices`: OmniVoice and Kikiri own their inventories. `_ensure_hosts_foreign_key_rules()` in `database.py` rebuilds the table idempotently while retaining the user FK.

`_get_custom_voice_or_400` was replaced by async `_require_engine_voice()`, which checks `tts_bridge.engine_voices()`. When `QWEN_TTS_ENABLED=false`, `/api/models` and `/api/models/current` short-circuit instead of contacting a removed service.

Episode-context research runs in batches of `EPISODE_CONTEXT_BATCH` (default 2) episodes per agent call, because one all-episode call with live web search exceeded the GLM-5.3 agent timeout. For provider `zai`, `vocarium-agent/runner.py` uses `run_zai_direct()` against the OpenAI-compatible coding endpoint with `thinking: disabled`; web search uses z.ai's `web_search` tool. JSON extraction tolerates code fences, citation marks, trailing commas, balanced-brace candidates, and one correction round-trip. Invalid answers are logged as `zai_invalid_json`; transient network/gateway errors are retried up to three times (`zai_transient_error`). Failure classification treats every `ValueError` as repairable `invalid_output` before rate-limit checks and matches `429` only as a standalone number.

Character-introduction cues are exempt from the source-detail-coverage gate. After `MAX_INVALID_OUTPUT_ATTEMPTS` (default 14), the runner drops exhausted optional beats, accepts unprovable character introductions as a gap (`introduction_gap_accepted`, `_tolerated_introductions`), then drops still-rejected non-book-edge cues (`cues_dropped_after_exhaustion`; min-cue targets lowered, gap limit lifted for that episode). Salvage is attempted on the last error, the attempt with the most surviving cues, and the last introduction error; `salvage_failed` events explain non-application.

**Quality repair loop:** `POST /api/hoerspiele/projects/{id}/quality-repairs` (optional `cue_ids`) derives rejected cues from failed `quality_report` checks, regenerates raw cues, maps existing project cues by source fragments + anchor episode (`locked_alignments_for_repair`), and calls the agent in repair mode (`_repair_cue_ids` + `_locked_alignments`). Packages without a rejected cue are assembled locally. When `narration_coverage` fails or density is violated, `coverage_gap_repair_ids()` requests fresh raw cues to close every gap above `narration_max_gap_ms` and top up `min_cues` per episode. Coverage-gap repair (`timeline_gap_repair_candidates()`) needs novel evidence for each new anchor: neighbouring cues' source fragments, otherwise (since 2026-09-17) the fragments of the chapters mapped to that episode; if an episode has neither (anime-only story), the gap is skipped with a project warning instead of aborting the whole repair run (Band 13, S1E115 had zero cues and killed every repair with "lacks novel evidence"). Locked alignments must carry schema-valid enum values (`native_audio_relation`, `beat_type`, `audio_strategy`, `placement`). `validated_alignment_request()` must pass `_locked_alignments`, `_locked_character_introductions`, `_repair_cue_ids`, `_validation_retry`, and `_tolerated_introductions`; until 2026-09-05 it stripped underscore keys.

`enforce_locked_alignments()` restores locked cues verbatim after each agent answer, and `REPAIR_LOCK_VERSION` invalidates old repair checkpoints. Backend-locked cues are marked `_engine_locked_cue_ids`; validator skips their text checks while keeping structural checks. Retry payloads derive from the previous attempt, so plain `ValueError` no longer wipes locks. `agent_post()` bridges agent restarts with connection-error retries for `AGENT_CONNECT_WAIT_SECONDS` (default 900 s) and waits while the single-slot agent is busy (`AGENT_BUSY_WAIT_SECONDS`, 30 s polling) so parallel runs queue instead of failing with 409.

Quality repair (September 10): normalize character requirements across the whole book before splitting episode packages. Explicit lexicon aliases, spacing/hyphens and parenthetical names share one identity; only the earliest required episode introduces that identity. Final gates use the same identities. Never guess aliases from fuzzy name similarity. Repair targeting shares the editorial gate's cue predicates and includes missing introduction candidates; structured `cue_ids`/`actual.cue_ids` make global editorial findings navigable. Raw-cue mapping is one-to-one and retains unmatched existing cues; locks preserve anchors and source-detail evidence as well as text. Explicit `_repair_cue_ids` runs must fail after exhausted validation/provider retries instead of accepting introduction/coverage gaps. A rendered repair with blocked final quality is reported as failed, with its artifact and report retained. Regression and rollout evidence: `docs/operations/quality-gate-repair/`.

Before rendering, `realign_scene_inserts()` resets scene-boundary inserts to their anchor boundary and `speech_safe_insert_ms()` snaps them idempotently. Narration clips are loudness-matched per episode (`measure_integrated_loudness` via ffmpeg ebur128, target = episode LUFS + `NARRATION_LOUDNESS_OFFSET_DB`, default 1.5) before time-fit; `cue.loudness_gain_db` records the gain. Narration clips that overrun their pause are stretched up to `NARRATION_MAX_TIME_FIT` (default 1.25, pitch-preserving atempo); beyond that the clip is stretched to maximum and recorded as a warning. Length is estimated with `NARRATION_MS_PER_WORD` (default 450 ms, measured on OmniVoice). The scene-alignment prompt forbids narrating action audible only later than ~15 s after the anchor.

CLI runs (Claude Code, Codex) retry transient failures inside `run_structured_agent()` up to `CLI_TRANSIENT_ATTEMPTS` (default 3, backoff 20/60/120 s): a result report with `api_error_status` in 401/408/409/425/429/5xx/529, or a report with zero input and output tokens and no answer (2026-09-17, Band 13: `stop_sequence`, `duration_api_ms` 0 during a Claude token refresh). `cli_result_failure()` now turns such "success" reports into a readable cause instead of the raw usage JSON (`cli_report_problem()`), and the event `cli_transient_error` is logged per retry.

Toggle the direct path with `ZAI_DIRECT` (default true). Agent profiles on GLM-5.3 run with reasoning `high`; timeouts are research 3600 s and scripting 5400 s (`AGENT_TIMEOUT_MAX` = 7200 in `engine.py`, mirrored in `vocarium-agent/runner.py`, agent `ZAI_API_TIMEOUT_MS` = 2 h). A single scene-alignment package takes more than 30 min on GLM. `--max-turns` is 6 without web search; with live web search `claude_turn_budget()` scales it as `min(240, 24 + 48 * research_units)` and `CLAUDE_MAX_TURNS` overrides it. Measured 2026-09-12: one episode of real research costs 47 turns / ~11 min for 14-18 checked sources. Claude Code needs both axes: `--tools` only offers a tool, `--allowedTools` permits it; under `--permission-mode dontAsk` an offered-but-unpermitted WebSearch is silently denied. The engine dumps packages to `artifacts/<project>/debug/scene-alignment-NN.json` for replay. Per-cue contract violations raise `AlignmentValidationError` with the cue id.

`GET /api/hoerspiele/projects/{id}/cover` serves the poster of the bound Plex series (fetched via `/library/metadata/{series_id}` → `thumb` → `/photo/:/transcode` at 600×900, cached under `artifacts/covers/plex-<series>.jpg`, 404 without a binding or for demo ids). The Android client uses it for Hörspiel cards; the web UI shows it in the Mediathek and on project cards via `ProjectCover` (monogram fallback on 404), and `sw.js` caches it cache-first.

`podcast/tags.py` contains 13 OmniVoice nonverbal tags. `GET /api/podcasts/tags` serves German labels; the script prompt teaches their use, `sanitize()` strips unsupported tags, and the editor exposes a palette.

The detail UI is tabbed: Quellen · Skript · Audio · Besetzung. Copy is German.

## Whisper STT

`whisper-stt` (port 8210 host / 8000 internal, GPU 0) is a regular Compose service built from `gpu-base`. It uses faster-whisper/CTranslate2 in `int8_float16` (~2 GB), loads one profile at a time, and unloads after `IDLE_TIMEOUT=300s`.

- `german` default: vanilla `large-v3`, cached in `models/ct2-vanilla/`.
- `swiss`: request with form field `model=swiss`, using `Flix-AI/flix-swissgerman-full`.

Do not make `swiss` the default: it can translate clean standard German to English despite `language=de`, `task=transcribe`, and `condition_on_previous_text=False`. A German `initial_prompt` can make it loop.

Flix conversion is cached under `models/ct2/`. Its missing `preprocessor_config.json` is written with `feature_size: 128`; otherwise faster-whisper uses 80 mel bins and the encoder rejects the shape. CUDA-12 runtime comes from `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` plus `LD_LIBRARY_PATH`.

Without `language`, the server defaults to `de`; `language=auto` restores detection.

Silero VAD (`vad_filter=True`) classifies singing over music as non-speech: a whole song yielded zero segments or a single sentence. Since 2026-09-16 `_transcribe()` runs in `vad="auto"` mode: after the VAD pass, if the kept segments cover less than `VAD_MIN_COVERAGE` (default 0.2) of the audio, it retries without VAD. Callers may force `vad=on|off` through `/api/transcribe` (form field, passed through to the worker). The response carries `vad: true|false` to show which pass produced the text.

**Bind-mount sources (2026-09-16):** every bind in `docker-compose.yml` hangs off `${VOCARIUM_HOST_ROOT:-.}`; `.env` sets it to `/mnt/user/AI/plum-code/voxtral` and points `WHISPER_MODEL_DIR` and `OMNIVOICE_HF_CACHE` at `/mnt/user/...` too. Never use `/mnt/cache/...` sources on Unraid: the mover relocates files to the array, a restart then fails ("mount a directory onto a file") and Docker leaves an empty directory that shadows the real file in `/mnt/user`. `voice-network` is declared `external: true` (it is shared with other stacks). From the Plum container the Docker socket proxy forbids network create/delete and the volumes endpoint, so `docker compose up` works for services without named volumes (one service at a time); `vocarium-api` (named volumes) must be created with `docker run` from `docker compose config --format json` (see `/tmp/run-vocarium-api.sh` pattern in the 2026-09-16 session notes) and then attached to `subwave_subwave-internal` with `docker network connect`. The named volumes carry the compose project prefix: mount `vocarium_vocarium-data:/app/data` and `vocarium_voices-data:/app/voices` (the logical names in `docker compose config` output are unprefixed; using them creates empty volumes and the library appears empty). After recreating `vocarium-api`, restart `vocarium-ui` and `sub-wave-vocarium-openai-proxy`, because Nginx caches the upstream IP.

`/api/transcribe` calls `WHISPER_URL` (default `http://whisper-stt:8000`) before `gpu_queue`, because Whisper manages its own VRAM. Whisper is the only STT path: `/api/transcribe` and `/v1/audio/transcriptions` return 502 on Whisper failure. `GET /v1/models` lists `german` and `swiss`.

## Three-area WebUI

The sidebar splits the UI into:

- **Hörbücher** (`/audiobooks`): upload PDF/EPUB/DOCX/TXT; persisted chapter segmentation under `audiobooks/<id>/segments.json`; OmniVoice/Kikiri generation; EBU-R128-normalised MP3 cache under `audio/<chunkerVersion>/<voice>/<ch>_<seg>.mp3`.
- **Podcasts** (`/podcast`): hosts use OmniVoice/Kikiri voices.
- **Lab**: Speech, Voices, Clone, Transcribe, Settings.

Backend module: `vocarium-api/audiobooks/`. Canto rebuild specification is in `Canto rebuild-spec/`; the original codebase is at `/mnt/cache/AI/plum-code/canto`.

### Audiobooks

- `audiobooks/semantics.py` uses an OpenAI-compatible embedding API. Default: local `embeddings` Compose service, `ghcr.io/ggml-org/llama.cpp:server` running bge-m3 GGUF on CPU at `http://embeddings:8080/v1`; override with `EMBEDDING_API_URL` and `EMBEDDING_MODEL`.
- Fetch weights with `scripts/download-embedding-model.sh`. bge-m3 uses 1024 dimensions. llama.cpp requires `--pooling cls` and `-ub` ≥ `-c`.
- `semantics.py` appends `/embeddings` to the configured URL; `podcast/embedding_client.py` posts directly and normalises through `_embeddings_endpoint()`.
- Embeddings support semantic chapter fallback and book-wide search. Both degrade silently without the backend.
- `normalize_text()` removes CRLF before chapter/segment processing, strips decorative separator lines, and deduplicates consecutive identical paragraphs. Separator regexes require `re.MULTILINE`.
- Chunker version is stored per book in `ab_books.chunker_version`; existing v1 books retain segmentation and cache, while new imports use v2.
- Reader playback uses `GET /{id}/audio-live/{voice}/{ch}/{seg}`. Hits serve immediately; misses synthesize into the regular cache through `render_segment()` using atomic rename. Rendering is serialized per `(book, voice)` and the client warms two segments ahead.
- `/audio/` remains cache-only. Exports are cache-only too: `POST /api/audiobooks/{id}/export` supports `m4b|mp3`; M4B chapters require `-f ffmetadata` and `-map_chapters 1`.
- `ab_generation_queue` is a persistent SQLite queue with a 5-second worker tick, one job at a time, crash recovery, and priority ≥8 restricted by `BUSINESS_HOURS_*` using Europe/Berlin.
- Pronunciation rules in `ab_pronunciation_rules` apply only to TTS text. The rules hash forms part of the audio cache directory: `<voice>+r<hash8>`.
- PWA service worker (`public/sw.js`, v2): audio cache-first for audiobook segments, covers, Hörspiel delivery artifacts, and podcast stream/download with Range support; read-only API responses of `/api/audiobooks*`, `/api/hoerspiele/projects*`, `/api/podcasts*` network-first with cache fallback; navigations fall back to `/index.html`. Requires HTTPS at `vocarium.zwaetschge-webui.ch`.
- Offline layer `src/lib/offline.ts` + `components/OfflineControls.tsx`: registry in localStorage (`vocarium:offline:v1`), `downloadAudiobook()` (per voice, all segments + JSON), `downloadSingleAudio()` (Hörspiel M4A, Podcast MP3), `removeOffline()`, and `announcePlayback()` for `navigator.mediaSession` and `window.VocariumAndroid.setPlayback()`. Entry points: library card menu ("Offline"), reader drawer, Hörspiele Mediathek (`pages/hoerspiele/Mediathek.tsx`, finished `delivery` artifacts), podcast audio row.
- Android WebView client: `android-app-creator` project `242819cb-6367-4c34-bb0e-75fa70fc0b7f`, package `ch.zwaetschge.vocarium`, v1.1.0. `MainActivity` exposes `VocariumAndroid`, starts `PlaybackService`, keeps the WebView running in the background, falls back to `assets/offline.html`, and handles `https://vocarium.zwaetschge-webui.ch` links. Build/install only through the android-builder MCP tools.

### UI cache and visual notes

Nginx sends `Cache-Control: no-cache` for `index.html`, `sw.js`, and manifest, and `immutable` for hashed `/assets/`.

The WebGL aurora lives in `vocarium-ui/src/components/ThreeAurora.tsx`, mounted by `Aurora.tsx`. It uses a fullscreen quad, FBM/simplex shader, mouse parallax, and audio glow from `lib/audioReactive.ts`. DPR is capped at 1.5, hidden tabs pause, reduced-motion renders a static frame, and failed context creation hides the canvas via `three-aurora-failed`.

The mobile layout applies up to **1024 px** CSS width so Galaxy Z Fold inner and outer screens use it. The reader clamps its title to two lines, hides the volume slider below 640 px, lets the player deck wrap, and sets `viewport-fit=cover` for gesture-navigation insets.

Do not use inverted `smoothstep(a, b, x)` where `a > b`; it is undefined in GLSL.

Do not hand-write `-webkit-backdrop-filter`. Tailwind 4 runs Lightning CSS through `@tailwindcss/vite`; manually specifying both can remove the standard property. Write only `backdrop-filter`; `browserslist` in `vocarium-ui/package.json` lets Lightning CSS add the prefix.

Verify after a build:

```bash
grep -o '\.card{[^}]*}' vocarium-ui/dist/assets/index-*.css   # beide Properties?
```

Browser check:

```text
getComputedStyle(document.querySelector('.card')).backdropFilter
```

It must return `blur(22px) saturate(1.8)`, not `none`.

The environment sets `omit=dev` (`NODE_ENV=production`). A plain `npm install <pkg>` in `vocarium-ui/` prunes dev dependencies such as tsc and vite. Always use:

```bash
npm install --include=dev
```

## Common commands

```bash
# Start / stop stack
docker compose up -d
docker compose down

# Pick up volume-mounted Python changes (server.py, main.py, etc.)
docker compose restart <service>

# Rebuild after Dockerfile / dependency changes
docker compose build <service> && docker compose up -d <service>

# Logs
docker compose logs -f <service>
docker logs --since 60s <container> --tail 30

# UI dev (inside vocarium-ui/)
npm run dev        # Vite dev server
npm run build      # tsc -b && vite build — required before `docker compose build vocarium-ui`

# Health + model checks
curl http://localhost:8280/api/health
curl http://localhost:8280/api/queue/status
curl http://localhost:8208/health          # OmniVoice (GPU 0, resident)
curl http://localhost:8205/v1/models       # Kikiri voice bank (CPU)
curl http://localhost:8210/health          # Whisper STT

# Manual ASR unload (free GPU 0 for TTS)
curl -X POST http://localhost:8210/unload

# Quick TTS test through the gateway (OpenAI-compatible)
curl -s -X POST http://localhost:8280/v1/audio/speech -H 'Content-Type: application/json' \
  -d '{"input":"Hallo, das ist ein Test","voice":"default","response_format":"wav"}' -o /tmp/test.wav

# Backend tests (runs inside the API image; the host has no pytest)
scripts/run-api-tests.sh

# UI lint
cd vocarium-ui && npm run lint
```

### Access control

- `VOCARIUM_ADMIN_USERS` (comma-separated) is the single admin list for gateway admin routes and Hörspiele settings. Empty means nobody is admin; the compose default is empty, `.env` sets the real account.
- `VOCARIUM_PROXY_SECRET`: when set, `Remote-User` counts only with matching `X-Vocarium-Proxy-Secret`. `vocarium-ui`'s Nginx adds it via envsubst in `nginx.conf.template`; the API reads the env var or `/app/data/proxy-secret`. A spoofed header without the secret is treated as no header.
- `ALLOW_ANONYMOUS` and `CORS_ORIGINS` default to off in compose; `.env` turns anonymous access back on for the internal Sub-Wave proxy.
- Hörspiele settings (KI-Profile, Zugänge, Integrationen) are instance-wide infrastructure settings and admin-only.

## Service notes

- **vocarium-api**: FastAPI on port 8280. Auth uses Authelia `Remote-User`; users are auto-created. SQLite runs in WAL mode at `/app/data/vocarium.db`.
- Gateway routes: OpenAI-style `/v1/audio/speech`, `/v1/models`, and ElevenLabs-style `/api/generate`, `/api/generate/stream` (SSE), `/api/voices`, `/api/voices/clone`, `/api/transcribe`, `/api/music/generate`. Text over 200 chars is auto-chunked with 400ms silence.
- **Podcast endpoints** (`/api/podcasts/*`): CRUD, SSE script generation, timeline audio generation, and downloadable/range-streamed audio.
- **Podcast nonverbal tags:** prompt targets roughly one OmniVoice tag per three segments (`tag_target` by duration, catalogue in `podcast/tags.py`); `tags.enrich_tags()` converts written-out sentence-start interjections into tags; Kikiri strips tags via `sanitize()`.
- **Podcast LLM path (2026-09-01):** Z.AI/Zhipu models get `thinking: {type: disabled}` from `podcast/llm_client.py:_provider_extras` (opt back in with `LLM_ENABLE_THINKING=true`). With thinking on, GLM-5.3-flash took >10 min for a medium script and Nginx cut silent SSE at 600 s; without it 2:49. Both SSE routes (`script/generate`, `audio/generate`) emit `: keepalive` comments every 15 s. If a source was ingested without embeddings, `ScriptGenerator._select_relevant_chunks` embeds chunks on demand (not persisted, ~30 s for 34 chunks).
- **Settings endpoints** (`/api/settings/llm-providers`, `/api/settings/llm-providers/{id}/active`): per-user LLM API URL, model, and key; active provider resolves at request time.
- **Music removed:** ACE-Step has no Compose service and music generation is removed from the UI. `MUSIC_URL` defaults to empty, so `AudioAssembler` skips manually added `music` segments and `/api/music/*` returns 503 rather than a DNS error. The script generator does not produce `music` segments.
- **vocarium-ui**: React 18, TypeScript, Vite 6, Tailwind 4, Framer Motion, served by unprivileged Nginx (`nginxinc/nginx-unprivileged`, uid 101). three.js loads lazily as its own chunk. ESLint flat config in `eslint.config.js`; `noUnusedLocals`/`noUnusedParameters` are on.
- **Non-root containers:** `vocarium-api` and `kikiri-tts` run as uid 1000, `vocarium-ui` as uid 101. `vocarium-data` and `voices-data` must be owned by 1000:1000. `omnivoice-tts` and `whisper-stt` still run as root because their CUDA base image and HF caches were not migrated.

## Persistent data

- `voices-data`: shared `/app/voices` data for voice metadata, cached reference embeddings, and prebuilt speaker prompts.
- `vocarium-data`: `/app/data/vocarium.db`, generated podcast assets, and uploads.
- Root `models/` bind-mounted as `/models`, `HF_HOME` of `whisper-stt`: ~11 GB Whisper HF cache plus CTranslate2 conversions under `models/ct2/` and `models/ct2-vanilla/`. Do not delete it unless you accept re-downloading large-v3 and reconverting Flix.
- `${OMNIVOICE_HF_CACHE}` (`/mnt/cache/AI/models/omnivoice-cache`): OmniVoice weights.

## Reference docs in repo

- `README.md`: public project overview, installation, quickstart, configuration.
- `VOCARIUM_API_GUIDE.md`: client API reference.
- `AGENTS.md`: coding-agent conventions, tests, debugging workflow, and common gotchas.

## Unrelated

Everything that was not OmniVoice, Whisper or Kikiri was deleted on 2026-09-01: `qwen3-tts/`, `acestep/`, `vibevoice-api/`, `ab-sfx/`, the agent worktrees and the trainer weights. `kikiri-trainer/` keeps only its scripts; weights under `base/` and `library/` must be re-fetched before manual use.

The Sub-Wave stack reaches the gateway through `sub-wave-vocarium-openai-proxy` (config at `/mnt/user/appdata/subwave/vocarium-openai-proxy.conf`), which sends `Remote-User: zwaetschge` plus the proxy secret to `vocarium-api:8280`.
