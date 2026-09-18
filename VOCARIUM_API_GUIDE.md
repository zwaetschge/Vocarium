# Vocarium API – Integration Guide

API reference for clients integrating with the Vocarium voice backend.

**Base URL (internal):** `http://vocarium-api:8280` (on the `voice-network` Docker bridge)
**External:** `https://vocarium.example.com` (your reverse proxy)

---

## Authentication

All endpoints require a `Remote-User` header, set by an upstream identity
proxy (Authelia, oauth2-proxy, Cloudflare Access, …). For internal
container-to-container calls you must set the header explicitly.

```
Remote-User: <username>
```

`X-Forwarded-User` is accepted as a fallback. Users are auto-created on
first request. If you set `ALLOW_ANONYMOUS=true` (default for single-user
setups), missing-header requests are routed to a shared `api` user.

### `GET /api/auth/me`
Returns the authenticated user.

**Response:**
```json
{
  "user": {
    "id": 1,
    "username": "alice",
    "display_name": "alice",
    "created_at": "2026-03-30T15:00:00"
  }
}
```

---

## GPU Queue System

**Alle GPU-Operationen laufen durch eine FIFO-Queue.** Im Single-GPU-Default teilt sich GPU 0 (RTX 3060, 12GB) OmniVoice (resident) und Whisper (lazy). Nur eine Operation gleichzeitig — alle anderen warten.

**Kikiri (CPU-Stimmen) umgeht die Queue:** Anfragen mit `engine="kikiri"` oder an eine Kikiri-Stimme laufen sofort auf der CPU, auch waehrend die GPU transkribiert.

Das bedeutet:
- TTS-Requests koennen nur dann parallel auf GPU 1 laufen, wenn Dual-GPU explizit aktiviert ist
- ASR/Music-Requests koennen lange dauern, wenn die Queue voll ist oder der GPU-Guard nicht genug freien VRAM sieht
- Timeouts grosszuegig setzen (mindestens 600s)
- Queue-Status abfragen um dem User Feedback zu geben

### `GET /api/queue/status`
Aktueller Queue-Zustand.

**Response:**
```json
{
  "current": {
    "job_id": "a1b2c3d4e5f6",
    "service_type": "tts",
    "description": "TTS Generate",
    "started_at": 1774962000.0
  },
  "queue": [
    {
      "job_id": "f6e5d4c3b2a1",
      "position": 1,
      "service_type": "music",
      "description": "Music Generate",
      "created_at": 1774962005.0
    }
  ],
  "queue_length": 1
}
```

`current` ist `null` wenn die GPU frei ist. `queue` ist die Warteschlange.

### `GET /api/queue/status/{job_id}`
Status eines spezifischen Jobs.

**Response:**
```json
{
  "job_id": "a1b2c3d4e5f6",
  "status": "running",
  "position": 0,
  "service_type": "tts",
  "description": "TTS Generate",
  "created_at": 1774962000.0,
  "started_at": 1774962002.0,
  "finished_at": null,
  "error": null
}
```

Status-Werte: `queued`, `running`, `completed`, `failed`

---

## Sprachsynthese (TTS)

### `POST /api/generate` *(GPU: tts)*
Generiert Sprache aus Text. **Haupt-Endpoint fuer Client-Integrationen.**

**Request:**
```json
{
  "text": "Dies ist ein Testtext.",
  "voice_id": "a1b2c3d4",
  "engine": "omnivoice",
  "language": "German",
  "response_format": "wav"
}
```

| Feld | Pflicht | Default | Beschreibung |
|------|---------|---------|-------------|
| `text` | ja | — | Zu sprechender Text |
| `voice_id` | nein | `"default"` | Voice-ID (muss dem User gehoeren) |
| `engine` | nein | stimmabhaengig | `"omnivoice"` (GPU, Klone) oder `"kikiri"` (CPU-Bank) erzwingen |
| `language` | nein | Voice-Sprache | Sprache der Ausgabe |
| `response_format` | nein | `"wav"` | Audio-Format |

**Response:** Binary Audio (WAV)
- Content-Type: `audio/wav`
- `X-Audio-Duration`: Dauer in Sekunden (z.B. `"3.45"`)
- `X-Generation-Time`: Generierungszeit in Sekunden
- `X-RTF`: Real-Time-Factor (Generation/Duration)
- `X-Model`: Verwendetes Modell
- `X-Voice`: Verwendete Voice-ID

**Beispiel (Python):**
```python
import requests

resp = requests.post(
    "http://vocarium-api:8280/api/generate",
    json={"text": "Hallo Welt", "voice_id": "a1b2c3d4", "language": "German"},
    headers={"Remote-User": "alice"},
    timeout=600,
)
wav_bytes = resp.content
audio_duration = float(resp.headers["X-Audio-Duration"])
```

### `POST /api/generate/stream` *(GPU: tts)*
Streaming TTS via Server-Sent Events. Nuetzlich fuer lange Texte — gibt Chunks zurueck sobald verfuegbar.

**Request:** Gleich wie `/api/generate` (ohne `response_format`).

**Response:** `text/event-stream`

```
event: chunk
data: {"index": 0, "total": 3, "audio": "<base64-wav>", "duration": 2.1, "text": "Erster Satz."}

event: chunk
data: {"index": 1, "total": 3, "audio": "<base64-wav>", "duration": 1.8, "text": "Zweiter Satz."}

event: done
data: {"total_duration": 5.4, "generation_time": 8.2, "rtf": 1.52, "model": "omnivoice", "voice": "a1b2c3d4", "chunks": 3}
```

**Hinweis:** Der Endpoint streamt Queue-/Progress- und Chunk-Events live per SSE. Lange TTS-Inferenz bleibt GPU-gebunden; setze trotzdem grosszuegige Timeouts.

---

## OpenAI-kompatible TTS-Endpunkte

OpenAI-kompatible Clients verwenden `voice`, aber Vocarium erwartet darin eine echte Voice-ID, nicht den Anzeigenamen aus der UI.

### `GET /v1/voices`
Listet die Voices des authentifizierten Users im OpenAI-kompatiblen Format.

```bash
curl -H "Remote-User: alice" http://vocarium-api:8280/v1/voices
curl -H "Remote-User: alice" "http://vocarium-api:8280/v1/voices?source=clone"
curl -H "Remote-User: alice" "http://vocarium-api:8280/v1/voices?source=design"
curl -H "Remote-User: alice" "http://vocarium-api:8280/v1/voices?source=custom"
```

### Clone/Base-Voices: `POST /v1/audio/speech`

```bash
curl -X POST http://vocarium-api:8280/v1/audio/speech \
  -H "Remote-User: alice" \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"Hallo Welt","voice":"a1b2c3d4","response_format":"wav"}' \
  --output speech.wav
```

`default` ist die eingebaute Base-Voice. Geklonte Stimmen muessen per `voice_id` angesprochen werden. Wenn ein SUB/WAVE-Client versehentlich Persona-Labels wie `Michael Scott` als `voice` sendet, routet Vocarium diese kompatibilitaetshalber auf `default`; fuer die echte geklonte Stimme muss weiterhin die konkrete `voice_id` verwendet werden.

### Designed Voices: `POST /v1/audio/speech/designed`

```bash
curl -X POST http://vocarium-api:8280/v1/audio/speech/designed \
  -H "Remote-User: alice" \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"Hallo Welt","voice":"DESIGNED_VOICE_ID","response_format":"wav"}' \
  --output designed.wav
```

### Custom Speaker Presets: `POST /v1/audio/speech/custom`

Historischer Qwen-Endpunkt. Die Qwen-Engine ist stillgelegt (`QWEN_TTS_ENABLED=false`); der Endpunkt bleibt fuer Kompatibilitaet bestehen und antwortet ohne Engine mit 503.

`/v1/audio/speech` akzeptiert `default` und Klon-Stimmen (OmniVoice) sowie die Kikiri-Stimmen.

### Identitaet hinter dem Proxy

Ist `VOCARIUM_PROXY_SECRET` gesetzt, zaehlt `Remote-User` nur zusammen mit dem Header `X-Vocarium-Proxy-Secret`. Der Nginx von `vocarium-ui` haengt ihn automatisch an; Direktzugriffe auf `vocarium-api:8280` ohne Secret laufen als anonymer `api`-Benutzer (bei `ALLOW_ANONYMOUS=true`) oder erhalten 401.

---

## Voice Management

### `GET /api/voices`
Alle Voices des authentifizierten Users.

**Response:**
```json
{
  "voices": [
    {
      "id": "a1b2c3d4",
      "name": "Meine Stimme",
      "language": "German",
      "source": "clone",
      "design_prompt": null,
      "ref_text": "Referenztext der beim Klonen verwendet wurde",
      "created_at": "2026-03-30T15:00:00Z",
      "has_audio": true
    }
  ]
}
```

**Wichtig:** Voices sind user-scoped. User A sieht nur seine eigenen Voices.

### `GET /api/voices/{voice_id}`
Einzelne Voice abrufen (muss dem User gehoeren).

### `GET /api/voices/{voice_id}/audio`
Referenz-Audio einer Voice als WAV herunterladen.

### `DELETE /api/voices/{voice_id}`
Voice loeschen.

**Response:**
```json
{"status": "deleted", "voice_id": "a1b2c3d4"}
```

### `POST /api/voices/clone` *(GPU: tts + optional asr)*
Neue Voice durch Klonen erstellen.

**Request:** `multipart/form-data`
- `name` (string, pflicht): Name der Voice
- `language` (string, default=`"German"`): Sprache
- `ref_text` (string): Referenztext (was im Audio gesagt wird)
- `auto_transcribe` (bool, default=**true**): Audio automatisch transkribieren falls kein ref_text. Benoetigt ASR (GPU Queue).
- `ref_audio` (file, pflicht): Audio-Datei (WAV empfohlen, wird konvertiert)

**Response:**
```json
{"status": "created", "voice_id": "a1b2c3d4", "name": "Meine Stimme"}
```

---

## Transkription (ASR)

### `POST /api/transcribe` *(GPU: asr)*
Audio/Video transkribieren.

**Request:** `multipart/form-data`
- `file` (file, optional): Audio-/Video-Datei
- `url` (string, optional): YouTube-URL

Eines von beiden muss gesetzt sein.

**Response:**
```json
{"text": "Der transkribierte Text aus dem Audio..."}
```

Unterstuetzte Formate: WAV, MP3, FLAC, MP4, MKV, AVI, etc.
Max. 30 Minuten Audio.

---

## Modelle & Sprecher

### `GET /api/models`
Historischer Qwen-Endpunkt; liefert seit der Stilllegung eine leere Modellliste. Die tatsaechlichen Engines stehen in `GET /api/voices` (`source`: `omnivoice` | `kikiri`) und `GET /api/health` (`tts.voices_loaded`).

### `GET /api/languages`
Unterstuetzte Sprachen.

```json
{"languages": ["Chinese", "English", "Japanese", "Korean", "German", "French", "Russian", "Portuguese", "Spanish", "Italian"]}
```

---

## Integrations-Tipps

### 1. Timeouts grosszuegig setzen
Die GPU-Queue kann Jobs verzoegern. Mindestens **600 Sekunden** Timeout fuer alle TTS-Requests.

### 2. Queue-Status vor Generierung pruefen
```python
queue = requests.get(
    "http://vocarium-api:8280/api/queue/status",
    headers={"Remote-User": username}
).json()

if queue["current"] or queue["queue_length"] > 0:
    # User informieren: "GPU beschaeftigt, Position X in Queue"
```

### 3. Voice-Liste cachen
`GET /api/voices` ist schnell (kein GPU noetig). Bei App-Start einmal laden, bei Bedarf refreshen.

### 4. Fuer lange Texte: Kapitelweise / abschnittsweise generieren
Nicht das ganze Buch / den ganzen Artikel auf einmal. Text splitten und sequentiell generieren — so bleibt die Queue-Wartezeit pro Abschnitt ueberschaubar.

### 5. Streaming vs. Non-Streaming
- Kurze Texte (< 500 Zeichen): `/api/generate` (einfacher)
- Lange Texte / Hoerbuch-Kapitel: `/api/generate/stream` (Fortschritts-Feedback)

### 6. Fehlerbehandlung
```python
resp = requests.post("http://vocarium-api:8280/api/generate", ...)
if resp.status_code == 401:
    # Remote-User Header fehlt
elif resp.status_code == 403:
    # Voice gehoert einem anderen User
elif resp.status_code >= 500:
    # GPU/TTS Fehler — retry nach kurzer Pause
```

### 7. Docker-Netzwerk
Client-Container koennen sich ins `voice-network` einklinken, statt ueber den Host-Port:

```yaml
services:
  your-client:
    build: ./your-client
    networks:
      - voice-network

networks:
  voice-network:
    external: true
    name: voice-network
```

Damit ist die API intern erreichbar unter `http://vocarium-api:8280`.
