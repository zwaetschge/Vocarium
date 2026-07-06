# F5-TTS German Integration Design

## Goal

Add an opt-in F5-TTS German engine to Vocarium so cloned German voices can be benchmarked against Qwen3-TTS without destabilizing the existing Qwen path.

## Context

Vocarium currently routes OpenAI-compatible TTS through `vocarium-api/main.py` to one or two Qwen workers at `TTS_URL` and `TTS_URL_2`. Voice clone metadata and reference audio live in the shared `voices-data` volume under `/app/voices`. Qwen remains the default production engine.

F5-TTS is worth testing because current community German checkpoints exist and the official F5-TTS shared model list includes `hvoss-techfak/F5-TTS-German`, a German checkpoint trained on Common Voice 19.0 plus additional crowdsourced data. The HPI `aihpi/F5-TTS-German` checkpoint is also German, but community reports indicate it may be sensitive to newer F5-TTS package versions. The integration must therefore be isolated and configurable.

## Architecture

Create a separate `f5-tts` FastAPI worker rather than mixing F5 dependencies into `qwen3-tts/server.py`. The worker mounts the same `voices-data` volume read/write, exposes Qwen-compatible endpoints where practical, and lazily loads F5 only on the first real inference request.

The API gateway keeps Qwen as default and routes to F5 only when a request explicitly asks for it via `engine: "f5"` or `model: "f5-german"`. `/api/models` and OpenAI-compatible model listing expose `f5-german` as an available opt-in model when `F5_TTS_URL` is configured.

## Worker Interface

`f5-tts/server.py` exposes:

- `GET /health`: returns import/load state, model repo, package version hint, loaded voices, and first-load download hints.
- `POST /unload`: unloads model state and clears CUDA memory.
- `GET /v1/models`: returns `f5-german`.
- `GET /v1/voices`: scans `/app/voices` metadata.
- `POST /v1/voices/register`: stores normalized reference audio and transcript, matching the existing Qwen worker contract.
- `DELETE /v1/voices/{voice_id}`: removes cached voice state.
- `POST /v1/audio/speech`: accepts OpenAI-like `{input,text,voice,response_format,engine,model}` and returns audio.

## First Implementation Scope

The first pass focuses on integration quality and benchmark readiness:

- Configurable `F5_MODEL_REPO`, default `hvoss-techfak/F5-TTS-German`.
- Configurable `F5_MODEL_NAME`, default `F5TTS_Base`.
- Configurable `F5_CKPT_FILE` and `F5_VOCAB_FILE`, defaulting to the official F5 shared German checkpoint paths.
- Lazy import of `f5_tts.api.F5TTS` so `/health` works even before dependencies are installed.
- Deterministic defaults: fixed seed unless request overrides.
- Reference audio normalized to 24 kHz mono PCM WAV like the Qwen path.
- Clear HTTP errors if F5 dependencies are unavailable, the model fails to load, or a requested clone voice has no reference audio.

## Gateway Behavior

`vocarium-api/main.py` adds:

- `F5_TTS_URL`, optional.
- `SUPPORTED_TTS_ENGINES = {"qwen", "f5"}` when F5 is configured.
- Engine selection helper:
  - `engine=f5` routes to `F5_TTS_URL`.
  - `model=f5-german` routes to `F5_TTS_URL`.
  - otherwise Qwen remains default.
- Voice registration forwards clone voices to F5 only when `F5_TTS_URL` is configured.
- GPU unloaders include F5 as a TTS-adjacent service when configured.

## Docker Behavior

Add a profile-gated Compose service:

- service name: `f5-tts`
- profile: `f5`
- port: `127.0.0.1:${F5_TTS_PORT:-8205}:8885`
- GPU: `${GPU_F5_TTS:-0}`
- mounts: `./f5-tts/server.py`, `./f5-tts/models`, `voices-data`

The service is opt-in so the default stack does not download extra models or consume VRAM.

## Testing

Tests cover behavior without requiring model downloads:

- F5 engine routing chooses `F5_TTS_URL`.
- `model=f5-german` implies F5.
- F5 disabled rejects explicit `engine=f5`.
- Clone registration includes configured F5 URL.
- F5 worker health works without model import.
- F5 worker returns a clear 503 when inference is requested without installed F5 dependencies.

Real GPU benchmark is a separate verification step after build:

- Generate identical German sentences with Qwen and F5.
- Save WAVs under a benchmark output directory.
- Report load time, generation time, audio duration, RTF, and file size.

## Risks

- The default German F5 checkpoint is CC-BY-NC-4.0 and therefore not suitable for commercial production unless licensing is acceptable.
- Community reports indicate some German checkpoints, especially `aihpi/F5-TTS-German`, can break with newer F5-TTS versions, so the Dockerfile must pin dependencies or make them adjustable.
- F5 output quality may be better or worse than Qwen depending on reference audio. The benchmark must compare actual Vocarium default voice and user clone voices, not generic demos.
