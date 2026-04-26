# AGENTS.md — Coding-agent conventions for Vocarium

## Scope

This file is for AI coding agents assisting with the Vocarium stack. Complements `CLAUDE.md` (architecture overview) with actionable conventions, common pitfalls, and workflow guidance.

## Testing philosophy

- **Test before, not after**: When you change any inference endpoint (`qwen3-tts/server.py`, `asr_proxy.py`, API routes), always run a direct container-level request to verify the model loads and responds. Do not assume "the code looks right" — CUDA/VRAM issues have silent failure modes.
- **Verify your changes were picked up**: After `docker restart` of a volume-mounted service, confirm the running code matches your edits (see `__pycache__` gotcha in CLAUDE.md).

## Common gotchas

### 1. Pycache poisoning (silent failure mode)

When a Python file is `:ro` mounted into a container, the interpreter may load stale `.pyc` instead of newer `.py` — with NO warning. This makes "fix didn't work" the hardest bug to debug.

**Always verify after restart:**
```bash
docker exec vocarium-api python -c "import sys, inspect, podcast.routes; print(sys.dont_write_bytecode); print(inspect.getsourcefile(podcast.routes))"
```

**Prevention:** `PYTHONDONTWRITEBYTECODE=1` and a `tmpfs` mount over `__pycache__` are already configured in `docker-compose.yml`.

### 2. "Unexpected keyword argument" after a refactor

Python resolves methods at **runtime**. If `VocariumTTSGenerator` inherited a `default_voice_for_speaker` from a Mixin that does NOT accept `user_id`, that shadowed version wins silently. The fix: remove the shadowing method or unify signatures.

**Pattern to watch for:** When adding a parameter (e.g., `user_id`) to a protocol method, grep for ALL implementations — including in Mixins, base classes, and test mocks.

### 3. SSE buffering → Gateway timeout

If a FastAPI endpoint returns `StreamingResponse` but collects all events in a list before yielding them, the client sees zero bytes for the entire duration. With a 30-60s browser/proxy timeout, this produces a gateway timeout on long-running operations.

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

### 4. TTS event-loop blocking → cascading SocketTimeoutError

Qwen3-TTS inference is CPU-heavy and **blocks the asyncio event loop**. If the endpoint calls `model.generate_custom_voice()` directly, all concurrent requests queue behind it, and each new request's timeout clock starts counting while it waits.

**Fix:** Wrap inference in `loop.run_in_executor(None, _blocking_inference)` so the event loop stays free to accept connections:
```python
def _gen():
    return model.model.generate_custom_voice(**kwargs)
wavs, sr = await loop.run_in_executor(None, _gen)
```

This was already applied to `/v1/audio/speech`, `/v1/audio/speech/custom`, and `/v1/audio/speech/stream` in `server.py`.

### 5. `model_lock` used for both model state + inference

`threading.Lock` around model load/unload is necessary. But holding it during the entire inference prevents concurrent inference even across different requests. The correct pattern:
1. Lock only for model state changes (load/unload) and counter manipulation
2. Release the lock before inference starts
3. Use a counter (`active_requests`) to prevent unload while inference is running

See `qwen3-tts/server.py` for the implementation with `with model_lock:` around `active_requests += 1` (fast), then inference outside the lock.

### 6. Timeout math: `Semaphore(N)` vs GPU queue depth

With 2 TTS GPUs and ~70s average inference:
- `Semaphore(8)` × 70s = queue depth per GPU of ~4 → 4th request waits ~210s + 70s = 280s → hits `total=300s` timeout on long segments
- `Semaphore(3)` keeps queue depth to ~1 per GPU → safe margin
- aiohttp `total=` must include both wait time + inference time. Use 3600s (1 hour) for long segments.

### 7. Model reload on every segment (`IDLE_TIMEOUT=0`)

`IDLE_TIMEOUT=0` triggers a model unload immediately when `active_requests` reaches zero. With sequential inference, this means every segment triggers a full model load (~14-20s). Fix: `IDLE_TIMEOUT=120` (or higher) so the next segment reuses the loaded model until 120s of idle.

**Watch for** `Loading model: ...` appearing repeatedly in TTS logs — that's the symptom.

### 8. `FasterQwen3TTS.from_pretrained()` crashes on kwargs

This wrapper does NOT accept arbitrary kwargs like `attn_impl`, `trust_remote_code`, or `eos_token_id`. Setting them in the constructor call causes a TypeError. The parameters passed to `from_pretrained` in `server.py` should only be `device`, `dtype`.

### 9. Script re-generation race condition

When "Regenerate Script" is clicked, the GPU Queue processes it asynchronously. If the user clicks "Render Audio" before the script is committed to the database, the audio worker loads the OLD script JSON.

**Fix:** Fresh `script_json` query at the start of the audio generation endpoint:
```python
# Re-load script fresh from DB in case it was just regenerated
row = db.execute("SELECT script_json FROM podcasts WHERE id=?", (podcast_id,)).fetchone()
if row and row[0]:
    fresh_script = json.loads(row[0])
    if fresh_script and fresh_script.get("segments"):
        script = fresh_script
```

### 10. Every DB query must filter by `user_id`

The `voices` and `hosts` tables are multi-tenant. Omitting `user_id=?` in `SELECT` or `UPDATE` leaks data between users. Search for `"WHERE.*user_id"` or `"AND.*user_id"` when reviewing new queries.

## Database schema conventions

### Adding a new entity
1. Add CREATE TABLE in `database.py` `init_db()` (with `ON DELETE CASCADE` where appropriate)
2. Add migration logic in `database.py` for existing DBs (check `sqlite_master` → ALTER TABLE)
3. Add CRUD endpoints in the appropriate router
4. Filter EVERY query by `user_id`

### Seeding prebuilt entities per-user

The custom voices and preset hosts are seeded automatically on first API request (`seed_prebuilt_custom_voices()` and `seed_prebuilt_hosts()`). They use `INSERT OR IGNORE` with user-scoped IDs to avoid duplicates. If you change seed data, existing users won't get new voices — they need to be added via migration or a new seeding trigger.

## Code style

- **Python:** Strict type annotations on public methods. Use `str | None` (Python 3.10+), not `Optional[str]`.
- **Error messages:** User-facing error messages are in English. Internal logs can be English or German.
- **Logging:** Use `logger = logging.getLogger(__name__)` per module. Never `print()` in `routes.py` or production paths.
- **File I/O:** Use `asyncio.to_thread()` or `aiofiles` for disk I/O in async paths when it might block.

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
  && echo "ASR: $(curl -s http://localhost:8200/health | head -c 5)"

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
4. [ ] If modifying SSE/streaming response, verify events are yielded before work completes (not after)
5. [ ] If modifying Docker compose, verify containers still communicate (`docker exec X curl http://Y:PORT/endpoint`)
6. [ ] If modifying timeout values, they are generous enough for the longest realistic operation
7. [ ] `CLAUDE.md` or `AGENTS.md` updated if architecture or conventions changed
