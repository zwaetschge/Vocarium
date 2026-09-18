# Vocarium

Self-hosted voice platform with an ElevenLabs-compatible API. Runs entirely
on your hardware — no per-character billing, no upload of recordings to a
third party. One Docker Compose stack gives you:

- **TTS** — OmniVoice zero-shot voice cloning on the GPU (a few seconds of
  reference audio per voice) plus a CPU bank of German Kokoro fine-tunes and
  Piper presets (Kikiri) that keeps speech flowing when the GPU is busy.
- **ASR** — faster-whisper `large-v3` (profile `german`) and a Swiss German
  fine-tune (profile `swiss`) for transcription and cloning references.
- **Hörbücher** — upload PDF/EPUB/DOCX/TXT, get chapter segmentation, a
  reader with live synthesis, M4B/MP3 export and a persistent render queue.
- **Podcast Studio** — turn an uploaded document into a multi-host dialogue
  script and render it to audio.
- **Hörspiele** — turn a series episode (Plex) plus its book into a narrated
  audio drama: transcript alignment, LLM-written narration, sample-accurate
  render (ported from Szenenklang).
- **React UI** for all four areas, plus per-user LLM provider settings.

The API is OpenAI-style on the inference path (`/v1/audio/speech`,
`/v1/audio/transcriptions`, `/v1/voices`) and ElevenLabs-style on the
management path (`/api/voices/clone`, `/api/generate`, `/api/transcribe`).

---

## Screenshots

**Text to Speech** — pick a voice, paste text, generate or stream.
![Speech](docs/screenshots/speech.png)

**Voice Library** — every cloned, designed and prebuilt voice in one place.
![Voice Library](docs/screenshots/voice-library.png)

**Podcast Studio (beta)** — turn a PDF into a multi-voice scripted podcast.
![Podcast Studio](docs/screenshots/podcast-studio.png)

---

## Hardware

- **GPU**: NVIDIA, ≥ 8 GB VRAM. **One GPU is the default** — OmniVoice
  stays resident (~2 GB), Whisper loads lazily and unloads after
  `WHISPER_IDLE_TIMEOUT`. Everything is pinned to GPU 0 by `GPU_*` in `.env`.
- Tested on **RTX 3060 (12 GB)**.
- **CPU/RAM**: 8 cores, 32 GB RAM is comfortable (Kikiri and the embedding
  service run on the CPU).
- **Disk**: ~20 GB for model weights + working space for generated audio.

---

## Quick start

```bash
git clone https://github.com/zwaetschge/Vocarium.git
cd Vocarium
./scripts/install.sh
```

That's it. The installer:
1. Verifies Docker, Compose v2, and the NVIDIA Container Toolkit.
2. Copies `.env.example` → `.env` (review it before re-running).
3. Pulls the Whisper and embedding weights into `./models/` and
   `llama-embeddings/models/`; OmniVoice fetches its weights on first start.
4. Builds and starts the containers.
5. Smoke-tests the API.

When it finishes, open **http://localhost:3100**.

### Without the installer

```bash
cp .env.example .env                  # adjust if needed
./scripts/download-models.sh          # one-time, ~10 GB
docker compose up -d
```

For production deployments behind a reverse proxy:

```bash
CORS_ORIGINS=https://vocarium.example.com \
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The `prod` overlay drops the host port mappings of the API and internal
GPU services, disables anonymous API fallback by default, and requires an
explicit `CORS_ORIGINS` value. Put your auth/reverse proxy on the
`voice-network` bridge if it needs to talk to `vocarium-api` directly.

---

## Configuration

Everything is in `.env`. Highlights:

| Variable           | Default        | Notes                                                     |
|--------------------|----------------|-----------------------------------------------------------|
| `VOCARIUM_UI_PORT` | `3100`         | Public UI port.                                           |
| `VOCARIUM_API_PORT`| `8280`         | Gateway API.                                              |
| `GPU_TTS_1`        | `0`            | GPU index for OmniVoice.                                  |
| `GPU_ASR`          | `0`            | GPU index the queue bills STT (whisper-stt) against.      |
| `WHISPER_IDLE_TIMEOUT` | `300` s    | Whisper unloads after this idle period.                   |
| `DEFAULT_TTS_ENGINE` | `omnivoice`  | Decides ties and the built-in `default` voice (`kikiri` reverses it). |
| `ALLOW_ANONYMOUS`  | `false`        | Route header-less requests to a shared `api` user.        |
| `VOCARIUM_ADMIN_USERS` | (empty)    | Comma-separated admins (gateway admin routes + Hörspiele settings). |
| `VOCARIUM_PROXY_SECRET` | (empty)   | Shared secret proving `Remote-User` came through `vocarium-ui`. |
| `CORS_ORIGINS`     | (empty)        | Only needed if a foreign origin calls the API directly.   |
| `MAX_VOICE_UPLOAD_BYTES` | `52428800` | Max reference-audio upload size.                         |
| `MAX_TRANSCRIBE_UPLOAD_BYTES` | `524288000` | Max STT upload/download size.                   |
| `MAX_TTS_TEXT_CHARS` | `20000`      | Max request text length for TTS endpoints.                |
| `LLM_API_URL` etc. | (empty)        | Required for **Podcast Studio**; see below.               |
| `PLEX_BASE_URL`, `CODEX_AGENT_BASE_URL` | (empty) | Required for **Hörspiele**; see `.env.example`. |

### Authentication

Vocarium is multi-tenant: every voice and podcast is scoped to a user.
Identity is read from the `Remote-User` (Authelia) or `X-Forwarded-User`
(oauth2-proxy, Cloudflare Access) header. Recommended deployment:

```
client → reverse proxy (auth) → vocarium-ui:3000 → vocarium-api:8280
```

For a quick local single-user setup, set `ALLOW_ANONYMOUS=true`. Every
header-less request is then routed to a shared `api` user. **Do not use
this in any deployment exposed to the internet.**

Because any container on the Docker network could set `Remote-User` itself,
set `VOCARIUM_PROXY_SECRET` in `.env`: the UI's Nginx then attaches it as
`X-Vocarium-Proxy-Secret` and the API ignores identity headers that arrive
without it. Admin rights (gateway admin routes, Hörspiele settings) come only
from `VOCARIUM_ADMIN_USERS`; an empty list means nobody.

### Podcast Studio (beta, optional)

> **Beta:** the podcast pipeline produces watchable output but the script
> prompts, pacing and voice-routing are still being iterated on. Quality
> varies with the input document and the LLM you point it at.

The podcast pipeline depends on three external services that you supply:

- **LLM** — any OpenAI-compatible chat-completions endpoint (vLLM,
  llama.cpp, an OpenAI key, etc.).
- **Embeddings** — shipped in the stack: the `embeddings` service runs
  `bge-m3` on the CPU via llama.cpp. Fetch the weights once with
  `scripts/download-embedding-model.sh`, or point `EMBEDDING_API_URL` at any
  other OpenAI-compatible embedding endpoint.
- **Docling** — the [`docling-serve`](https://github.com/DS4SD/docling)
  HTTP service for PDF/document text extraction.

Set `LLM_API_URL`, `EMBEDDING_API_URL`, `DOCLING_API_URL` (and friends) in
`.env`. Leave them empty to disable the Podcast Studio; the rest of
Vocarium works without them.

---

## Architecture

```
   browser ─► vocarium-ui:3000 (Nginx/React)
                    │
                    ▼
              vocarium-api:8280 (FastAPI)
                    │
                    ├──► omnivoice-tts    (port 8880) — GPU 0, resident, cloned voices
                    ├──► kikiri-tts       (port 8881) — CPU, Kokoro fine-tunes + Piper presets
                    ├──► whisper-stt      (port 8000) — GPU 0, lazy, self-unloading
                    ├──► embeddings       (port 8080) — CPU, bge-m3 via llama.cpp
                    └──► hoerspiele-agent (port 8090) — Hörspiele LLM runner (vocarium-agent/)
```

- **GPU coordination** is handled by `vocarium-api/gpu_queue.py` (FIFO with
  per-job kind; conflicting models are evicted before the next job runs).
- **Whisper** loads on first request and unloads after
  `WHISPER_IDLE_TIMEOUT` seconds — this is what makes single-GPU operation
  viable next to the resident OmniVoice model.
- **Kikiri** never touches the GPU queue, so speech keeps flowing while the
  GPU transcribes.

See `CLAUDE.md` for the full architecture write-up and `AGENTS.md` for
non-obvious gotchas (pycache poisoning with read-only mounts, SSE
buffering pitfalls, …).

---

## Common operations

```bash
# Logs
docker compose logs -f vocarium-api
docker compose logs -f omnivoice-tts

# Restart a service after editing a volume-mounted .py file
docker compose restart vocarium-api      # or whisper-stt, omnivoice-tts, …

# Rebuild after Dockerfile / requirements change
docker compose build vocarium-api && docker compose up -d vocarium-api

# Health
curl http://localhost:8280/api/health
curl http://localhost:8280/api/queue/status

# Free GPU 0 by unloading Whisper by hand
curl -X POST http://localhost:8210/unload

# Quick TTS test through the gateway
curl -s -X POST http://localhost:8280/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input":"Hallo Welt","voice":"default","response_format":"wav"}' \
  -o test.wav

# Backend tests (inside the API image) and UI lint
scripts/run-api-tests.sh
(cd vocarium-ui && npm run lint)
```

---

## Reference docs in the repo

- [`VOCARIUM_API_GUIDE.md`](VOCARIUM_API_GUIDE.md) — API reference for
  client integrations.
- [`CLAUDE.md`](CLAUDE.md) — architecture overview (also the briefing for
  AI coding assistants).
- [`AGENTS.md`](AGENTS.md) — engineering conventions and known gotchas.

---

## Models & licenses

Vocarium ships **no model weights**. The installer pulls them from
HuggingFace at install time. Each model has its own license — review them
before any commercial use:

| Model              | License page                                                                                              |
|--------------------|-----------------------------------------------------------------------------------------------------------|
| OmniVoice          | https://huggingface.co/k2-fsa/OmniVoice                                                                   |
| Kokoro (Kikiri)    | https://huggingface.co/hexgrad/Kokoro-82M                                                                 |
| Piper voices       | https://huggingface.co/rhasspy/piper-voices                                                               |
| faster-whisper     | https://huggingface.co/Systran/faster-whisper-large-v3                                                    |
| Flix Swiss German  | https://huggingface.co/Flix-AI/flix-swissgerman-full                                                      |
| bge-m3 (GGUF)      | https://huggingface.co/BAAI/bge-m3                                                                        |

Vocarium itself is MIT-licensed — see [LICENSE](LICENSE).

---

## Troubleshooting

**`nvidia runtime not found`**
Install the NVIDIA Container Toolkit and restart Docker:
https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html

**`fix didn't work` after editing a volume-mounted `*.py`**
Stale `__pycache__` from before `PYTHONDONTWRITEBYTECODE=1` was set.
Inspect with: `docker exec vocarium-api python -c "import sys, inspect, podcast.routes; print(inspect.getsourcefile(podcast.routes))"`

**Podcast Studio shows "no LLM provider configured"**
Either set `LLM_API_URL` in `.env` and restart, or open Settings in the UI
and add a provider there (per-user, persisted to SQLite).

**Stack runs but UI is empty**
The UI is built statically into the Nginx image. After editing
`vocarium-ui/src/`, re-run `docker compose build vocarium-ui &&
docker compose up -d vocarium-ui`.

---

## Contributing

PRs welcome. Before pushing changes:
- run `docker compose build` for the affected service,
- restart it,
- verify `/api/health` and a representative endpoint still respond.

Architecture changes belong in `CLAUDE.md`; new gotchas / non-obvious
operational notes go in `AGENTS.md`.
