# CLAUDE.md

This file provides guidance to Claude Code (Claude.ai/code) when working with code in this repository.

## What this repo is

Self-hosted, ElevenLabs-compatible voice platform named **Vocarium**. A Docker Compose stack that orchestrates a central FastAPI gateway (`vocarium-api`), a React/Vite UI, and four GPU inference workers (TTS, ASR, music, SFX). A second TTS replica (`qwen3-tts-2`) and a second API (`vocarium-api-2`) are gated behind the `dual-gpu` Compose profile and only start when explicitly enabled. Inter-service traffic runs on the `voice-network` bridge; public traffic lands on the gateway.

The stack also includes a **Podcast Studio** (`/podcasts` endpoints): an LLM-driven script generator that turns uploaded documents into a multi-speaker dialogue script, then renders the script to audio with optional failover to the second TTS replica.

## Architecture

```
UI (3100) ──► vocarium-api (8280) ──► gpu_queue ──┬─► qwen3-tts     (8880 internal, 8201 host)
                   │                               ├─► qwen3-tts-2   (dual-gpu profile only)
                   │                               ├─► qwen3-asr     (8000 internal, 8200 host)
                   │                               ├─► acestep       (8003 internal, 8203 host)
                   │                               └─► mmaudio       (8004 internal, 8204 host)
                   └── SQLite at /app/data/vocarium.db (users, voices, podcasts, benchmarks, hosts, llm_providers)
                   └── Shared voices volume: /app/voices (custom voice metadata + ref audio)
```

Inter-container URLs use service names on `voice-network`: `http://qwen3-tts:8880`, `http://qwen3-asr:8000`, `http://acestep:8003`, `http://mmaudio:8004`. `TTS_URL_2` is empty by default; in dual-GPU mode it's set to `http://qwen3-tts-2:8880`.

### GPU coordination (non-obvious)

GPU placement is env-driven (`GPU_TTS_1`, `GPU_TTS_2`, `GPU_ASR`, `GPU_MUSIC`, `GPU_SFX`). Defaults put everything on GPU 0; idle-unload timeouts are what make that viable.

- **Single-GPU (default)**: All workers target GPU 0. `qwen3-tts` uses `IDLE_TIMEOUT=120` so sequential podcast segments reuse the loaded model without a 20s reload, while still freeing VRAM after idle so ASR/music/SFX can claim the GPU. `qwen3-asr` runs as a lazy-start proxy (`asr_proxy.py`) — spins up vLLM on first `/v1/models` request, unloads after `IDLE_TIMEOUT=300s`. ACE-Step/MMAudio are similarly lazy; they request TTS eviction before starting, mutually exclude each other, and both unload after 600s idle.
- **Emergency fallback (opt-in via `COMPOSE_PROFILES=dual-gpu` + `TTS_URL_2=http://qwen3-tts-2:8880`)**: `qwen3-tts-2` is available on GPU 1 (`GPU_TTS_2=1`), but every API replica always tries the RTX 3060 service first. The RTX 5060 Ti is contacted only after a real primary TTS request failure. ASR/music/SFX remain pinned to GPU 0.
- **`vocarium-api/gpu_queue.py`** is an env-aware FIFO coordinator that serializes GPU work. It reads `GPU_TTS_PRIMARY`, `GPU_TTS_EXTRA`, `GPU_ASR`, `GPU_MUSIC`, and `GPU_SFX`; before running a job it unloads services assigned to the same GPU.
- **Podcast TTS uses one queue job per audio render, not one per segment** — generated music/SFX assets are rendered first through `gpu_queue`; then the speech/reaction phase runs inside a single `tts` queue job. `VocariumTTSGenerator` always sends segments to `qwen3-tts` on the RTX 3060 first and only retries a failed request on optional `qwen3-tts-2`.

### Podcast Data Flow

1. User uploads source documents (PDF/TXT) → chunks via Docling API
2. User creates podcast → assigns hosts (preset or custom) with `voice_id` (must be `source='custom'`)
3. `POST /api/podcasts/{id}/script/generate` → LLM generates multi-speaker script (SSE streamed). Segments have `speaker`, `text`, `type` (`speech`|`reaction`|`pause`|`sfx`|`music`), `voice`, optional `notes` (steers TTS emotion via `instruct`), and optional timeline fields (`overlap_ms`, `prompt`, `duration_ms`, `volume_db`).
4. `POST /api/podcasts/{id}/audio/generate` → `AudioAssembler` renders generated music/SFX first, synthesizes speech/reactions in parallel, then mixes all files on a timeline with ffmpeg `filter_complex`/`amix` plus EBU R128 loudness normalisation.
5. Audio file is served via `/api/podcasts/{id}/audio/download` (with Content-Disposition filename) or `/audio/stream` (HTTP Range support for scrubbing).

### Multi-tenancy

EVERY DB query filters by `user_id`. Custom voices are isolated per user. The TTS voice resolver (`VocariumTTSGenerator._resolve_voice`) enforces `source='custom'` for podcast TTS. Hosts and LLM providers are also per-user. The UI's `Remote-User` header (set by Authelia) identifies the caller.

### Volume-mounted source (critical for dev workflow)

These files are mounted read-only into containers and picked up on `restart` — **no rebuild needed**:
- `qwen3-tts/server.py`
- `qwen3-tts-2/` shares the same `./qwen3-tts/server.py` mount
- `vocarium-api/{main.py,database.py,gpu_queue.py,podcast/*.py}`
- `acestep/proxy.py`
- `asr_proxy.py` and `entrypoint.sh`

Rebuild is only required when Dockerfiles, `package.json`, or `requirements.txt` change, or for the UI (built statically into the Nginx image).

#### CRITICAL: Read-only mounts + Python `__pycache__` poisoning

When `:ro` mounts prevent Python from writing `.pyc`, the interpreter may **silently load stale cached bytecode** even when the source `.py` is newer. This produces "fix didn't work" symptoms that are extremely hard to debug.

**Prevention in this repo:**
- `PYTHONDONTWRITEBYTECODE=1` is set in `docker-compose.yml` for both `vocarium-api` and `qwen3-tts` services
- `tmpfs` mounts cover `/app/__pycache__` and `/app/podcast/__pycache__` so stale image-layer bytecode cannot shadow mounted source

**How to verify your change was picked up:**
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
docker exec qwen3-tts curl -s http://localhost:8880/v1/models

# Manual model management (TTS)
curl -X POST http://localhost:8201/v1/models/load -H 'Content-Type: application/json' -d '{"model_id":"1.7b-custom"}'
curl -X POST http://localhost:8202/v1/models/load -H 'Content-Type: application/json' -d '{"model_id":"1.7b-custom"}'
curl -X POST http://localhost:8201/unload
curl -X POST http://localhost:8202/unload

# Manual ASR unload (free GPU 0 for TTS)
curl -X POST http://localhost:8200/unload

# Direct TTS health + quick test
curl http://localhost:8201/health
curl -s -X POST http://localhost:8201/v1/audio/speech/custom -H 'Content-Type: application/json' \
  -d '{"text":"Hallo, das ist ein Test","speaker":"Vivian","language":"German","response_format":"wav"}'
```

## Service notes

- **qwen3-tts & qwen3-tts-2** — Both are the same image but on different GPUs. Expose three model checkpoints: `1.7b-base` (voice cloning via `generate_voice_clone`), `1.7b-design` (`generate_voice_design` with `instruct`), and `1.7b-custom` (`generate_custom_voice` with prebuilt speakers + optional `instruct` steering). The podcast uses `1.7b-custom`. Both use `ATTN_IMPL=eager`. Base/clone CUDA graphs default to `TTS_ENABLE_CUDA_GRAPHS=auto`: enabled on RTX 30/40 GPUs, disabled on RTX 50xx/Blackwell. Base/clone generation must use `non_streaming_mode=True`; without it, short German prompts can repeat or stop early. The server wraps blocking inference in `run_in_executor` to keep the asyncio event loop unblocked for concurrent request queuing. `DEFAULT_MODEL` is `1.7b-base`, but podcast generation forces `1.7b-custom` via `ensure_model()`.
- **qwen3-asr** — Uses the official `qwenllm/qwen3-asr` image with a custom `entrypoint.sh` launching `asr_proxy.py`. The proxy reads free VRAM via `nvidia-smi` and sets vLLM's `gpu_memory_utilization` dynamically so it doesn't collide with TTS.
- **vocarium-api** — FastAPI, port 8280. Auth via `Remote-User` header (set upstream by Authelia); users auto-created. DB is SQLite in WAL mode at `/app/data/vocarium.db`. Exposes OpenAI-style `/v1/audio/speech`, `/v1/models` and ElevenLabs-style `/api/generate`, `/api/generate/stream` (SSE), `/api/voices`, `/api/voices/clone`, `/api/transcribe`, `/api/music/generate`, `/api/sfx/generate`. Text longer than 200 chars is auto-chunked with 400ms silence between chunks.
  - **Podcast endpoints** (`/api/podcasts/*`): Full CRUD, script generation (SSE with real-time progress), audio generation (timeline mixing with optional music/SFX overlays and TTS failover when configured), audio download/stream with Range support.
  - **Settings endpoints** (`/api/settings/llm-providers`, `/api/settings/llm-providers/{id}/active`): Per-user LLM provider configuration (API URL, model, key). Active provider resolved at request time.
- **acestep** — Music generation via cloned ACE-Step repo, uses `uv` for deps, CUDA 12.6.
- **mmaudio** — SFX from text, CUDA 12.6-devel + PyTorch cu124.
- **vocarium-ui** — React 18 + TypeScript + Vite 6 + Tailwind 4 + Framer Motion. Served by Nginx. Pages: Speech, Voices, Clone, Design, Music, SoundEffects, Benchmark, Transcribe, PodcastStudio, Settings.

## Persistent data

- `voices-data` — shared between `qwen3-tts`, `qwen3-tts-2` (`/app/voices`) and `vocarium-api` (`/app/voices`): voice metadata, cached reference embeddings, prebuilt speaker prompts (9 per user: Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee).
- `vocarium-data` — `/app/data/vocarium.db` SQLite + generated assets (podcast audio, sfx, uploads). Both API replicas mount this same volume in dual-GPU mode.
- `qwen3-tts/models/` — HF model cache (bind-mounted, not a named volume). Shared model weights between the two TTS containers.
- `acestep-cache`, `acestep-output`, `mmaudio-cache` — named volumes for HF caches.

## Reference docs in repo

- `README.md` — public-facing project overview, install/quickstart, configuration.
- `VOCARIUM_API_GUIDE.md` — API reference for client integrations.
- `AGENTS.md` — coding-agent-specific conventions (testing, debugging workflows, common gotchas).

## Unrelated

- `vibevoice-api/` has its own `docker-compose.yml` and is a separate, independent stack — not part of this Compose project.
- `models/` at the repo root is the shared HF cache mount for `qwen3-asr`.

<!-- webui-managed: project-context:start -->
# Project: voxtral

Vocarium

## Tech Stack
Docker Compose

## Key Directories
scripts/

Available Skills: agent-team-orchestration, android-build, api-design, auto-researcher, book-promo-website, brainstorming, campaign-architect, codex-prompt-rewriter, comfyui-asset-gen, data-visualization, debugging-playbook, decensor-engine, developmental-editor, devops-deploy, dispatching-parallel-agents, documentation-writer, e-ink-application-architecture, e-ink-device-qa, e-ink-rendering-performance, epub-forge, executing-plans, fallacy-finder, finishing-a-development-branch, frontend-design, game-accessibility-localization, game-ai-behavior, game-android-porting, game-animation-state-machines, game-art-direction, game-asset-pipeline, game-audio-haptics, game-automated-qa, game-backend-services, game-build-ci, game-cinematics-camera, game-controller-input, game-core-architecture, game-economy-monetization, game-engine-godot, game-engine-unity, game-engine-unreal, game-feel-motion, game-first-person, game-gameplay-combat, game-level-content, game-metroidvania, game-mobile-performance, game-mod-support, game-multiplayer-networking, game-narrative-dialogue, game-on-device-playtest, game-physics-collision, game-platform-compliance, game-platformer, game-procedural-generation, game-racing-vehicle, game-release-builds, game-roguelike, game-rpg-quest, game-save-progression, game-shader-vfx, game-steam-deck-publishing, game-survival-crafting, game-systems-design, game-tactics-strategy, game-third-person, game-tools-editor, game-top-down-action, game-touch-input, game-ui-hud-menus, game-world-streaming, idea-forge, idea-to-code-plan, literary-critique, mental-reflection, musical-architect, musical-composer, nano-banana-prompt-engineer, openai-image-gen, panel-transcription, pentest-analyst, performance-tuning, plum-discord-automation-client, plum-discord-qa-gate, plum-discord-supervisor-gateway, premium-frontend-design, prompt-architect, prompt-expander, receiving-code-review, refactor-guide, requesting-code-review, research-prompt-architect, reverse-prompt-engineer, ricardo-marketplace-discovery-liquidity, ricardo-marketplace-frontend-design, ricardo-marketplace-payments-shipping-disputes, ricardo-marketplace-product-architect, ricardo-marketplace-trust-safety, schreiner-planer-kontext, screenplay-to-novel, security-review, session-handover, skill-designer, social-navigator, storysmith-60, strudel-livecode, subagent-driven-development, suno-v5-songwriter, svg-expert, systematic-debugging, test-driven-development, testing-playbook, thinker-frameworks, tutorial-architect, unraid-array-health, unraid-backup-recovery, unraid-cache-filesystem-maintenance, unraid-docker-appdata-maintenance, unraid-security-network-maintenance, using-git-worktrees, using-superpowers, verification-before-completion, visual-prompt-architect, vocarium-audio-api, web-game-polish, writing-plans
Available Agents: agent-team-strategist, api-designer, backend-dev, comfyui-asset-gen, data-engineer, database-specialist, debugging-expert, devops-engineer, documentation-writer, Explore, frontend-developer, fullstack-dev, game-accessibility-localization-specialist, game-ai-behavior-engineer, game-android-porting-engineer, game-animation-state-engineer, game-art-asset-director, game-asset-pipeline-producer, game-audio-haptics-engineer, game-automated-qa-engineer, game-backend-services-engineer, game-build-ci-engineer, game-cinematics-camera-director, game-combat-engineer, game-controller-input-engineer, game-core-architect, game-economy-monetization-designer, game-feel-motion-engineer, game-first-person-engineer, game-godot-engineer, game-level-content-designer, game-liveops-telemetry-engineer, game-metroidvania-architect, game-mobile-performance-engineer, game-mod-support-engineer, game-multiplayer-network-engineer, game-narrative-dialogue-designer, game-on-device-playtester, game-physics-collision-engineer, game-platform-compliance-producer, game-platformer-engineer, game-procedural-generation-architect, game-racing-vehicle-engineer, game-release-build-engineer, game-roguelike-architect, game-rpg-quest-designer, game-save-progression-engineer, game-shader-vfx-engineer, game-steam-deck-release-producer, game-survival-crafting-designer, game-systems-designer, game-tactics-strategy-architect, game-third-person-engineer, game-tools-editor-engineer, game-top-down-action-engineer, game-touch-input-designer, game-ui-hud-engineer, game-unity-engineer, game-unreal-engineer, game-world-streaming-engineer, git-operations, mobile-developer, performance-optimizer, Plan, playtest-qa-engineer, release-manager, research-bot, security-auditor, system-architect, test-engineer, ui-designer, unraid-array-health, unraid-backup-recovery, unraid-cache-filesystem, unraid-docker-appdata, unraid-security-network, web-game-engineer
<!-- webui-managed: project-context:end -->
