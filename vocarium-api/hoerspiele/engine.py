"""Hörspiele — Szenenklang-Engine, in Vocarium integriert.

Portiert aus dem eigenständigen Projekt *szenenklang*
(``series-episode-to-audiobook/backend/app/main.py``). Die Fachlogik ist
unverändert übernommen: Plex-Bindung, Chromaprint-Erkennung von Intro,
Commercial-Bumper und Abspann, ASR-Chunking, Untertitel-Abgleich, die
Erzähler-Dramaturgie samt Recherche-Agent, der sample-genaue 48-kHz-Renderer
und die automatischen Qualitätsprüfungen.

Ersetzt wurde nur die Hülle:

* **Persistenz** — statt einer 144 MB großen ``state.json``, die bei jedem
  ``save_state()`` komplett neu geschrieben wurde, liegt der Zustand in der
  Vocarium-SQLite-Datenbank; geschrieben wird nur die geänderte Zeile
  (siehe :mod:`hoerspiele.store`).
* **Mandanten** — Projekte und Läufe tragen ``owner_user_id`` und werden bei
  jedem Zugriff danach gefiltert, wie überall sonst in Vocarium.
* **Sprachausgabe** — OmniVoice/Kikiri über die Podcast-TTS-Brücke. Der
  espeak-/Sinuston-Notfallpfad von szenenklang ist entfernt: eine Erzählspur,
  die still zum Piepton degradiert, ist kein akzeptables Ergebnis.
* **Transkription** — whisper-stt über die eigene ``/v1/audio/transcriptions``.
  Die Herkunftsfelder heißen nicht mehr nach dem abgeschalteten Qwen3-ASR.
* **Dokumentenimport** — der abhängigkeitsfreie Parser aus
  :mod:`audiobooks.text_pipeline` statt ``ebooklib`` und ``python-docx``.
"""

from __future__ import annotations

import array
import hashlib
import difflib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import sys
import threading
import time
import unicodedata
import uuid
import wave
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable, Literal

import httpx

from access_control import is_admin_username
from .scene_evidence import apply_reviewed_scene_evidence, grounded_picture_cue_ids, reviewed_anchor_evidence
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .store import StateStore

logger = logging.getLogger("vocarium.hoerspiele")

# ── Von Vocarium injizierte Abhängigkeiten ──────────────────────────────────
_DEPS: dict[str, Any] = {
    "get_current_user": None,   # Callable[[Request], dict]
    "tts_bridge": None,         # podcast.routes.VocariumTTSGenerator
    "store": None,              # StateStore
    "gpu_submit": None,
}

router = APIRouter(prefix="/api/hoerspiele", tags=["hoerspiele"])


ROOT = Path(os.getenv("HOERSPIELE_DATA_DIR") or (os.getenv("DATA_DIR", "/app/data") + "/hoerspiele"))
UPLOADS = ROOT / "uploads"
ARTIFACTS = ROOT / "artifacts"
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
SAMPLE_RATE = 48_000
AUTOMATIC_QUALITY_EVALUATOR = "szenenklang-automatic-quality-v25"
LEGACY_AUTOMATIC_QUALITY_EVALUATOR = "szenenklang-automatic-quality-v3"
NARRATION_EDITOR_VERSION = 15
SCENE_ALIGNMENT_CHECKPOINT_VERSION = 11
VISUAL_SOURCE_COVERAGE_PURPOSES = {
    "character_introduction",
    "visual_action",
    "book_boundary",
}
AUDIO_DRAMA_BEAT_TYPES = {
    "scene_setup",
    "pre_action",
    "action_sync",
    "reaction",
    "dialogue_bridge",
    "scene_close",
}
AUDIO_DRAMA_STRATEGIES = {
    "prefer_ambience_overlay",
    "pause_at_scene_boundary",
}
AUDIO_DRAMA_DURATION_LIMITS_MS = {
    "character_introduction": 24_000,
    "scene_transition": 20_000,
    "visual_action": 20_000,
    "internal_motivation": 16_000,
    "offscreen_context": 16_000,
    "continuity_bridge": 16_000,
    "foreshadowing": 12_000,
    "book_boundary": 36_000,
}
# A beat that only names an object or a mood leaves the listener without the
# picture.  Every explaining beat therefore needs a real spoken sentence.
AUDIO_DRAMA_MIN_DURATION_MS = {
    "character_introduction": 6_000,
    "scene_transition": 4_500,
    "visual_action": 5_500,
    "internal_motivation": 4_000,
    "offscreen_context": 4_000,
    "continuity_bridge": 3_500,
    "foreshadowing": 3_500,
    "book_boundary": 6_000,
}
AUDIO_DRAMA_MIN_WORDS = {
    "character_introduction": 16,
    "scene_transition": 12,
    "visual_action": 14,
    "internal_motivation": 10,
    "offscreen_context": 10,
    "continuity_bridge": 9,
    "foreshadowing": 9,
    "book_boundary": 16,
}
# Continuous audio-drama companionship: aim for one spoken comment per this
# much retained source audio and never leave a longer stretch uncommented.
NARRATION_COVERAGE_TARGET_MS = 120_000
NARRATION_MAX_GAP_MS = 150_000
# Under this many concrete, not-yet-audible source details the coverage ratio is
# noise rather than evidence, so the 30% floor does not apply to that cue.
MIN_SOURCE_DETAIL_REFERENCE_TOKENS = 3
AUDIO_DRAMA_CADENCE_MS = {
    "scene_setup": (300, 550),
    "pre_action": (180, 320),
    "action_sync": (120, 260),
    "reaction": (200, 420),
    "dialogue_bridge": (160, 300),
    "scene_close": (280, 650),
}
INTRO_SEARCH_SECONDS = 300
INTRO_MIN_DURATION_MS = 30_000
INTRO_MIN_EPISODE_RATIO = 0.6
INTRO_MAX_FINGERPRINT_BIT_DISTANCE = 10
ENDING_SEARCH_SECONDS = 360
ENDING_MIN_DURATION_MS = 20_000
COMMERCIAL_BUMPER_WINDOW_START_RATIO = 0.32
COMMERCIAL_BUMPER_WINDOW_END_RATIO = 0.62
COMMERCIAL_BUMPER_MIN_DURATION_MS = 4_000
COMMERCIAL_BUMPER_CLUSTER_RADIUS_MS = 30_000
LOCK = threading.RLock()
CLI_LOGIN_OWNERS: dict[str, str] = {}
ACTIVE_RUN_STATUSES = {"queued", "running"}
# Ein Szenenausrichtungs-Paket braucht auf GLM-5.3 regelmaessig ueber 30 Minuten;
# die Obergrenze je Agentenaufruf liegt deshalb bei zwei Stunden.
AGENT_TIMEOUT_MAX = 7200
CODEX_MODEL_OPTIONS = [
    {"value": "gpt-5.6-sol", "label": "GPT-5.6 Sol · beste Qualität (empfohlen)"},
    {"value": "automatic", "label": "Automatisch"},
    {"value": "gpt-5.6-terra", "label": "GPT-5.6 Terra · Allrounder"},
    {"value": "gpt-5.6-luna", "label": "GPT-5.6 Luna · wiederholbare Aufgaben"},
]
CODEX_REASONING_OPTIONS = [
    {"value": "automatic", "label": "Automatisch"},
    {"value": "low", "label": "Niedrig"},
    {"value": "medium", "label": "Mittel"},
    {"value": "high", "label": "Hoch"},
    {"value": "xhigh", "label": "Sehr hoch / Max (empfohlen)"},
]
CODEX_MODEL_VALUES = {option["value"] for option in CODEX_MODEL_OPTIONS}
CODEX_MODEL_ALIASES = {"gpt-5.6": "gpt-5.6-sol"}
AGENT_PROVIDER_OPTIONS = [
    {"value": "codex", "label": "Codex CLI · ChatGPT"},
    {"value": "claude", "label": "Claude Code CLI · Anthropic"},
    {"value": "zai", "label": "Claude Code CLI · Z.AI Coding Plan"},
]
AGENT_MODEL_OPTIONS = {
    "codex": CODEX_MODEL_OPTIONS,
    "claude": [
        {"value": "sonnet", "label": "Claude Sonnet · empfohlen"},
        {"value": "automatic", "label": "Konto-Standard"},
        {"value": "opus", "label": "Claude Opus · maximale Qualität"},
        {"value": "haiku", "label": "Claude Haiku · geringe Limits"},
    ],
    "zai": [
        {"value": "glm-5.3", "label": "GLM-5.3 · maximale Qualität (empfohlen)"},
        {"value": "glm-5.3-flash", "label": "GLM-5.3 Flash · schnell"},
        {"value": "glm-5.2", "label": "GLM-5.2 · Vorgängergeneration"},
        {"value": "glm-5.1", "label": "GLM-5.1 · Vorgängergeneration"},
        {"value": "glm-5-turbo", "label": "GLM-5 Turbo · schnell"},
        {"value": "glm-4.7", "label": "GLM-4.7 · ausgewogen"},
        {"value": "glm-4.5-air", "label": "GLM-4.5 Air · geringe Limits"},
        {"value": "automatic", "label": "Plan-Standard"},
    ],
}
AGENT_PROVIDER_VALUES = {option["value"] for option in AGENT_PROVIDER_OPTIONS}
# Vocarium fährt den kompletten Stack auf dem Z.AI-Coding-Plan. Das
# angeschlossene ChatGPT-Konto gibt die Codex-Modelle gar nicht frei, ein
# Codex-Standard würde also jeden frischen KI-Lauf sofort abbrechen.
DEFAULT_AGENT_PROVIDER = "zai"
DEFAULT_AGENT_MODEL = "glm-5.3"
AGENT_REASONING_OPTIONS = [
    {"value": "automatic", "label": "Automatisch"},
    {"value": "low", "label": "Niedrig"},
    {"value": "medium", "label": "Mittel"},
    {"value": "high", "label": "Hoch"},
    {"value": "xhigh", "label": "Sehr hoch / Max"},
]


def default_codex_settings() -> dict[str, Any]:
    return {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "timeout_seconds": 1200,
    }


def codex_settings() -> dict[str, Any]:
    stored = STATE.get("codex_settings") if "STATE" in globals() else None
    settings = {**default_codex_settings(), **dict(stored or {})}
    if settings["reasoning_effort"] not in {option["value"] for option in CODEX_REASONING_OPTIONS}:
        settings["reasoning_effort"] = default_codex_settings()["reasoning_effort"]
    model = CODEX_MODEL_ALIASES.get(str(settings.get("model") or "").strip(), str(settings.get("model") or "").strip())
    settings["model"] = model if model in CODEX_MODEL_VALUES else default_codex_settings()["model"]
    try:
        timeout_seconds = int(settings.get("timeout_seconds") or 1200)
    except (TypeError, ValueError):
        timeout_seconds = default_codex_settings()["timeout_seconds"]
    settings["timeout_seconds"] = max(60, min(AGENT_TIMEOUT_MAX, timeout_seconds))
    return settings


def codex_settings_response() -> dict[str, Any]:
    settings = codex_settings()
    return {
        **settings,
        "model_options": CODEX_MODEL_OPTIONS,
        "reasoning_options": CODEX_REASONING_OPTIONS,
        "fixed_runtime": {
            "mapping_web_search": "live",
            "episode_context_web_search": "live",
            "scene_alignment_web_search": "disabled",
            "sandbox": "read-only",
            "approval_policy": "never",
            "output_schema": "required",
            "user_config": "ignored",
        },
        "updated_at": str((STATE.get("codex_settings") or {}).get("updated_at") or ""),
    }


def codex_execution_settings() -> dict[str, Any]:
    settings = codex_settings()
    return {
        "model": settings["model"],
        "reasoning_effort": settings["reasoning_effort"],
        "timeout_seconds": settings["timeout_seconds"],
    }


def default_agent_profile() -> dict[str, Any]:
    return {
        "provider": DEFAULT_AGENT_PROVIDER,
        "model": DEFAULT_AGENT_MODEL,
        "reasoning_effort": "xhigh",
        "timeout_seconds": 1200,
    }


def agent_model_options() -> dict[str, list[dict[str, str]]]:
    options = {key: list(value) for key, value in AGENT_MODEL_OPTIONS.items()}
    stored = STATE.get("agent_settings") or {} if "STATE" in globals() else {}
    for provider, catalog in (stored.get("catalogs") or {}).items():
        if provider not in options:
            continue
        existing = {item["value"] for item in options[provider]}
        for item in catalog.get("models", []) + catalog.get("retained_models", []):
            model = str(item.get("value") or "")
            if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/\[\]-]{0,119}", model) and model not in existing:
                options[provider].append({"value": model, "label": str(item.get("label") or model)[:160]})
                existing.add(model)
    return options


def normalized_agent_candidate(value: Any, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = {**(fallback or default_agent_profile()), **dict(value or {})}
    provider = str(settings.get("provider") or DEFAULT_AGENT_PROVIDER).strip()
    if provider not in AGENT_PROVIDER_VALUES:
        provider = DEFAULT_AGENT_PROVIDER
    aliases = CODEX_MODEL_ALIASES if provider == "codex" else {}
    model = aliases.get(str(settings.get("model") or "").strip(), str(settings.get("model") or "").strip())
    allowed_models = {option["value"] for option in agent_model_options()[provider]}
    if model not in allowed_models:
        model = AGENT_MODEL_OPTIONS[provider][0]["value"]
    reasoning = str(settings.get("reasoning_effort") or "automatic").strip()
    if reasoning not in {option["value"] for option in AGENT_REASONING_OPTIONS}:
        reasoning = "automatic"
    try:
        timeout_seconds = int(settings.get("timeout_seconds") or 1200)
    except (TypeError, ValueError):
        timeout_seconds = 1200
    return {
        "provider": provider,
        "model": model,
        "reasoning_effort": reasoning,
        "timeout_seconds": max(60, min(AGENT_TIMEOUT_MAX, timeout_seconds)),
    }


def normalized_agent_profile(value: Any, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(value or {})
    profile = normalized_agent_candidate(raw, fallback)
    raw_fallbacks = raw.get("fallbacks")
    profile["fallbacks"] = [
        normalized_agent_candidate(item)
        for item in (raw_fallbacks if isinstance(raw_fallbacks, list) else [])[:2]
        if isinstance(item, dict)
    ]
    return profile


def agent_settings() -> dict[str, Any]:
    stored_codex = STATE.get("codex_settings") if "STATE" in globals() else None
    # Nur ein ueber die Oberflaeche gespeichertes Codex-Profil ist Altbestand.
    # Der blosse Vorgabe-Datensatz traegt kein updated_at und darf den
    # Vocarium-Standard nicht ueberschreiben.
    saved_by_user = bool((stored_codex or {}).get("updated_at"))
    fallback = {"provider": "codex", **codex_settings()} if saved_by_user else default_agent_profile()
    stored = STATE.get("agent_settings") if "STATE" in globals() else None
    # base_state() legt beim ersten Speichern eine Vorbelegung ohne updated_at
    # ab. Die darf den jeweils aktuellen Standard nicht einfrieren -- sonst
    # haengt eine bestehende Installation ewig am Modell von damals.
    if not (stored or {}).get("updated_at"):
        stored = None
    return {
        "research": normalized_agent_profile((stored or {}).get("research"), fallback),
        "scripting": normalized_agent_profile((stored or {}).get("scripting"), fallback),
    }


def agent_settings_response() -> dict[str, Any]:
    settings = agent_settings()
    return {
        **settings,
        "provider_options": AGENT_PROVIDER_OPTIONS,
        "model_options": agent_model_options(),
        "reasoning_options": AGENT_REASONING_OPTIONS,
        "fixed_runtime": {
            "mapping_web_search": "live",
            "episode_context_web_search": "live",
            "scene_alignment_web_search": "disabled",
            "sandbox": "read-only",
            "approval_policy": "never",
            "output_schema": "required",
            "user_config": "ignored",
        },
        "updated_at": str((STATE.get("agent_settings") or {}).get("updated_at") or ""),
    }


def agent_execution_settings(profile: Literal["research", "scripting"]) -> dict[str, Any]:
    return dict(agent_settings()[profile])


def default_narration_rules() -> dict[str, Any]:
    return {
        "version": 11,
        "name_source": "target_subtitles",
        "preserve_existing_narrator": True,
        "native_narrator_policy": "complement_without_paraphrase",
        "detailed_style": "novelistic_visual",
        "detailed_source_coverage_min": 0.3,
        "audio_drama_source_coverage_min": 0.22,
        "audio_drama_style": "continuous_scene_companion",
        "audio_drama_duration_limits_ms": AUDIO_DRAMA_DURATION_LIMITS_MS,
        "audio_drama_min_duration_ms": AUDIO_DRAMA_MIN_DURATION_MS,
        "audio_drama_min_words": AUDIO_DRAMA_MIN_WORDS,
        "narration_coverage_target_ms": NARRATION_COVERAGE_TARGET_MS,
        "narration_max_gap_ms": NARRATION_MAX_GAP_MS,
        "audio_drama_cadence_ms": {
            beat: {"before": before, "after": after}
            for beat, (before, after) in AUDIO_DRAMA_CADENCE_MS.items()
        },
        "stage_direction_policy": "sound_bed_not_speech",
        "ambience_ducking": "voice_triggered_soft_sidechain",
        "character_visual_traits_min": 2,
        "placement_priority": ["speech_free_overlay", "dense_action_insert", "aligned_scene_insert"],
        "speech_margin_ms": 300,
        "max_anchor_distance_ms": 6_000,
        "character_introduction_max_lead_ms": 4_000,
        "narration_max_lead_ms": 4_000,
        "dense_action_min_sentences": 2,
        "dense_action_min_words": 24,
        "dense_action_min_confidence": 0.82,
        "min_forced_insert_spacing_ms": 240_000,
        "required_insert_spacing_ms": 45_000,
        "automated_quality_gate_required": True,
        "listener_review_policy": "calibration",
        "test_listener_review": False,
        "test_listener_min_score": 0.92,
        "test_listener_max_rounds": 4,
        "require_one_shot_pass": False,
    }


def narration_rules(project: dict[str, Any]) -> dict[str, Any]:
    rules = {**default_narration_rules(), **dict(project.get("narration_rules") or {})}
    rules.update(
        {
            "version": default_narration_rules()["version"],
            "native_narrator_policy": default_narration_rules()["native_narrator_policy"],
            "detailed_style": default_narration_rules()["detailed_style"],
            "detailed_source_coverage_min": default_narration_rules()["detailed_source_coverage_min"],
            "audio_drama_source_coverage_min": default_narration_rules()["audio_drama_source_coverage_min"],
            "audio_drama_style": default_narration_rules()["audio_drama_style"],
            "audio_drama_duration_limits_ms": default_narration_rules()["audio_drama_duration_limits_ms"],
            "audio_drama_min_duration_ms": default_narration_rules()["audio_drama_min_duration_ms"],
            "audio_drama_min_words": default_narration_rules()["audio_drama_min_words"],
            "narration_coverage_target_ms": default_narration_rules()["narration_coverage_target_ms"],
            "narration_max_gap_ms": default_narration_rules()["narration_max_gap_ms"],
            "audio_drama_cadence_ms": default_narration_rules()["audio_drama_cadence_ms"],
            "stage_direction_policy": default_narration_rules()["stage_direction_policy"],
            "ambience_ducking": default_narration_rules()["ambience_ducking"],
            "character_visual_traits_min": default_narration_rules()["character_visual_traits_min"],
            "placement_priority": default_narration_rules()["placement_priority"],
            "max_anchor_distance_ms": default_narration_rules()["max_anchor_distance_ms"],
            "character_introduction_max_lead_ms": default_narration_rules()["character_introduction_max_lead_ms"],
            "narration_max_lead_ms": default_narration_rules()["narration_max_lead_ms"],
            "automated_quality_gate_required": True,
            "listener_review_policy": "calibration",
            "test_listener_review": False,
            "require_one_shot_pass": False,
        }
    )
    return rules


def default_narration_review() -> dict[str, Any]:
    return {
        "mode": "calibration",
        "required": False,
        "status": "automatic_pending",
        "automatic_status": "not_run",
        "score": None,
        "one_shot_ready": False,
        "calibration_mismatch": False,
        "rounds": [],
        "one_shot_guidance": [],
    }


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def base_state() -> dict[str, Any]:
    return {
        "projects": {},
        "runs": {},
        "codex_settings": default_codex_settings(),
        "agent_settings": {
            "research": default_agent_profile(),
            "scripting": default_agent_profile(),
        },
        "integrations": {
            "plex": {"status": "demo", "label": "Demo-Bibliothek aktiv"},
            "vocarium": {"status": "unconfigured", "label": "Nicht konfiguriert"},
            "aligner": {"status": "demo", "label": "Demo-Zeitmarken"},
            "tts": {"status": "local", "label": "Lokale Systemstimme"},
            "codex": {"status": "terminal", "label": "Im Admin-Terminal anmelden"},
        },
    }


def load_state() -> dict[str, Any]:
    ROOT.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    store: StateStore | None = _DEPS.get("store")
    if store is None:
        return base_state()
    return store.load(base_state())


# Wird beim Mounten des Routers durch ``configure()`` aus der Datenbank
# hydriert. Bis dahin eine leere, aber formgleiche Struktur, damit
# Modulimport und Typprüfung ohne Datenbank funktionieren.
STATE: dict[str, Any] = base_state()


def save_state() -> None:
    """Persistiert nur die tatsächlich geänderten Zeilen.

    Szenenklang schrieb hier die vollständige ``state.json`` — 144 MB, auch
    wenn sich nur ein Fortschrittszähler bewegt hatte, und das einmal pro
    transkribiertem Kapitel.
    """
    store: StateStore | None = _DEPS.get("store")
    if store is None:
        return
    with LOCK:
        store.save(STATE)


def project_runs(project_id: str | None = None, user: "User | None" = None) -> list[dict[str, Any]]:
    with LOCK:
        runs = [
            run
            for run in STATE.get("runs", {}).values()
            if (project_id is None or run.get("project_id") == project_id)
            and (user is None or owns(run, user))
        ]
    return sorted(runs, key=lambda run: str(run.get("created_at") or ""), reverse=True)


def active_project_run(project_id: str) -> dict[str, Any] | None:
    return next(
        (run for run in project_runs(project_id) if run.get("status") in ACTIVE_RUN_STATUSES),
        None,
    )


def recover_interrupted_runs() -> None:
    changed = False
    for run in STATE.get("runs", {}).values():
        if run.get("status") in {"queued", "running"}:
            run.update({"status": "failed", "message": "Verarbeitung durch Dienstneustart unterbrochen", "error": "Der Lauf kann sicher erneut gestartet werden", "updated_at": now()})
            changed = True
    if changed:
        save_state()


class User(BaseModel):
    username: str
    display_name: str
    roles: list[str]
    user_id: int = 0


def current_user(request: Request) -> User:
    """Vocarium-Identität (``Remote-User`` via Authelia) statt eigener Auth.

    Szenenklang brachte eine zweite Rechteebene mit (viewer/editor/admin aus
    Authelia-Gruppen plus ein Proxy-Nachweis-Secret). Innerhalb von Vocarium
    wäre das eine zweite, abweichende Wahrheit über denselben Benutzer.
    Maßgeblich ist deshalb ``get_current_user`` des Gateways; Admin ist, wer in
    ``VOCARIUM_ADMIN_USERS`` steht. Die Einstellungen (KI-Profile, Zugänge,
    Integrationen) sind bewusst instanzweit wie in Szenenklang: sie
    beschreiben Infrastruktur, keine Nutzerdaten, und nur Admins ändern sie.
    """
    getter = _DEPS.get("get_current_user")
    if getter is None:
        raise HTTPException(503, "Hörspiele sind nicht initialisiert")
    account = getter(request)
    username = str(account.get("username") or account.get("name") or "user")
    roles = ["viewer", "editor"]
    # Dieselbe Admin-Liste wie das Gateway (``_require_admin``): eine leere
    # Liste bedeutet "niemand", nicht "jeder".
    if is_admin_username(username, os.getenv("VOCARIUM_ADMIN_USERS", "")):
        roles.append("admin")
    return User(
        username=username,
        display_name=str(account.get("display_name") or username.title()),
        roles=roles,
        user_id=int(account.get("id") or account.get("user_id") or 0),
    )


def secret_value(name: str) -> str:
    secret_file = os.getenv(f"{name}_FILE")
    if secret_file:
        try:
            return Path(secret_file).read_text("utf-8").strip()
        except OSError:
            return ""
    return os.getenv(name, "")


def require_editor(user: User = Depends(current_user)) -> User:
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if "admin" not in user.roles:
        raise HTTPException(403, "Admin-Rolle erforderlich")
    return user


def owns(record: dict[str, Any], user: User) -> bool:
    """Mandantentrennung: Altbestand ohne Besitzer gehört niemandem."""
    return int(record.get("owner_user_id") or 0) == int(user.user_id)


class Binding(BaseModel):
    series_id: str
    title: str
    year: int | None = None
    audio_language: str = "de"
    narrator_voice: str = Field(default_factory=lambda: os.getenv("TTS_VOICE", "default"))
    seasons: int | None = None
    episodes: int | None = None


class CuePatch(BaseModel):
    text: str = Field(min_length=1, max_length=1200)


class NarratorVoicePatch(BaseModel):
    voice_id: str = Field(min_length=1, max_length=120)


class NarrationPlanRequest(BaseModel):
    density: Literal["compact", "balanced", "detailed", "audio_drama"] = "audio_drama"


class ReviewedPictureEvidence(BaseModel):
    start_ms: int = Field(ge=1000)
    visual_evidence: str = Field(min_length=30, max_length=1600)
    evidence_frames: list[str] = Field(min_length=1, max_length=12)


class ReviewedNarrationAnchor(BaseModel):
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    anchor_segment_id: str = Field(min_length=1, max_length=160)


class SceneEvidenceRequest(BaseModel):
    episode_id: str = Field(min_length=1, max_length=80)
    pictures: list[ReviewedPictureEvidence] = Field(default_factory=list, max_length=50)
    narration_anchors: list[ReviewedNarrationAnchor] = Field(default_factory=list, max_length=100)


class NarrationNameEntry(BaseModel):
    canonical: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(min_length=1, max_length=12)
    evidence: str = Field(default="Manuell bestätigt", max_length=500)
    confidence: float = Field(default=1.0, ge=0, le=1)


class NarrationNameLexiconPatch(BaseModel):
    entries: list[NarrationNameEntry] = Field(max_length=100)


class SubtitleReconciliationRequest(BaseModel):
    language: str = Field(default="de", min_length=2, max_length=12)


class ListenerReviewRequest(BaseModel):
    verdict: Literal["passed", "changes_requested"]
    score: float = Field(ge=0, le=1)
    notes: str = Field(default="", max_length=4000)
    findings: list[str] = Field(default_factory=list, max_length=12)


class IntegrationPatch(BaseModel):
    base_url: str | None = None
    token: str | None = None
    language: str | None = None


class CodexSettingsPatch(BaseModel):
    model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=80)
    reasoning_effort: Literal["automatic", "low", "medium", "high", "xhigh"] = "xhigh"
    timeout_seconds: int = Field(default=1200, ge=60, le=7200)


class AgentCandidatePatch(BaseModel):
    provider: Literal["codex", "claude", "zai"] = DEFAULT_AGENT_PROVIDER
    model: str = Field(min_length=1, max_length=80)
    reasoning_effort: Literal["automatic", "low", "medium", "high", "xhigh"] = "automatic"
    timeout_seconds: int = Field(default=1200, ge=60, le=7200)


class AgentProfilePatch(AgentCandidatePatch):
    fallbacks: list[AgentCandidatePatch] = Field(default_factory=list, max_length=2)


class AgentSettingsPatch(BaseModel):
    research: AgentProfilePatch
    scripting: AgentProfilePatch


class CliLoginCode(BaseModel):
    code: str = Field(min_length=1, max_length=256)


class ZaiCredentialsPatch(BaseModel):
    api_key: str = Field(min_length=1, max_length=1000)
    base_url: str = Field(default="https://api.z.ai/api/anthropic", min_length=8, max_length=500)


@router.get("/health/live")
def health_live():
    return {"status": "ok"}


@router.get("/session")
def session(user: User = Depends(current_user)):
    return user


@router.get("/capabilities")
def capabilities(user: User = Depends(current_user)):
    return {
        "formats": ["txt", "md", "pdf", "docx", "epub"],
        "max_upload_bytes": MAX_UPLOAD,
        "sample_rate": SAMPLE_RATE,
        # Vocarium hat keinen Terminal-Container; die Oberflaeche darf den
        # Reiter also gar nicht erst anbieten.
        "terminal_enabled": os.getenv("TERMINAL_ENABLED", "false").lower() == "true",
        "demo_mode": not bool(os.getenv("PLEX_BASE_URL") and secret_value("PLEX_TOKEN")),
    }


# The web UI never reads these, but they dominate the payload: one project
# carried 12 MB of word timings, so the project list grew to 75 MB and timed
# out on mobile connections.
PROJECT_INTERNAL_FIELDS = (
    "transcript_words",
    "subtitle_cues",
    "semantic_cues",
    "narration_script_research",
    "mapping_research",
    "scene_alignment_execution",
    "media_assets",
)
# Only the detail view needs these; the card shows title, stage and status.
PROJECT_DETAIL_ONLY_FIELDS = (
    "transcript",
    "reconciled_transcript",
    "cues",
    "chapters",
)


def project_detail_response(project: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in project.items()
        if key not in PROJECT_INTERNAL_FIELDS
    }


def project_summary_response(project: dict[str, Any]) -> dict[str, Any]:
    summary = project_detail_response(project)
    for key in PROJECT_DETAIL_ONLY_FIELDS:
        value = project.get(key)
        # Keep the shape so the card can render before the detail arrives.
        summary[key] = [] if isinstance(value, list) else value
    summary["chapter_count"] = len(project.get("chapters") or [])
    summary["cue_count"] = len(project.get("cues") or [])
    summary["is_summary"] = True
    return summary


@router.get("/projects")
def list_projects(user: User = Depends(current_user)):
    # The overview must remain a cheap read. Revalidating every project here
    # blocks the tab after evaluator upgrades and can exceed gateway timeouts.
    # Detail reads and production completion refresh the quality report.
    with LOCK:
        projects = [project for project in STATE["projects"].values() if owns(project, user)]
    return [
        project_summary_response(project)
        for project in sorted(projects, key=lambda item: item["updated_at"], reverse=True)
        if owns(project, user)
    ]


@router.get("/projects/{project_id}")
def get_project(project_id: str, user: User = Depends(current_user)):
    project = project_or_404(project_id, user)
    if ensure_project_quality_state(project):
        save_state()
    return project_detail_response(project)


@router.post("/projects", status_code=201)
async def create_project(
    title: str = Form(...),
    source_language: str = Form("de"),
    audio_language: str = Form("de"),
    rights_confirmed: bool = Form(...),
    source: UploadFile = File(...),
    user: User = Depends(require_editor),
):
    if not rights_confirmed:
        raise HTTPException(422, "Die Nutzungsrechte müssen bestätigt werden")
    suffix = Path(source.filename or "source.txt").suffix.lower()
    if suffix not in {".txt", ".md", ".pdf", ".docx", ".epub"}:
        raise HTTPException(415, "Unterstützt werden TXT, Markdown, PDF, DOCX und EPUB")
    payload = await source.read(MAX_UPLOAD + 1)
    if len(payload) > MAX_UPLOAD:
        raise HTTPException(413, "Datei überschreitet das Upload-Limit")
    if not payload:
        raise HTTPException(422, "Die Datei ist leer")
    sha = hashlib.sha256(payload).hexdigest()
    upload_path = UPLOADS / f"{sha}{suffix}"
    upload_path.write_bytes(payload)
    try:
        text = extract_text(upload_path, suffix)
    except Exception as error:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(422, f"Text konnte nicht extrahiert werden: {error}") from error
    if len(text.strip()) < 20:
        raise HTTPException(422, "Zu wenig verwertbarer Text gefunden")
    project_id = new_id("prj")
    chapters = split_chapters(text)
    project = {
        "id": project_id,
        "title": title.strip(),
        "source_language": source_language,
        "audio_language": audio_language,
        "mode": "audio_drama_adaptation",
        "status": "source_ready",
        "stage": 1,
        "created_at": now(),
        "updated_at": now(),
        "owner": user.username,
        "owner_user_id": user.user_id,
        "source": {"filename": source.filename, "sha256": sha, "characters": len(text)},
        "chapters": chapters,
        "binding": None,
        "mapping": [],
        "transcript": [],
        "transcript_words": [],
        "cues": [],
        "narration_name_lexicon": [],
        "narration_rules": default_narration_rules(),
        "narration_review": default_narration_review(),
        "intro_detection": None,
        "commercial_bumper_detection": None,
        "ending_detection": None,
        "quality_report": None,
        "timeline": None,
        "artifacts": [],
        "warnings": [],
    }
    with LOCK:
        STATE["projects"][project_id] = project
        save_state()
    return project


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, user: User = Depends(require_editor)):
    # Wie jede andere Projektroute: fremde Projekte existieren nicht.
    project_or_404(project_id, user)
    with LOCK:
        if project_id not in STATE["projects"]:
            raise HTTPException(404, "Projekt nicht gefunden")
        if active_project_run(project_id):
            raise HTTPException(
                409,
                "Das Projekt wird gerade verarbeitet und kann erst nach Abschluss des Laufs gelöscht werden",
            )
        artifact_root = ARTIFACTS.resolve()
        project_directory = (artifact_root / project_id).resolve()
        if project_directory.parent != artifact_root:
            raise HTTPException(400, "Ungültige Projekt-ID")
        if project_directory.exists():
            if not project_directory.is_dir():
                raise HTTPException(500, "Projektdaten konnten nicht sicher gelöscht werden")
            try:
                shutil.rmtree(project_directory)
            except OSError as error:
                raise HTTPException(500, f"Projektdaten konnten nicht gelöscht werden: {error}") from error
        STATE["projects"].pop(project_id)
        STATE["runs"] = {
            run_id: run
            for run_id, run in STATE.get("runs", {}).items()
            if run.get("project_id") != project_id
        }
        save_state()


@router.get("/plex/series")
def plex_series(query: str = "", user: User = Depends(current_user)):
    try:
        real = fetch_plex_series(query)
    except RuntimeError as error:
        raise HTTPException(502, str(error)) from error
    if real is not None:
        return {"mode": "live", "items": real}
    demo = [
        {"id": "demo_dragon_ball_1986", "title": "Dragon Ball", "year": 1986, "seasons": 9, "episodes": 153, "summary": "Son-Gokus erste Suche nach den Dragon Balls"},
        {"id": "demo_dragon_ball_z_1989", "title": "Dragon Ball Z", "year": 1989, "seasons": 9, "episodes": 291, "summary": "Die Saiyajin-Saga und ihre Folgen"},
        {"id": "demo_one_piece_1999", "title": "One Piece", "year": 1999, "seasons": 21, "episodes": 1100, "summary": "Die Reise der Strohhutpiraten"},
    ]
    needle = query.casefold().strip()
    return {"mode": "demo", "items": [item for item in demo if needle in item["title"].casefold()]}


@router.get("/tts/voices")
def tts_voices(user: User = Depends(current_user)):
    voices = fetch_tts_voices()
    return {
        "mode": "live" if voices is not None else "local",
        "items": voices or [{"id": "default", "name": "Standardstimme", "language": "de", "source": "vocarium"}],
    }


@router.put("/projects/{project_id}/plex-binding")
def bind_plex(project_id: str, binding: Binding, user: User = Depends(require_editor)):
    project = project_or_404(project_id, user)
    with LOCK:
        project["binding"] = binding.model_dump()
        project["status"] = "series_ready"
        project["stage"] = max(project["stage"], 2)
        project["updated_at"] = now()
        save_state()
    return project


@router.delete("/projects/{project_id}/mapping", status_code=204)
def reset_mapping(project_id: str, user: User = Depends(require_editor)):
    """Verwirft die Kapitelzuordnung; der nächste Lauf recherchiert sie neu."""
    project = project_or_404(project_id, user)
    if active_project_run(project_id):
        raise HTTPException(409, "Während eines laufenden Laufs kann die Zuordnung nicht zurückgesetzt werden")
    with LOCK:
        project["mapping"] = []
        project["mapping_research"] = None
        if project.get("binding"):
            project["status"] = "series_ready"
            project["stage"] = min(int(project.get("stage") or 2), 2)
        project["updated_at"] = now()
        save_state()
    return Response(status_code=204)


@router.post("/projects/{project_id}/pipeline-runs", status_code=202)
def start_pipeline(project_id: str, user: User = Depends(require_editor)):
    project = project_or_404(project_id, user)
    if not project.get("binding"):
        raise HTTPException(409, "Bitte zuerst eine Plex-Serie auswählen")
    agent_url = os.getenv("CODEX_AGENT_BASE_URL", "").strip()
    if agent_url:
        agent_status = codex_agent_status()
        if not agent_status.get("authenticated"):
            status_code = 409 if agent_status.get("status") == "login_required" else 503
            raise HTTPException(status_code, f"{agent_status['label']}. Bitte die gewählten Provider unter Einstellungen → Terminal anmelden oder als Deployment-Secret konfigurieren.")
    active = active_project_run(project_id)
    if active:
        return active
    run_id = new_id("run")
    run = {
        "id": run_id,
        "project_id": project_id,
        "owner_user_id": user.user_id,
        "status": "queued",
        "stage": "mapping",
        "message": "Analyse wird vorbereitet",
        "completed_units": 0,
        "total_units": max(len(project["chapters"]), 1),
        "created_at": now(),
        "updated_at": now(),
        "error": None,
    }
    with LOCK:
        STATE["runs"][run_id] = run
        save_state()
    threading.Thread(target=run_pipeline, args=(project_id, run_id), daemon=True).start()
    return run


@router.post("/projects/{project_id}/alignments", status_code=202)
def refresh_alignment(project_id: str, user: User = Depends(require_editor)):
    project = project_or_404(project_id, user)
    if not project.get("binding") or not project.get("mapping"):
        raise HTTPException(409, "Für die Neuausrichtung werden eine Plex-Serie und eine geprüfte Zuordnung benötigt")
    active = active_project_run(project_id)
    if active:
        return active
    run_id = new_id("run")
    run = {"id": run_id, "project_id": project_id, "owner_user_id": user.user_id, "status": "queued", "stage": "transcribing", "message": "Forced Alignment wird vorbereitet", "completed_units": 0, "total_units": max(len(project["chapters"]), 1), "created_at": now(), "updated_at": now(), "error": None}
    with LOCK:
        STATE["runs"][run_id] = run
        save_state()
    threading.Thread(target=realign_project, args=(project_id, run_id), daemon=True).start()
    return run


@router.get("/pipeline-runs")
def list_pipeline_runs(
    project_id: str | None = None,
    limit: int = 100,
    user: User = Depends(current_user),
):
    return project_runs(project_id, user)[:max(1, min(250, limit))]


@router.get("/pipeline-runs/{run_id}")
def get_run(run_id: str, user: User = Depends(current_user)):
    with LOCK:
        run = STATE["runs"].get(run_id)
    if not run or not owns(run, user):
        raise HTTPException(404, "Lauf nicht gefunden")
    return run


@router.patch("/projects/{project_id}/cues/{cue_id}")
def update_cue(project_id: str, cue_id: str, patch: CuePatch, user: User = Depends(require_editor)):
    project = project_or_404(project_id, user)
    if active_project_run(project_id):
        raise HTTPException(409, "A production is active; editing is temporarily locked")
    cue = next((item for item in project["cues"] if item["id"] == cue_id), None)
    if not cue:
        raise HTTPException(404, "Erzähler-Cue nicht gefunden")
    with LOCK:
        corrected_text = patch.text.strip()
        cue["text"] = corrected_text
        cue["revision"] = cue.get("revision", 1) + 1
        # Keep the reusable, evidence-bearing script in sync with a deliberate
        # editorial correction. Otherwise a later voice change or replan can
        # silently resurrect the pre-edit wording from semantic_cues.
        semantic_cue = next(
            (
                item
                for item in project.get("semantic_cues", [])
                if item.get("id") == cue_id
            ),
            None,
        )
        if semantic_cue is not None and semantic_cue is not cue:
            semantic_cue["text"] = corrected_text
            semantic_cue["revision"] = semantic_cue.get("revision", 1) + 1
        project["status"] = "script_review"
        project["stage"] = 5
        project["timeline_revision"] = max(int(project.get("timeline_revision", 0)), int((project.get("timeline") or {}).get("revision", 0)))
        project["timeline"] = None
        project["audio_stale"] = bool(project.get("artifacts"))
        invalidate_automatic_quality(project)
        project["updated_at"] = now()
        save_state()
    return cue


@router.patch("/projects/{project_id}/narrator-voice")
def update_narrator_voice(project_id: str, patch: NarratorVoicePatch, user: User = Depends(require_editor)):
    project = project_or_404(project_id, user)
    if active_project_run(project_id):
        raise HTTPException(409, "A production is active; editing is temporarily locked")
    if not project.get("binding"):
        raise HTTPException(409, "Bitte zuerst eine Plex-Serie auswählen")
    voice_id = patch.voice_id.strip()
    voices = fetch_tts_voices()
    if voices is None:
        raise HTTPException(503, "Die Stimmenliste des TTS-Providers ist derzeit nicht erreichbar")
    if voices is not None and voice_id not in {voice["id"] for voice in voices} and voice_id != "default":
        raise HTTPException(422, "Die gewählte Erzählstimme ist beim TTS-Provider nicht verfügbar")
    with LOCK:
        project["binding"]["narrator_voice"] = voice_id
        project["status"] = "script_review"
        project["stage"] = 5
        project["timeline_revision"] = max(int(project.get("timeline_revision", 0)), int((project.get("timeline") or {}).get("revision", 0)))
        project["timeline"] = None
        project["audio_stale"] = bool(project.get("artifacts"))
        invalidate_automatic_quality(project)
        project["updated_at"] = now()
        save_state()
    return {"voice_id": voice_id, "status": "saved"}


@router.patch("/projects/{project_id}/narration-name-lexicon")
def update_narration_name_lexicon(
    project_id: str,
    patch: NarrationNameLexiconPatch,
    user: User = Depends(require_editor),
):
    project = project_or_404(project_id, user)
    if active_project_run(project_id):
        raise HTTPException(409, "A production is active; editing is temporarily locked")
    entries = normalize_narration_name_lexicon([entry.model_dump() for entry in patch.entries])
    if len(entries) != len(patch.entries):
        raise HTTPException(422, "Jeder Synchronname benötigt mindestens einen eindeutigen Alias")
    with LOCK:
        previous_entries = normalize_narration_name_lexicon(
            project.get("narration_name_lexicon", [])
        )
        previous_cue_texts = [str(cue.get("text") or "") for cue in project.get("cues", [])]
        project["narration_rules"] = narration_rules(project)
        apply_narration_name_lexicon(project, entries)
        render_required = previous_cue_texts != [
            str(cue.get("text") or "") for cue in project.get("cues", [])
        ]
        if render_required:
            project["timeline_revision"] = max(
                int(project.get("timeline_revision", 0)),
                int((project.get("timeline") or {}).get("revision", 0)),
            )
            project["timeline"] = None
            project["audio_stale"] = bool(project.get("artifacts"))
            invalidate_automatic_quality(project)
            project["status"] = "script_review"
            project["stage"] = 5
        elif previous_entries != project["narration_name_lexicon"]:
            # Name evidence participates in formal character-introduction
            # validation even when the already-spoken cue text is unchanged.
            invalidate_automatic_quality(project)
        project["updated_at"] = now()
        save_state()
    return {
        "entries": project["narration_name_lexicon"],
        "status": "saved",
        "render_required": render_required,
    }


@router.post("/projects/{project_id}/renders", status_code=202)
def rerender(project_id: str, user: User = Depends(require_editor)):
    project = project_or_404(project_id, user)
    if not project.get("cues"):
        raise HTTPException(409, "Es gibt noch kein freigegebenes Skript")
    with LOCK:
        active = active_project_run(project_id)
        if active:
            return active
        run_id = new_id("run")
        run = {"id": run_id, "project_id": project_id, "owner_user_id": user.user_id, "status": "queued", "stage": "synthesizing", "message": "Neuer Render wird vorbereitet", "completed_units": 0, "total_units": len(project["cues"]), "created_at": now(), "updated_at": now(), "error": None}
        STATE["runs"][run_id] = run
        save_state()
    threading.Thread(target=render_only, args=(project_id, run_id), daemon=True).start()
    return run


@router.post("/projects/{project_id}/narration-plans", status_code=202)
def regenerate_narration(project_id: str, request: NarrationPlanRequest, user: User = Depends(require_editor)):
    project = project_or_404(project_id, user)
    if not project.get("mapping") or not project.get("transcript"):
        raise HTTPException(409, "Für ein neues Erzähler-Skript werden Zuordnung und Transkript benötigt")
    active = active_project_run(project_id)
    if active:
        return active
    run_id = new_id("run")
    run = {
        "id": run_id,
        "project_id": project_id,
        "owner_user_id": user.user_id,
        "status": "queued",
        "stage": "writing",
        "message": (
            "Professionelles Hörspiel-Skript wird aufgebaut"
            if request.density == "audio_drama"
            else "Erzähler-Skript wird aufgebaut"
        ),
        "completed_units": 0,
        "total_units": len(unique_episode_ids(project.get("mapping", []))),
        "created_at": now(),
        "updated_at": now(),
        "error": None,
    }
    with LOCK:
        STATE["runs"][run_id] = run
        save_state()
    threading.Thread(target=rewrite_narration, args=(project_id, run_id, request.density), daemon=True).start()
    return run


@router.post("/projects/{project_id}/scene-evidence")
def save_scene_evidence(project_id: str, request: SceneEvidenceRequest, user: User = Depends(require_editor)) -> dict[str, Any]:
    """Save reviewed picture sources and anchor corrections without starting work."""
    project = project_or_404(project_id, user)
    with LOCK:
        if active_project_run(project_id):
            raise HTTPException(409, "Scene evidence cannot change during an active production")
        rows = [s for s in project.get("reconciled_transcript", []) if str(s.get("episode_id")) == request.episode_id]
        if not rows:
            raise HTTPException(400, "Episode has no reconciled source transcript")
        row_ids = {str(s["id"]) for s in rows}
        if any(a.anchor_segment_id not in row_ids for a in request.narration_anchors):
            raise HTTPException(400, "Narration anchor must belong to this episode")
        duration = max((int(a.get("duration_ms") or 0) for a in project.get("media_assets", [])
                        if str(a.get("episode_id")) == request.episode_id), default=0)
        if any(p.start_ms >= duration or not transcript_item_outside_intros(project, {
            "episode_id": request.episode_id, "episode_start_ms": p.start_ms, "episode_end_ms": p.start_ms + 1,
        }) for p in request.pictures):
            raise HTTPException(400, "Picture evidence must be inside retained episode content")
        pictures = {str(s["id"]): dict(s) for s in project.get("reviewed_visual_scene_anchors", [])}
        for picture in request.pictures:
            reference = min(rows, key=lambda s: abs(int(s["episode_start_ms"]) - picture.start_ms))
            offset = int(reference.get("start_sample") or 0) - int(reference["episode_start_ms"]) * 48
            sample = offset + picture.start_ms * 48
            identifier = f"visual_{request.episode_id}_{picture.start_ms}"
            pictures[identifier] = {
                "id": identifier, "episode_id": request.episode_id,
                "episode_start_ms": picture.start_ms, "episode_end_ms": picture.start_ms + 1,
                "start_sample": sample, "end_sample": sample + 48,
                "start_ms": sample // 48, "end_ms": sample // 48 + 1,
                "text": picture.visual_evidence, "reconciled_text": picture.visual_evidence,
                "visual_scene_anchor": True, "visual_evidence": picture.visual_evidence,
                "evidence_frames": picture.evidence_frames, "narration_candidate": True,
                "timestamp_source": "reviewed_original_picture", "confidence": 0.95,
                "reviewed_at": now(), "reviewed_by_user_id": user.user_id,
            }
        overrides = {(str(a["episode_id"]), str(a["source_digest"])): dict(a)
                     for a in project.get("reviewed_narration_anchors", [])}
        row_by_id = {str(row["id"]): row for row in rows}
        for anchor in request.narration_anchors:
            overrides[(request.episode_id, anchor.source_digest)] = {
                **anchor.model_dump(), "episode_id": request.episode_id,
                "anchor_evidence": reviewed_anchor_evidence(row_by_id[anchor.anchor_segment_id]),
            }
        project["reviewed_visual_scene_anchors"] = list(pictures.values())
        project["reviewed_narration_anchors"] = list(overrides.values())
        project["reconciled_transcript"] = [s for s in project["reconciled_transcript"] if str(s["id"]) not in pictures] + list(pictures.values())
        project["updated_at"] = now()
        save_state()
    return {"episode_id": request.episode_id, "pictures_saved": len(request.pictures),
            "anchors_saved": len(request.narration_anchors), "production_started": False}


class QualityRepairRequest(BaseModel):
    cue_ids: list[str] = Field(default_factory=list)
    preserve_valid_cues: bool = False


def editorial_repair_cue_ids(project: dict[str, Any]) -> set[str]:
    rules = narration_rules(project)
    drama = project.get("narration_density") == "audio_drama"
    contract = int((project.get("narration_density_contract") or {}).get("editorial_contract_version") or 0) >= NARRATION_EDITOR_VERSION
    minimum = float(rules["audio_drama_source_coverage_min" if drama else "detailed_source_coverage_min"])
    cues = project.get("cues", [])
    rejected = {str(cue["id"]) for cue in cues if not cue_editorial_metadata_complete(cue)
                or (contract and not cue_novelistic_metadata_complete(cue, minimum))
                or (contract and drama and not cue_audio_drama_contract_complete(cue))}
    if contract:
        rejected.update(str(item["cue_id"]) for item in native_audio_repetition_violations(project, cues))
    return rejected


def ungrounded_narration_cue_ids(project: dict[str, Any]) -> set[str]:
    expected_episodes = set(unique_episode_ids(project.get("mapping", [])))
    introductions = grounded_character_introduction_cue_ids(project)
    pictures = grounded_picture_cue_ids(project)
    minimum = float(narration_rules(project)["dense_action_min_confidence"])
    return {
        str(cue.get("id") or "") for cue in project.get("cues", [])
        if (not cue.get("source_fragment_ids") and str(cue.get("id") or "") not in introductions | pictures)
        or not cue.get("anchor_segment_id")
        or str(cue.get("anchor_episode_id") or "") not in expected_episodes
        or float(cue.get("confidence") or 0) < minimum
    }


def obsolete_introduction_candidate_ids(
    project: dict[str, Any], research: dict[str, Any],
) -> set[str]:
    """Synthetic intro candidates have no action source outside their required first episode."""
    normalized = normalize_character_introduction_contexts(project, research)
    identities = character_identity_keys(project, normalized)
    def key(name: Any) -> str:
        value = "".join(normalized_character_evidence(name).split())
        return identities.get(value, value)
    required = {
        (key(character.get("name")), str(context.get("episode_id") or ""))
        for context in normalized.get("episode_contexts", [])
        for character in context.get("character_introductions", [])
        if character.get("introduction_required")
    }
    return {
        str(cue.get("id") or "") for cue in project.get("cues", [])
        if cue.get("character_introduction_candidate") and not cue.get("source_fragment_ids")
        and (key(cue.get("introduction_research_name")), str(cue.get("anchor_episode_id") or "")) not in required
    }


def quality_repair_targets(project: dict[str, Any]) -> tuple[set[str], list[str]]:
    """Leitet aus den offenen Qualitaetskriterien die zu reparierenden Cues ab."""
    report = project.get("quality_report") or {}
    cue_ids: set[str] = set()
    reasons: list[str] = []
    cues = project.get("cues", [])
    intros = project.get("character_introductions", [])
    for check in report.get("checks", []):
        if check.get("status") != "failed":
            continue
        detail = str(check.get("detail") or "")
        check_id = str(check.get("id") or "")
        found = {str(cue.get("id")) for cue in cues if str(cue.get("id") or "") and re.search(r"(?<![\w])" + re.escape(str(cue["id"])) + r"(?![\w])", detail)}
        found.update(str(value) for value in check.get("cue_ids", []))
        if check_id == "narration_editorial_quality":
            found |= editorial_repair_cue_ids(project)
        if check_id == "narration_grounding":
            found |= ungrounded_narration_cue_ids(project)
        if check_id == "narration_coverage":
            protected = {str(intro.get("cue_id")) for intro in intros}
            for gap in (check.get("actual") or {}).get("gaps", []):
                found.update(str(gap[key]) for key in ("before_cue_id", "after_cue_id")
                             if gap.get(key) and str(gap[key]) not in protected)

        if check_id == "character_introduction_timing" or (
            check_id == "narration_editorial_quality" and quality_repair_needs_introductions(project)
        ):
            # Duplicates and missing mappings are global: a truncated display
            # string must not keep a broken introduction locked.
            found |= {str(intro.get("cue_id") or "") for intro in intros}
            found |= {str(c.get("id")) for c in cues if c.get("narrative_purpose") == "character_introduction"}
        if found:
            cue_ids |= {cue_id for cue_id in found if cue_id}
            reasons.append(f"{check.get('label') or check_id}: {detail[:400]}")
    valid = {str(c.get("id")) for c in cues}
    return cue_ids & valid, reasons


def quality_repair_needs_coverage(project: dict[str, Any]) -> bool:
    """Offene Kommentarluecken oder Dichte-Verstoesse verlangen zusaetzliche Cues."""
    report = project.get("quality_report") or {}
    for check in report.get("checks", []):
        if check.get("status") != "failed":
            continue
        check_id = str(check.get("id") or "")
        detail = str(check.get("detail") or "")
        if check_id == "narration_coverage" or "Cue-Dichte verletzt" in detail:
            return True
    return False


@router.get("/projects/{project_id}/quality-repair-scope")
def quality_repair_scope(project_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
    project = project_or_404(project_id, user)
    targets, reasons = quality_repair_targets(project)
    coverage = quality_repair_needs_coverage(project)
    introductions = quality_repair_needs_introductions(project)
    return {"cue_ids": sorted(targets), "locked_count": len(project.get("cues", [])) - len(targets),
            "adds_coverage": coverage, "reasons": reasons, "adds_introductions": introductions, "available": bool(targets or coverage or introductions),
            "sample_rate": SAMPLE_RATE}


@router.post("/projects/{project_id}/quality-repairs", status_code=202)
def repair_from_quality_report(project_id: str, request: QualityRepairRequest, user: User = Depends(require_editor)):
    """Gezielter Reparaturlauf statt Komplettneubau.

    Nimmt die im Qualitaetsreport beanstandeten Cues (oder die uebergebenen),
    laesst nur diese vom Agenten neu schreiben, uebernimmt alle anderen
    wortgleich und rendert danach neu.
    """
    project = project_or_404(project_id, user)
    if not project.get("cues"):
        raise HTTPException(409, "Für einen Reparaturlauf muss bereits ein Skript vorliegen")
    active = active_project_run(project_id)
    if active:
        return active
    valid = {str(c.get("id")) for c in project.get("cues", [])}
    cue_ids = {cue_id for cue_id in request.cue_ids if cue_id in valid}
    reasons: list[str] = []
    if not cue_ids:
        cue_ids, reasons = quality_repair_targets(project)
    repair_coverage = quality_repair_needs_coverage(project)
    repair_introductions = quality_repair_needs_introductions(project)
    if request.preserve_valid_cues and not request.cue_ids:
        # Additions can close measured gaps without rewriting their valid
        # neighbours. Other failed content/timing checks still release cues.
        content_report = {**(project.get("quality_report") or {}), "checks": [
            check for check in (project.get("quality_report") or {}).get("checks", [])
            if check.get("id") != "narration_coverage"
        ]}
        cue_ids, reasons = quality_repair_targets({**project, "quality_report": content_report})
    if repair_coverage:
        reasons.append("Kommentarlücken: zu lange Strecken ohne Kommentar bzw. zu wenige Cues je Folge werden mit zusätzlichen Cues geschlossen")
    if repair_introductions:
        reasons.append("Figuren pro Buch genau einmal in ihrer ersten belegten Folge einführen; fehlende Zuordnungen ergänzen und Mehrfachzuordnungen entfernen")
    if not cue_ids and not repair_coverage and not repair_introductions:
        raise HTTPException(409, "Der Qualitätsreport benennt keine reparierbaren Cues")
    run_id = new_id("run")
    run = {
        "id": run_id,
        "project_id": project_id,
        "owner_user_id": user.user_id,
        "quality_repair": True,
        "status": "queued",
        "stage": "writing",
        "message": f"Reparaturlauf für {len(cue_ids)} beanstandete Cues",
        "completed_units": 0,
        "total_units": len(unique_episode_ids(project.get("mapping", []))),
        "created_at": now(),
        "updated_at": now(),
        "error": None,
    }
    with LOCK:
        STATE["runs"][run_id] = run
        save_state()
    density = str(project.get("narration_density") or "audio_drama")
    threading.Thread(
        target=rewrite_narration,
        args=(project_id, run_id, density, cue_ids, "Qualitätsreport: " + " | ".join(reasons)[:1200], repair_coverage, repair_introductions),
        daemon=True,
    ).start()
    return run


@router.post("/projects/{project_id}/subtitle-reconciliations", status_code=202)
def reconcile_subtitles(project_id: str, request: SubtitleReconciliationRequest, user: User = Depends(require_editor)):
    project = project_or_404(project_id, user)
    if not project.get("binding") or not project.get("mapping") or not project.get("transcript"):
        raise HTTPException(409, "Für den Untertitelabgleich werden Plex-Zuordnung und Transkript benötigt")
    active = active_project_run(project_id)
    if active:
        return active
    run_id = new_id("run")
    episode_count = len(unique_episode_ids(project.get("mapping", [])))
    run = {
        "id": run_id,
        "project_id": project_id,
        "owner_user_id": user.user_id,
        "status": "queued",
        "stage": "reconciling_subtitles",
        "message": "Plex-Untertitel werden vorbereitet",
        "completed_units": 0,
        "total_units": episode_count,
        "created_at": now(),
        "updated_at": now(),
        "error": None,
    }
    with LOCK:
        STATE["runs"][run_id] = run
        save_state()
    threading.Thread(target=reconcile_project_subtitles, args=(project_id, run_id, request.language), daemon=True).start()
    return run


@router.post("/projects/{project_id}/listener-reviews", status_code=201)
def create_listener_review(
    project_id: str,
    request: ListenerReviewRequest,
    user: User = Depends(require_editor),
):
    project = project_or_404(project_id, user)
    if not project.get("timeline") or not project.get("artifacts"):
        raise HTTPException(409, "Kalibrierungsfeedback benötigt einen fertigen Render")
    ensure_project_quality_state(project)
    report = project.get("quality_report") or {}
    findings = list(dict.fromkeys(item.strip()[:120] for item in request.findings if item.strip()))
    review_round = {
        "id": new_id("review"),
        "created_at": now(),
        "reviewer": user.username,
        "timeline_revision": int(project["timeline"].get("revision", 0)),
        "artifact_sha256": str(project["artifacts"][0].get("sha256") or ""),
        "automatic_status": report.get("status", "not_run"),
        "verdict": request.verdict,
        "score": round(request.score, 4),
        "notes": request.notes.strip(),
        "findings": findings,
    }
    with LOCK:
        review = {**default_narration_review(), **dict(project.get("narration_review") or {})}
        review["rounds"] = [*list(review.get("rounds") or []), review_round]
        project["narration_review"] = review
        sync_narration_review(project)
        project["updated_at"] = now()
        save_state()
    return project["narration_review"]


@router.get("/projects/{project_id}/cover")
def project_cover(project_id: str, user: User = Depends(current_user)):
    """Poster der gebundenen Plex-Serie als Projektcover (600×900, serverseitig
    skaliert und unter artifacts/covers zwischengespeichert). Ohne Bindung oder
    im Demo-Modus 404, damit Clients ihren typografischen Ersatz behalten."""
    project = project_or_404(project_id, user)
    binding = project.get("binding") or {}
    series_id = str(binding.get("series_id") or "")
    if not series_id or series_id.startswith("demo_") or not re.fullmatch(r"[0-9]+", series_id):
        raise HTTPException(404, "Kein Cover")
    covers = ARTIFACTS / "covers"
    covers.mkdir(parents=True, exist_ok=True)
    target = covers / f"plex-{series_id}.jpg"
    if not target.is_file() or target.stat().st_size == 0:
        try:
            meta = plex_json(f"/library/metadata/{series_id}").get("MediaContainer", {}).get("Metadata", [])
            thumb = (meta[0].get("thumb") if meta else None) or ""
            if not thumb.startswith("/"):
                raise HTTPException(404, "Kein Cover")
            config = STATE["integrations"].get("plex", {})
            base_url = (os.getenv("PLEX_BASE_URL") or config.get("base_url") or "").rstrip("/")
            response = httpx.get(
                f"{base_url}/photo/:/transcode",
                params={"width": 600, "height": 900, "minSize": 1, "upscale": 1, "url": thumb, "X-Plex-Token": secret_value("PLEX_TOKEN")},
                headers={"X-Plex-Client-Identifier": "series-audio-webui"},
                timeout=20,
            )
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                raise HTTPException(404, "Kein Cover")
            tmp = target.with_suffix(".part")
            tmp.write_bytes(response.content)
            tmp.replace(target)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(404, "Kein Cover") from error
    return FileResponse(target, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/artifacts/{artifact_id}/content")
def artifact_content(artifact_id: str, user: User = Depends(current_user)):
    for project in list(STATE["projects"].values()):
        if not owns(project, user):
            continue
        artifact = next((item for item in project.get("artifacts", []) + project.get("audio_history", []) if item["id"] == artifact_id), None)
        if artifact:
            path = (ARTIFACTS / artifact["storage_name"]).resolve()
            if ARTIFACTS.resolve() not in path.parents or not path.exists():
                raise HTTPException(404, "Artefakt nicht gefunden")
            return FileResponse(path, media_type=artifact["mime_type"], filename=artifact["filename"])
    raise HTTPException(404, "Artefakt nicht gefunden")


@router.get("/integrations")
def integrations(user: User = Depends(require_admin)):
    safe = json.loads(json.dumps(STATE["integrations"]))
    plex = safe["plex"]
    plex["configured"] = bool(os.getenv("PLEX_BASE_URL") or plex.get("base_url"))
    plex.pop("token", None)
    safe["plex"] = integration_health("plex", os.getenv("PLEX_BASE_URL"), "/identity", token=secret_value("PLEX_TOKEN"))
    engine_voices = engine_voice_map()
    omnivoice_count = sum(1 for value in engine_voices.values() if value == "omnivoice")
    kikiri_count = len(engine_voices) - omnivoice_count
    safe["tts"] = (
        {"status": "connected", "label": f"OmniVoice ({omnivoice_count}) und Kikiri ({kikiri_count})", "configured": True}
        if engine_voices
        else {"status": "offline", "label": "Sprach-Engines antworten nicht", "configured": True}
    )
    safe["vocarium"] = integration_health(
        "Whisper-STT", os.getenv("WHISPER_URL", "http://whisper-stt:8000"), "/health"
    )
    safe["codex"] = codex_agent_status()
    if safe["vocarium"]["status"] == "connected":
        safe["aligner"] = {"status": "connected", "label": "Whisper-Wortzeitmarken aktiv", "configured": True}
    else:
        safe["aligner"] = {"status": "offline", "label": "Wortzeitmarken warten auf Whisper-STT", "configured": True}
    return safe


@router.get("/settings/codex")
def get_codex_settings(user: User = Depends(require_admin)):
    return codex_settings_response()


@router.put("/settings/codex")
def update_codex_settings(patch: CodexSettingsPatch, user: User = Depends(require_admin)):
    requested_model = patch.model.strip()
    model = CODEX_MODEL_ALIASES.get(requested_model, requested_model)
    if model not in CODEX_MODEL_VALUES:
        supported = ", ".join(sorted(CODEX_MODEL_VALUES))
        raise HTTPException(422, f"Codex-Modell wird für den ChatGPT-Login nicht unterstützt. Erlaubt: {supported}")
    with LOCK:
        STATE["codex_settings"] = {
            "model": model,
            "reasoning_effort": patch.reasoning_effort,
            "timeout_seconds": patch.timeout_seconds,
            "updated_at": now(),
        }
        save_state()
    return codex_settings_response()


@router.get("/settings/agents")
def get_agent_settings(user: User = Depends(require_admin)):
    return agent_settings_response()


@router.put("/settings/agents")
def update_agent_settings(patch: AgentSettingsPatch, user: User = Depends(require_admin)):
    profiles = {
        "research": normalized_agent_profile(patch.research.model_dump()),
        "scripting": normalized_agent_profile(patch.scripting.model_dump()),
    }
    for profile, requested in (("research", patch.research), ("scripting", patch.scripting)):
        requested_chain = [requested, *requested.fallbacks]
        normalized_chain = [profiles[profile], *profiles[profile]["fallbacks"]]
        providers = [candidate.provider for candidate in requested_chain]
        if len(providers) != len(set(providers)):
            raise HTTPException(422, f"{profile}: Jeder Provider darf in der Kette nur einmal vorkommen")
        for requested_candidate, normalized_candidate in zip(requested_chain, normalized_chain):
            requested_model = CODEX_MODEL_ALIASES.get(
                requested_candidate.model.strip(), requested_candidate.model.strip()
            )
            if normalized_candidate["model"] != requested_model:
                supported = ", ".join(
                    option["value"] for option in agent_model_options()[requested_candidate.provider]
                )
                raise HTTPException(
                    422,
                    f"Modell wird von {requested_candidate.provider} nicht unterstützt. Erlaubt: {supported}",
                )
    with LOCK:
        STATE["agent_settings"] = {
            **dict(STATE.get("agent_settings") or {}),
            **profiles,
            "updated_at": now(),
        }
        save_state()
    return agent_settings_response()


def sync_agent_catalogs(result: dict[str, Any]) -> dict[str, Any]:
    catalogs = result.get("catalogs")
    if isinstance(catalogs, dict) and catalogs:
        with LOCK:
            stored = STATE.setdefault("agent_settings", {})
            if stored.get("catalogs") != catalogs:
                stored["catalogs"] = catalogs
                save_state()
    return result


@router.get("/settings/agents/maintenance")
def get_agent_maintenance(user: User = Depends(require_admin)) -> dict[str, Any]:
    return sync_agent_catalogs(agent_control_request("GET", "/maintenance"))


@router.post("/settings/agents/maintenance/{action:path}", status_code=202)
def start_agent_maintenance(action: str, user: User = Depends(require_admin)) -> dict[str, Any]:
    if action not in {"check", "models/refresh", "clis/codex/update", "clis/claude/update"}:
        raise HTTPException(404, "Unknown maintenance action")
    # Admin maintenance is global: another user's production also owns the runner.
    if action.startswith("clis/"):
        with LOCK:
            if any(run.get("status") in {"queued", "running"} for run in STATE.get("runs", {}).values()):
                raise HTTPException(409, "An audio-drama production is active. Update the CLI after it finishes.")
    return sync_agent_catalogs(agent_control_request("POST", "/maintenance/" + action))


@router.get("/settings/agents/status")
def get_agent_provider_status(user: User = Depends(require_admin)):
    status = codex_agent_status()
    return {
        "status": status.get("status", "offline"),
        "label": status.get("label", "KI-Runner antwortet nicht"),
        "providers": status.get("providers", {}),
    }


@router.post("/settings/agents/{provider}/login-sessions", status_code=201)
def create_cli_login_session(provider: Literal["codex", "claude"], user: User = Depends(require_admin)):
    result = agent_control_request("POST", f"/auth/cli/{provider}/start")
    session_id = str(result.get("id") or "")
    if not session_id:
        raise HTTPException(502, "KI-Runner lieferte keine Login-Sitzung")
    with LOCK:
        CLI_LOGIN_OWNERS[session_id] = user.username
    return result


def owned_cli_login_session(session_id: str, user: User) -> None:
    with LOCK:
        owner = CLI_LOGIN_OWNERS.get(session_id)
    if owner != user.username:
        raise HTTPException(404, "Login-Sitzung nicht gefunden oder abgelaufen")


@router.get("/settings/agents/login-sessions/{session_id}")
def get_cli_login_session(session_id: str, user: User = Depends(require_admin)):
    owned_cli_login_session(session_id, user)
    return agent_control_request("GET", f"/auth/cli/sessions/{session_id}")


@router.post("/settings/agents/login-sessions/{session_id}/code")
def submit_cli_login_code(session_id: str, patch: CliLoginCode, user: User = Depends(require_admin)):
    owned_cli_login_session(session_id, user)
    return agent_control_request("POST", f"/auth/cli/sessions/{session_id}/code", {"code": patch.code.strip()})


@router.delete("/settings/agents/login-sessions/{session_id}", status_code=204)
def delete_cli_login_session(session_id: str, user: User = Depends(require_admin)):
    owned_cli_login_session(session_id, user)
    agent_control_request("DELETE", f"/auth/cli/sessions/{session_id}")
    with LOCK:
        CLI_LOGIN_OWNERS.pop(session_id, None)
    return Response(status_code=204)


@router.put("/settings/agents/zai-credentials")
def update_zai_credentials(patch: ZaiCredentialsPatch, user: User = Depends(require_admin)):
    return agent_control_request(
        "PUT",
        "/auth/zai",
        {"api_key": patch.api_key, "base_url": patch.base_url.strip()},
    )


@router.put("/integrations/{provider}/configuration")
def configure_integration(provider: str, patch: IntegrationPatch, user: User = Depends(require_admin)):
    if provider not in STATE["integrations"]:
        raise HTTPException(404, "Integration nicht bekannt")
    with LOCK:
        target = STATE["integrations"][provider]
        if patch.base_url is not None:
            if not patch.base_url.startswith(("http://", "https://")):
                raise HTTPException(422, "Basis-URL muss mit http:// oder https:// beginnen")
            target["base_url"] = patch.base_url.rstrip("/")
        if patch.token:
            raise HTTPException(501, "Secrets werden in diesem Release nur per Container-Secret bzw. Umgebungsvariable gesetzt und nie in state.json gespeichert")
        if patch.language:
            target["language"] = patch.language
        target["status"] = "configured"
        target["label"] = "Konfiguriert – Neustart für Workerzugriff empfohlen"
        save_state()
    return {"provider": provider, "configured": True, "status": target["status"], "label": target["label"]}


def project_or_404(project_id: str, user: "User") -> dict[str, Any]:
    """Ein fremdes Projekt ist nicht verboten, sondern nicht vorhanden.

    Szenenklang war Einzelmandant: Projekte trugen zwar ein ``owner``-Feld,
    aber weder Liste noch Detail noch Löschen filterten danach.
    """
    with LOCK:
        project = STATE["projects"].get(project_id)
    if not project or not owns(project, user):
        raise HTTPException(404, "Projekt nicht gefunden")
    return project


def extract_text(path: Path, suffix: str) -> str:
    """Dokumentenimport über die Hörbuch-Pipeline.

    Szenenklang brachte dafür ``pypdf``, ``python-docx`` und ``ebooklib`` mit.
    ``audiobooks.text_pipeline`` leistet dasselbe ohne Zusatzabhängigkeit und
    liefert zusätzlich Kapitelüberschriften, die hier erhalten bleiben und von
    ``split_chapters`` weiterverwendet werden.
    """
    from audiobooks.text_pipeline import parse_document

    if suffix in {".txt", ".md"}:
        return path.read_text("utf-8", errors="replace")
    fmt = {".pdf": "pdf", ".docx": "docx", ".epub": "epub"}.get(suffix)
    if not fmt:
        raise ValueError("Dateiformat nicht unterstützt")
    parsed = parse_document(path.read_bytes(), fmt)
    blocks: list[str] = []
    for chapter in parsed.chapters:
        title = str(getattr(chapter, "title", "") or "").strip()
        body = str(getattr(chapter, "content", "") or "").strip()
        if title and body:
            blocks.append(f"{title}\n\n{body}")
        elif body:
            blocks.append(body)
    return "\n\n".join(blocks)


def split_chapters(text: str) -> list[dict[str, Any]]:
    cleaned = re.sub(r"\r\n?", "\n", text).strip()
    marker = re.compile(r"(?im)^(?:kapitel|chapter)\s+([\divxlc]+)(?:\s*[:\-–]\s*|\s+)?(.*)$")
    matches = list(marker.finditer(cleaned))
    parts: list[tuple[str, str]] = []
    if matches:
        preface = cleaned[:matches[0].start()].strip()
        # A short Markdown document title is metadata, not an extra chapter.
        if preface and not (len(preface) < 120 and all(line.lstrip().startswith("#") for line in preface.splitlines() if line.strip())):
            parts.append(("Auftakt", preface))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
            name = match.group(2).strip() or f"Kapitel {match.group(1)}"
            parts.append((name, cleaned[match.end():end].strip()))
    else:
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", cleaned) if item.strip()]
        chunk_size = max(1, math.ceil(len(paragraphs) / min(6, max(1, len(paragraphs) // 5))))
        for index in range(0, len(paragraphs), chunk_size):
            parts.append((f"Kapitel {len(parts) + 1}", "\n\n".join(paragraphs[index:index + chunk_size])))
    chapters = []
    for index, (title, body) in enumerate(parts):
        if not body:
            continue
        chapter_id = new_id("ch")
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        fragments = [{"id": new_id("frag"), "text": p, "order": number + 1, "sha256": hashlib.sha256(p.encode()).hexdigest()} for number, p in enumerate(paragraphs)]
        chapters.append({"id": chapter_id, "order": index + 1, "title": title[:160], "characters": len(body), "preview": body[:320], "fragments": fragments})
    return chapters or [{"id": new_id("ch"), "order": 1, "title": "Kapitel 1", "characters": len(cleaned), "preview": cleaned[:320], "fragments": [{"id": new_id("frag"), "text": cleaned, "order": 1, "sha256": hashlib.sha256(cleaned.encode()).hexdigest()}]}]


def fetch_plex_series(query: str) -> list[dict[str, Any]] | None:
    config = STATE["integrations"].get("plex", {})
    base_url = os.getenv("PLEX_BASE_URL") or config.get("base_url")
    token = secret_value("PLEX_TOKEN")
    sections = [item.strip() for item in os.getenv("PLEX_LIBRARY_SECTION", "").split(",") if item.strip()]
    if not base_url or not token or not sections:
        return None
    try:
        found: dict[str, dict[str, Any]] = {}
        for section in sections:
            response = httpx.get(
                f"{base_url.rstrip('/')}/library/sections/{section}/all",
                params={"type": 2, "title": query, "X-Plex-Token": token},
                headers={"Accept": "application/json", "X-Plex-Client-Identifier": "series-audio-webui"},
                timeout=8,
            )
            response.raise_for_status()
            for item in response.json().get("MediaContainer", {}).get("Metadata", []):
                found[str(item["ratingKey"])] = {"id": str(item["ratingKey"]), "title": item.get("title", "Unbenannt"), "year": item.get("year"), "seasons": item.get("childCount", 0), "episodes": item.get("leafCount", 0), "summary": item.get("summary", "")[:240]}
        return sorted(found.values(), key=lambda item: (item["title"].casefold(), item.get("year") or 0))
    except Exception as error:
        raise RuntimeError("Plex ist konfiguriert, aber derzeit nicht erreichbar") from error


def plex_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    config = STATE["integrations"].get("plex", {})
    base_url = os.getenv("PLEX_BASE_URL") or config.get("base_url")
    token = secret_value("PLEX_TOKEN")
    if not base_url or not token:
        raise RuntimeError("Plex ist nicht vollständig konfiguriert")
    response = httpx.get(
        f"{base_url.rstrip('/')}{path}",
        params=params,
        headers={"Accept": "application/json", "X-Plex-Token": token, "X-Plex-Client-Identifier": "series-audio-webui"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def fetch_plex_episodes(series_id: str) -> list[dict[str, Any]]:
    leaves = plex_json(f"/library/metadata/{series_id}/allLeaves").get("MediaContainer", {}).get("Metadata", [])
    episodes = []
    for leaf in leaves:
        detail = plex_json(f"/library/metadata/{leaf['ratingKey']}").get("MediaContainer", {}).get("Metadata", [])
        if not detail:
            continue
        item = detail[0]
        media = item.get("Media") or []
        if not media:
            continue
        selected_media = media[0]
        parts = selected_media.get("Part") or []
        if not parts:
            continue
        part = parts[0]
        audio_streams = [stream for stream in part.get("Stream", []) if stream.get("streamType") == 2]
        subtitle_streams = [stream for stream in part.get("Stream", []) if stream.get("streamType") == 3]
        episodes.append({
            "id": str(item["ratingKey"]),
            "season": int(item.get("parentIndex") or 0),
            "episode": int(item.get("index") or 0),
            "title": item.get("title", "Unbenannt"),
            "summary": item.get("summary", ""),
            "originally_available_at": item.get("originallyAvailableAt"),
            "duration_ms": int(item.get("duration") or part.get("duration") or 0),
            "part_key": part.get("key"),
            "server_file": part.get("file"),
            "media_count": len(media),
            "part_count": len(parts),
            "audio_streams": [{"index": index, "id": stream.get("id"), "language": stream.get("language"), "language_code": stream.get("languageCode"), "codec": stream.get("codec"), "channels": stream.get("channels"), "selected": bool(stream.get("selected"))} for index, stream in enumerate(audio_streams)],
            "subtitle_streams": [{"index": index, "id": stream.get("id"), "language": stream.get("language"), "language_code": stream.get("languageCode"), "codec": stream.get("codec"), "title": stream.get("title") or stream.get("displayTitle"), "selected": bool(stream.get("selected")), "forced": bool(stream.get("forced"))} for index, stream in enumerate(subtitle_streams)],
        })
    return sorted(episodes, key=lambda item: (item["season"], item["episode"], item["id"]))


def local_plex_media_path(server_file: str) -> Path:
    server_root = Path(os.getenv("PLEX_MEDIA_SERVER_ROOT", "/data"))
    local_root = Path(os.getenv("PLEX_MEDIA_LOCAL_ROOT", "/plex-media")).resolve()
    try:
        relative = Path(server_file).relative_to(server_root)
    except ValueError as error:
        raise RuntimeError("Plex-Medienpfad liegt außerhalb des konfigurierten Server-Roots") from error
    target = (local_root / relative).resolve(strict=True)
    if target != local_root and local_root not in target.parents:
        raise RuntimeError("Aufgelöster Plex-Medienpfad verlässt den erlaubten Root")
    return target


def select_episode_audio(episode: dict[str, Any], language: str) -> dict[str, Any]:
    streams = episode.get("audio_streams") or []
    if not streams:
        raise RuntimeError(f"Plex-Episode {episode.get('id')} enthält keinen Audiostream")
    wanted = {"de": "deu", "en": "eng", "ja": "jpn"}.get(language, language)
    selected = next((stream for stream in streams if stream.get("language_code") == wanted), None)
    selected = selected or next((stream for stream in streams if stream.get("selected")), None) or streams[0]
    episode["selected_audio"] = selected
    return selected


def select_episode_subtitle(episode: dict[str, Any], language: str) -> dict[str, Any] | None:
    streams = episode.get("subtitle_streams") or []
    wanted = {"de": "deu", "en": "eng", "ja": "jpn"}.get(language, language)
    same_language = [stream for stream in streams if stream.get("language_code") == wanted]
    candidates = same_language or streams
    selected = next((stream for stream in candidates if not stream.get("forced")), None)
    selected = selected or next((stream for stream in candidates if stream.get("selected")), None) or (candidates[0] if candidates else None)
    episode["selected_subtitle"] = selected
    return selected


def extract_episode_audio(episode: dict[str, Any], destination: Path) -> Path:
    return _extract_cached_episode_audio(episode, destination, 16000, "flac")


def _extract_cached_episode_audio(episode: dict[str, Any], destination: Path, sample_rate: int, codec: str) -> Path:
    source = local_plex_media_path(str(episode.get("server_file") or ""))
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected_ms = int(episode.get("duration_ms") or 0)

    def complete(path: Path) -> bool:
        if not path.exists() or path.stat().st_size <= 1024:
            return False
        try:
            duration = media_duration_ms(path)
            return duration > 1 and (not expected_ms or abs(duration - expected_ms) <= 2000)
        except (ValueError, subprocess.SubprocessError):
            return False

    if complete(destination):
        return destination
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to extract episode audio")
    audio_index = int((episode.get("selected_audio") or {}).get("index", 0))
    pending = destination.with_name(f".{destination.stem}.{new_id('extract')}.pending{destination.suffix}")
    try:
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(source), "-map", f"0:a:{audio_index}", "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", codec, str(pending)], check=True, timeout=1800)
        if not complete(pending):
            raise RuntimeError("Extracted episode audio is incomplete; the source must be readable in full")
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)
    return destination


def extract_episode_subtitles(episode: dict[str, Any], destination: Path, language: str) -> tuple[Path, dict[str, Any]] | None:
    subtitle = select_episode_subtitle(episode, language)
    if not subtitle:
        return None
    source = local_plex_media_path(str(episode.get("server_file") or ""))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.stat().st_size < 16:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg fehlt für die Plex-Untertitelextraktion")
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(source), "-map", f"0:s:{int(subtitle['index'])}", "-c:s", "srt", str(destination)],
            check=True,
            timeout=300,
        )
    return destination, subtitle


def _srt_timestamp_ms(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+):(\d+):(\d+)[,.](\d{1,3})\s*", value)
    if not match:
        raise ValueError("Ungültiger SRT-Zeitstempel")
    hours, minutes, seconds, milliseconds = match.groups()
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(milliseconds.ljust(3, "0"))


def parse_srt(text: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        try:
            start_raw, end_raw = [part.strip().split()[0] for part in lines[timing_index].split("-->", 1)]
            start_ms = _srt_timestamp_ms(start_raw)
            end_ms = _srt_timestamp_ms(end_raw)
        except (ValueError, IndexError):
            continue
        cue_text = " ".join(lines[timing_index + 1:])
        cue_text = re.sub(r"<[^>]+>", "", cue_text)
        cue_text = re.sub(r"\{\\[^}]+\}", "", cue_text)
        cue_text = re.sub(r"\s+", " ", cue_text).strip()
        if cue_text and end_ms > start_ms:
            cues.append({"id": new_id("sub"), "start_ms": start_ms, "end_ms": end_ms, "text": cue_text})
    return cues


def transcript_subtitle_match(segment: dict[str, Any], subtitles: list[dict[str, Any]]) -> dict[str, Any]:
    start_ms = int(segment.get("episode_start_ms", 0))
    end_ms = int(segment.get("episode_end_ms", start_ms))
    overlapping = [
        cue for cue in subtitles
        if int(cue["end_ms"]) > start_ms and int(cue["start_ms"]) < end_ms
    ]
    if not overlapping:
        return {"subtitle_match_status": "missing", "subtitle_match_score": 0.0}
    subtitle_text = " ".join(dict.fromkeys(str(cue["text"]) for cue in overlapping))
    asr_text = clean_transcription_markers(segment.get("text"))
    score = transcript_text_similarity(asr_text, subtitle_text)
    overlap_ms = sum(max(0, min(end_ms, int(cue["end_ms"])) - max(start_ms, int(cue["start_ms"]))) for cue in overlapping)
    subtitle_duration_ms = sum(int(cue["end_ms"]) - int(cue["start_ms"]) for cue in overlapping)
    timing_coverage = round(min(1.0, overlap_ms / max(1, min(end_ms - start_ms, subtitle_duration_ms))), 3)
    return {
        "subtitle_text": subtitle_text,
        "subtitle_start_ms": min(int(cue["start_ms"]) for cue in overlapping),
        "subtitle_end_ms": max(int(cue["end_ms"]) for cue in overlapping),
        "subtitle_cue_ids": [cue["id"] for cue in overlapping],
        "subtitle_match_score": score,
        "subtitle_match_status": "matched",
        "subtitle_text_status": "consistent" if score >= 0.45 else "different",
        "subtitle_timing_coverage": timing_coverage,
        "subtitle_source": "plex_embedded_srt",
    }


def transcript_text_similarity(asr_text: str, subtitle_text: str) -> float:
    asr_tokens = alignment_tokens(asr_text)
    subtitle_tokens = alignment_tokens(subtitle_text)
    jaccard = len(asr_tokens & subtitle_tokens) / max(len(asr_tokens | subtitle_tokens), 1)
    sequence = difflib.SequenceMatcher(None, asr_text.casefold(), subtitle_text.casefold()).ratio()
    return round(jaccard * 0.55 + sequence * 0.45, 3)


def build_reconciled_transcript(project: dict[str, Any], subtitle_cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chapters = {chapter["id"]: chapter for chapter in project.get("chapters", [])}
    mapping_by_episode: dict[str, dict[str, Any]] = {}
    for mapping in project.get("mapping", []):
        mapping_by_episode.setdefault(str(mapping.get("episode_id")), mapping)
    words_by_episode: dict[str, list[dict[str, Any]]] = {}
    seen_words: set[tuple[str, int, int, str]] = set()
    for word in sorted(project.get("transcript_words", []), key=lambda item: (str(item.get("episode_id")), int(item.get("episode_start_ms", 0)), int(item.get("episode_end_ms", 0)))):
        episode_id = str(word.get("episode_id") or "")
        signature = (episode_id, int(word.get("episode_start_ms", 0)), int(word.get("episode_end_ms", 0)), str(word.get("text") or ""))
        if not episode_id or signature in seen_words:
            continue
        seen_words.add(signature)
        words_by_episode.setdefault(episode_id, []).append(word)

    reconciled = []
    used_words: set[tuple[str, int, int, str]] = set()
    for cue in sorted(subtitle_cues, key=lambda item: (str(item.get("episode_id")), int(item.get("start_ms", 0)), int(item.get("end_ms", 0)))):
        episode_id = str(cue.get("episode_id") or "")
        start_ms = int(cue.get("start_ms", 0))
        end_ms = int(cue.get("end_ms", start_ms))
        aligned_words = []
        for word in words_by_episode.get(episode_id, []):
            signature = (episode_id, int(word.get("episode_start_ms", 0)), int(word.get("episode_end_ms", 0)), str(word.get("text") or ""))
            midpoint = (int(word.get("episode_start_ms", 0)) + int(word.get("episode_end_ms", 0))) / 2
            if signature not in used_words and start_ms <= midpoint <= end_ms:
                aligned_words.append(word)
                used_words.add(signature)
        asr_text = clean_transcription_markers(" ".join(str(word.get("text") or "") for word in aligned_words))
        subtitle_text = str(cue.get("text") or "")
        score = transcript_text_similarity(asr_text, subtitle_text) if asr_text else 0.0
        mapping = mapping_by_episode.get(episode_id, {})
        chapter = chapters.get(mapping.get("chapter_id"), {})
        reconciled.append({
            "id": new_id("rt"),
            "subtitle_cue_id": cue.get("id"),
            "chapter_id": mapping.get("chapter_id"),
            "chapter_title": chapter.get("title") or mapping.get("chapter_title") or "Plex-Untertitel",
            "episode_id": episode_id,
            "episode": mapping.get("episode"),
            "episode_title": cue.get("episode_title") or mapping.get("episode_title"),
            "episode_start_ms": start_ms,
            "episode_end_ms": end_ms,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_sample": round(start_ms * SAMPLE_RATE / 1000),
            "end_sample": round(end_ms * SAMPLE_RATE / 1000),
            "text": subtitle_text,
            "reconciled_text": subtitle_text,
            "subtitle_text": subtitle_text,
            "asr_text": asr_text,
            "asr_word_count": len(aligned_words),
            "subtitle_match_score": score,
            "subtitle_match_status": "matched" if asr_text else "missing",
            "subtitle_text_status": "consistent" if score >= 0.45 else "different" if asr_text else "missing",
            "subtitle_source": "plex_embedded_srt",
            "timestamp_source": "plex_srt",
            "transcription_source": "plex_srt_reconciled_with_whisper_words",
            "language": cue.get("language"),
            "confidence": 0.98 if asr_text else 0.9,
        })
    existing_ids = {str(row["id"]) for row in reconciled}
    reconciled.extend(dict(row) for row in project.get("reviewed_visual_scene_anchors", [])
                      if row.get("visual_scene_anchor") and row.get("visual_evidence")
                      and str(row.get("id")) not in existing_ids)
    return reconciled


def extract_episode_delivery_audio(episode: dict[str, Any], destination: Path) -> Path:
    return _extract_cached_episode_audio(episode, destination, SAMPLE_RATE, "pcm_s16le")


def media_duration_ms(path: Path) -> int:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe fehlt für die Audiolängenbestimmung")
    result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], check=True, capture_output=True, text=True, timeout=60)
    return max(1, round(float(result.stdout.strip()) * 1000))


def _longest_connected_match(good_indices: list[int], max_gap_frames: int = 6) -> tuple[int, int, int]:
    if not good_indices:
        return 0, 0, 0
    best = (good_indices[0], good_indices[0] + 1, 1)
    start = previous = good_indices[0]
    matched = 1
    for current in good_indices[1:]:
        if current - previous > max_gap_frames:
            if previous + 1 - start > best[1] - best[0]:
                best = (start, previous + 1, matched)
            start = current
            matched = 1
        else:
            matched += 1
        previous = current
    if previous + 1 - start > best[1] - best[0]:
        best = (start, previous + 1, matched)
    return best


def _best_fingerprint_match(left: list[int], right: list[int]) -> dict[str, Any] | None:
    if not left or not right:
        return None
    positions: dict[tuple[int, int], list[int]] = {}
    for index, value in enumerate(right):
        for band in range(4):
            positions.setdefault((band, (int(value) >> (band * 8)) & 0xFF), []).append(index)
    offsets: Counter[int] = Counter()
    for index, value in enumerate(left):
        for band in range(4):
            for other_index in positions.get((band, (int(value) >> (band * 8)) & 0xFF), ()):
                offsets[other_index - index] += 1

    best: dict[str, Any] | None = None
    for offset, anchor_hits in offsets.most_common(32):
        left_start = max(0, -offset)
        right_start = left_start + offset
        overlap = min(len(left) - left_start, len(right) - right_start)
        if overlap <= 0:
            continue
        good = [
            frame
            for frame in range(overlap)
            if (int(left[left_start + frame]) ^ int(right[right_start + frame])).bit_count()
            <= INTRO_MAX_FINGERPRINT_BIT_DISTANCE
        ]
        span_start, span_end, matched = _longest_connected_match(good)
        span_frames = span_end - span_start
        if span_frames <= 0:
            continue
        quality = matched / span_frames
        candidate = {
            "left_start": left_start + span_start,
            "left_end": left_start + span_end,
            "right_start": right_start + span_start,
            "right_end": right_start + span_end,
            "offset": offset,
            "quality": quality,
            "anchor_hits": anchor_hits,
        }
        if best is None or (span_frames * quality, anchor_hits) > (
            (best["left_end"] - best["left_start"]) * best["quality"],
            best["anchor_hits"],
        ):
            best = candidate
    return best


def detect_repeated_intro_fingerprints(
    fingerprints: dict[str, dict[str, Any]],
    *,
    min_duration_ms: int = INTRO_MIN_DURATION_MS,
) -> dict[str, Any]:
    episode_ids = sorted(fingerprints)
    required_occurrences = max(3, math.ceil(len(episode_ids) * INTRO_MIN_EPISODE_RATIO))
    base = {
        "version": 1,
        "detector": "chromaprint_consensus_v1",
        "search_window_ms": INTRO_SEARCH_SECONDS * 1000,
        "episode_count": len(episode_ids),
        "required_occurrences": required_occurrences,
        "occurrences": [],
        "occurrence_count": 0,
        "removed_duration_ms": 0,
    }
    if len(episode_ids) < 3:
        return {**base, "status": "not_applicable", "reason": "Für eine Wiederholungserkennung werden mindestens drei Folgen benötigt"}

    pair_matches: dict[tuple[str, str], dict[str, Any]] = {}
    for left_index, left_id in enumerate(episode_ids):
        for right_id in episode_ids[left_index + 1:]:
            match = _best_fingerprint_match(
                list(fingerprints[left_id].get("fingerprint") or []),
                list(fingerprints[right_id].get("fingerprint") or []),
            )
            if match:
                pair_matches[(left_id, right_id)] = match

    candidates = []
    for reference_id in episode_ids:
        reference_step_ms = float(fingerprints[reference_id].get("frame_ms") or 125)
        offsets = {reference_id: 0}
        reference_intervals = []
        pair_qualities = []
        for other_id in episode_ids:
            if other_id == reference_id:
                continue
            key = (reference_id, other_id) if reference_id < other_id else (other_id, reference_id)
            match = pair_matches.get(key)
            if not match:
                continue
            if reference_id < other_id:
                reference_start = int(match["left_start"])
                reference_end = int(match["left_end"])
                offset = int(match["offset"])
            else:
                reference_start = int(match["right_start"])
                reference_end = int(match["right_end"])
                offset = -int(match["offset"])
            duration_ms = (reference_end - reference_start) * reference_step_ms
            if duration_ms < min_duration_ms or float(match["quality"]) < 0.82:
                continue
            offsets[other_id] = offset
            reference_intervals.append((reference_start, reference_end))
            pair_qualities.append(float(match["quality"]))
        if len(offsets) < required_occurrences:
            continue

        reference_fingerprint = list(fingerprints[reference_id].get("fingerprint") or [])
        coarse_start = max(0, round(median(start for start, _ in reference_intervals)) - 12)
        coarse_end = min(
            len(reference_fingerprint),
            round(median(end for _, end in reference_intervals)) + 12,
        )
        agreements = []
        for reference_frame in range(coarse_start, coarse_end):
            values = []
            for episode_id, offset in offsets.items():
                frame = reference_frame + offset
                fingerprint = list(fingerprints[episode_id].get("fingerprint") or [])
                if 0 <= frame < len(fingerprint):
                    values.append(int(fingerprint[frame]))
            comparisons = [
                (left ^ right).bit_count() <= INTRO_MAX_FINGERPRINT_BIT_DISTANCE
                for value_index, left in enumerate(values)
                for right in values[value_index + 1:]
            ]
            agreements.append(sum(comparisons) / len(comparisons) if comparisons else 0.0)
        smoothed = []
        for index in range(len(agreements)):
            window = agreements[max(0, index - 4):min(len(agreements), index + 5)]
            smoothed.append(sum(window) / len(window))
        good_indices = [index for index, agreement in enumerate(smoothed) if agreement >= 0.8]
        span_start, span_end, _ = _longest_connected_match(good_indices, max_gap_frames=2)
        span_start += coarse_start
        span_end += coarse_start
        if (span_end - span_start) * reference_step_ms < min_duration_ms:
            continue

        confidence_values = agreements[
            max(0, span_start - coarse_start):max(0, span_end - coarse_start)
        ]
        confidence = sum(confidence_values) / max(1, len(confidence_values))
        occurrences = []
        for episode_id, offset in sorted(offsets.items()):
            frame_ms = float(fingerprints[episode_id].get("frame_ms") or reference_step_ms)
            fingerprint_length = len(list(fingerprints[episode_id].get("fingerprint") or []))
            start_frame = max(0, min(fingerprint_length, span_start + offset))
            end_frame = max(start_frame, min(fingerprint_length, span_end + offset))
            occurrences.append(
                {
                    "episode_id": episode_id,
                    "start_ms": round(start_frame * frame_ms),
                    "end_ms": round(end_frame * frame_ms),
                    "duration_ms": round((end_frame - start_frame) * frame_ms),
                    "confidence": round(confidence, 4),
                    "evidence": "Wiederkehrende Chromaprint-Sequenz im frühen Episodenaudio",
                }
            )
        candidates.append(
            {
                "occurrences": occurrences,
                "confidence": confidence,
                "duration_ms": round((span_end - span_start) * reference_step_ms),
                "pair_count": len(pair_qualities),
            }
        )

    if not candidates:
        return {**base, "status": "not_detected", "reason": "Keine ausreichend lange, in der Mehrheit wiederkehrende Öffnungssequenz"}
    selected = max(
        candidates,
        key=lambda item: (
            len(item["occurrences"]),
            item["duration_ms"] * item["confidence"],
            item["pair_count"],
        ),
    )
    occurrences = selected["occurrences"]
    return {
        **base,
        "status": "detected",
        "confidence": round(float(selected["confidence"]), 4),
        "occurrences": occurrences,
        "occurrence_count": len(occurrences),
        "removed_duration_ms": sum(int(item["duration_ms"]) for item in occurrences),
    }


def fingerprint_opening_audio(path: Path, search_seconds: int = INTRO_SEARCH_SECONDS) -> dict[str, Any]:
    fpcalc = shutil.which("fpcalc")
    if not fpcalc:
        raise RuntimeError("fpcalc fehlt für die automatische Intro-Erkennung")
    result = subprocess.run(
        [fpcalc, "-raw", "-json", "-length", str(search_seconds), str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    payload = json.loads(result.stdout)
    fingerprint = [int(value) for value in payload.get("fingerprint") or []]
    if not fingerprint:
        raise RuntimeError(f"Chromaprint lieferte keinen Audio-Fingerabdruck für {path.name}")
    processed_ms = min(search_seconds * 1000, round(float(payload.get("duration") or search_seconds) * 1000))
    return {
        "fingerprint": fingerprint,
        "frame_ms": processed_ms / len(fingerprint),
        "processed_ms": processed_ms,
    }


def fingerprint_ending_audio(path: Path, search_seconds: int = ENDING_SEARCH_SECONDS) -> dict[str, Any]:
    fpcalc = shutil.which("fpcalc")
    if not fpcalc:
        raise RuntimeError("fpcalc fehlt für die automatische Abspannerkennung")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg fehlt für die automatische Abspannerkennung")
    episode_duration_ms = media_duration_ms(path)
    window_start_ms = max(0, episode_duration_ms - search_seconds * 1000)
    with tempfile.TemporaryDirectory(prefix="szenenklang-ending-") as temp_dir:
        ending_window = Path(temp_dir) / "ending-window.wav"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-y",
                "-ss", f"{window_start_ms / 1000:.3f}",
                "-i", str(path),
                "-t", str(search_seconds),
                "-vn", "-ac", "1", "-ar", "11025",
                "-c:a", "pcm_s16le",
                str(ending_window),
            ],
            check=True,
            timeout=180,
        )
        result = subprocess.run(
            [fpcalc, "-raw", "-json", "-length", str(search_seconds), str(ending_window)],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    if not result.stdout.strip():
        raise RuntimeError(
            f"Chromaprint konnte das Endfenster von {path.name} nicht lesen: "
            f"{result.stderr.strip()[:300]}"
        )
    payload = json.loads(result.stdout)
    fingerprint = [int(value) for value in payload.get("fingerprint") or []]
    if not fingerprint:
        raise RuntimeError(f"Chromaprint lieferte keinen End-Fingerabdruck für {path.name}")
    processed_ms = min(
        search_seconds * 1000,
        max(1, episode_duration_ms - window_start_ms),
        round(float(payload.get("duration") or search_seconds) * 1000),
    )
    return {
        "fingerprint": fingerprint,
        "frame_ms": processed_ms / len(fingerprint),
        "processed_ms": processed_ms,
        "window_start_ms": window_start_ms,
        "episode_duration_ms": episode_duration_ms,
    }


def detect_repeated_ending_fingerprints(fingerprints: dict[str, dict[str, Any]]) -> dict[str, Any]:
    detection = detect_repeated_intro_fingerprints(
        fingerprints,
        min_duration_ms=ENDING_MIN_DURATION_MS,
    )
    detection.update({
        "version": 1,
        "detector": "chromaprint_ending_consensus_v1",
        "search_window_ms": ENDING_SEARCH_SECONDS * 1000,
    })
    if detection.get("status") != "detected":
        if detection.get("status") == "not_detected":
            detection["reason"] = "Keine ausreichend lange, in der Mehrheit wiederkehrende Endsequenz"
        return detection
    occurrences = []
    for occurrence in detection.get("occurrences", []):
        episode_id = str(occurrence.get("episode_id") or "")
        fingerprint = fingerprints.get(episode_id, {})
        window_start_ms = int(fingerprint.get("window_start_ms") or 0)
        episode_duration_ms = int(
            fingerprint.get("episode_duration_ms")
            or window_start_ms + int(fingerprint.get("processed_ms") or 0)
        )
        theme_start_ms = min(
            episode_duration_ms,
            window_start_ms + int(occurrence.get("start_ms") or 0),
        )
        theme_end_ms = min(
            episode_duration_ms,
            window_start_ms + int(occurrence.get("end_ms") or 0),
        )
        occurrences.append({
            **occurrence,
            "start_ms": theme_start_ms,
            "end_ms": episode_duration_ms,
            "duration_ms": max(0, episode_duration_ms - theme_start_ms),
            "matched_theme_end_ms": theme_end_ms,
            "evidence": (
                "Wiederkehrende Chromaprint-Endsequenz; Abspann und danach liegende "
                "Episodenvorschau bilden einen ausgeschlossenen Endblock"
            ),
        })
    detection["occurrences"] = occurrences
    detection["removed_duration_ms"] = sum(int(item["duration_ms"]) for item in occurrences)
    return detection


def detect_project_intros(project: dict[str, Any], episode_audio: dict[str, Path]) -> dict[str, Any]:
    fingerprints = (
        {
            episode_id: fingerprint_opening_audio(path)
            for episode_id, path in sorted(episode_audio.items())
        }
        if len(episode_audio) >= 3
        else {
            episode_id: {"fingerprint": [], "frame_ms": 125}
            for episode_id in episode_audio
        }
    )
    detection = detect_repeated_intro_fingerprints(fingerprints)
    detection["generated_at"] = now()
    return detection


def detect_project_endings(project: dict[str, Any], episode_audio: dict[str, Path]) -> dict[str, Any]:
    fingerprints = (
        {
            episode_id: fingerprint_ending_audio(path)
            for episode_id, path in sorted(episode_audio.items())
        }
        if len(episode_audio) >= 3
        else {
            episode_id: {
                "fingerprint": [],
                "frame_ms": 125,
                "window_start_ms": 0,
                "episode_duration_ms": media_duration_ms(path),
            }
            for episode_id, path in episode_audio.items()
        }
    )
    detection = detect_repeated_ending_fingerprints(fingerprints)
    detection["generated_at"] = now()
    return detection


def fingerprint_commercial_bumper_audio(path: Path) -> dict[str, Any]:
    fpcalc = shutil.which("fpcalc")
    ffmpeg = shutil.which("ffmpeg")
    if not fpcalc or not ffmpeg:
        raise RuntimeError("FFmpeg und fpcalc werden für die Commercial-Bumper-Erkennung benötigt")
    episode_duration_ms = media_duration_ms(path)
    window_start_ms = round(episode_duration_ms * COMMERCIAL_BUMPER_WINDOW_START_RATIO)
    window_end_ms = round(episode_duration_ms * COMMERCIAL_BUMPER_WINDOW_END_RATIO)
    window_duration_ms = max(1, window_end_ms - window_start_ms)
    with tempfile.TemporaryDirectory(prefix="szenenklang-midroll-") as temp_dir:
        window = Path(temp_dir) / "midroll-window.wav"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-y",
                "-ss", f"{window_start_ms / 1000:.3f}",
                "-i", str(path),
                "-t", f"{window_duration_ms / 1000:.3f}",
                "-vn", "-ac", "1", "-ar", "11025",
                "-c:a", "pcm_s16le",
                str(window),
            ],
            check=True,
            timeout=180,
        )
        result = subprocess.run(
            [
                fpcalc, "-raw", "-json",
                "-length", str(max(1, math.ceil(window_duration_ms / 1000))),
                str(window),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    if not result.stdout.strip():
        raise RuntimeError(
            f"Chromaprint konnte das Mittelfenster von {path.name} nicht lesen: "
            f"{result.stderr.strip()[:300]}"
        )
    payload = json.loads(result.stdout)
    fingerprint = [int(value) for value in payload.get("fingerprint") or []]
    if not fingerprint:
        raise RuntimeError(f"Chromaprint lieferte keinen Mittelfenster-Fingerabdruck für {path.name}")
    processed_ms = min(
        window_duration_ms,
        round(float(payload.get("duration") or window_duration_ms / 1000) * 1000),
    )
    return {
        "fingerprint": fingerprint,
        "frame_ms": processed_ms / len(fingerprint),
        "processed_ms": processed_ms,
        "window_start_ms": window_start_ms,
        "episode_duration_ms": episode_duration_ms,
    }


def _cluster_midroll_intervals(
    intervals: list[tuple[float, float, float, str]],
) -> list[tuple[float, float, float, str]]:
    clusters: list[list[tuple[float, float, float, str]]] = []
    for interval in sorted(intervals, key=lambda item: (item[0] + item[1]) / 2):
        center = (interval[0] + interval[1]) / 2
        for cluster in clusters:
            cluster_center = median((item[0] + item[1]) / 2 for item in cluster)
            if abs(center - cluster_center) <= COMMERCIAL_BUMPER_CLUSTER_RADIUS_MS:
                cluster.append(interval)
                break
        else:
            clusters.append([interval])
    return max(
        clusters,
        key=lambda cluster: (
            len({item[3] for item in cluster}),
            median(item[1] - item[0] for item in cluster),
            sum(item[2] for item in cluster) / len(cluster),
        ),
        default=[],
    )


def detect_repeated_commercial_bumper_fingerprints(
    fingerprints: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    episode_ids = sorted(fingerprints)
    base = {
        "version": 1,
        "detector": "chromaprint_midroll_cluster_v1",
        "episode_count": len(episode_ids),
        "required_occurrences": len(episode_ids),
        "occurrences": [],
        "occurrence_count": 0,
        "removed_duration_ms": 0,
    }
    if len(episode_ids) < 3:
        return {
            **base,
            "status": "not_applicable",
            "reason": "Für eine Commercial-Bumper-Erkennung werden mindestens drei Folgen benötigt",
        }

    intervals_by_episode: dict[str, list[tuple[float, float, float, str]]] = {
        episode_id: [] for episode_id in episode_ids
    }
    for left_index, left_id in enumerate(episode_ids):
        for right_id in episode_ids[left_index + 1:]:
            match = _best_fingerprint_match(
                list(fingerprints[left_id].get("fingerprint") or []),
                list(fingerprints[right_id].get("fingerprint") or []),
            )
            if not match or float(match["quality"]) < 0.82:
                continue
            left_step = float(fingerprints[left_id].get("frame_ms") or 125)
            right_step = float(fingerprints[right_id].get("frame_ms") or 125)
            left_duration = (int(match["left_end"]) - int(match["left_start"])) * left_step
            right_duration = (int(match["right_end"]) - int(match["right_start"])) * right_step
            if min(left_duration, right_duration) < COMMERCIAL_BUMPER_MIN_DURATION_MS:
                continue
            quality = float(match["quality"])
            intervals_by_episode[left_id].append(
                (
                    int(match["left_start"]) * left_step,
                    int(match["left_end"]) * left_step,
                    quality,
                    right_id,
                )
            )
            intervals_by_episode[right_id].append(
                (
                    int(match["right_start"]) * right_step,
                    int(match["right_end"]) * right_step,
                    quality,
                    left_id,
                )
            )

    occurrences = []
    for episode_id in episode_ids:
        cluster = _cluster_midroll_intervals(intervals_by_episode[episode_id])
        support_count = len({item[3] for item in cluster})
        if support_count < 2:
            continue
        fingerprint = fingerprints[episode_id]
        window_start_ms = int(fingerprint.get("window_start_ms") or 0)
        relative_start_ms = round(median(item[0] for item in cluster))
        relative_end_ms = round(median(item[1] for item in cluster))
        if relative_end_ms - relative_start_ms < COMMERCIAL_BUMPER_MIN_DURATION_MS:
            continue
        confidence = sum(item[2] for item in cluster) / len(cluster)
        occurrences.append({
            "episode_id": episode_id,
            "start_ms": window_start_ms + relative_start_ms,
            "end_ms": window_start_ms + relative_end_ms,
            "duration_ms": relative_end_ms - relative_start_ms,
            "confidence": round(confidence, 4),
            "support_count": support_count,
            "evidence": (
                "Wiederkehrender Chromaprint-Midroll-Cluster mit mindestens "
                "zwei unabhängigen Folgenpaaren"
            ),
        })
    if len(occurrences) != len(episode_ids):
        return {
            **base,
            "status": "not_detected",
            "occurrences": occurrences,
            "occurrence_count": len(occurrences),
            "reason": (
                f"Commercial-Bumper nur in {len(occurrences)} von "
                f"{len(episode_ids)} Folgen belastbar erkannt"
            ),
        }
    return {
        **base,
        "status": "detected",
        "confidence": round(
            sum(float(item["confidence"]) for item in occurrences) / len(occurrences),
            4,
        ),
        "occurrences": occurrences,
        "occurrence_count": len(occurrences),
        "removed_duration_ms": sum(int(item["duration_ms"]) for item in occurrences),
    }


def detect_project_commercial_bumpers(
    project: dict[str, Any],
    episode_audio: dict[str, Path],
) -> dict[str, Any]:
    fingerprints = (
        {
            episode_id: fingerprint_commercial_bumper_audio(path)
            for episode_id, path in sorted(episode_audio.items())
        }
        if len(episode_audio) >= 3
        else {
            episode_id: {
                "fingerprint": [],
                "frame_ms": 125,
                "window_start_ms": 0,
                "episode_duration_ms": media_duration_ms(path),
            }
            for episode_id, path in episode_audio.items()
        }
    )
    detection = detect_repeated_commercial_bumper_fingerprints(fingerprints)
    subtitle_intervals_by_episode: dict[str, list[tuple[int, int]]] = {}
    for item in project.get("reconciled_transcript") or []:
        episode_id = str(item.get("episode_id") or "")
        start_ms = int(item.get("episode_start_ms", item.get("start_ms", 0)))
        end_ms = int(item.get("episode_end_ms", item.get("end_ms", start_ms)))
        if episode_id and end_ms > start_ms:
            subtitle_intervals_by_episode.setdefault(episode_id, []).append((start_ms, end_ms))
    if detection.get("status") == "detected" and subtitle_intervals_by_episode:
        clamped_occurrences = []
        for occurrence in detection.get("occurrences", []):
            episode_id = str(occurrence.get("episode_id") or "")
            start_ms = int(occurrence.get("start_ms") or 0)
            end_ms = int(occurrence.get("end_ms") or start_ms)
            candidates = source_ranges_after_exclusions(
                start_ms,
                end_ms,
                subtitle_intervals_by_episode.get(episode_id, []),
            )
            if not candidates:
                continue
            safe_start_ms, safe_end_ms = max(
                candidates,
                key=lambda interval: interval[1] - interval[0],
            )
            if safe_end_ms - safe_start_ms < COMMERCIAL_BUMPER_MIN_DURATION_MS:
                continue
            clamped_occurrences.append({
                **occurrence,
                "start_ms": safe_start_ms,
                "end_ms": safe_end_ms,
                "duration_ms": safe_end_ms - safe_start_ms,
                "subtitle_safety": "clamped_to_target_subtitle_boundaries",
            })
        detection["occurrences"] = clamped_occurrences
        detection["occurrence_count"] = len(clamped_occurrences)
        detection["removed_duration_ms"] = sum(
            int(item["duration_ms"]) for item in clamped_occurrences
        )
        if len(clamped_occurrences) != len(fingerprints):
            detection["status"] = "not_detected"
            detection["reason"] = (
                f"Commercial-Bumper nur in {len(clamped_occurrences)} von "
                f"{len(fingerprints)} Folgen dialogfrei belegt"
            )
    detection["generated_at"] = now()
    return detection


def intro_intervals_by_episode(project: dict[str, Any]) -> dict[str, list[tuple[int, int]]]:
    detection = project.get("intro_detection") or {}
    if detection.get("status") != "detected":
        return {}
    result: dict[str, list[tuple[int, int]]] = {}
    for occurrence in detection.get("occurrences", []):
        episode_id = str(occurrence.get("episode_id") or "")
        start_ms = max(0, int(occurrence.get("start_ms", 0)))
        end_ms = max(start_ms, int(occurrence.get("end_ms", start_ms)))
        if episode_id and end_ms > start_ms:
            result.setdefault(episode_id, []).append((start_ms, end_ms))
    return {episode_id: sorted(intervals) for episode_id, intervals in result.items()}


def ending_intervals_by_episode(project: dict[str, Any]) -> dict[str, list[tuple[int, int]]]:
    detection = project.get("ending_detection") or {}
    if detection.get("status") != "detected":
        return {}
    result: dict[str, list[tuple[int, int]]] = {}
    for occurrence in detection.get("occurrences", []):
        episode_id = str(occurrence.get("episode_id") or "")
        start_ms = max(0, int(occurrence.get("start_ms", 0)))
        end_ms = max(start_ms, int(occurrence.get("end_ms", start_ms)))
        if episode_id and end_ms > start_ms:
            result.setdefault(episode_id, []).append((start_ms, end_ms))
    return {episode_id: sorted(intervals) for episode_id, intervals in result.items()}


def commercial_bumper_intervals_by_episode(
    project: dict[str, Any],
) -> dict[str, list[tuple[int, int]]]:
    detection = project.get("commercial_bumper_detection") or {}
    if detection.get("status") != "detected":
        return {}
    result: dict[str, list[tuple[int, int]]] = {}
    for occurrence in detection.get("occurrences", []):
        episode_id = str(occurrence.get("episode_id") or "")
        start_ms = max(0, int(occurrence.get("start_ms", 0)))
        end_ms = max(start_ms, int(occurrence.get("end_ms", start_ms)))
        if episode_id and end_ms > start_ms:
            result.setdefault(episode_id, []).append((start_ms, end_ms))
    return {episode_id: sorted(intervals) for episode_id, intervals in result.items()}


def non_content_intervals_by_episode(project: dict[str, Any]) -> dict[str, list[tuple[int, int]]]:
    result: dict[str, list[tuple[int, int]]] = {}
    for source in (
        intro_intervals_by_episode(project),
        commercial_bumper_intervals_by_episode(project),
        ending_intervals_by_episode(project),
    ):
        for episode_id, intervals in source.items():
            result.setdefault(episode_id, []).extend(intervals)
    return {episode_id: sorted(intervals) for episode_id, intervals in result.items()}


def retained_content_windows(project: dict[str, Any]) -> dict[str, list[tuple[int, int]]]:
    """Return the source ranges per episode that actually survive into the render."""
    exclusions = non_content_intervals_by_episode(project)
    spans: dict[str, tuple[int, int]] = {}
    for segment in project.get("reconciled_transcript") or project.get("transcript", []):
        episode_id = str(segment.get("episode_id") or "")
        if not episode_id or not transcript_item_outside_intros(project, segment):
            continue
        start_ms = int(segment.get("episode_start_ms", 0))
        end_ms = int(segment.get("episode_end_ms", start_ms))
        first, last = spans.get(episode_id, (start_ms, end_ms))
        spans[episode_id] = (min(first, start_ms), max(last, end_ms))
    return {
        episode_id: source_ranges_after_exclusions(start, end, exclusions.get(episode_id, []))
        for episode_id, (start, end) in spans.items()
    }


def narration_coverage_targets(
    project: dict[str, Any],
    cues: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Per episode: how many comments the audio drama needs and how far they may sit apart.

    The listener must not lose the picture for minutes at a time, so the floor is
    derived from the retained running time instead of a fixed per-episode count.
    """
    rules = narration_rules(project)
    mappings = project.get("mapping", [])
    target_ms = max(30_000, int(rules["narration_coverage_target_ms"]))
    max_gap_ms = max(target_ms, int(rules["narration_max_gap_ms"]))
    windows = retained_content_windows(project)
    anchors_by_episode: dict[str, dict[str, int]] = {}
    coverage_ids: dict[str, set[str]] = {}
    for cue in cues:
        episode_id = str(cue.get("anchor_episode_id") or "")
        if not episode_id:
            continue
        if not cue.get("character_introduction_candidate"):
            coverage_ids.setdefault(episode_id, set()).add(str(cue.get("id") or ""))
        anchors_by_episode.setdefault(episode_id, {})[str(cue.get("id") or "")] = int(
            cue.get("anchor_episode_start_ms") or cue.get("anchor_insert_ms") or 0
        )
    targets: dict[str, dict[str, Any]] = {}
    for episode_id, anchors in anchors_by_episode.items():
        episode_windows = windows.get(episode_id) or []
        retained_ms = sum(end - start for start, end in episode_windows)
        candidate_count = len(anchors)
        needed = math.ceil(retained_ms / target_ms) if retained_ms else 0
        targets[episode_id] = {
            "retained_ms": retained_ms,
            "candidate_count": candidate_count,
            "min_cues": max(4, min(candidate_count, needed)),
            "max_cues": candidate_count,
            "max_gap_ms": max_gap_ms,
            "candidate_anchor_ms": sorted(home for cue_id, home in anchors.items() if cue_id in coverage_ids.get(episode_id, set())),
            "coverage_candidate_ids": sorted(coverage_ids.get(episode_id, set())),
            "candidate_anchors": dict(anchors),
        }
    # A book usually starts and ends mid-episode; the lead-in and run-out are
    # trimmed by the boundary plan, so demanding comments there punishes the
    # editor for correctly staying out of the neighbouring book.
    ordered = [episode_id for episode_id in unique_episode_ids(mappings) if episode_id in targets]
    if ordered:
        targets[ordered[0]]["book_edge_episode"] = "start"
        targets[ordered[-1]]["book_edge_episode"] = (
            "both" if len(ordered) == 1 else "end"
        )
    return targets


def annotate_pause_capacity(
    project: dict[str, Any],
    targets: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Tell the editor how many real pauses an episode offers for short beats."""
    segments_by_episode: dict[str, list[dict[str, Any]]] = {}
    words_by_episode: dict[str, list[dict[str, Any]]] = {}
    for segment in project.get("reconciled_transcript") or []:
        if transcript_item_outside_intros(project, segment):
            segments_by_episode.setdefault(str(segment.get("episode_id") or ""), []).append(segment)
    for word in project.get("transcript_words", []):
        if transcript_item_outside_intros(project, word):
            words_by_episode.setdefault(str(word.get("episode_id") or ""), []).append(word)
    durations = {
        str(asset.get("episode_id")): int(asset.get("duration_ms") or 0)
        for asset in project.get("media_assets", [])
    }
    for episode_id, target in targets.items():
        rows = sorted(
            segments_by_episode.get(episode_id, []),
            key=lambda item: int(item.get("episode_start_ms") or 0),
        )
        duration_ms = durations.get(episode_id) or max(
            (int(row.get("episode_end_ms", 0)) for row in rows),
            default=1,
        )
        gaps = speech_free_gaps_without_intros(
            project,
            episode_id,
            rows,
            words_by_episode.get(episode_id, []),
            duration_ms,
        )
        target["pause_beats_available"] = sum(1 for start, end in gaps if end - start >= 3_500)
        target["roomy_pause_beats_available"] = sum(1 for start, end in gaps if end - start >= 7_000)
    return targets
    return targets


def source_ranges_after_exclusions(
    start: int,
    end: int,
    exclusions: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    cursor = max(0, start)
    limit = max(cursor, end)
    included = []
    for excluded_start, excluded_end in sorted(exclusions):
        excluded_start = max(cursor, min(limit, excluded_start))
        excluded_end = max(excluded_start, min(limit, excluded_end))
        if excluded_start > cursor:
            included.append((cursor, excluded_start))
        cursor = max(cursor, excluded_end)
        if cursor >= limit:
            break
    if cursor < limit:
        included.append((cursor, limit))
    return [(range_start, range_end) for range_start, range_end in included if range_end > range_start]


def transcript_item_outside_intros(project: dict[str, Any], item: dict[str, Any]) -> bool:
    episode_id = str(item.get("episode_id") or "")
    start_ms = int(item.get("episode_start_ms", item.get("start_ms", 0)))
    end_ms = int(item.get("episode_end_ms", item.get("end_ms", start_ms)))
    midpoint = (start_ms + end_ms) / 2
    return not any(start <= midpoint < end for start, end in non_content_intervals_by_episode(project).get(episode_id, []))


def speech_free_gaps_without_intros(
    project: dict[str, Any],
    episode_id: str,
    segments: list[dict[str, Any]],
    words: list[dict[str, Any]],
    duration_ms: int,
) -> list[tuple[int, int]]:
    gaps = speech_free_gaps(segments, words, duration_ms)
    exclusions = non_content_intervals_by_episode(project).get(episode_id, [])
    return [
        included
        for gap_start, gap_end in gaps
        for included in source_ranges_after_exclusions(gap_start, gap_end, exclusions)
        if included[1] - included[0] >= 900
    ]


SPEECH_SNAP_MARGIN_MS = 120
SPEECH_SNAP_MAX_SHIFT_MS = 4_000


def merged_speech_intervals(project: dict[str, Any]) -> dict[str, list[tuple[int, int]]]:
    """Everything actually audible as speech, from subtitles and ASR words alike."""
    raw: dict[str, list[tuple[int, int]]] = {}
    sources: list[tuple[str, list[dict[str, Any]]]] = [
        ("sub", project.get("subtitle_cues") or []),
        ("word", project.get("transcript_words") or []),
    ]
    for _, items in sources:
        for item in items:
            episode_id = str(item.get("episode_id") or "")
            start = item.get("episode_start_ms", item.get("start_ms"))
            if start is None:
                continue
            end = item.get("episode_end_ms", item.get("end_ms", start))
            start, end = int(start), int(end)
            if end > start:
                raw.setdefault(episode_id, []).append((start, end))
    merged: dict[str, list[tuple[int, int]]] = {}
    for episode_id, spans in raw.items():
        ordered = sorted(spans)
        collapsed: list[tuple[int, int]] = []
        for start, end in ordered:
            if collapsed and start <= collapsed[-1][1]:
                collapsed[-1] = (collapsed[-1][0], max(collapsed[-1][1], end))
            else:
                collapsed.append((start, end))
        merged[episode_id] = collapsed
    return merged


def speech_safe_insert_ms(
    insert_ms: int,
    speech: list[tuple[int, int]],
    max_shift_ms: int = SPEECH_SNAP_MAX_SHIFT_MS,
) -> int:
    """Move a cut out of a running utterance, to whichever edge is nearer.

    Splicing narration into the middle of a word cuts it twice: once going in
    and once coming back.  Measured on Band 5, 32 of 103 inserts landed inside
    an utterance with a median of 524 ms already spoken.  Snapping to the
    nearer edge costs a median of 757 ms of timing and removes the clip.
    """
    for index, (start, end) in enumerate(speech):
        # The utterance start uses the same pre-roll margin as a point
        # inside it; otherwise two introductions in one opening utterance
        # straddle the book boundary by exactly SPEECH_SNAP_MARGIN_MS.
        if not start <= insert_ms < end:
            continue
        before = insert_ms - start
        after = end - insert_ms
        if min(before, after) > max_shift_ms:
            # A very long utterance would drag the comment far off its scene;
            # the hard cut is the lesser evil and the gate still reports it.
            return insert_ms
        # Der Sicherheitsabstand darf nicht in die Nachbaraeusserung rutschen:
        # sonst wandert der Schnitt bei jedem weiteren Aufruf hin und her
        # (Band 1: 17 Einfuegungen lagen deshalb neben ihrer Szenengrenze).
        # In einer engen Pause sitzt der Schnitt stabil in deren Mitte.
        if before <= after:
            target = start - SPEECH_SNAP_MARGIN_MS
            previous_end = speech[index - 1][1] if index else None
            if previous_end is not None and target <= previous_end:
                target = (previous_end + start) // 2
        else:
            target = end + SPEECH_SNAP_MARGIN_MS
            next_start = speech[index + 1][0] if index + 1 < len(speech) else None
            if next_start is not None and target >= next_start:
                target = (end + next_start) // 2
        return max(0, target)
    return insert_ms


def realign_scene_inserts(project: dict[str, Any]) -> int:
    """Setzt jede Szenengrenzen-Einfuegung auf die Grenze ihres Ankersegments zurueck."""
    segments = {
        str(item.get("id") or ""): item
        for item in (project.get("reconciled_transcript") or project.get("transcript", []))
    }
    changed = 0
    lists = [project.get("cues", [])]
    if project.get("semantic_cues") is not project.get("cues"):
        lists.append(project.get("semantic_cues", []))
    for cues in lists:
        for cue in cues:
            if cue.get("placement_policy") != ALIGNED_SCENE_INSERT_POLICY:
                continue
            anchor = segments.get(str(cue.get("anchor_segment_id") or ""))
            if not anchor or str(anchor.get("episode_id") or "") != str(cue.get("anchor_episode_id") or ""):
                continue
            boundary_ms = aligned_scene_boundary_ms(cue, anchor)
            if int(cue.get("anchor_insert_ms", -1)) != boundary_ms or int(cue.get("aligned_boundary_ms", -1)) != boundary_ms:
                cue["anchor_insert_ms"] = boundary_ms
                cue["aligned_boundary_ms"] = boundary_ms
                changed += 1
    return changed


def snap_inserts_out_of_speech(project: dict[str, Any]) -> int:
    """Keep every timeline insert off a running utterance."""
    speech = merged_speech_intervals(project)
    if not speech:
        return 0
    changed = 0
    lists = [project.get("cues", [])]
    if project.get("semantic_cues") is not project.get("cues"):
        lists.append(project.get("semantic_cues", []))
    for cues in lists:
        for cue in cues:
            if cue.get("placement_policy") == "overlay_speech_free":
                continue
            episode_id = str(cue.get("anchor_episode_id") or "")
            spans = speech.get(episode_id)
            if not spans:
                continue
            for key in ("anchor_insert_ms", "aligned_boundary_ms"):
                current = cue.get(key)
                if current is None:
                    continue
                moved = speech_safe_insert_ms(int(current), spans)
                if moved != int(current):
                    cue[key] = moved
                    changed += 1
    return changed


def reanchor_cues_outside_intros(project: dict[str, Any]) -> int:
    intervals = non_content_intervals_by_episode(project)
    if not intervals:
        return 0
    segments_by_episode: dict[str, list[dict[str, Any]]] = {}
    for segment in project.get("reconciled_transcript") or project.get("transcript", []):
        if transcript_item_outside_intros(project, segment):
            segments_by_episode.setdefault(str(segment.get("episode_id") or ""), []).append(segment)
    for segments in segments_by_episode.values():
        segments.sort(key=lambda item: int(item.get("episode_start_ms", 0)))

    changed = 0
    lists = [project.get("cues", [])]
    if project.get("semantic_cues") is not project.get("cues"):
        lists.append(project.get("semantic_cues", []))
    for cues in lists:
        for cue in cues:
            episode_id = str(cue.get("anchor_episode_id") or "")
            placement_ms = int(cue.get("anchor_insert_ms", cue.get("anchor_episode_start_ms", 0)))
            affected = next(
                (
                    interval
                    for interval in intervals.get(episode_id, [])
                    if interval[0] <= placement_ms < interval[1]
                    or int(cue.get("safe_gap_start_ms", placement_ms)) < interval[1]
                    and int(cue.get("safe_gap_end_ms", placement_ms)) > interval[0]
                ),
                None,
            )
            if not affected:
                continue
            candidates = [
                segment
                for segment in segments_by_episode.get(episode_id, [])
                if int(segment.get("episode_start_ms", 0)) >= affected[1]
            ]
            if not candidates:
                # The removed block reaches the end of the episode (credits and
                # next-episode preview): nothing is spoken after it, so the
                # cue moves in front of the block, onto the last retained line.
                candidates = [
                    segment
                    for segment in segments_by_episode.get(episode_id, [])
                    if int(segment.get("episode_end_ms", segment.get("episode_start_ms", 0))) <= affected[0]
                ]
                if not candidates:
                    raise RuntimeError(f"Erzähler-Cue {cue.get('id')} kann nach dem entfernten Intro nicht neu verankert werden")
                candidates = candidates[-1:]
                logger.warning(
                    "Cue %s lag im entfernten Endblock von Folge %s und wird vor den Block gelegt",
                    cue.get("id"), episode_id,
                )
            anchor = candidates[0]
            cue["intro_reanchored_from"] = {
                "anchor_segment_id": cue.get("anchor_segment_id"),
                "anchor_insert_ms": placement_ms,
            }
            cue.update(
                {
                    "anchor_segment_id": anchor.get("id"),
                    "anchor_subtitle_cue_id": anchor.get("subtitle_cue_id"),
                    "anchor_episode_start_ms": int(anchor.get("episode_start_ms", 0)),
                    "anchor_global_start_sample": int(anchor.get("start_sample", 0)),
                    "anchor_text": str(anchor.get("reconciled_text") or anchor.get("text") or ""),
                    "anchor_text_source": str(anchor.get("transcription_source") or anchor.get("timestamp_source") or ""),
                    "alignment_placement": "after_anchor",
                }
            )
            apply_aligned_scene_insert(cue, anchor)
            changed += 1
    return changed


def clean_transcription_markers(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"language\s+(?:[A-Za-z]+?|None)\s*(?:<asr_text>|asr\s*text)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:[A-Za-z]+?\s*)?asr\s*text", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"language(?:\s+[A-Za-z]+)?(?:\s+asr)?\s*$", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_timestamp_items(items: Any, duration_ms: int) -> list[dict[str, Any]]:
    normalized = []
    if not isinstance(items, list):
        return normalized
    duration_seconds = max(0.001, duration_ms / 1000)
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            text = clean_transcription_markers(item.get("text") or item.get("word"))
            start_value = float(item["start"]) if "start" in item else float(item["start_ms"]) / 1000
            end_value = float(item["end"]) if "end" in item else float(item["end_ms"]) / 1000
            start = max(0.0, min(start_value, duration_seconds))
            end = max(start, min(end_value, duration_seconds))
        except (KeyError, TypeError, ValueError):
            continue
        if text:
            start_ms = round(start * 1000)
            end_ms = max(start_ms + 1, round(end * 1000))
            normalized.append({"text": text, "start_ms": start_ms, "end_ms": min(duration_ms, end_ms)})
    return normalized


def normalize_vocarium_transcription(payload: Any, duration_ms: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Vocarium hat kein JSON-Objekt geliefert")
    text = clean_transcription_markers(payload.get("text"))
    words = remove_timestamp_marker_words(normalize_timestamp_items(payload.get("words"), duration_ms))
    if not text and not words:
        # Ein stummer Abschnitt (Intro-Musik, Titelkarte, Abspann) ist kein
        # Fehler: Whisper liefert dann zu Recht nichts. Der Chunk bleibt als
        # leerer Zeitraum erhalten, damit die Offsets der Folge-Chunks stimmen.
        return {
            "schema_version": 1,
            "text": "",
            "language": str(payload.get("language") or "Unknown").strip() or "Unknown",
            "words": [],
            "segments": [],
            "timestamp_source": "silence",
        }
    if not text:
        text = " ".join(item["text"] for item in words)
    segments = normalize_timestamp_items(payload.get("segments"), duration_ms)
    if not segments and words:
        segments = [{"text": " ".join(item["text"] for item in words), "start_ms": words[0]["start_ms"], "end_ms": words[-1]["end_ms"]}]
    timestamp_source = "whisper_segments" if segments else "asr_chunk_bounds"
    if not segments:
        segments = [{"text": text, "start_ms": 0, "end_ms": duration_ms}]
    return {
        "schema_version": 1,
        "text": text,
        "language": str(payload.get("language") or "Unknown").strip() or "Unknown",
        "words": words,
        "segments": segments,
        "timestamp_source": timestamp_source,
    }


def remove_timestamp_marker_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = []
    index = 0
    language_names = {"arabic", "cantonese", "chinese", "english", "french", "german", "italian", "japanese", "korean", "none", "portuguese", "russian", "spanish"}
    while index < len(words):
        tokens = [str(words[position].get("text", "")).casefold() for position in range(index, min(index + 4, len(words)))]
        if len(tokens) == 4 and tokens[0] == "language" and tokens[2:] == ["asr", "text"]:
            index += 4
            continue
        if len(tokens) >= 2 and tokens[0] == "language" and any(tokens[1] == f"{name}asrtext" for name in language_names):
            index += 2
            continue
        if len(tokens) >= 3 and tokens[0] in language_names and tokens[1:3] == ["asr", "text"]:
            index += 3
            continue
        if any(tokens[0] == f"{name}asrtext" for name in language_names):
            index += 1
            continue
        cleaned.append(words[index])
        index += 1
    return cleaned


def migrate_persisted_transcription_markers() -> None:
    changed = False
    for project in STATE.get("projects", {}).values():
        for field in ("transcript", "transcript_words"):
            items = project.get(field)
            if not isinstance(items, list):
                continue
            cleaned_items = []
            for item in items:
                if not isinstance(item, dict):
                    cleaned_items.append(item)
                    continue
                text = clean_transcription_markers(item.get("text") or item.get("word"))
                if text:
                    if text != item.get("text"):
                        item["text"] = text
                        changed = True
                    cleaned_items.append(item)
                else:
                    changed = True
            if len(cleaned_items) != len(items):
                project[field] = cleaned_items
        for cue in project.get("cues", []):
            if isinstance(cue, dict) and cue.get("anchor_text"):
                anchor_text = clean_transcription_markers(cue["anchor_text"])
                if anchor_text != cue["anchor_text"]:
                    cue["anchor_text"] = anchor_text
                    changed = True
    if changed:
        save_state()




def transcribe_with_vocarium(audio_path: Path, language: str, total_duration_ms: int) -> list[dict[str, Any]]:
    base_url = os.getenv("WHISPER_URL", "http://whisper-stt:8000").rstrip("/")
    if not base_url:
        raise RuntimeError("Whisper-STT ist nicht konfiguriert (WHISPER_URL)")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg fehlt für das ASR-Chunking")
    # whisper-stt hält large-v3 in int8_float16 (~2 GiB) auf GPU 0, teilt sich
    # die Karte aber mit dem residenten OmniVoice. 90-Sekunden-Chunks halten
    # den Spitzenbedarf sicher unter den 12 GiB der RTX 3060.
    chunk_seconds = max(30, min(int(os.getenv("ASR_CHUNK_SECONDS", "90")), 120))
    chunk_dir = audio_path.parent / f"{audio_path.stem}-chunks-{chunk_seconds}s"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks = sorted(chunk_dir.glob("chunk-*.flac"))
    if not chunks:
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(audio_path), "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1", "-c:a", "flac", str(chunk_dir / "chunk-%04d.flac")], check=True, timeout=900)
        chunks = sorted(chunk_dir.glob("chunk-*.flac"))
    if not chunks:
        raise RuntimeError("Die Plex-Audiospur konnte nicht in ASR-Chunks zerlegt werden")
    results = []
    offset_ms = 0
    for index, chunk in enumerate(chunks):
        duration_ms = min(chunk_seconds * 1000, max(1, total_duration_ms - offset_ms))
        cache = chunk.with_suffix(".timestamps.json")
        transcription = None
        if cache.exists():
            try:
                cached = json.loads(cache.read_text("utf-8"))
                if cached.get("schema_version") == 1:
                    transcription = normalize_vocarium_transcription(cached, duration_ms)
            except (AttributeError, json.JSONDecodeError, OSError, RuntimeError):
                transcription = None
        if transcription is None:
            with chunk.open("rb") as source:
                response = httpx.post(
                    f"{base_url}/v1/audio/transcriptions",
                    files={"file": (chunk.name, source, "audio/flac")},
                    data={"model": os.getenv("WHISPER_PROFILE", ""), "language": language},
                    timeout=1200,
                )
            response.raise_for_status()
            try:
                transcription = normalize_vocarium_transcription(response.json(), duration_ms)
            except RuntimeError as error:
                raise RuntimeError(f"Vocarium-Transkript für ASR-Chunk {index + 1} ist ungültig: {error}") from error
            cache.write_text(json.dumps(transcription, ensure_ascii=False, indent=2), "utf-8")
        results.append({
            "index": index,
            "start_ms": offset_ms,
            "end_ms": offset_ms + duration_ms,
            "duration_ms": duration_ms,
            "text": transcription["text"],
            "language": transcription["language"],
            "words": transcription["words"],
            "segments": transcription["segments"],
            "timestamp_source": transcription["timestamp_source"],
            "chunk_file": str(chunk.relative_to(ARTIFACTS)),
            "timestamp_cache": str(cache.relative_to(ARTIFACTS)),
        })
        offset_ms += duration_ms
    return results


ENGINE_VOICE_CACHE: tuple[float, dict[str, str]] = (0.0, {})


def engine_base_urls() -> dict[str, str]:
    return {
        "omnivoice": os.getenv("OMNIVOICE_TTS_URL", "http://omnivoice-tts:8880").rstrip("/"),
        "kikiri": os.getenv("KIKIRI_TTS_URL", "http://kikiri-tts:8881").rstrip("/"),
    }


def engine_voice_map(*, force: bool = False) -> dict[str, str]:
    """``voice_id`` -> ``"omnivoice"`` | ``"kikiri"``.

    Szenenklang fragte zwei ``/v1/voices``-Endpunkte ab, von denen der zweite
    (``QWEN_TTS_BASE_URL``) auf den abgeschalteten Qwen-Dienst zeigte — jeder
    Aufruf lief dort in einen Verbindungsfehler. Vocarium kennt genau zwei
    Sprach-Engines. Der Abgleich entspricht
    ``VocariumTTSGenerator.engine_voices``, bleibt aber synchron: die Render-
    und Pipeline-Threads besitzen keinen Event-Loop.
    """
    global ENGINE_VOICE_CACHE
    fetched, cached = ENGINE_VOICE_CACHE
    if not force and cached and time.monotonic() - fetched < 30:
        return cached
    bases = engine_base_urls()
    voices: dict[str, str] = {}
    try:
        response = httpx.get(f"{bases['omnivoice']}/v1/audio/voices", timeout=8)
        response.raise_for_status()
        for item in response.json().get("voices", []):
            if item.get("voice_id"):
                voices[str(item["voice_id"])] = "omnivoice"
    except Exception as error:
        logger.warning("Hörspiele: OmniVoice-Stimmen nicht erreichbar (%r)", error)
    try:
        response = httpx.get(f"{bases['kikiri']}/v1/models", timeout=8)
        response.raise_for_status()
        for item in response.json().get("data", []):
            if item.get("id"):
                voices[str(item["id"])] = "kikiri"
    except Exception as error:
        logger.warning("Hörspiele: Kikiri-Stimmen nicht erreichbar (%r)", error)
    if voices:
        ENGINE_VOICE_CACHE = (time.monotonic(), voices)
        return voices
    return cached


def fetch_tts_voices() -> list[dict[str, Any]] | None:
    voices = engine_voice_map()
    if not voices:
        return None
    return [
        {
            "id": voice_id,
            "name": voice_id.replace("-", " "),
            "language": "de",
            "source": engine,
        }
        for voice_id, engine in sorted(
            voices.items(),
            key=lambda item: (item[1] != "omnivoice", item[0].casefold()),
        )
    ]


def integration_health(name: str, base_url: str | None, path: str, token: str = "") -> dict[str, Any]:
    if not base_url:
        return {"status": "unconfigured", "label": "Nicht konfiguriert", "configured": False}
    try:
        response = httpx.get(f"{base_url.rstrip('/')}{path}", headers={"X-Plex-Token": token} if token else {}, timeout=3)
        response.raise_for_status()
        return {"status": "connected", "label": f"Verbunden mit {name}", "configured": True}
    except Exception:
        return {"status": "offline", "label": f"Konfiguriert, aber {name} antwortet nicht", "configured": True}


def agent_control_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url = os.getenv("CODEX_AGENT_BASE_URL", "").rstrip("/")
    if not base_url:
        raise HTTPException(503, "KI-Runner ist nicht konfiguriert")
    try:
        response = httpx.request(method, f"{base_url}{path}", json=payload, timeout=75)
    except httpx.RequestError as error:
        raise HTTPException(503, "KI-Runner antwortet nicht") from error
    if response.status_code >= 400:
        try:
            detail = str(response.json().get("detail") or "KI-Runner hat die Anfrage abgelehnt")
        except Exception:
            detail = "KI-Runner hat die Anfrage abgelehnt"
        status_code = response.status_code if response.status_code in {404, 409, 422, 429} else 502
        raise HTTPException(status_code, detail[:500])
    if response.status_code == 204 or not response.content:
        return {}
    result = response.json()
    if not isinstance(result, dict):
        raise HTTPException(502, "KI-Runner lieferte eine ungültige Antwort")
    return result


def codex_agent_status() -> dict[str, Any]:
    base_url = os.getenv("CODEX_AGENT_BASE_URL", "").rstrip("/")
    if not base_url:
        return {"status": "unconfigured", "label": "Codex Runner nicht konfiguriert", "configured": False, "authenticated": False}
    try:
        response = httpx.get(f"{base_url}/status", timeout=15)
        response.raise_for_status()
        status = response.json()
        providers = status.get("providers") if isinstance(status.get("providers"), dict) else {}
        profiles = agent_settings()
        phase_chains = {
            name: [profile["provider"], *(item["provider"] for item in profile.get("fallbacks", []))]
            for name, profile in profiles.items()
        }
        if providers and phase_chains:
            phase_ready = {
                name: any(bool(providers.get(provider, {}).get("authenticated")) for provider in chain)
                for name, chain in phase_chains.items()
            }
            authenticated = all(phase_ready.values())
            missing = [
                "Recherche: kein Provider angemeldet" if name == "research"
                else "Skripting: kein Provider angemeldet"
                for name, ready in phase_ready.items()
                if not ready
            ]
            return {
                "status": "connected" if authenticated else "login_required",
                "label": "KI-Runner bereit" if authenticated else "; ".join(missing),
                "configured": True,
                "authenticated": authenticated,
                "providers": providers,
            }
        return {
            "status": status.get("status", "offline"),
            "label": status.get("label", "Codex-Status unbekannt"),
            "configured": True,
            "authenticated": bool(status.get("authenticated")),
        }
    except Exception:
        return {"status": "offline", "label": "Codex Runner antwortet nicht", "configured": True, "authenticated": False}


def chapter_mapping_excerpt(chapter: dict[str, Any], max_chars: int = 5000) -> str:
    text = "\n\n".join(str(fragment.get("text") or "") for fragment in chapter.get("fragments", []))
    if len(text) <= max_chars:
        return text
    marker = "\n\n[… Kapitelmitte gekürzt; Anfang und Ende erhalten …]\n\n"
    available = max(0, max_chars - len(marker))
    leading_chars = available // 2
    trailing_chars = available - leading_chars
    return f"{text[:leading_chars]}{marker}{text[-trailing_chars:]}"


def agent_post(url: str, payload: dict[str, Any], timeout: int) -> "httpx.Response":
    """POST an den Agenten; bei 409 (anderer KI-Lauf aktiv) wird gewartet.

    Der Agent bearbeitet nur einen Lauf gleichzeitig. Laufen zwei Projekte
    parallel (z. B. Reparaturlauf und Neuanlage), soll der zweite anstehen
    statt mit 409 zu scheitern. Gewartet wird hoechstens AGENT_BUSY_WAIT_SECONDS.
    """
    deadline = time.monotonic() + int(os.getenv("AGENT_BUSY_WAIT_SECONDS", "21600"))
    # Ein Neustart des Agenten (Rebuild, Deploy) darf einen stundenlangen
    # Lauf nicht abbrechen: Verbindungsfehler werden bis zu 15 min ueberbrueckt.
    connect_wait = int(os.getenv("AGENT_CONNECT_WAIT_SECONDS", "900"))
    connect_deadline: float | None = None
    while True:
        try:
            response = httpx.post(url, json=payload, timeout=timeout)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.PoolTimeout) as error:
            # Die Frist beginnt beim ersten Verbindungsfehler, nicht beim Aufruf:
            # ein Paket laeuft selbst 30 min und laenger.
            if connect_deadline is None:
                connect_deadline = time.monotonic() + connect_wait
            if time.monotonic() > connect_deadline:
                raise
            logging.warning("Agent nicht erreichbar (%s), neuer Versuch in 15 s", error)
            time.sleep(15)
            continue
        if response.status_code != 409 or time.monotonic() > deadline:
            return response
        time.sleep(30)


def agent_request_timeout(execution: dict[str, Any]) -> int:
    # Each provider may repair its own output several times, so the runner's real
    # budget is a multiple of a single call.  Cutting the connection earlier
    # discards finished work and reports an unreachable runner instead of the
    # actual contract violation.
    candidates = [execution, *execution.get("fallbacks", [])]
    return sum(int(candidate["timeout_seconds"]) for candidate in candidates) * 2 + 60


def research_episode_mapping(project: dict[str, Any], episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    base_url = os.getenv("CODEX_AGENT_BASE_URL", "").rstrip("/")
    if not base_url:
        return None
    payload = {
        "series": {"title": project["binding"]["title"], "year": project["binding"].get("year")},
        "chapters": [
            {
                "id": chapter["id"],
                "order": chapter["order"],
                "title": chapter["title"],
                "text_excerpt": chapter_mapping_excerpt(chapter),
            }
            for chapter in project["chapters"]
        ],
        "episodes": [
            {
                "id": episode["id"],
                "season": episode["season"],
                "episode": episode["episode"],
                "title": episode["title"],
                "summary": episode.get("summary", ""),
                "originally_available_at": episode.get("originally_available_at"),
            }
            for episode in episodes
        ],
        "execution": agent_execution_settings("research"),
    }
    try:
        response = agent_post(
            f"{base_url}/research/mapping",
            payload,
            agent_request_timeout(payload["execution"]),
        )
        if response.status_code == 401:
            raise RuntimeError("Codex Device Auth fehlt; bitte im Admin-Terminal anmelden")
        response.raise_for_status()
        response_payload = response.json()
        result = response_payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Codex Runner lieferte kein Mapping-Objekt")
        if isinstance(response_payload.get("execution"), dict):
            result["execution"] = response_payload["execution"]
        return result
    except httpx.HTTPStatusError as error:
        try:
            detail = error.response.json().get("detail")
        except Exception:
            detail = None
        raise RuntimeError(str(detail or "Codex-Recherche wurde vom Runner abgelehnt")) from error
    except httpx.RequestError as error:
        raise RuntimeError("Codex Runner ist während der Mapping-Recherche nicht erreichbar") from error


def mapped_episode_descriptors(project: dict[str, Any]) -> list[dict[str, Any]]:
    descriptors: dict[str, dict[str, Any]] = {}
    media = {
        str(item.get("episode_id") or ""): item
        for item in project.get("media_assets", [])
    }
    for item in project.get("mapping", []):
        episode_id = str(item.get("episode_id") or "")
        if not episode_id or episode_id in descriptors:
            continue
        asset = media.get(episode_id, {})
        descriptors[episode_id] = {
            "id": episode_id,
            "season": int(item.get("season") or asset.get("season") or 0),
            "episode": int(item.get("episode") or asset.get("episode") or 0),
            "title": str(item.get("episode_title") or asset.get("title") or episode_id),
            "summary": str(item.get("episode_summary") or ""),
            "originally_available_at": item.get("originally_available_at"),
        }
    return list(descriptors.values())


def episode_context_entry_is_usable(entry: Any) -> bool:
    """Prueft einen einzelnen Folgenkontext gegen den Quellenvertrag des Agenten.

    Dieselbe Bedingung, an der `validate_episode_context_result()` im Agenten
    scheitert: Synopsis vorhanden und mindestens eine pruefbare HTTP(S)-Quelle.
    """
    if not isinstance(entry, dict) or not str(entry.get("episode_id") or "").strip():
        return False
    if not str(entry.get("synopsis") or "").strip():
        return False
    sources = entry.get("sources")
    return (
        isinstance(sources, list)
        and bool(sources)
        and all(
            str(source.get("url") or "").startswith(("https://", "http://"))
            for source in sources
            if isinstance(source, dict)
        )
        and all(isinstance(source, dict) for source in sources)
    )


def research_episode_context(
    project: dict[str, Any],
    progress: "Callable[[str], None] | None" = None,
    persist: "Callable[[dict[str, Any]], None] | None" = None,
) -> dict[str, Any]:
    """Recherchiert den Folgenkontext in kleinen Paketen.

    Ein einziger Agentenlauf fuer alle Folgen mit Live-Websuche und maximaler
    Denkstufe sprengte auf GLM regelmaessig das Zeitlimit (8 Folgen > 20 min).
    Pakete von EPISODE_CONTEXT_BATCH Folgen bleiben jeweils unter dem Limit,
    liefern Zwischenstand und lassen einen Abbruch nicht alles verlieren.

    Der Zwischenstand wird nach jedem Paket ueber `persist` gesichert, und ein
    neuer Lauf uebernimmt bereits belegte Folgen daraus. Zuvor schrieb erst der
    vollstaendige Durchlauf, sodass ein Fehlschlag im letzten Paket saemtliche
    fertigen Pakete verwarf und die naechste Recherche bei null begann.
    """
    episodes = mapped_episode_descriptors(project)
    if not episodes:
        raise RuntimeError("Keine gemappten Episoden für die Kontext-Recherche vorhanden")
    batch_size = max(1, int(os.getenv("EPISODE_CONTEXT_BATCH", "2") or 2))
    merged: dict[str, Any] = {"series_title": project["binding"]["title"], "episode_contexts": [], "notes": ""}
    notes: list[str] = []
    known_characters = {
        str(entry["canonical"]): {
            "name": str(entry["canonical"]), "aliases": list(entry.get("aliases") or []),
            "role": "", "visual_description": "", "introduced_in_episode": "",
        }
        for entry in project.get("narration_name_lexicon", []) if entry.get("canonical")
    }
    contexts_by_episode: dict[str, dict[str, Any]] = {}

    def absorb_characters(context: dict[str, Any]) -> None:
        for character in context.get("character_introductions") or []:
            name = canonicalize_narration_names(str(character.get("name") or ""),
                                                project.get("narration_name_lexicon", []))
            if not name:
                continue
            existing = known_characters.get(name) or {"name": name, "aliases": []}
            known_characters[name] = {
                **existing, "role": character.get("role") or existing.get("role", ""),
                "visual_description": character.get("visual_description") or existing.get("visual_description", ""),
                "introduced_in_episode": existing.get("introduced_in_episode") or (
                    str(context.get("episode_id") or "") if character.get("introduction_required") else ""
                ),
            }

    def snapshot() -> dict[str, Any]:
        ordered = [contexts_by_episode[item["id"]] for item in episodes if item["id"] in contexts_by_episode]
        return {**merged, "episode_contexts": ordered, "notes": "\n".join(notes)}

    expected_ids = {str(item["id"]) for item in episodes}
    previous = project.get("episode_context_research")
    if isinstance(previous, dict):
        if previous.get("series_title"):
            merged["series_title"] = previous["series_title"]
        for entry in previous.get("episode_contexts") or []:
            episode_id = str(entry.get("episode_id") or "")
            if episode_id in expected_ids and episode_id not in contexts_by_episode \
                    and episode_context_entry_is_usable(entry):
                contexts_by_episode[episode_id] = json.loads(json.dumps(entry))
                absorb_characters(entry)

    pending = [item for item in episodes if item["id"] not in contexts_by_episode]
    reused = len(episodes) - len(pending)
    if reused and progress:
        progress(f"{reused} von {len(episodes)} Folgenkontexten sind belegt und werden übernommen")

    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        done = reused + start
        if progress:
            progress(
                f"Folgenkontext wird recherchiert: Folge {done + 1}-{done + len(batch)} von {len(episodes)}"
            )
        part = _research_episode_context_batch(project, batch, list(known_characters.values()))
        for context in part.get("episode_contexts") or []:
            absorb_characters(context)
            episode_id = str(context.get("episode_id") or "")
            if episode_id:
                contexts_by_episode[episode_id] = context
        if part.get("series_title"):
            merged["series_title"] = part["series_title"]
        if part.get("notes"):
            notes.append(str(part["notes"]))
        if isinstance(part.get("execution"), dict):
            merged["execution"] = part["execution"]
        if persist:
            # Jedes fertige Paket sofort sichern: ein Fehlschlag im naechsten
            # Paket kostet dann nur dieses Paket, nicht die ganze Recherche.
            persist(snapshot())
    return snapshot()


def _research_episode_context_batch(
    project: dict[str, Any], episodes: list[dict[str, Any]],
    known_characters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base_url = os.getenv("CODEX_AGENT_BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("Codex Runner ist für die Episodenkontext-Recherche nicht konfiguriert")
    payload = {
        "series": {
            "title": project["binding"]["title"],
            "year": project["binding"].get("year"),
        },
        "episodes": episodes,
        "known_characters": known_characters or [],
        "execution": agent_execution_settings("research"),
    }
    try:
        response = agent_post(
            f"{base_url}/research/episode-context",
            payload,
            agent_request_timeout(payload["execution"]),
        )
        if response.status_code == 401:
            raise RuntimeError("Codex Device Auth fehlt; bitte im Admin-Terminal anmelden")
        response.raise_for_status()
        response_payload = response.json()
        result = response_payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Codex Runner lieferte keinen Episodenkontext")
        if isinstance(response_payload.get("execution"), dict):
            result["execution"] = response_payload["execution"]
        return result
    except httpx.HTTPStatusError as error:
        try:
            detail = error.response.json().get("detail")
        except Exception:
            detail = None
        raise RuntimeError(str(detail or "Codex-Episodenkontext wurde vom Runner abgelehnt")) from error
    except httpx.RequestError as error:
        raise RuntimeError("Codex Runner ist während der Episodenkontext-Recherche nicht erreichbar") from error


def episode_context_research_is_current(
    project: dict[str, Any],
    research: Any,
) -> bool:
    if not isinstance(research, dict):
        return False
    expected_episode_ids = set(unique_episode_ids(project.get("mapping", [])))
    contexts = research.get("episode_contexts")
    if not isinstance(contexts, list) or len(contexts) != len(expected_episode_ids):
        return False
    returned_episode_ids = [str(item.get("episode_id") or "") for item in contexts]
    if set(returned_episode_ids) != expected_episode_ids or len(set(returned_episode_ids)) != len(returned_episode_ids):
        return False
    return all(
        str(item.get("synopsis") or "").strip()
        and isinstance(item.get("sources"), list)
        and bool(item["sources"])
        and all(
            str(source.get("url") or "").startswith(("https://", "http://"))
            for source in item["sources"]
        )
        and all(
            not character.get("introduction_required")
            or (bool(str(character.get("visual_description") or "").strip())
                and not re.search(r"\b(?:nicht (?:verifiziert|belegt|bestätigt)|unverified|unconfirmed)\b",
                                  str(character.get("visual_description") or ""), re.IGNORECASE))
            for character in item.get("character_introductions", [])
        )
        for item in contexts
    )


def raw_scene_alignment_seed(chunk_payload: dict[str, Any]) -> dict[str, Any] | None:
    if str(chunk_payload.get("narration_density") or "") != "detailed":
        return None
    cues = sorted(
        chunk_payload.get("cues", []),
        key=lambda item: int(item.get("order") or 0),
    )
    segments = sorted(
        chunk_payload.get("segments", []),
        key=lambda item: int(item.get("start_ms") or 0),
    )
    if not cues or len(segments) < len(cues):
        return None
    used_anchor_ids: set[str] = set()
    previous_anchor_ms = -1
    alignments = []
    for cue in cues:
        proposed_anchor_ms = int(cue.get("proposed_anchor_ms") or 0)
        candidates = [
            segment
            for segment in segments
            if (
                str(segment.get("id") or "") not in used_anchor_ids
                and int(segment.get("start_ms") or 0) >= previous_anchor_ms
            )
        ]
        if not candidates:
            return None
        anchor = min(
            candidates,
            key=lambda segment: (
                abs(int(segment.get("start_ms") or 0) - proposed_anchor_ms),
                int(segment.get("start_ms") or 0),
            ),
        )
        anchor_id = str(anchor.get("id") or "")
        used_anchor_ids.add(anchor_id)
        previous_anchor_ms = int(anchor.get("start_ms") or 0)
        narration_text = str(cue.get("text") or "").strip()
        alignments.append({
            "cue_id": str(cue.get("id") or ""),
            "narration_text": narration_text,
            "purpose": (
                "book_boundary"
                if cue.get("book_edge") in {"start", "end", "start_and_end"}
                else "visual_action"
            ),
            "introduced_characters": [],
            "information_gain": (
                "Bewahrt die konkrete, im Serienaudio noch unabhängig zu prüfende "
                "Handlung der quellengebundenen Rohpassage."
            ),
            "source_detail_coverage": 1.0,
            "native_audio_relation": (
                "complements_existing_audio"
                if str(cue.get("nearby_subtitles") or "").strip()
                else "no_relevant_existing_narration"
            ),
            "retained_visual_details": [narration_text[:240]],
            "anchor_segment_id": anchor_id,
            "placement": "before_anchor",
            "confidence": 0.9,
            "reasoning": (
                "Automatische Reparaturbasis am nächsten eindeutigen lokalen "
                "Untertitelanker; der unabhängige Agent-Validator entscheidet "
                "vor jeder Wiederverwendung über Text und Timing."
            ),
        })
    return {
        "alignments": alignments,
        "character_introductions": [],
        "name_aliases": [],
        "notes": [
            "Quellengebundene Rohpassagen wurden ausschließlich als unabhängige "
            "Validator- und Reparaturbasis verwendet."
        ],
    }


def normalized_character_evidence(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    characters = list(text)
    for index, character in enumerate(characters):
        if (
            character == "I"
            and index > 0
            and index + 1 < len(characters)
            and characters[index - 1].isalpha()
            and characters[index + 1].isalpha()
        ):
            characters[index] = "l"
    return " ".join(
        re.findall(r"[a-zäöüß0-9]+", "".join(characters).casefold())
    )


def character_name_evidenced(corpus: Any, name: Any) -> bool:
    raw_name = str(name or "").strip()
    if len(re.sub(r"\W", "", raw_name, flags=re.UNICODE)) <= 3:
        return bool(
            raw_name
            and re.search(rf"(?<!\w){re.escape(raw_name)}(?!\w)", str(corpus or ""))
        )
    corpus_tokens = normalized_character_evidence(corpus).split()
    name_tokens = normalized_character_evidence(raw_name).split()
    if not corpus_tokens or not name_tokens:
        return False
    return all(
        any(
            token == candidate
            or (
                len(token) >= 4
                and len(candidate) >= 4
                and difflib.SequenceMatcher(None, token, candidate).ratio() >= 0.8
            )
            for candidate in corpus_tokens
        )
        for token in name_tokens
    )


def character_name_variants(project: dict[str, Any], research_name: Any) -> list[str]:
    raw_name = str(research_name or "").strip()
    variants = [
        value.strip(" -")
        for value in re.split(r"[()/]", raw_name)
        if value.strip(" -")
    ]
    for entry in project.get("narration_name_lexicon", []):
        canonical = str(entry.get("canonical") or "").strip()
        aliases = [
            str(value).strip()
            for value in entry.get("aliases", [])
            if str(value).strip()
        ]
        # A character's name inside a role name is not an alias for that
        # person: "Panputto" and "Panputtos Manager" must stay distinct.
        explicit_keys = {"".join(normalized_character_evidence(value).split()) for value in variants}
        if any(
            "".join(normalized_character_evidence(value).split()) in explicit_keys
            for value in [canonical, *aliases]
            if normalized_character_evidence(value)
        ):
            variants.extend([canonical, *aliases])
    return list(dict.fromkeys(value for value in variants if value))


def character_identity_keys(project: dict[str, Any], research: dict[str, Any]) -> dict[str, str]:
    """Use explicit aliases and parenthetical names, never fuzzy character matches."""
    def key(value: Any) -> str:
        return "".join(normalized_character_evidence(value).split())

    parents: dict[str, str] = {}
    def root(value: str) -> str:
        parents.setdefault(value, value)
        while parents[value] != value:
            value = parents[value]
        return value

    groups = [[str(entry.get("canonical") or ""), *entry.get("aliases", [])]
              for entry in project.get("narration_name_lexicon", [])]
    names = [str(character.get("name") or "")
             for context in research.get("episode_contexts", [])
             for character in context.get("character_introductions", [])]
    names.extend(str(item.get("research_name") or "") for item in project.get("character_introductions", []))
    groups.extend([name, *re.split(r"[()/]", name)] for name in names)
    for group in groups:
        keys = [key(value) for value in group if key(value)]
        if keys:
            for value in keys[1:]:
                parents[root(value)] = root(keys[0])
    return {value: root(value) for value in parents}


def normalize_character_introduction_contexts(
    project: dict[str, Any], research: dict[str, Any],
) -> dict[str, Any]:
    """One required introduction per character across the whole book, before batching."""
    result = json.loads(json.dumps(research))
    identities = character_identity_keys(project, research)
    def identity(value: Any) -> str:
        key = "".join(normalized_character_evidence(value).split())
        return identities.get(key, key)

    preferred = {identity(entry.get("canonical")): str(entry["canonical"])
                 for entry in project.get("narration_name_lexicon", []) if entry.get("canonical")}
    order = {ep: index for index, ep in enumerate(unique_episode_ids(project.get("mapping", [])))}
    contexts = result.get("episode_contexts", [])
    seen: set[str] = set()
    for context in sorted(contexts, key=lambda c: order.get(str(c.get("episode_id") or ""), len(order))):
        for character in context.get("character_introductions", []):
            name = str(character.get("name") or "")
            ident = identity(name)
            preferred.setdefault(ident, name.replace("-", " "))
            character["name"] = preferred[ident]
            if character.get("introduction_required"):
                character["introduction_required"] = ident not in seen
                seen.add(ident)
    return result


def quality_repair_needs_introductions(project: dict[str, Any]) -> bool:
    return any(check.get("status") == "failed" and (
        check.get("id") == "character_introduction_timing"
        or (check.get("id") == "narration_editorial_quality" and "Figuren-Einführungen" in str(check.get("detail") or ""))
    ) for check in (project.get("quality_report") or {}).get("checks", []))


def add_dedicated_character_introduction_candidates(
    project: dict[str, Any],
    cues: list[dict[str, Any]],
    episode_context_research: dict[str, Any],
    *, force: bool = False, preserve_existing: bool = False,
) -> list[dict[str, Any]]:
    """Add a clean first-look cue when no book passage can carry one."""
    episode_context_research = normalize_character_introduction_contexts(project, episode_context_research)
    fragments = {
        str(fragment.get("id") or ""): fragment
        for chapter in project.get("chapters", [])
        for fragment in chapter.get("fragments", [])
    }
    episode_segments: dict[str, list[dict[str, Any]]] = {}
    for segment in project.get("reconciled_transcript", []):
        if transcript_item_outside_intros(project, segment):
            episode_segments.setdefault(
                str(segment.get("episode_id") or ""), []
            ).append(segment)
    for rows in episode_segments.values():
        rows.sort(key=lambda item: int(item.get("episode_start_ms") or 0))

    result = list(cues)
    used_candidate_anchor_ids: dict[str, set[str]] = {}
    cue_source_by_episode: dict[str, str] = {}
    for cue in cues:
        episode_id = str(cue.get("anchor_episode_id") or "")
        source_text = "\n".join(
            str(fragments.get(str(fragment_id), {}).get("text") or "")
            for fragment_id in cue.get("source_fragment_ids", [])
        )
        cue_source_by_episode[episode_id] = (
            cue_source_by_episode.get(episode_id, "") + "\n" + source_text
        )

    existing_ids = {str(cue.get("id") or "") for cue in result}
    identities = character_identity_keys(project, episode_context_research)
    existing_cues = {str(cue.get("id") or ""): cue for cue in project.get("cues", [])}
    existing_introduction_episodes: set[tuple[str, str]] = set()
    retained_carriers: set[tuple[str, str]] = set()
    for intro in project.get("character_introductions", []):
        name = "".join(normalized_character_evidence(intro.get("research_name")).split())
        existing_cue = existing_cues.get(str(intro.get("cue_id") or ""))
        if existing_cue:
            existing_introduction_episodes.add((
                identities.get(name, name), str(existing_cue.get("anchor_episode_id") or "")
            ))
            if not existing_cue.get("character_introduction_candidate"):
                retained_carriers.add((identities.get(name, name), str(existing_cue.get("anchor_episode_id") or "")))
    for context in episode_context_research.get("episode_contexts", []):
        episode_id = str(context.get("episode_id") or "")
        episode_source = cue_source_by_episode.get(episode_id, "")
        required_characters = [
            character
            for character in context.get("character_introductions", [])
            if character.get("introduction_required")
        ]
        group_variants = {
            str(character.get("name") or ""): character_name_variants(
                project, character.get("name")
            )
            for character in required_characters
        }
        for character in required_characters:
            if not character.get("introduction_required"):
                continue
            research_name = str(character.get("name") or "").strip()
            visual_description = str(
                character.get("visual_description") or ""
            ).strip()
            name_variants = group_variants.get(research_name, [research_name])
            name_key = "".join(normalized_character_evidence(research_name).split())
            if preserve_existing and not force and (identities.get(name_key, name_key), episode_id) in retained_carriers:
                # A coverage-only repair retains its already accepted normal or
                # picture introduction; a new dedicated task would conflict
                # with that immutable formal mapping.
                continue
            needs_replacement = force and (
                identities.get(name_key, name_key), episode_id
            ) not in existing_introduction_episodes
            if (
                not research_name
                or not visual_description
                or (not needs_replacement and any(
                    character_name_evidenced(episode_source, variant)
                    for variant in name_variants
                ))
            ):
                continue
            rows = episode_segments.get(episode_id, [])
            def first_match_index(variants: list[str]) -> int | None:
                for index, segment in enumerate(rows):
                    segment_text = str(
                        segment.get("reconciled_text") or segment.get("text") or ""
                    ).strip()
                    # Quoted episode-title cards are not character appearances.
                    if segment_text.startswith(("„", "“", '"')):
                        continue
                    if any(
                        character_name_evidenced(segment_text, variant)
                        for variant in variants
                    ):
                        return index
                return None

            match_index = first_match_index(name_variants)
            if match_index is None:
                related_variants = [
                    variant
                    for other_name, variants in group_variants.items()
                    if other_name != research_name
                    for variant in variants
                ]
                match_index = first_match_index(related_variants)
            anchor = None
            if match_index is not None:
                scene_candidates = rows[max(0, match_index - 2):match_index + 1]
                used_ids = used_candidate_anchor_ids.setdefault(episode_id, set())
                anchor = next(
                    (
                        segment
                        for segment in scene_candidates
                        if str(segment.get("id") or "") not in used_ids
                    ),
                    None,
                )
            if anchor is None and rows:
                # These are search hints, not proved first-appearance anchors.
                # A crowded three-subtitle window must never erase a required
                # character from the model's available repair candidates.
                used_ids = used_candidate_anchor_ids.setdefault(episode_id, set())
                hint_index = match_index if match_index is not None else 0
                available = [
                    (index, row) for index, row in enumerate(rows)
                    if str(row.get("id") or "") not in used_ids
                ]
                if available:
                    anchor = min(available, key=lambda item: (
                        abs(item[0] - hint_index), item[0] > hint_index, item[0],
                    ))[1]
                else:
                    anchor = rows[hint_index]
            if anchor is None:
                continue
            used_candidate_anchor_ids.setdefault(episode_id, set()).add(
                str(anchor.get("id") or "")
            )
            digest = hashlib.sha256(
                f"{project.get('id')}|{episode_id}|{normalized_character_evidence(research_name)}".encode()
            ).hexdigest()[:20]
            cue_id = f"cue_intro_{digest}"
            if cue_id in existing_ids:
                continue
            source_context = (
                f"{research_name} trat erstmals ins Bild: {visual_description}"
            ).rstrip(".") + "."
            result.append({
                "id": cue_id,
                "chapter_id": "",
                "chapter_title": "Figureneinführung",
                "text": source_context,
                "source_fragment_ids": [],
                "editorial_source_context": source_context,
                "character_introduction_candidate": True,
                "introduction_research_name": research_name,
                "anchor_segment_id": str(anchor.get("id") or ""),
                "anchor_episode_id": episode_id,
                "anchor_episode_start_ms": int(anchor.get("episode_start_ms") or 0),
                "anchor_global_start_sample": int(anchor.get("start_sample") or 0),
                "anchor_text": str(anchor.get("reconciled_text") or anchor.get("text") or ""),
                "anchor_match_score": 0.95,
                "placement_policy": "after_aligned_speech_gap",
                "estimated_duration_ms": max(1_800, len(source_context.split()) * 390),
                "confidence": 0.95,
                "revision": 1,
            })
            existing_ids.add(cue_id)
    return result


def scene_alignment_book_edge(cue: dict[str, Any], first_fragment_id: str, last_fragment_id: str) -> str:
    """Coverage context can cite edge paragraphs without becoming another edge."""
    if cue.get("coverage_repair_window"):
        return "none"
    fragment_ids = {str(value) for value in cue.get("source_fragment_ids", [])}
    if first_fragment_id and first_fragment_id == last_fragment_id and first_fragment_id in fragment_ids:
        return "start_and_end"
    if first_fragment_id in fragment_ids:
        return "start"
    if last_fragment_id in fragment_ids:
        return "end"
    return "none"


SCENE_ALIGNMENT_MAX_CUES = max(4, int(os.getenv("SCENE_ALIGNMENT_MAX_CUES") or 12))
SCENE_ALIGNMENT_MAX_INTROS = max(1, int(os.getenv("SCENE_ALIGNMENT_MAX_INTROS") or 4))


def scene_alignment_packages(
    project: dict[str, Any],
    payload: dict[str, Any],
    coverage_targets: dict[str, dict[str, Any]],
    episode_ids: list[str],
) -> list[dict[str, Any]]:
    """Zerlegt jede Folge in zusammenhaengende Abschnitte der Tonspur.

    Ein Paket je Folge war die Arbeitseinheit, bis eine Folge mit vielen
    Pflicht-Einfuehrungen (Band 10, Folge 56857: zwoelf) den Editor in eine
    gemeinsame Zuordnung schickte, die er nicht mehr abschloss: ein Lauf
    verbrauchte das gesamte Ausgabebudget von 256.000 Token auf Denken und
    antwortete nie.  Geschnitten wird entlang der Ankerzeit, weil der Runner
    Kandidatenzahl und Abdeckungsluecken aus der uebergebenen Nutzlast
    ableitet und nicht aus der Folge als Ganzes -- ein Abschnitt ohne
    Kandidaten erzeugt daher keine Phantomluecke.
    """
    rules = narration_rules(project)
    target_ms = max(30_000, int(rules["narration_coverage_target_ms"]))
    retained_windows = retained_content_windows(project)

    def identity(value: Any) -> str:
        return "".join(normalized_character_evidence(value).split())

    required_by_episode: dict[str, dict[str, str]] = {}
    for context in payload.get("episode_contexts", []):
        episode_id = str(context.get("episode_id") or "")
        for introduction in context.get("character_introductions", []):
            if not introduction.get("introduction_required"):
                continue
            name = str(introduction.get("name") or "").strip()
            if name:
                required_by_episode.setdefault(episode_id, {})[identity(name)] = name

    cues_by_episode: dict[str, list[dict[str, Any]]] = {}
    for cue in payload.get("cues", []):
        cues_by_episode.setdefault(str(cue.get("proposed_episode_id") or ""), []).append(cue)
    for rows in cues_by_episode.values():
        rows.sort(key=lambda item: (
            int(item.get("proposed_anchor_ms") or 0),
            int(item.get("order") or 0),
        ))

    packages: list[dict[str, Any]] = []
    for episode_id in episode_ids:
        rows = cues_by_episode.get(episode_id) or []
        if not rows:
            continue
        required = required_by_episode.get(episode_id) or {}

        # Wo kann eine Pflicht-Einfuehrung ueberhaupt getragen werden?  Ein
        # eigener Einfuehrungs-Cue nennt seinen Namen selbst; sonst traegt sie
        # die frueheste Buchpassage, die den Namen belegt.  Beides ist
        # unabhaengig vom Schnitt, der Schnitt darf sich also darauf stuetzen.
        carrier_of: dict[str, str] = {}
        for key, name in required.items():
            variants = character_name_variants(project, name) or [name]
            dedicated = [
                cue for cue in rows
                if cue.get("character_introduction_candidate")
                and identity(cue.get("introduction_research_name")) == key
            ]
            if dedicated:
                carrier_of[key] = str(dedicated[0].get("id") or "")
                continue
            for cue in rows:
                if cue.get("character_introduction_candidate"):
                    continue
                corpus = f"{cue.get('text') or ''}\n{cue.get('source_context') or ''}"
                if any(character_name_evidenced(corpus, variant) for variant in variants):
                    carrier_of[key] = str(cue.get("id") or "")
                    break

        carried_by_cue: dict[str, set[str]] = {}
        for key, cue_id in carrier_of.items():
            carried_by_cue.setdefault(cue_id, set()).add(key)
        # Ohne belegten Traeger bleibt die Einfuehrung unerfuellbar -- wie
        # bisher; sie faellt dem ersten Abschnitt zu, damit der Fehler dort
        # sichtbar wird statt in jedem Abschnitt der Folge.
        orphans = {key for key in required if key not in carrier_of}

        runs: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_keys: set[str] = set()
        for cue in rows:
            cue_keys = carried_by_cue.get(str(cue.get("id") or ""), set())
            if current and (
                len(current) >= SCENE_ALIGNMENT_MAX_CUES
                or len(current_keys | cue_keys) > SCENE_ALIGNMENT_MAX_INTROS
            ):
                runs.append(current)
                current, current_keys = [], set()
            current.append(cue)
            current_keys |= cue_keys
        if current:
            runs.append(current)

        book_edge = str((coverage_targets.get(episode_id) or {}).get("book_edge_episode") or "none")
        boundaries: list[int | None] = [None]
        for left, right in zip(runs, runs[1:]):
            boundaries.append((
                int(left[-1].get("proposed_anchor_ms") or 0)
                + int(right[0].get("proposed_anchor_ms") or 0)
            ) // 2)
        boundaries.append(None)

        for index, run in enumerate(runs):
            window_start, window_end = boundaries[index], boundaries[index + 1]
            retained_ms = sum(
                max(0, min(end, window_end if window_end is not None else end)
                    - max(start, window_start if window_start is not None else start))
                for start, end in retained_windows.get(episode_id) or []
            )
            candidate_anchor_ms = sorted(
                int(cue.get("proposed_anchor_ms") or 0)
                for cue in run
                if not cue.get("character_introduction_candidate")
            )
            needed = math.ceil(retained_ms / target_ms) if retained_ms else 0
            edge = "none"
            if book_edge in {"start", "both"} and index == 0:
                edge = "start"
            if book_edge in {"end", "both"} and index == len(runs) - 1:
                edge = "both" if edge == "start" else "end"
            keys = {key for cue in run for key in carried_by_cue.get(str(cue.get("id") or ""), set())}
            packages.append({
                "episode_id": episode_id,
                "label": (
                    f"{episode_id} · Abschnitt {index + 1}/{len(runs)}"
                    if len(runs) > 1 else episode_id
                ),
                "cue_ids": {str(cue.get("id") or "") for cue in run},
                "window": (window_start, window_end),
                "required_keys": keys | (orphans if index == 0 else set()),
                "coverage_overrides": {
                    "retained_ms": retained_ms,
                    # Zaehlung und Obergrenze gehen ueber *alle* Cues des
                    # Abschnitts, genau wie narration_coverage_targets() sie
                    # ueber alle Cues der Folge rechnet; nur candidate_anchor_ms
                    # laesst Einfuehrungen weg, weil die von der Abdeckung
                    # befreit sind.  Aus der gefilterten Liste gerechnet ergab
                    # ein Abschnitt mit einem normalen und drei Einfuehrungs-
                    # Cues die Forderung "1 bis 1", und der Editor waehlte vier.
                    "candidate_count": len(run),
                    "min_cues": min(len(candidate_anchor_ms), needed) if candidate_anchor_ms else 0,
                    "max_cues": len(run),
                    "candidate_anchor_ms": candidate_anchor_ms,
                    "book_edge_episode": edge,
                },
            })
    return packages


def research_scene_alignment(
    project: dict[str, Any],
    cues: list[dict[str, Any]],
    episode_context_research: dict[str, Any],
    density: str,
    repair: dict[str, Any] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """``repair`` = {"cue_ids": {raw cue ids}, "locked": {raw cue id: alignment}}.

    Im Reparaturmodus bekommt der Agent je Folgenpaket nur die beanstandeten
    Cues zur Neubearbeitung; alle anderen gehen als gesperrte Alignments mit
    und bleiben wortgleich erhalten. Pakete ohne beanstandeten Cue werden
    lokal aus den gesperrten Alignments zusammengesetzt, ohne Agentenaufruf.
    """
    episode_context_research = normalize_character_introduction_contexts(project, episode_context_research)
    base_url = os.getenv("CODEX_AGENT_BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("Codex Runner ist für die Szenenausrichtung nicht konfiguriert")
    fragments = {
        str(fragment["id"]): fragment
        for chapter in project.get("chapters", [])
        for fragment in chapter.get("fragments", [])
    }
    episode_ids = unique_episode_ids(project.get("mapping", []))
    episode_order = {episode_id: index for index, episode_id in enumerate(episode_ids)}
    fixed_policies = {"replace_existing_narration", "overlay_intro_music"}
    regular_cues = [cue for cue in cues if cue.get("placement_policy") not in fixed_policies]
    if repair:
        # New gap pictures must be ordered around the retained scene positions,
        # not around rough source proposals superseded by accepted alignments.
        segment_lookup = {str(segment["id"]): segment for segment in project.get("reconciled_transcript", [])}
        positioned_cues = []
        for cue in regular_cues:
            locked = (repair.get("locked") or {}).get(str(cue["id"])) or {}
            anchor = segment_lookup.get(str(locked.get("anchor_segment_id") or ""))
            if anchor and str(anchor.get("episode_id")) == str(cue.get("anchor_episode_id")):
                cue = {**cue, "anchor_segment_id": anchor["id"],
                       "anchor_episode_start_ms": int(anchor["episode_start_ms"]),
                       "anchor_insert_ms": int(anchor["episode_start_ms"])}
            positioned_cues.append(cue)
        regular_cues = positioned_cues
    cue_aliases = {f"c{index:03d}": str(cue["id"]) for index, cue in enumerate(regular_cues)}
    chronology_order_by_cue_id = {
        str(cue["id"]): order
        for order, (_source_order, cue) in enumerate(sorted(
            enumerate(regular_cues),
            key=lambda indexed_cue: (
                episode_order.get(
                    str(indexed_cue[1].get("anchor_episode_id") or ""),
                    9999,
                ),
                int(
                    indexed_cue[1].get("anchor_episode_start_ms")
                    or indexed_cue[1].get("anchor_insert_ms")
                    or 0
                ),
                indexed_cue[0],
            ),
        ))
    }
    content_fragments = source_content_fragments(project)
    first_fragment_id = str(content_fragments[0].get("id") or "") if content_fragments else ""
    last_fragment_id = str(content_fragments[-1].get("id") or "") if content_fragments else ""
    segment_rows = [
        segment
        for segment in project.get("reconciled_transcript", [])
        if str(segment.get("episode_id") or "") in episode_order
        and transcript_item_outside_intros(project, segment)
    ]
    segments_by_episode: dict[str, list[dict[str, Any]]] = {}
    for segment in segment_rows:
        segments_by_episode.setdefault(str(segment.get("episode_id") or ""), []).append(segment)
    for rows in segments_by_episode.values():
        rows.sort(key=lambda item: int(item.get("episode_start_ms") or 0))

    words_by_episode: dict[str, list[dict[str, Any]]] = {}
    for word in project.get("transcript_words", []):
        if transcript_item_outside_intros(project, word):
            words_by_episode.setdefault(str(word.get("episode_id") or ""), []).append(word)
    media_durations = {
        str(asset.get("episode_id")): int(asset.get("duration_ms") or 0)
        for asset in project.get("media_assets", [])
    }
    speech_gaps_by_episode: dict[str, list[tuple[int, int]]] = {}
    for episode_id, rows in segments_by_episode.items():
        duration_ms = media_durations.get(episode_id) or max(
            (int(row.get("episode_end_ms", 0)) for row in rows),
            default=1,
        )
        speech_gaps_by_episode[episode_id] = speech_free_gaps_without_intros(
            project,
            episode_id,
            rows,
            words_by_episode.get(episode_id, []),
            duration_ms,
        )

    def nearby_speech_gaps(cue: dict[str, Any], window_ms: int = 30_000) -> list[dict[str, int]]:
        """Real music/atmosphere pauses around the anchor, so the editor can use them."""
        episode_id = str(cue.get("anchor_episode_id") or "")
        anchor_ms = int(
            cue.get("anchor_episode_start_ms")
            or cue.get("anchor_insert_ms")
            or 0
        )
        gaps = [
            {
                "start_ms": start,
                "end_ms": end,
                "duration_ms": end - start,
            }
            for start, end in speech_gaps_by_episode.get(episode_id, [])
            if end >= anchor_ms - window_ms
            and start <= anchor_ms + window_ms
            and end - start >= 2_000
        ]
        gaps.sort(key=lambda item: abs(item["start_ms"] - anchor_ms))
        return gaps[:6]

    def nearby_subtitles(cue: dict[str, Any], window_ms: int = 60_000) -> str:
        episode_id = str(cue.get("anchor_episode_id") or "")
        anchor_ms = int(
            cue.get("anchor_episode_start_ms")
            or cue.get("anchor_insert_ms")
            or 0
        )
        rows = [
            segment
            for segment in segments_by_episode.get(episode_id, [])
            if not segment.get("visual_scene_anchor")
            and abs(int(segment.get("episode_start_ms") or 0) - anchor_ms) <= window_ms
        ]
        return "\n".join(
            f"{int(segment.get('episode_start_ms') or 0)} ms: "
            f"{str(segment.get('reconciled_text') or segment.get('text') or '').strip()}"
            for segment in rows
        )[:4_000]

    segment_aliases = {f"s{index:04d}": str(segment["id"]) for index, segment in enumerate(segment_rows)}
    payload = {
        "series": {
            "title": project["binding"]["title"],
            "year": project["binding"].get("year"),
        },
        "preferred_names": [
            str(item.get("canonical") or "")
            for item in project.get("narration_name_lexicon", [])
            if item.get("canonical")
        ],
        "known_character_names": list(dict.fromkeys(
            str(character.get("name") or "").strip()
            for context in episode_context_research.get("episode_contexts", [])
            for character in context.get("character_introductions", [])
            if str(character.get("name") or "").strip()
        )),
        "narration_density": density,
        "episode_contexts": episode_context_research.get("episode_contexts", []),
        "cues": [
            {
                "id": alias,
                "order": chronology_order_by_cue_id[str(cue["id"])],
                "text": cue["text"],
                "source_context": str(cue.get("editorial_source_context") or "")
                or "\n".join(
                    str(fragments.get(str(fragment_id), {}).get("text") or "")
                    for fragment_id in cue.get("source_fragment_ids", [])
                ),
                **({
                    "character_introduction_candidate": True,
                    "introduction_research_name": str(
                        cue.get("introduction_research_name") or ""
                    ),
                } if cue.get("character_introduction_candidate") else {}),
                **({"coverage_repair_window": cue["coverage_repair_window"]} if cue.get("coverage_repair_window") else {}),
                "proposed_episode_id": str(cue.get("anchor_episode_id") or ""),
                "proposed_anchor_ms": int(
                    cue.get("anchor_episode_start_ms")
                    or cue.get("anchor_insert_ms")
                    or 0
                ),
                "nearby_subtitles": nearby_subtitles(cue),
                "nearby_speech_gaps": nearby_speech_gaps(cue),
                "book_edge": scene_alignment_book_edge(cue, first_fragment_id, last_fragment_id),
            }
            for index, (alias, cue) in enumerate(zip(cue_aliases, regular_cues))
        ],
        "segments": [
            {
                "id": alias,
                "episode_id": str(segment.get("episode_id") or ""),
                "episode_order": episode_order.get(str(segment.get("episode_id") or ""), 9999),
                "visual_scene_anchor": bool(segment.get("visual_scene_anchor")),
                "start_ms": int(segment.get("episode_start_ms", 0)),
                "end_ms": int(
                    segment.get("episode_end_ms", segment.get("episode_start_ms", 0))
                ),
                "text": str(segment.get("reconciled_text") or segment.get("text") or ""),
            }
            for alias, segment in zip(segment_aliases, segment_rows)
        ],
        "execution": agent_execution_settings("scripting"),
    }
    combined_result: dict[str, Any] = {
        "alignments": [],
        "character_introductions": [],
        "name_aliases": [],
        "notes": [],
    }
    execution_chunks: list[dict[str, Any]] = []
    coverage_targets = annotate_pause_capacity(
        project,
        narration_coverage_targets(project, regular_cues),
    )
    packages = scene_alignment_packages(project, payload, coverage_targets, episode_ids)
    segment_start_by_id = {
        str(segment["id"]): int(segment.get("episode_start_ms") or 0)
        for segment in segment_rows
    }
    for chunk_index, package in enumerate(packages, start=1):
        chunk_episode_ids = [package["episode_id"]]
        chunk_label = str(package["label"])
        if progress:
            progress(
                f"Szenenausrichtung: Paket {chunk_index}/{len(packages)} "
                f"({chunk_label}) wird bearbeitet",
                chunk_index - 1, len(packages),
            )
        chunk_set = set(chunk_episode_ids)
        package_cue_ids = set(package["cue_ids"])
        window_start, window_end = package["window"]
        if repair:
            # Bestehende Sperren stammen aus Laeufen mit folgenweiten Paketen;
            # ihr Anker kann ausserhalb des Abschnittsfensters liegen.  Ohne
            # sein Segment in der Nutzlast liesse sich die Sperre nicht mehr
            # aufloesen, also waechst das Fenster ueber sie hinaus.
            locked_anchors = [
                segment_start_by_id[anchor_id]
                for alias in package_cue_ids
                for locked in [(repair.get("locked") or {}).get(cue_aliases.get(alias, ""))]
                if isinstance(locked, dict)
                for anchor_id in [str(locked.get("anchor_segment_id") or "")]
                if anchor_id in segment_start_by_id
            ]
            if locked_anchors and window_start is not None:
                window_start = min(window_start, min(locked_anchors))
            if locked_anchors and window_end is not None:
                window_end = max(window_end, max(locked_anchors) + 1)
        chunk_payload = {
            **payload,
            "coverage_targets": {
                episode_id: {
                    **{
                        key: value
                        for key, value in coverage_targets[episode_id].items()
                        if key not in {"candidate_anchors", "coverage_candidate_ids"}
                    },
                    **package["coverage_overrides"],
                }
                for episode_id in chunk_episode_ids
                if episode_id in coverage_targets
            },
            # Nur die Pflicht-Einfuehrungen dieses Abschnitts: der Runner
            # leitet seine Pflichtliste genau aus diesem Feld ab.  Die
            # uebrigen Figuren bleiben als Beschreibung erhalten.
            "episode_contexts": [
                {
                    **item,
                    "character_introductions": [
                        introduction
                        for introduction in item.get("character_introductions", [])
                        if not introduction.get("introduction_required")
                        or "".join(
                            normalized_character_evidence(introduction.get("name")).split()
                        ) in package["required_keys"]
                    ],
                }
                for item in payload["episode_contexts"]
                if str(item.get("episode_id") or "") in chunk_set
            ],
            "cues": [
                item for item in payload["cues"]
                if str(item.get("id") or "") in package_cue_ids
            ],
            # Das Fenster grenzt an die Nachbarabschnitte: so kann kein Anker
            # hinter einen Anker des vorigen Pakets rutschen.
            "segments": [
                item for item in payload["segments"]
                if str(item.get("episode_id") or "") in chunk_set
                and (window_start is None or int(item.get("start_ms") or 0) >= window_start)
                and (window_end is None or int(item.get("start_ms") or 0) < window_end)
            ],
        }
        chunk_payload["cues"].sort(key=lambda item: int(item.get("order") or 0))
        if not chunk_payload["cues"] or not chunk_payload["segments"]:
            continue
        if repair:
            alias_by_cue_id = {raw_id: alias for alias, raw_id in cue_aliases.items()}
            alias_by_segment_id = {raw_id: alias for alias, raw_id in segment_aliases.items()}
            chunk_cue_ids = {str(item.get("id") or "") for item in chunk_payload["cues"]}
            repair_aliases = sorted(
                alias for alias in chunk_cue_ids if cue_aliases.get(alias) in repair.get("cue_ids", set())
            )
            locked_items = []
            for alias in sorted(chunk_cue_ids):
                raw_id = cue_aliases.get(alias, "")
                locked = (repair.get("locked") or {}).get(raw_id)
                if not locked or alias in repair_aliases:
                    continue
                item = json.loads(json.dumps(locked))
                item["cue_id"] = alias
                item["anchor_segment_id"] = alias_by_segment_id.get(str(item.get("anchor_segment_id") or ""), "")
                if item["anchor_segment_id"]:
                    locked_items.append(item)
            locked_intros = [
                {**intro, "cue_id": alias_by_cue_id.get(str(intro.get("cue_id") or ""), "")}
                for intro in (repair.get("locked_introductions") or [])
                if alias_by_cue_id.get(str(intro.get("cue_id") or "")) in chunk_cue_ids
                and alias_by_cue_id.get(str(intro.get("cue_id") or "")) not in repair_aliases
            ]
            if not repair_aliases:
                # Nichts zu reparieren: das Paket bleibt, wie es ist -- aber in
                # Zielaudio-Chronologie wie eine Agentenantwort: Der Aligner
                # unten verwirft jede Zuordnung, die zeitlich vor der vorigen
                # liegt, und eine Figuren-Einfuehrung sitzt in Cue-Reihenfolge
                # oft spaeter als ihr (frueherer) Erstauftritt-Anker.
                locked_items = chronological_alignments(locked_items, chunk_payload, chunk_episode_ids)
                for item in locked_items:
                    item["cue_id"] = cue_aliases.get(item["cue_id"], "")
                    item["anchor_segment_id"] = segment_aliases.get(str(item.get("anchor_segment_id") or ""), "")
                for intro in locked_intros:
                    intro["cue_id"] = cue_aliases.get(str(intro.get("cue_id") or ""), "")
                combined_result["alignments"].extend(locked_items)
                combined_result["character_introductions"].extend(locked_intros)
                execution_chunks.append({"chunk": chunk_index, "episode_ids": chunk_episode_ids, "repair": "unchanged"})
                continue
            chunk_payload["_repair_cue_ids"] = repair_aliases
            chunk_payload["_locked_alignments"] = locked_items
            chunk_payload["_locked_character_introductions"] = locked_intros
            chunk_payload["_validation_retry"] = str(repair.get("reason") or "")[:1500]
            # Teil des Checkpoint-Digests: Pakete aus Laeufen, in denen Sperren
            # noch verloren gehen konnten, werden nicht wiederverwendet.
            chunk_payload["_repair_lock_version"] = REPAIR_LOCK_VERSION
        # Das Paket landet als Datei neben den Artefakten: so laesst sich ein
        # Agentenaufruf, der ins Zeitlimit laeuft, ausserhalb der Pipeline
        # nachstellen, statt jedes Mal den ganzen Lauf zu wiederholen.
        try:
            debug_dir = ARTIFACTS / project["id"] / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"scene-alignment-{chunk_index:02d}.json").write_text(
                json.dumps({k: v for k, v in chunk_payload.items()}, ensure_ascii=False), "utf-8"
            )
        except OSError:
            pass
        try:
            def _digest(payload_for_digest: dict[str, Any]) -> str:
                return hashlib.sha256(
                    json.dumps(
                        {
                            "version": SCENE_ALIGNMENT_CHECKPOINT_VERSION,
                            "editorial_contract_version": NARRATION_EDITOR_VERSION,
                            "payload": payload_for_digest,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()

            # Runtime knobs such as the provider timeout do not change what a
            # valid alignment for this episode looks like.  Keeping them out of
            # the digest means a longer budget no longer discards finished work.
            checkpoint_digest = _digest(
                {key: value for key, value in chunk_payload.items() if key != "execution"}
            )
            legacy_checkpoint_digest = _digest(chunk_payload)
            project_id = str(project.get("id") or "")
            checkpoint_path = (
                ARTIFACTS
                / project_id
                / "scene-alignment-checkpoints"
                / f"{checkpoint_digest}.json"
                if project_id
                else None
            )
            response_payload = None
            if checkpoint_path and not checkpoint_path.exists():
                legacy_path = checkpoint_path.with_name(f"{legacy_checkpoint_digest}.json")
                if legacy_path.exists():
                    checkpoint_path = legacy_path
            if checkpoint_path and checkpoint_path.exists():
                try:
                    cached_payload = json.loads(checkpoint_path.read_text("utf-8"))
                    if (
                        int(cached_payload.get("version") or 0)
                        == SCENE_ALIGNMENT_CHECKPOINT_VERSION
                        and isinstance(cached_payload.get("result"), dict)
                    ):
                        response_payload = cached_payload
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    response_payload = None
            if response_payload is None:
                supports_full_cue_seed = density == "detailed"
                seed_result = (
                    raw_scene_alignment_seed(chunk_payload)
                    if supports_full_cue_seed
                    else None
                )
                if seed_result is None and checkpoint_path and supports_full_cue_seed:
                    expected_cue_ids = {
                        str(item.get("id") or "")
                        for item in chunk_payload["cues"]
                    }
                    seed_candidates = []
                    for candidate_path in checkpoint_path.parent.glob("*.json"):
                        if candidate_path == checkpoint_path:
                            continue
                        try:
                            candidate_payload = json.loads(candidate_path.read_text("utf-8"))
                            candidate_result = candidate_payload.get("result")
                            candidate_alignments = (
                                candidate_result.get("alignments")
                                if isinstance(candidate_result, dict)
                                else None
                            )
                            candidate_cue_ids = [
                                str(item.get("cue_id") or "")
                                for item in candidate_alignments
                                if isinstance(item, dict)
                            ] if isinstance(candidate_alignments, list) else []
                            if (
                                len(candidate_cue_ids) == len(expected_cue_ids)
                                and set(candidate_cue_ids) == expected_cue_ids
                            ):
                                seed_candidates.append(
                                    (candidate_path.stat().st_mtime_ns, candidate_result)
                                )
                        except (OSError, json.JSONDecodeError, TypeError, ValueError):
                            continue
                    if seed_candidates:
                        seed_result = max(seed_candidates, key=lambda item: item[0])[1]
                response = agent_post(
                    f"{base_url}/research/scene-alignment",
                    {
                        **chunk_payload,
                        **({"seed_result": seed_result} if seed_result else {}),
                    },
                    agent_request_timeout(payload["execution"]),
                )
                response.raise_for_status()
                response_payload = response.json()
                if checkpoint_path and isinstance(response_payload.get("result"), dict):
                    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                    checkpoint = {
                        "version": SCENE_ALIGNMENT_CHECKPOINT_VERSION,
                        "result": response_payload["result"],
                        "execution": response_payload.get("execution"),
                    }
                    temporary_checkpoint = checkpoint_path.with_name(
                        f".{checkpoint_path.name}.{uuid.uuid4().hex}.tmp"
                    )
                    temporary_checkpoint.write_text(
                        json.dumps(checkpoint, ensure_ascii=False),
                        "utf-8",
                    )
                    temporary_checkpoint.replace(checkpoint_path)
            result = json.loads(json.dumps(response_payload.get("result")))
            if not isinstance(result, dict):
                raise RuntimeError("Codex Runner lieferte kein Szenenausrichtungs-Objekt")
            if repair:
                restored_locks = enforce_locked_alignments(result, chunk_payload, chunk_episode_ids)
                if restored_locks:
                    logging.warning(
                        "Reparaturpaket %s: %s gesperrte Cues/Einfuehrungen wortgleich wiederhergestellt",
                        chunk_index,
                        restored_locks,
                    )
            execution = response_payload.get("execution")
            if isinstance(execution, dict):
                execution_chunks.append({
                    **json.loads(json.dumps(execution)),
                    "chunk": chunk_index,
                    "episode_ids": chunk_episode_ids,
                })
            for item in result.get("alignments", []):
                item["cue_id"] = cue_aliases.get(str(item.get("cue_id") or ""), "")
                anchor_alias = str(item.get("anchor_segment_id") or "")
                item["anchor_segment_id"] = segment_aliases.get(anchor_alias, "") if anchor_alias else ""
            for item in result.get("character_introductions", []):
                item["cue_id"] = cue_aliases.get(str(item.get("cue_id") or ""), "")
            for key in ("alignments", "character_introductions", "name_aliases", "notes"):
                values = result.get(key)
                if isinstance(values, list):
                    combined_result[key].extend(values)
            if progress:
                progress(
                    f"Szenenausrichtung: Paket {chunk_index}/{len(packages)} akzeptiert",
                    chunk_index, len(packages),
                )
        except httpx.HTTPStatusError as error:
            try:
                detail = error.response.json().get("detail")
            except Exception:
                detail = None
            raise RuntimeError(
                f"Szenenausrichtungs-Paket {chunk_index} ({chunk_label}) wurde abgelehnt: "
                f"{detail or 'unbekannter Providerfehler'}"
            ) from error
        except httpx.RequestError as error:
            raise RuntimeError("Codex Runner ist während der Szenenausrichtung nicht erreichbar") from error
    if execution_chunks:
        providers = {str(item.get("provider") or "") for item in execution_chunks}
        execution = json.loads(json.dumps(execution_chunks[-1]))
        execution["provider"] = next(iter(providers)) if len(providers) == 1 else "mixed"
        execution["chunk_count"] = len(execution_chunks)
        execution["chunks"] = execution_chunks
        execution["attempts"] = [
            {
                **attempt,
                "chunk": chunk["chunk"],
                "episode_ids": chunk["episode_ids"],
            }
            for chunk in execution_chunks
            for attempt in chunk.get("attempts", [])
            if isinstance(attempt, dict)
        ]
        execution["fallback_used"] = any(bool(item.get("fallback_used")) for item in execution_chunks)
        combined_result["execution"] = execution
    return combined_result


def update_run(run_id: str, **changes: Any) -> None:
    with LOCK:
        run = STATE["runs"][run_id]
        run.update(changes)
        run["updated_at"] = now()
        save_state()


def strip_stage_direction_markers(value: Any) -> str:
    """Remove SDH/audio-bed labels while preserving any actual spoken text."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\{\\[^}]+\}|<[^>]+>", " ", text)
    text = re.sub(r"\[[^\]]{1,80}\]", " ", text)
    text = re.sub(
        r"\([^)]{0,60}(?:musik|music|applaus|gelächter|lachen|atmo|geräusch|"
        r"sound|jubel|stille|schritte|motor|krachen)[^)]*\)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip()


def transcript_item_has_spoken_content(item: dict[str, Any]) -> bool:
    text = str(item.get("reconciled_text") or item.get("text") or "")
    # Timestamp-only rows are conservatively treated as speech. Only explicit
    # SDH stage directions may open a bed for narration.
    if not text.strip():
        return True
    return bool(re.search(r"[\wÄÖÜäöüß]", strip_stage_direction_markers(text)))


def alignment_tokens(text: str) -> set[str]:
    text = strip_stage_direction_markers(text)
    return {token.casefold() for token in re.findall(r"\w+", text, flags=re.UNICODE) if len(token) >= 3}


NARRATION_ALIGNMENT_STOPWORDS = {
    "aber", "alle", "als", "also", "auch", "auf", "aus", "bei", "bin", "bis", "das", "dass",
    "dem", "den", "der", "des", "die", "doch", "ein", "eine", "einem", "einen", "einer", "er",
    "es", "für", "ganz", "hat", "haben", "hier", "ich", "ihm", "ihn", "ihr", "ihre",
    "im", "in", "ist", "ja", "jetzt", "mit", "nach", "nicht", "noch", "nur", "oder", "schon",
    "sehr", "sie", "sich", "sind", "son", "und", "vom", "von", "vor", "war", "was", "wie",
    "wieder", "wir", "wird", "wo", "zu", "zum", "zur",
}
EDITORIAL_AUDIO_STOPWORDS = NARRATION_ALIGNMENT_STOPWORDS | {
    "beim", "bereits", "dabei", "dann", "etwas", "gegen", "hatte", "immer", "kein",
    "keine", "mehr", "seine", "seiner", "seinem", "seinen", "während", "wurde",
    "dieser", "diese", "dieses", "durch", "konnte", "könnte", "sollte", "würde",
    "ihren", "ihrem", "ihres", "ihnen",
}


def narration_alignment_tokens(text: str) -> set[str]:
    return alignment_tokens(text) - NARRATION_ALIGNMENT_STOPWORDS


def editorial_audio_token_stems(value: Any) -> set[str]:
    raw_text = unicodedata.normalize("NFKC", strip_stage_direction_markers(value))
    characters = list(raw_text)
    for index, character in enumerate(characters):
        if (
            character == "I"
            and index > 0
            and index + 1 < len(characters)
            and characters[index - 1].isalpha()
            and characters[index + 1].isalpha()
        ):
            characters[index] = "l"
    text = "".join(characters).casefold()
    text = text.translate(
        str.maketrans({"’": "'", "‘": "'", "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-"})
    )
    text = re.sub(r"»[^»]*«|„[^“]*“|\"[^\"]*\"", " ", text)
    stems: set[str] = set()
    for token in re.findall(r"[a-zäöüß][a-zäöüß'-]+", text):
        token = token.strip("'-")
        if len(token) < 4 or token in EDITORIAL_AUDIO_STOPWORDS:
            continue
        stem = token
        for suffix in ("ern", "est", "em", "en", "er", "es", "e", "n", "s"):
            if stem.endswith(suffix) and len(stem) - len(suffix) >= 5:
                stem = stem[:-len(suffix)]
                break
        stems.add(stem)
    return stems


def fuzzy_editorial_audio_token_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < 5:
        return False
    left_variants = {left, left[2:] if left.startswith("ge") and len(left) > 7 else left}
    right_variants = {right, right[2:] if right.startswith("ge") and len(right) > 7 else right}
    return any(
        (
            min(len(left_variant), len(right_variant))
            / max(len(left_variant), len(right_variant))
            >= 0.7
            and (
                left_variant in right_variant
                or right_variant in left_variant
                or difflib.SequenceMatcher(None, left_variant, right_variant).ratio() >= 0.8
            )
        )
        for left_variant in left_variants
        for right_variant in right_variants
    )


def matched_editorial_audio_tokens(
    expected: set[str],
    actual: set[str],
) -> list[str]:
    remaining = set(actual)
    matched = []
    for expected_token in sorted(expected):
        actual_token = next(
            (
                candidate
                for candidate in sorted(remaining)
                if (expected_token == candidate or min(len(expected_token), len(candidate)) >= 6)
                and fuzzy_editorial_audio_token_match(expected_token, candidate)
            ),
            None,
        )
        if actual_token is not None:
            matched.append(expected_token)
            remaining.remove(actual_token)
    return matched


def narration_audio_sentences(value: Any) -> list[str]:
    return [
        match.group(0).strip()
        for match in re.finditer(
            r"[^.!?]+(?:[.!?]+[»”\"]?|$)",
            str(value or ""),
        )
        if match.group(0).strip()
    ]


def native_audio_repetition_violations(
    project: dict[str, Any],
    cues: list[dict[str, Any]] | None = None,
    window_ms: int = 45_000,
) -> list[dict[str, Any]]:
    fragments = {
        str(fragment.get("id") or ""): str(fragment.get("text") or "")
        for chapter in project.get("chapters", [])
        for fragment in chapter.get("fragments", [])
    }
    segments = {
        str(item.get("id") or ""): item
        for item in project.get("reconciled_transcript", [])
        if transcript_item_outside_intros(project, item)
    }
    segments_by_episode: dict[str, list[dict[str, Any]]] = {}
    for segment in segments.values():
        segments_by_episode.setdefault(str(segment.get("episode_id") or ""), []).append(segment)
    ignored_names = [
        str(character.get("name") or "")
        for context in (project.get("episode_context_research") or {}).get("episode_contexts", [])
        for character in context.get("character_introductions", [])
        if character.get("name")
    ]
    ignored_names.extend(
        str(item.get("canonical") or "")
        for item in project.get("narration_name_lexicon", [])
        if item.get("canonical")
    )
    ignored_tokens = editorial_audio_token_stems(" ".join(ignored_names))
    for name in ignored_names:
        ignored_tokens.update(editorial_audio_token_stems(re.sub(r"[\s-]+", "", name)))
    ignored_tokens.update({
        "antwort", "erklär", "fragte", "fragt", "meint", "sagte", "sagt",
        "sprach", "wollt", "wissen",
    })
    violations = []
    for cue in cues if cues is not None else project.get("cues", []):
        anchor = segments.get(str(cue.get("anchor_segment_id") or ""))
        if not anchor:
            continue
        source_tokens = editorial_audio_token_stems(
            str(cue.get("editorial_source_context") or "") or "\n".join(
                fragments.get(str(fragment_id), "")
                for fragment_id in cue.get("source_fragment_ids", [])
            )
        ) - ignored_tokens
        narration_token_sets = [
            editorial_audio_token_stems(sentence) - ignored_tokens
            for sentence in narration_audio_sentences(cue.get("text"))
        ]
        narration_token_sets = [tokens for tokens in narration_token_sets if tokens]
        if not source_tokens or not narration_token_sets:
            continue
        episode_id = str(anchor.get("episode_id") or "")
        anchor_ms = int(anchor.get("episode_start_ms") or 0)
        for segment in segments_by_episode.get(episode_id, []):
            if segment.get("visual_scene_anchor") or abs(int(segment.get("episode_start_ms") or 0) - anchor_ms) > window_ms:
                continue
            audible_tokens = editorial_audio_token_stems(
                segment.get("reconciled_text") or segment.get("text")
            ) - ignored_tokens
            grounded_audible_tokens = {
                token
                for token in audible_tokens
                if any(
                    (token == source_token or min(len(token), len(source_token)) >= 6)
                    and fuzzy_editorial_audio_token_match(token, source_token)
                    for source_token in source_tokens
                )
            }
            if len(grounded_audible_tokens) < 2:
                continue
            sentence_matches = [
                matched_editorial_audio_tokens(
                    grounded_audible_tokens,
                    narration_tokens,
                )
                for narration_tokens in narration_token_sets
            ]
            matched = max(
                sentence_matches,
                key=len,
            )
            coverage = len(matched) / len(grounded_audible_tokens)
            if len(matched) >= 3 or (len(matched) >= 2 and coverage >= 0.5):
                violations.append({
                    "cue_id": str(cue.get("id") or ""),
                    "segment_id": str(segment.get("id") or ""),
                    "text": str(segment.get("reconciled_text") or segment.get("text") or "")[:180],
                    "matched_tokens": matched[:8],
                    "coverage": round(coverage, 3),
                })
                break
    return violations


def select_aligned_anchor(chapter: dict[str, Any], transcript: list[dict[str, Any]]) -> tuple[dict[str, Any], float]:
    candidates = [item for item in transcript if item.get("chapter_id") == chapter["id"]]
    if not candidates:
        raise RuntimeError(f"Für {chapter['title']} wurde kein Transkript erzeugt")
    source_tokens = alignment_tokens(" ".join(fragment.get("text", "") for fragment in chapter.get("fragments", [])))
    ranked = []
    for item in candidates:
        segment_tokens = alignment_tokens(item.get("reconciled_text") or item.get("text", ""))
        overlap = len(source_tokens & segment_tokens) / max(len(segment_tokens), 1)
        ranked.append((overlap, -int(item.get("start_sample", 0)), item))
    overlap, _, anchor = max(ranked, key=lambda item: (item[0], item[1]))
    return anchor, overlap


NARRATION_INTERVAL_MS = {
    "compact": 12 * 60 * 1000,
    "balanced": 8 * 60 * 1000,
    "detailed": 60 * 1000,
    # Give the agent enough scene candidates to choose professional beats;
    # unlike detailed mode it will keep only 4–12 per normal episode.
    "audio_drama": 60 * 1000,
}
BOOK_BOUNDARY_CONTEXT_MS = 30_000
BOOK_BOUNDARY_MIN_PREFIX_GAP_MS = 180_000
BOOK_BOUNDARY_MIN_SUFFIX_GAP_MS = 90_000


def unique_episode_ids(mapping: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in mapping:
        episode_id = str(item.get("episode_id") or "")
        if episode_id and episode_id not in seen:
            seen.add(episode_id)
            result.append(episode_id)
    return result


def fragment_narrative_sentences(fragment: dict[str, Any]) -> list[str]:
    text = re.sub(r"\s+", " ", str(fragment.get("text") or "")).strip()
    if not text or re.fullmatch(r"#*\s*(?:Kapitel|Chapter)\s+[\divxlc]+.*", text, flags=re.IGNORECASE):
        return []
    if len(text.split()) <= 4 and not re.search(r"[.!?]$", text):
        return []
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if sentence.strip() and not any(mark in sentence for mark in ("»", "«", "“", "”", "\""))
    ]


def source_content_fragments(project: dict[str, Any]) -> list[dict[str, Any]]:
    fragments = []
    for chapter in project.get("chapters", []):
        for fragment in chapter.get("fragments", []):
            text = re.sub(r"\s+", " ", str(fragment.get("text") or "")).strip()
            if not text:
                continue
            if re.fullmatch(r"#*\s*(?:Kapitel|Chapter)\s+[\divxlc]+.*", text, flags=re.IGNORECASE):
                continue
            if len(text.split()) <= 8 and not re.search(r"[.!?]$", text):
                continue
            # Only fragments the cue builder can narrate count as book edges.
            # generate_narration_cues() skips groups without a narrative
            # sentence (dialogue lines are excluded by
            # fragment_narrative_sentences), so a book that closes on a spoken
            # line would otherwise fail the boundary gate on every run.
            if not fragment_narrative_sentences(fragment):
                continue
            fragments.append(fragment)
    return fragments


def book_boundary_plan(
    project: dict[str, Any],
    episode_durations_ms: dict[str, int] | None = None,
) -> dict[str, Any]:
    content_fragments = source_content_fragments(project)
    episode_ids = unique_episode_ids(project.get("mapping", []))
    if not content_fragments or not episode_ids or not project.get("cues"):
        return {
            "status": "not_applicable",
            "first_source_covered": True,
            "last_source_covered": True,
            "exclusions": {},
            "detail": "Keine auswertbaren Buchgrenzen vorhanden",
        }

    durations = dict(episode_durations_ms or {})
    for asset in project.get("media_assets", []):
        episode_id = str(asset.get("episode_id") or "")
        if episode_id and episode_id not in durations:
            durations[episode_id] = int(asset.get("duration_ms") or 0)
    boundary_cues = project.get("semantic_cues") or project.get("cues", [])
    covered_ids = {
        str(fragment_id)
        for cue in boundary_cues
        for fragment_id in cue.get("source_fragment_ids", [])
    }
    first_fragment_id = str(content_fragments[0].get("id") or "")
    last_fragment_id = str(content_fragments[-1].get("id") or "")
    first_covered = first_fragment_id in covered_ids
    last_covered = last_fragment_id in covered_ids
    first_episode_id = episode_ids[0]
    last_episode_id = episode_ids[-1]
    first_candidates = [
        cue for cue in boundary_cues
        if first_fragment_id in {str(value) for value in cue.get("source_fragment_ids", [])}
        and str(cue.get("anchor_episode_id") or "") == first_episode_id
    ]
    last_candidates = [
        cue for cue in boundary_cues
        if last_fragment_id in {str(value) for value in cue.get("source_fragment_ids", [])}
        and str(cue.get("anchor_episode_id") or "") == last_episode_id
    ]
    first_cue = min(first_candidates, key=lambda cue: int(cue.get("anchor_episode_start_ms", 0)), default=None)
    last_cue = max(last_candidates, key=lambda cue: int(cue.get("anchor_episode_start_ms", 0)), default=None)
    segments = {
        str(item.get("id") or ""): item
        for item in project.get("reconciled_transcript") or project.get("transcript", [])
    }
    exclusions: dict[str, list[dict[str, Any]]] = {}
    first_anchor_ms = int(first_cue.get("anchor_episode_start_ms", 0)) if first_cue else None
    last_anchor_end_ms = None

    if first_cue and first_covered:
        intro_end_ms = max(
            (end for _, end in intro_intervals_by_episode(project).get(first_episode_id, [])),
            default=0,
        )
        if first_anchor_ms is not None and first_anchor_ms - intro_end_ms >= BOOK_BOUNDARY_MIN_PREFIX_GAP_MS:
            boundary_end_ms = max(intro_end_ms, first_anchor_ms - BOOK_BOUNDARY_CONTEXT_MS)
            if boundary_end_ms > intro_end_ms:
                exclusions.setdefault(first_episode_id, []).append({
                    "start_ms": intro_end_ms,
                    "end_ms": boundary_end_ms,
                    "reason": "book_boundary_prefix",
                    "confidence": float(first_cue.get("confidence") or 0),
                })

    if last_cue and last_covered:
        anchor = segments.get(str(last_cue.get("anchor_segment_id") or ""), {})
        last_anchor_end_ms = int(
            anchor.get(
                "episode_end_ms",
                last_cue.get("anchor_episode_start_ms", 0),
            )
        )
        episode_duration_ms = int(durations.get(last_episode_id) or 0)
        if episode_duration_ms - last_anchor_end_ms >= BOOK_BOUNDARY_MIN_SUFFIX_GAP_MS:
            boundary_start_ms = min(
                episode_duration_ms,
                last_anchor_end_ms + BOOK_BOUNDARY_CONTEXT_MS,
            )
            if episode_duration_ms > boundary_start_ms:
                exclusions.setdefault(last_episode_id, []).append({
                    "start_ms": boundary_start_ms,
                    "end_ms": episode_duration_ms,
                    "reason": "book_boundary_suffix",
                    "confidence": float(last_cue.get("confidence") or 0),
                })

    status = "passed" if first_covered and last_covered and first_cue and last_cue else "review"
    missing = []
    if not first_covered or not first_cue:
        missing.append("Anfang")
    if not last_covered or not last_cue:
        missing.append("Ende")
    excluded_ms = sum(
        int(item["end_ms"]) - int(item["start_ms"])
        for items in exclusions.values()
        for item in items
    )
    excluded_seconds = excluded_ms // 1000
    excluded_label = f"{excluded_seconds // 60}:{excluded_seconds % 60:02d} min"
    return {
        "status": status,
        "first_source_covered": bool(first_covered and first_cue),
        "last_source_covered": bool(last_covered and last_cue),
        "first_episode_id": first_episode_id,
        "last_episode_id": last_episode_id,
        "first_anchor_ms": first_anchor_ms,
        "last_anchor_end_ms": last_anchor_end_ms,
        "excluded_duration_ms": excluded_ms,
        "exclusions": exclusions,
        "detail": (
            f"Erste und letzte Buchpassage belegt; {excluded_label} "
            "serienfremder Randinhalt ausgeschlossen"
            if status == "passed"
            else f"Buchrand nicht vollständig belegt: {', '.join(missing)}"
        ),
    }


def narration_requires_fresh_scene_alignment(project: dict[str, Any]) -> bool:
    if int(project.get("narration_editor_version") or 0) < NARRATION_EDITOR_VERSION:
        return True
    editorial_checks = [
        check
        for check in (project.get("quality_report") or {}).get("checks", [])
        if check.get("id") in {
            "narration_editorial_quality",
            "character_introduction_timing",
        }
    ]
    if any(check.get("status") != "passed" for check in editorial_checks):
        return True
    plan = book_boundary_plan(project)
    if (
        plan.get("status") == "review"
        and (
            not plan.get("first_source_covered")
            or not plan.get("last_source_covered")
        )
    ):
        return True
    content_fragments = source_content_fragments(project)
    if not content_fragments:
        return False
    boundary_cues = project.get("semantic_cues") or project.get("cues", [])
    minimum_confidence = float(narration_rules(project)["dense_action_min_confidence"])
    for fragment in (content_fragments[0], content_fragments[-1]):
        fragment_id = str(fragment.get("id") or "")
        confidences = [
            float(cue.get("confidence") or 0)
            for cue in boundary_cues
            if fragment_id in {str(value) for value in cue.get("source_fragment_ids", [])}
        ]
        if not confidences or max(confidences) < minimum_confidence:
            return True
    return False


def normalize_narration_name_lexicon(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    seen_aliases: set[str] = set()
    for entry in entries[:100]:
        canonical = re.sub(r"\s+", " ", str(entry.get("canonical") or "")).strip()[:120]
        aliases = []
        for value in entry.get("aliases", [])[:12]:
            alias = re.sub(r"\s+", " ", str(value or "")).strip()[:120]
            key = alias.casefold()
            # An alias that contains the canonical name ("Tao Baibai Baibai")
            # is an artefact of the old re-application bug, not a real variant.
            # Match complete words: Violet/Violett is a legitimate spelling pair.
            contains_canonical_name = bool(re.search(
                rf"(?<!\w){re.escape(canonical.casefold())}(?!\w)", key,
            ))
            if alias and key != canonical.casefold() and not contains_canonical_name and key not in seen_aliases:
                aliases.append(alias)
                seen_aliases.add(key)
        if canonical and aliases:
            normalized.append({
                "canonical": canonical,
                "aliases": aliases,
                "evidence": str(entry.get("evidence") or "Automatisch aus Textquelle und Ziel-Untertiteln abgeglichen")[:500],
                "confidence": round(float(entry.get("confidence", 1.0)), 3),
            })
    return normalized


def merge_narration_name_lexicon(existing: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_canonical = {str(item.get("canonical") or "").casefold(): dict(item) for item in normalize_narration_name_lexicon(existing)}
    for item in normalize_narration_name_lexicon(discovered):
        key = item["canonical"].casefold()
        previous = by_canonical.get(key)
        if previous:
            item["aliases"] = list(dict.fromkeys([*previous.get("aliases", []), *item["aliases"]]))
            item["confidence"] = max(float(previous.get("confidence", 0)), float(item["confidence"]))
        by_canonical[key] = item
    return list(by_canonical.values())


def collapse_repeated_name_tokens(text: str, canonical_names: list[str]) -> str:
    """Fold "Son Son Son Goku" or "Tao Baibai Baibai" back to the canonical name.

    Earlier versions re-applied aliases that are part of their own canonical
    name ("Goku" -> "Son Goku") on every pass, multiplying the prefix or suffix
    once per pipeline stage. Any run of a canonical name's words with a word
    repeated collapses to the canonical spelling.
    """
    for canonical in canonical_names:
        words = [word for word in re.split(r"\s+", canonical.strip()) if word]
        if not words:
            continue
        # The last word may carry a German genitive "s" ("Son Son Gokus").
        pattern = (
            r"(?<!\w)"
            + r"\s+".join(rf"(?:{re.escape(word)}\s+)*{re.escape(word)}" for word in words[:-1])
            + (r"\s+" if len(words) > 1 else "")
            + rf"(?:{re.escape(words[-1])}\s+)*{re.escape(words[-1])}(?P<suffix>s)?(?!\w)"
        )

        def fold(match: re.Match[str], canonical: str = canonical) -> str:
            suffix = match.group("suffix") or ""
            body = match.group(0)[: len(match.group(0)) - len(suffix)]
            if not re.search(r"\s", body) or body.casefold() == canonical.casefold():
                return match.group(0)
            return canonical + suffix

        text = re.sub(pattern, fold, text, flags=re.IGNORECASE)
    return text


def canonicalize_narration_names(text: str, lexicon: list[dict[str, Any]] | None = None) -> str:
    entries = normalize_narration_name_lexicon(lexicon or [])
    replacements = [
        (alias, str(entry.get("canonical") or ""))
        for entry in entries
        for alias in entry.get("aliases", [])
    ]
    canonical_names = [str(entry.get("canonical") or "") for entry in entries]
    # Fold artefacts from earlier passes first, otherwise every surplus
    # "Chun" in "Jackie Chun Chun Chun" would be expanded again.
    text = collapse_repeated_name_tokens(text, canonical_names)
    for alias, replacement in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        suffix = r"(?P<suffix>s)?" if alias[-1:].isalpha() and not alias.casefold().endswith("s") else ""
        # Occurrences of the canonical name are left alone: "Goku" inside
        # "Son Goku" must not become "Son Son Goku" on the next pass.
        protected = [
            match.span()
            for match in re.finditer(rf"(?<!\w){re.escape(replacement)}s?(?!\w)", text, flags=re.IGNORECASE)
        ]

        def substitute(match: re.Match[str]) -> str:
            start, end = match.span()
            if any(begin <= start and end <= finish for begin, finish in protected):
                return match.group(0)
            return replacement + (match.groupdict().get("suffix") or "")

        text = re.sub(
            rf"(?<!\w){re.escape(alias)}{suffix}(?!\w)",
            substitute,
            text,
            flags=re.IGNORECASE,
        )
    return collapse_repeated_name_tokens(text, canonical_names)


def spoken_name_matches_research_character(
    research_name: Any,
    spoken_name: Any,
    lexicon: list[dict[str, Any]] | None = None,
) -> bool:
    def normalise(value: str) -> str:
        # The dub writes "Muten Roshi", research writes "Muten-Roshi".  A hyphen
        # is not a different character.
        return re.sub(r"\s+", " ", value.replace("-", " ")).strip().casefold()

    spoken = normalise(canonicalize_narration_names(str(spoken_name or ""), lexicon))
    if not spoken:
        return False
    variants = [
        value.strip(" -")
        for value in re.split(r"[()/]", str(research_name or ""))
        if value.strip(" -")
    ]
    return any(
        normalise(canonicalize_narration_names(variant, lexicon)) == spoken
        for variant in variants
    )


def apply_narration_name_lexicon(project: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    project["narration_name_lexicon"] = normalize_narration_name_lexicon(entries)
    for collection_name in ("cues", "semantic_cues"):
        for cue in project.get(collection_name, []):
            cue["text"] = canonicalize_narration_names(
                str(cue.get("text") or ""),
                project["narration_name_lexicon"],
            )


def narration_from_fragments(
    fragments: list[dict[str, Any]],
    title: str,
    *,
    max_sentences: int = 2,
    max_characters: int = 360,
) -> str:
    narrative = [sentence for fragment in fragments for sentence in fragment_narrative_sentences(fragment)]
    selected = narrative[:max(1, max_sentences)]
    result = " ".join(selected).strip()
    if len(result) > max_characters:
        result = result[:max(1, max_characters - 3)].rsplit(" ", 1)[0] + "…"
    return result or f"Die Handlung von {title} geht weiter."


def opening_action_from_fragments(
    fragments: list[dict[str, Any]],
    title: str,
    *,
    max_sentences: int = 2,
    max_characters: int = 360,
) -> str:
    return narration_from_fragments(
        fragments,
        title,
        max_sentences=max_sentences,
        max_characters=max_characters,
    ).replace(f"Die Handlung von {title} geht weiter.", f"Hier beginnt {title}.")


def ending_action_from_fragments(fragments: list[dict[str, Any]], title: str) -> str:
    narrative = [
        sentence
        for fragment in fragments
        for sentence in fragment_narrative_sentences(fragment)
    ]
    if narrative:
        return " ".join(narrative[-2:])
    return f"Hier endet {title}."


def opening_narration_block(segments: list[dict[str, Any]]) -> tuple[int, int, list[dict[str, Any]]] | None:
    eligible = sorted(
        [segment for segment in segments if 30_000 <= int(segment.get("episode_start_ms", 0)) <= 180_000],
        key=lambda segment: int(segment.get("episode_start_ms", 0)),
    )
    if not eligible:
        return None
    cluster = [eligible[0]]
    for segment in eligible[1:]:
        gap = int(segment.get("episode_start_ms", 0)) - int(cluster[-1].get("episode_end_ms", 0))
        if gap > 6_000:
            break
        cluster.append(segment)
    start_ms = int(cluster[0].get("episode_start_ms", 0))
    end_ms = int(cluster[-1].get("episode_end_ms", start_ms))
    word_count = sum(len(str(segment.get("reconciled_text") or segment.get("text") or "").split()) for segment in cluster)
    if len(cluster) < 2 or end_ms - start_ms < 8_000 or word_count < 10:
        return None
    return start_ms, end_ms, cluster


def _split_evenly(items: list[Any], count: int) -> list[list[Any]]:
    if not items:
        return []
    count = max(1, min(count, len(items)))
    return [items[round(index * len(items) / count):round((index + 1) * len(items) / count)] for index in range(count)]


def speech_free_gaps(
    segments: list[dict[str, Any]],
    words: list[dict[str, Any]],
    duration_ms: int,
    *,
    margin_ms: int = 300,
    min_gap_ms: int = 900,
) -> list[tuple[int, int]]:
    intervals = []
    for item in [*segments, *words]:
        if not transcript_item_has_spoken_content(item):
            continue
        start = int(item.get("episode_start_ms", item.get("start_ms", 0)))
        end = int(item.get("episode_end_ms", item.get("end_ms", start)))
        if end <= start:
            continue
        intervals.append((max(0, start - margin_ms), min(duration_ms, end + margin_ms)))
    if not intervals:
        return [(0, duration_ms)] if duration_ms >= min_gap_ms else []
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    gaps = []
    cursor = 0
    for start, end in merged:
        if start - cursor >= min_gap_ms:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if duration_ms - cursor >= min_gap_ms:
        gaps.append((cursor, duration_ms))
    return gaps


def select_opening_narration_gap(
    gaps: list[tuple[int, int]],
    narration_duration_ms: int,
    search_end_ms: int = 180_000,
) -> tuple[int, int, int, int] | None:
    required_ms = narration_duration_ms + 1_150
    candidates = [
        (index, gap)
        for index, gap in enumerate(gaps)
        if gap[0] < search_end_ms and min(gap[1], search_end_ms) - gap[0] >= required_ms
    ]
    if not candidates:
        return None
    gap_index, (gap_start, gap_end) = min(
        candidates,
        key=lambda item: (item[1][0], -(min(item[1][1], search_end_ms) - item[1][0])),
    )
    usable_end = min(gap_end, search_end_ms)
    insert_ms = gap_start + (usable_end - gap_start) // 2
    return insert_ms, gap_start, usable_end, gap_index


def select_safe_narration_gap(
    gaps: list[tuple[int, int]],
    used_gaps: set[int],
    *,
    anchor_end_ms: int,
    target_ms: int,
    used_insertions: list[int] | None = None,
    min_spacing_ms: int = 30_000,
    min_duration_ms: int = 0,
    placement_side: Literal["before", "after"] = "after",
    max_distance_ms: int | None = None,
) -> tuple[int, int, int, int] | None:
    available = [
        (index, gap)
        for index, gap in enumerate(gaps)
        if index not in used_gaps and gap[1] - gap[0] >= min_duration_ms
    ]
    if not available:
        return None
    if used_insertions:
        spaced = [
            (index, gap)
            for index, gap in available
            if all(abs(round((gap[0] + gap[1]) / 2) - existing) >= min_spacing_ms for existing in used_insertions)
        ]
        if not spaced:
            return None
        available = spaced
    if placement_side == "before":
        positioned = [
            (index, gap)
            for index, gap in available
            if gap[1] <= anchor_end_ms
            and (max_distance_ms is None or anchor_end_ms - gap[1] <= max_distance_ms)
        ]
    else:
        positioned = [
            (index, gap)
            for index, gap in available
            if gap[0] >= anchor_end_ms
            and (max_distance_ms is None or gap[0] - anchor_end_ms <= max_distance_ms)
        ]
    if not positioned:
        return None
    candidates = positioned

    def rank(item: tuple[int, tuple[int, int]]) -> tuple[float, float]:
        _, (start, end) = item
        midpoint = round((start + end) / 2)
        anchor_distance = abs(midpoint - anchor_end_ms)
        target_distance = abs(midpoint - target_ms)
        return target_distance * 0.65 + anchor_distance * 0.35, -min(end - start, 10_000)

    gap_index, (gap_start, gap_end) = min(candidates, key=rank)
    used_gaps.add(gap_index)
    insertion = round((gap_start + gap_end) / 2)
    if used_insertions is not None:
        used_insertions.append(insertion)
    return insertion, gap_start, gap_end, gap_index


ALIGNED_SCENE_INSERT_POLICY = "insert_at_aligned_scene_boundary"


def aligned_scene_boundary_ms(cue: dict[str, Any], anchor: dict[str, Any]) -> int:
    if cue.get("alignment_placement") == "before_anchor":
        return int(anchor.get("episode_start_ms", cue.get("anchor_episode_start_ms", 0)))
    return int(
        anchor.get(
            "episode_end_ms",
            anchor.get("episode_start_ms", cue.get("anchor_episode_start_ms", 0)),
        )
    )


def apply_aligned_scene_insert(
    cue: dict[str, Any],
    anchor: dict[str, Any],
) -> None:
    insert_ms = aligned_scene_boundary_ms(cue, anchor)
    cue.update(
        {
            "anchor_insert_ms": insert_ms,
            "aligned_boundary_ms": insert_ms,
            "placement_policy": ALIGNED_SCENE_INSERT_POLICY,
        }
    )
    cue.pop("safe_gap_start_ms", None)
    cue.pop("safe_gap_end_ms", None)
    cue.pop("overlay_alignment", None)


def before_anchor_gap_distance_ms(
    cue: dict[str, Any],
    rules: dict[str, Any],
    placement_side: Literal["before", "after"],
) -> int:
    if placement_side == "before":
        return int(
            rules["character_introduction_max_lead_ms"]
            if cue.get("narrative_purpose") == "character_introduction"
            else rules["narration_max_lead_ms"]
        )
    return int(rules["max_anchor_distance_ms"])


def mark_before_anchor_overlay_alignment(cue: dict[str, Any]) -> None:
    if (
        cue.get("alignment_placement") == "before_anchor"
        and cue.get("narrative_purpose") == "character_introduction"
    ):
        cue["overlay_alignment"] = "end"
    else:
        cue.pop("overlay_alignment", None)


def closest_overlay_start_sample(
    cue: dict[str, Any],
    gap_start_sample: int,
    gap_end_sample: int,
    duration_samples: int,
) -> int:
    """Keep narration on the requested side and as close to its scene as possible."""
    latest_start = gap_end_sample - duration_samples
    placement = cue.get("alignment_placement")
    if placement == "before_anchor":
        desired_start = latest_start
    elif placement == "after_anchor":
        desired_start = gap_start_sample
    elif cue.get("overlay_alignment") == "end":
        desired_start = latest_start
    else:
        desired_start = (
            round(int(cue.get("anchor_insert_ms", 0)) * SAMPLE_RATE / 1000)
            - duration_samples // 2
        )
    return max(gap_start_sample, min(desired_start, latest_start))


def narration_cadence_ms(cue: dict[str, Any]) -> tuple[int, int]:
    """Return an oral-editing pause that fits the dramaturgical beat."""
    return AUDIO_DRAMA_CADENCE_MS.get(
        str(cue.get("beat_type") or ""),
        (400, 750),
    )


def enforce_character_introduction_sync(
    cue: dict[str, Any],
    anchor: dict[str, Any],
    rules: dict[str, Any],
) -> bool:
    """Keep every overlay tight to its scene; otherwise cut it in exactly at the boundary.

    A comment that arrives seconds early or late describes something the listener
    is no longer hearing, so an imprecise music bed loses against an exact insert.
    """
    if cue.get("placement_policy") != "overlay_speech_free":
        return False
    boundary_ms = aligned_scene_boundary_ms(cue, anchor)
    if cue.get("alignment_placement") == "before_anchor":
        gap_end_ms = int(cue.get("safe_gap_end_ms", 0))
        maximum_ms = (
            int(rules["character_introduction_max_lead_ms"])
            if cue.get("narrative_purpose") == "character_introduction"
            else int(rules["narration_max_lead_ms"])
        )
        if gap_end_ms > boundary_ms or boundary_ms - gap_end_ms > maximum_ms:
            apply_aligned_scene_insert(cue, anchor)
            return True
        mark_before_anchor_overlay_alignment(cue)
        return False
    if cue.get("alignment_placement") == "after_anchor":
        gap_start_ms = int(cue.get("safe_gap_start_ms", 0))
        if (
            gap_start_ms < boundary_ms
            or gap_start_ms - boundary_ms > int(rules["max_anchor_distance_ms"])
        ):
            apply_aligned_scene_insert(cue, anchor)
            return True
    return False


def source_fragment_groups_for_episodes(
    project: dict[str, Any], fragments: list[dict[str, Any]], episode_ids: list[str],
) -> list[list[dict[str, Any]]]:
    """Propose monotone chapter cuts from distinguishing subtitle vocabulary.

    Mapping a chapter to two episodes does not mean half its paragraphs belong
    in each. Keep every fragment exactly once; timing remains the alignment
    agent's responsibility. Weak/absent evidence retains the balanced fallback.
    """
    fallback = _split_evenly(fragments, len(episode_ids))
    if len(episode_ids) < 2 or not fragments:
        return fallback
    corpus = {episode_id: set() for episode_id in episode_ids}
    for segment in project.get("reconciled_transcript") or project.get("transcript", []):
        episode_id = str(segment.get("episode_id") or "")
        if episode_id in corpus and transcript_item_outside_intros(project, segment):
            corpus[episode_id].update(narration_alignment_tokens(
                str(segment.get("reconciled_text") or segment.get("text") or "")
            ))
    if not all(corpus.values()):
        return fallback
    distinguishing = [
        corpus[episode_id] - set().union(*(words for other, words in corpus.items() if other != episode_id))
        for episode_id in episode_ids
    ]
    scores = [
        [len(narration_alignment_tokens(str(fragment.get("text") or "")) & words)
         for words in distinguishing]
        for fragment in fragments
    ]
    balanced = [index for index, group in enumerate(fallback) for _ in group]
    # Score evidence first, distance from the previous balanced cut only on ties.
    states = [((scores[0][index], -abs(index - balanced[0])), [index])
              for index in range(len(episode_ids))]
    for row_index, row in enumerate(scores[1:], 1):
        next_states = []
        for index in range(len(episode_ids)):
            (score, proximity), path = max(states[:index + 1], key=lambda state: state[0])
            next_states.append(((score + row[index], proximity - abs(index - balanced[row_index])), path + [index]))
        states = next_states
    (best_score, _), assignment = max(states, key=lambda state: state[0])
    baseline_score = sum(row[index] for row, index in zip(scores, balanced))
    if best_score - baseline_score < max(3, math.ceil(baseline_score * 0.15)):
        return fallback
    groups: list[list[dict[str, Any]]] = [[] for _ in episode_ids]
    for fragment, index in zip(fragments, assignment):
        groups[index].append(fragment)
    return groups


def generate_narration_cues(project: dict[str, Any], density: str = "audio_drama") -> list[dict[str, Any]]:
    interval_ms = NARRATION_INTERVAL_MS.get(density, NARRATION_INTERVAL_MS["detailed"])
    chapters = {chapter["id"]: chapter for chapter in project.get("chapters", [])}
    mappings = project.get("mapping", [])
    episode_ids = unique_episode_ids(mappings)
    media_by_episode: dict[str, dict[str, Any]] = {}
    for asset in project.get("media_assets", []):
        media_by_episode.setdefault(str(asset.get("episode_id")), asset)

    fragments_by_episode: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {episode_id: [] for episode_id in episode_ids}
    for chapter_id, chapter in chapters.items():
        mapped_ids = unique_episode_ids([item for item in mappings if item.get("chapter_id") == chapter_id])
        fragments = list(chapter.get("fragments") or [])
        for episode_id, group in zip(mapped_ids, source_fragment_groups_for_episodes(project, fragments, mapped_ids)):
            fragments_by_episode.setdefault(episode_id, []).extend((chapter, fragment) for fragment in group)

    transcript_by_episode: dict[str, list[dict[str, Any]]] = {}
    seen_segments: set[tuple[str, int, int, str]] = set()
    for segment in project.get("reconciled_transcript") or project.get("transcript", []):
        if segment.get("visual_scene_anchor"):
            continue
        if not transcript_item_outside_intros(project, segment):
            continue
        episode_id = str(segment.get("episode_id") or "")
        signature = (
            episode_id,
            int(segment.get("episode_start_ms", 0)),
            int(segment.get("episode_end_ms", 0)),
            str(segment.get("reconciled_text") or segment.get("text") or ""),
        )
        if episode_id and signature not in seen_segments:
            seen_segments.add(signature)
            transcript_by_episode.setdefault(episode_id, []).append(segment)
    for segments in transcript_by_episode.values():
        segments.sort(key=lambda item: (int(item.get("episode_start_ms", 0)), int(item.get("episode_end_ms", 0))))
    words_by_episode: dict[str, list[dict[str, Any]]] = {}
    seen_words: set[tuple[str, int, int, str]] = set()
    for word in project.get("transcript_words", []):
        if not transcript_item_outside_intros(project, word):
            continue
        episode_id = str(word.get("episode_id") or "")
        signature = (
            episode_id,
            int(word.get("episode_start_ms", 0)),
            int(word.get("episode_end_ms", 0)),
            str(word.get("text") or ""),
        )
        if episode_id and signature not in seen_words:
            seen_words.add(signature)
            words_by_episode.setdefault(episode_id, []).append(word)

    cues = []
    for episode_index, episode_id in enumerate(episode_ids):
        source_items = fragments_by_episode.get(episode_id, [])
        segments = transcript_by_episode.get(episode_id, [])
        if not source_items or not segments:
            continue
        asset = media_by_episode.get(episode_id, {})
        duration_ms = int(asset.get("duration_ms") or max((int(item.get("episode_end_ms", 0)) for item in segments), default=1))
        cue_count = min(len(source_items), max(3, math.ceil(duration_ms / interval_ms)))
        safe_gaps = speech_free_gaps_without_intros(
            project,
            episode_id,
            segments,
            words_by_episode.get(episode_id, []),
            duration_ms,
        )
        opening_block = opening_narration_block(segments) if episode_index == 0 else None
        opening_segment_ids: set[str] = set()
        opening_source_end_index = -1
        opening_gap_index: int | None = None
        opening_insert_ms: int | None = None
        if opening_block:
            opening_start_ms, opening_end_ms, opening_segments = opening_block
            opening_segment_ids = {str(segment.get("id")) for segment in opening_segments}
            opening_source_start_index = next(
                (index for index, item in enumerate(source_items) if fragment_narrative_sentences(item[1])),
                0,
            )
            opening_source_indices = [
                index
                for index in range(opening_source_start_index, len(source_items))
                if fragment_narrative_sentences(source_items[index][1])
            ][:3 if density in {"detailed", "audio_drama"} else 1]
            opening_source_end_index = (
                opening_source_indices[-1]
                if opening_source_indices
                else opening_source_start_index
            )
            opening_chapter = source_items[opening_source_end_index][0]
            opening_fragments = [
                source_items[index][1]
                for index in opening_source_indices
            ] or [source_items[opening_source_end_index][1]]
            opening_text = opening_action_from_fragments(
                opening_fragments,
                opening_chapter["title"],
                max_sentences=(
                    8 if density == "detailed" else 4 if density == "audio_drama" else 2
                ),
                max_characters=(
                    900 if density == "detailed" else 700 if density == "audio_drama" else 360
                ),
            )
            estimated_duration_ms = max(1_800, len(opening_text.split()) * 390)
            if density == "audio_drama":
                # The raw opening deliberately carries enough source evidence for the
                # editor to choose the essential image details.  Size the candidate
                # gap for the final oral character beat, not for reading that raw
                # evidence verbatim; otherwise a usable music bed is discarded before
                # the agent can shorten it.
                estimated_duration_ms = min(
                    estimated_duration_ms,
                    AUDIO_DRAMA_DURATION_LIMITS_MS["character_introduction"],
                )
            opening_gap = select_opening_narration_gap(safe_gaps, estimated_duration_ms)
            if opening_gap:
                insert_ms, gap_start_ms, gap_end_ms, opening_gap_index = opening_gap
                opening_insert_ms = insert_ms
                cues.append({
                    "id": new_id("cue"),
                    "chapter_id": opening_chapter["id"],
                    "chapter_title": opening_chapter["title"],
                    "text": opening_text,
                    "source_fragment_ids": [fragment["id"] for fragment in opening_fragments],
                    "anchor_segment_id": opening_segments[0]["id"],
                    "anchor_episode_id": episode_id,
                    "anchor_episode_start_ms": 0,
                    "anchor_insert_ms": insert_ms,
                    "safe_gap_start_ms": gap_start_ms,
                    "safe_gap_end_ms": gap_end_ms,
                    "anchor_global_start_sample": 0,
                    "anchor_text": "Frühe sprachfreie Passage (Plex-SRT + Whisper)",
                    "anchor_match_score": 1.0,
                    "placement_policy": "overlay_opening_gap",
                    "estimated_duration_ms": estimated_duration_ms,
                    "confidence": 0.99,
                    "revision": 1,
                })
            else:
                opening_block = None
                opening_segment_ids = set()
                opening_source_end_index = -1
        regular_source_items = source_items[opening_source_end_index + 1:] if opening_block else source_items
        regular_cue_count = max(0, cue_count - (1 if opening_block else 0))
        source_groups = _split_evenly(regular_source_items, regular_cue_count)
        opening_end_ms = opening_block[1] if opening_block else 0
        content_start_ms = opening_end_ms or min(
            (int(segment.get("episode_start_ms", 0)) for segment in segments),
            default=0,
        )
        regular_segments = [
            segment
            for segment in segments
            if str(segment.get("id")) not in opening_segment_ids
            and int(segment.get("episode_start_ms", 0)) > opening_end_ms
        ]
        final_content_segment = regular_segments[-1] if regular_segments else None
        reviewed_end = next((cue for cue in project.get("cues", []) if cue.get("verified_book_boundary_anchor")
                             and str(cue.get("anchor_episode_id")) == episode_id), None)
        if reviewed_end:
            final_content_segment = next((segment for segment in regular_segments if str(segment.get("id")) == str(reviewed_end.get("anchor_segment_id"))), final_content_segment)

        final_content_signature = (
            (
                int(final_content_segment.get("episode_start_ms", 0)),
                int(final_content_segment.get("episode_end_ms", 0)),
                str(final_content_segment.get("reconciled_text") or final_content_segment.get("text") or ""),
            )
            if final_content_segment
            else None
        )
        used_segments: set[tuple[int, int, str]] = set()
        used_gaps: set[int] = {opening_gap_index} if opening_gap_index is not None else set()
        used_insertions: list[int] = [opening_insert_ms] if opening_insert_ms is not None else []
        last_anchor_start_ms = opening_end_ms
        for cue_index, group in enumerate(source_groups):
            chapter = group[0][0]
            fragments = [item[1] for item in group]
            is_final_book_group = (
                episode_index == len(episode_ids) - 1
                and cue_index == len(source_groups) - 1
            )
            narration = (
                ending_action_from_fragments(fragments, chapter["title"])
                if is_final_book_group
                else narration_from_fragments(
                    fragments,
                    chapter["title"],
                    max_sentences=(
                        8 if density == "detailed" else 5 if density == "audio_drama" else 2
                    ),
                    max_characters=(
                        900 if density == "detailed" else 700 if density == "audio_drama" else 360
                    ),
                )
            )
            if not any(fragment_narrative_sentences(fragment) for fragment in fragments):
                continue
            source_tokens = narration_alignment_tokens(" ".join(str(fragment.get("text") or "") for fragment in fragments))
            target_ms = round(content_start_ms + (cue_index + 1) * (duration_ms - content_start_ms) / (len(source_groups) + 1))
            window_ms = max(interval_ms, 120_000)
            if is_final_book_group and final_content_segment:
                candidates = [final_content_segment]
            else:
                candidates = [
                    item for item in regular_segments
                    if int(item.get("episode_start_ms", 0)) > last_anchor_start_ms
                    if abs(int(item.get("episode_start_ms", 0)) - target_ms) <= window_ms
                    and (int(item.get("episode_start_ms", 0)), int(item.get("episode_end_ms", 0)), str(item.get("reconciled_text") or item.get("text") or "")) not in used_segments
                    and (
                        not is_final_book_group
                        and (
                            int(item.get("episode_start_ms", 0)),
                            int(item.get("episode_end_ms", 0)),
                            str(item.get("reconciled_text") or item.get("text") or ""),
                        ) != final_content_signature
                    )
                ]
                candidates = candidates or [
                    item for item in regular_segments
                    if int(item.get("episode_start_ms", 0)) > last_anchor_start_ms
                    if (int(item.get("episode_start_ms", 0)), int(item.get("episode_end_ms", 0)), str(item.get("reconciled_text") or item.get("text") or "")) not in used_segments
                    and (
                        not is_final_book_group
                        and (
                            int(item.get("episode_start_ms", 0)),
                            int(item.get("episode_end_ms", 0)),
                            str(item.get("reconciled_text") or item.get("text") or ""),
                        ) != final_content_signature
                    )
                ]
            if not candidates:
                continue

            def rank(candidate: dict[str, Any]) -> tuple[float, float]:
                candidate_tokens = narration_alignment_tokens(str(candidate.get("reconciled_text") or candidate.get("text") or ""))
                overlap = len(source_tokens & candidate_tokens) / max(len(candidate_tokens), 1)
                proximity = 1 - min(1, abs(int(candidate.get("episode_start_ms", 0)) - target_ms) / max(window_ms, 1))
                return overlap * 0.7 + proximity * 0.3, -abs(int(candidate.get("episode_start_ms", 0)) - target_ms)

            semantically_matched = [
                item for item in candidates
                if source_tokens & narration_alignment_tokens(str(item.get("reconciled_text") or item.get("text") or ""))
            ]
            anchor = max(semantically_matched or candidates, key=rank)
            anchor_signature = (int(anchor.get("episode_start_ms", 0)), int(anchor.get("episode_end_ms", 0)), str(anchor.get("reconciled_text") or anchor.get("text") or ""))
            used_segments.add(anchor_signature)
            last_anchor_start_ms = int(anchor.get("episode_start_ms", 0))
            anchor_text = str(anchor.get("reconciled_text") or anchor.get("text") or "")
            anchor_tokens = narration_alignment_tokens(anchor_text)
            match_score = len(source_tokens & anchor_tokens) / max(len(anchor_tokens), 1)
            safe_gap = select_safe_narration_gap(
                safe_gaps,
                used_gaps,
                anchor_end_ms=int(anchor.get("episode_end_ms", anchor.get("episode_start_ms", 0))),
                target_ms=target_ms,
                used_insertions=used_insertions,
            )
            is_aligned = anchor.get("timestamp_source") in {"whisper_segments", "plex_srt"}
            cue = {
                "id": new_id("cue"),
                "chapter_id": chapter["id"],
                "chapter_title": chapter["title"],
                "text": narration,
                "source_fragment_ids": [fragment["id"] for fragment in fragments],
                "anchor_segment_id": anchor["id"],
                "anchor_episode_id": episode_id,
                "anchor_episode_start_ms": int(anchor.get("episode_start_ms", 0)),
                "anchor_global_start_sample": int(anchor.get("start_sample", 0)),
                "anchor_text": anchor_text,
                "anchor_match_score": round(match_score, 3),
                "placement_policy": "after_aligned_speech_gap" if is_aligned else "chapter_boundary",
                "estimated_duration_ms": max(1800, len(narration.split()) * 390),
                "confidence": round(0.72 + min(match_score, 1) * 0.23, 3) if is_aligned else 0.55,
                "revision": 1,
            }
            cue["aligned_boundary_ms"] = aligned_scene_boundary_ms(cue, anchor)
            if safe_gap is None:
                apply_aligned_scene_insert(cue, anchor)
            else:
                insert_ms, gap_start_ms, gap_end_ms, _ = safe_gap
                cue.update(
                    {
                        "anchor_insert_ms": insert_ms,
                        "safe_gap_start_ms": gap_start_ms,
                        "safe_gap_end_ms": gap_end_ms,
                    }
                )
            if is_final_book_group and reviewed_end:
                cue["verified_book_boundary_anchor"] = reviewed_end["verified_book_boundary_anchor"]
            cues.append(cue)
    lexicon = project.get("narration_name_lexicon", [])
    for cue in cues:
        cue["text"] = canonicalize_narration_names(str(cue.get("text") or ""), lexicon)
    return sorted(cues, key=lambda cue: (episode_ids.index(str(cue.get("anchor_episode_id"))), int(cue.get("anchor_insert_ms", 0))))


def apply_researched_scene_alignment(
    project: dict[str, Any],
    cues: list[dict[str, Any]],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    rules = narration_rules(project)
    segments = {
        str(item["id"]): item
        for item in project.get("reconciled_transcript", [])
        if transcript_item_outside_intros(project, item)
    }
    episode_ids = unique_episode_ids(project.get("mapping", []))
    episode_order = {episode_id: index for index, episode_id in enumerate(episode_ids)}
    media_by_episode = {}
    for asset in project.get("media_assets", []):
        media_by_episode.setdefault(str(asset.get("episode_id")), asset)
    segments_by_episode: dict[str, list[dict[str, Any]]] = {episode_id: [] for episode_id in episode_ids}
    words_by_episode: dict[str, list[dict[str, Any]]] = {episode_id: [] for episode_id in episode_ids}
    for segment in project.get("reconciled_transcript", []):
        if not transcript_item_outside_intros(project, segment):
            continue
        segments_by_episode.setdefault(str(segment.get("episode_id") or ""), []).append(segment)
    for word in project.get("transcript_words", []):
        if not transcript_item_outside_intros(project, word):
            continue
        words_by_episode.setdefault(str(word.get("episode_id") or ""), []).append(word)
    gaps_by_episode = {}
    for episode_id in episode_ids:
        duration_ms = int(
            media_by_episode.get(episode_id, {}).get("duration_ms")
            or max((int(item.get("episode_end_ms", 0)) for item in segments_by_episode.get(episode_id, [])), default=1)
        )
        gaps_by_episode[episode_id] = speech_free_gaps_without_intros(
            project,
            episode_id,
            segments_by_episode.get(episode_id, []),
            words_by_episode.get(episode_id, []),
            duration_ms,
        )
    cues_by_id = {str(cue.get("id")): cue for cue in cues}
    used_gaps: dict[str, set[int]] = {episode_id: set() for episode_id in episode_ids}
    used_insertions: dict[str, list[int]] = {episode_id: [] for episode_id in episode_ids}
    forced_insertions: dict[str, list[int]] = {episode_id: [] for episode_id in episode_ids}
    aligned = []
    last_position = (-1, -1)
    for match in result.get("alignments", []):
        source_cue = cues_by_id.get(str(match.get("cue_id") or ""))
        if not source_cue:
            raise RuntimeError("Codex-Szenenausrichtung verweist auf eine unbekannte Rohpassage")
        cue = json.loads(json.dumps(source_cue))
        revised_text = canonicalize_narration_names(
            str(match.get("narration_text") or ""),
            project.get("narration_name_lexicon", []),
        )
        if not revised_text:
            continue
        estimated_duration_ms = max(1_800, len(revised_text.split()) * 390)
        cue = {
            **cue,
            "text": revised_text,
            "narrative_purpose": str(match.get("purpose") or ""),
            "introduced_characters": [
                str(value)[:120]
                for value in match.get("introduced_characters", [])[:20]
                if str(value).strip()
            ],
            "information_gain": str(match.get("information_gain") or "")[:500],
            "source_detail_coverage": round(float(match.get("source_detail_coverage") or 0), 3),
            "source_detail_reference_count": int(
                match.get("source_detail_reference_count")
                if match.get("source_detail_reference_count") is not None
                else MIN_SOURCE_DETAIL_REFERENCE_TOKENS
            ),
            "preserved_concrete_detail_count": int(
                match.get("preserved_concrete_detail_count") or 0
            ),
            "native_audio_relation": str(match.get("native_audio_relation") or ""),
            "retained_visual_details": [
                str(value)[:240]
                for value in match.get("retained_visual_details", [])[:12]
                if str(value).strip()
            ],
            "beat_type": str(match.get("beat_type") or ""),
            "audio_strategy": str(match.get("audio_strategy") or ""),
            "target_duration_ms": int(
                match.get("target_duration_ms") or estimated_duration_ms
            ),
            "estimated_duration_ms": estimated_duration_ms,
        }
        if cue.get("placement_policy") in {"replace_existing_narration", "overlay_intro_music", "overlay_opening_gap"}:
            episode_id = str(cue.get("anchor_episode_id") or "")
            used_insertions.setdefault(episode_id, []).append(int(cue.get("anchor_insert_ms", 0)))
            cue["alignment_reasoning"] = str(match.get("reasoning") or "")[:500]
            cue["alignment_placement"] = str(match.get("placement") or "before_anchor")
            cue["confidence"] = round(float(match.get("confidence") or 0), 3)
            aligned.append(cue)
            continue
        anchor_id = str(match.get("anchor_segment_id") or "")
        confidence = float(match.get("confidence") or 0)
        anchor = segments.get(anchor_id)
        if not anchor or confidence < 0.45:
            continue
        episode_id = str(anchor.get("episode_id") or "")
        position = (episode_order.get(episode_id, 9999), int(anchor.get("episode_start_ms", 0)))
        if position < last_position:
            continue
        placement_side = "before" if match.get("placement") == "before_anchor" else "after"
        anchor_edge_ms = (
            int(anchor.get("episode_start_ms", 0))
            if placement_side == "before"
            else int(anchor.get("episode_end_ms", anchor.get("episode_start_ms", 0)))
        )
        safe_gap = (
            None
            if cue.get("audio_strategy") == "pause_at_scene_boundary"
            else select_safe_narration_gap(
                gaps_by_episode.get(episode_id, []),
                used_gaps.setdefault(episode_id, set()),
                anchor_end_ms=anchor_edge_ms,
                target_ms=anchor_edge_ms,
                used_insertions=used_insertions.setdefault(episode_id, []),
                min_duration_ms=int(cue.get("estimated_duration_ms", 0)) + 1_000,
                placement_side=placement_side,
                max_distance_ms=before_anchor_gap_distance_ms(cue, rules, placement_side),
            )
        )
        placement_policy = "overlay_speech_free"
        if safe_gap is None and cue.get("audio_strategy") != "pause_at_scene_boundary":
            sentence_count = len(re.findall(r"[.!?](?:\s|$)", str(cue.get("text") or "")))
            word_count = len(str(cue.get("text") or "").split())
            anchor_start_ms = int(anchor.get("episode_start_ms", 0))
            dense_action = (
                placement_side == "after"
                and str(cue.get("narrative_purpose") or "") not in {"character_introduction", "book_boundary"}
                and sentence_count >= int(rules["dense_action_min_sentences"])
                and word_count >= int(rules["dense_action_min_words"])
                and confidence >= float(rules["dense_action_min_confidence"])
            )
            far_from_forced_insert = all(
                abs(anchor_start_ms - existing) >= int(rules["min_forced_insert_spacing_ms"])
                for existing in forced_insertions.setdefault(episode_id, [])
            )
            if dense_action and far_from_forced_insert:
                dense_gap = select_safe_narration_gap(
                    gaps_by_episode.get(episode_id, []),
                    used_gaps.setdefault(episode_id, set()),
                    anchor_end_ms=int(anchor.get("episode_end_ms", anchor.get("episode_start_ms", 0))),
                    target_ms=int(anchor.get("episode_end_ms", anchor.get("episode_start_ms", 0))),
                    used_insertions=used_insertions.setdefault(episode_id, []),
                    placement_side="after",
                    max_distance_ms=int(rules["max_anchor_distance_ms"]),
                )
                if dense_gap is not None and not any(
                    abs(int(dense_gap[0]) - existing) < int(rules["min_forced_insert_spacing_ms"])
                    for existing in forced_insertions[episode_id]
                ):
                    # Insert near the start of the pause, not in its middle: the
                    # proximity gate measures the audible start, so a centred
                    # insert drifts seconds past the scene it belongs to.  Keep
                    # one speech margin of distance, because the collision check
                    # pads spoken words and requires a strictly inner position.
                    _insert_ms, gap_start_ms, gap_end_ms, gap_index = dense_gap
                    near_edge_ms = min(
                        gap_start_ms + int(rules["speech_margin_ms"]),
                        max(gap_start_ms + 1, gap_end_ms - 1),
                    )
                    safe_gap = (near_edge_ms, gap_start_ms, gap_end_ms, gap_index)
                    forced_insertions[episode_id].append(int(near_edge_ms))
                    placement_policy = "insert_for_dense_action"
        anchor_text = str(anchor.get("reconciled_text") or anchor.get("text") or "")
        aligned_cue = {
            **cue,
            "anchor_segment_id": anchor["id"],
            "anchor_episode_id": episode_id,
            "anchor_episode_start_ms": int(anchor.get("episode_start_ms", 0)),
            "anchor_global_start_sample": int(anchor.get("start_sample", 0)),
            "anchor_text": anchor_text,
            "anchor_match_score": round(confidence, 3),
            "alignment_reasoning": str(match.get("reasoning") or "")[:500],
            "alignment_placement": str(match.get("placement") or "after_anchor"),
            "placement_policy": placement_policy,
            "confidence": round(confidence, 3),
        }
        aligned_cue["aligned_boundary_ms"] = aligned_scene_boundary_ms(aligned_cue, anchor)
        if safe_gap is None:
            apply_aligned_scene_insert(aligned_cue, anchor)
        else:
            insert_ms, gap_start_ms, gap_end_ms, _ = safe_gap
            aligned_cue.update(
                {
                    "anchor_insert_ms": insert_ms,
                    "safe_gap_start_ms": gap_start_ms,
                    "safe_gap_end_ms": gap_end_ms,
                }
            )
            mark_before_anchor_overlay_alignment(aligned_cue)
        aligned.append(aligned_cue)
        last_position = position
    aligned_ids = {str(cue.get("id") or "") for cue in aligned}
    missing_introductions = [
        str(item.get("research_name") or item.get("spoken_name") or item.get("cue_id") or "unbekannte Figur")
        for item in result.get("character_introductions", [])
        if str(item.get("cue_id") or "") not in aligned_ids
    ]
    if missing_introductions:
        raise RuntimeError(
            "Codex-Szenenausrichtung verlor verpflichtende Figuren-Einführungen: "
            + ", ".join(missing_introductions)
        )
    counts = Counter(str(cue.get("anchor_episode_id") or "") for cue in aligned)
    # research_scene_alignment() skips every episode without a proposed raw
    # cue (its package would be empty), so the agent never saw it and cannot
    # have anchored anything there. That happens when one short chapter maps
    # to two episodes and the proposer places all of it in the first one, or
    # when an episode is anime-only filler the book never covers. Demanding a
    # cue there would discard a finished multi-episode alignment for an
    # episode nobody could have served; it is reported as a warning instead.
    proposed_episode_ids = {str(cue.get("anchor_episode_id") or "") for cue in cues}
    required_episode_ids = [episode_id for episode_id in episode_ids if episode_id in proposed_episode_ids]
    uncovered_episode_ids = [episode_id for episode_id in episode_ids if episode_id not in proposed_episode_ids]
    result["uncovered_episode_ids"] = uncovered_episode_ids
    if uncovered_episode_ids:
        note = (
            "Folgen ohne zugeordnete Roman-Rohpassage erhalten keine Erzählpassagen: "
            + ", ".join(uncovered_episode_ids)
        )
        result.setdefault("notes", []).append(note)
        logger.warning("%s", note)
    minimum_total = min(len(cues), max(3, len(required_episode_ids) * 2))
    if len(aligned) < minimum_total or any(counts.get(episode_id, 0) < 1 for episode_id in required_episode_ids):
        raise RuntimeError(
            f"Codex-Szenenausrichtung lieferte zu wenig chronologisch belegte Handlungspassagen: "
            f"{len(aligned)} gesamt, je Folge {dict(counts)}"
        )
    repetition_violations = [
        violation
        for violation in native_audio_repetition_violations(project, aligned)
        # A formal introduction describes what the listener cannot see.  That the
        # dialogue happens to mention the same trait ("rasiert sich den Kopf" next
        # to "kahlrasierter Moenchsjunge") is not a paraphrase of speech, and it
        # must not discard a finished eight-episode alignment.
        if str(
            next(
                (
                    cue.get("narrative_purpose")
                    for cue in aligned
                    if str(cue.get("id") or "") == str(violation.get("cue_id") or "")
                ),
                "",
            )
        ) != "character_introduction"
    ]
    if repetition_violations:
        # The editor already enforces this rule with targeted repair feedback.
        # Repeating it here as a hard abort throws away a finished alignment over
        # generic overlap ("Kopf", "Moench"), so record it and let the release
        # gate judge the finished render instead.
        first = repetition_violations[0]
        print(
            "narration_repetition_warning: "
            + f"{first.get('cue_id')} · {str(first.get('text') or '')[:160]}",
            flush=True,
        )
    return sorted(
        aligned,
        key=lambda cue: (episode_order.get(str(cue.get("anchor_episode_id") or ""), 9999), int(cue.get("anchor_insert_ms", 0))),
    )


def replan_semantic_cues_for_overlay(project: dict[str, Any], semantic_cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules = narration_rules(project)
    lexicon = project.get("narration_name_lexicon", [])
    episode_ids = unique_episode_ids(project.get("mapping", []))
    episode_order = {episode_id: index for index, episode_id in enumerate(episode_ids)}
    media_by_episode = {}
    for asset in project.get("media_assets", []):
        media_by_episode.setdefault(str(asset.get("episode_id")), asset)
    segments_by_episode: dict[str, list[dict[str, Any]]] = {episode_id: [] for episode_id in episode_ids}
    words_by_episode: dict[str, list[dict[str, Any]]] = {episode_id: [] for episode_id in episode_ids}
    for segment in project.get("reconciled_transcript", []):
        if not transcript_item_outside_intros(project, segment):
            continue
        segments_by_episode.setdefault(str(segment.get("episode_id") or ""), []).append(segment)
    for word in project.get("transcript_words", []):
        if not transcript_item_outside_intros(project, word):
            continue
        words_by_episode.setdefault(str(word.get("episode_id") or ""), []).append(word)
    gaps_by_episode = {}
    for episode_id in episode_ids:
        duration_ms = int(
            media_by_episode.get(episode_id, {}).get("duration_ms")
            or max((int(item.get("episode_end_ms", 0)) for item in segments_by_episode.get(episode_id, [])), default=1)
        )
        gaps_by_episode[episode_id] = speech_free_gaps_without_intros(
            project,
            episode_id,
            segments_by_episode.get(episode_id, []),
            words_by_episode.get(episode_id, []),
            duration_ms,
        )
    used_gaps: dict[str, set[int]] = {episode_id: set() for episode_id in episode_ids}
    used_insertions: dict[str, list[int]] = {episode_id: [] for episode_id in episode_ids}
    forced_insertions: dict[str, list[int]] = {episode_id: [] for episode_id in episode_ids}
    planned = []
    for cue_index, source_cue in enumerate(semantic_cues):
        cue = json.loads(json.dumps(source_cue))
        for stale_field in (
            "measured_duration_ms",
            "placement_sample_in_episode",
            "render_status",
            "timeline_start_sample",
        ):
            cue.pop(stale_field, None)
        cue["text"] = canonicalize_narration_names(str(cue.get("text") or ""), lexicon)
        full_text = cue["text"]
        cue["estimated_duration_ms"] = max(1_800, len(cue["text"].split()) * 390)
        episode_id = str(cue.get("anchor_episode_id") or "")
        if (
            cue_index == 0
            and episode_id == episode_ids[0]
            and cue.get("audio_strategy") != "pause_at_scene_boundary"
        ):
            opening_gap = select_opening_narration_gap(
                gaps_by_episode.get(episode_id, []),
                int(cue["estimated_duration_ms"]),
            )
            if opening_gap:
                insert_ms, gap_start_ms, gap_end_ms, gap_index = opening_gap
                cue.pop("replacement_start_ms", None)
                cue.pop("replacement_end_ms", None)
                cue.update({
                    "anchor_episode_start_ms": 0,
                    "anchor_insert_ms": insert_ms,
                    "safe_gap_start_ms": gap_start_ms,
                    "safe_gap_end_ms": gap_end_ms,
                    "anchor_global_start_sample": 0,
                    "anchor_text": "Frühe sprachfreie Passage (Plex-SRT + Whisper)",
                    "placement_policy": "overlay_opening_gap",
                })
                used_gaps[episode_id].add(gap_index)
                used_insertions[episode_id].append(insert_ms)
                planned.append(cue)
                continue
        anchor_start_ms = int(cue.get("anchor_episode_start_ms", 0))
        anchor_end_ms = max(
            anchor_start_ms,
            next(
                (
                    int(segment.get("episode_end_ms", anchor_start_ms))
                    for segment in segments_by_episode.get(episode_id, [])
                    if str(segment.get("id")) == str(cue.get("anchor_segment_id"))
                ),
                anchor_start_ms,
            ),
        )
        anchor = next(
            (
                segment
                for segment in segments_by_episode.get(episode_id, [])
                if str(segment.get("id")) == str(cue.get("anchor_segment_id"))
            ),
            {
                "episode_start_ms": anchor_start_ms,
                "episode_end_ms": anchor_end_ms,
            },
        )
        placement_side: Literal["before", "after"] = (
            "before" if cue.get("alignment_placement") == "before_anchor" else "after"
        )
        anchor_edge_ms = anchor_start_ms if placement_side == "before" else anchor_end_ms
        cue["aligned_boundary_ms"] = aligned_scene_boundary_ms(cue, anchor)
        safe_gap = (
            None
            if cue.get("audio_strategy") == "pause_at_scene_boundary"
            else select_safe_narration_gap(
                gaps_by_episode.get(episode_id, []),
                used_gaps.setdefault(episode_id, set()),
                anchor_end_ms=anchor_edge_ms,
                target_ms=anchor_edge_ms,
                used_insertions=used_insertions.setdefault(episode_id, []),
                min_duration_ms=int(cue["estimated_duration_ms"]) + 1_000,
                placement_side=placement_side,
                max_distance_ms=before_anchor_gap_distance_ms(cue, rules, placement_side),
            )
        )
        placement_policy = "overlay_speech_free"
        if safe_gap is None and cue.get("audio_strategy") != "pause_at_scene_boundary":
            first_sentence = re.split(r"(?<=[.!?])\s+", full_text, maxsplit=1)[0].strip()
            if first_sentence and first_sentence != full_text and len(first_sentence.split()) >= 6:
                concise_estimate_ms = max(1_800, len(first_sentence.split()) * 390)
                safe_gap = select_safe_narration_gap(
                    gaps_by_episode.get(episode_id, []),
                    used_gaps.setdefault(episode_id, set()),
                    anchor_end_ms=anchor_edge_ms,
                    target_ms=anchor_edge_ms,
                    used_insertions=used_insertions.setdefault(episode_id, []),
                    min_duration_ms=concise_estimate_ms + 800,
                    placement_side=placement_side,
                    max_distance_ms=before_anchor_gap_distance_ms(cue, rules, placement_side),
                )
                if safe_gap is not None:
                    cue["text"] = first_sentence
                    cue["estimated_duration_ms"] = concise_estimate_ms
        if safe_gap is None and cue.get("audio_strategy") != "pause_at_scene_boundary":
            cue["text"] = full_text
            cue["estimated_duration_ms"] = max(1_800, len(full_text.split()) * 390)
            sentence_count = len(re.findall(r"[.!?](?:\s|$)", full_text))
            dense_action = (
                placement_side == "after"
                and str(cue.get("narrative_purpose") or "") not in {"character_introduction", "book_boundary"}
                and sentence_count >= int(rules["dense_action_min_sentences"])
                and len(full_text.split()) >= int(rules["dense_action_min_words"])
                and float(cue.get("confidence") or 0) >= float(rules["dense_action_min_confidence"])
            )
            if dense_action and not any(
                abs(anchor_start_ms - existing) < int(rules["min_forced_insert_spacing_ms"])
                for existing in forced_insertions[episode_id]
            ):
                dense_gap = select_safe_narration_gap(
                    gaps_by_episode.get(episode_id, []),
                    used_gaps.setdefault(episode_id, set()),
                    anchor_end_ms=anchor_end_ms,
                    target_ms=anchor_end_ms,
                    used_insertions=used_insertions.setdefault(episode_id, []),
                    placement_side="after",
                    max_distance_ms=int(rules["max_anchor_distance_ms"]),
                )
                if dense_gap is not None and not any(
                    abs(int(dense_gap[0]) - existing) < int(rules["min_forced_insert_spacing_ms"])
                    for existing in forced_insertions[episode_id]
                ):
                    # Insert near the start of the pause, not in its middle: the
                    # proximity gate measures the audible start, so a centred
                    # insert drifts seconds past the scene it belongs to.  Keep
                    # one speech margin of distance, because the collision check
                    # pads spoken words and requires a strictly inner position.
                    _insert_ms, gap_start_ms, gap_end_ms, gap_index = dense_gap
                    near_edge_ms = min(
                        gap_start_ms + int(rules["speech_margin_ms"]),
                        max(gap_start_ms + 1, gap_end_ms - 1),
                    )
                    safe_gap = (near_edge_ms, gap_start_ms, gap_end_ms, gap_index)
                    forced_insertions[episode_id].append(int(near_edge_ms))
                    placement_policy = "insert_for_dense_action"
        if safe_gap is None:
            apply_aligned_scene_insert(cue, anchor)
        else:
            insert_ms, gap_start_ms, gap_end_ms, _ = safe_gap
            cue.update({
                "anchor_insert_ms": insert_ms,
                "safe_gap_start_ms": gap_start_ms,
                "safe_gap_end_ms": gap_end_ms,
                "placement_policy": placement_policy,
            })
            mark_before_anchor_overlay_alignment(cue)
        planned.append(cue)
    counts = Counter(str(cue.get("anchor_episode_id") or "") for cue in planned)
    minimum_total = min(len(semantic_cues), max(3, len(episode_ids) * 2))
    if len(planned) < minimum_total or any(counts.get(episode_id, 0) < 1 for episode_id in episode_ids):
        raise RuntimeError(f"Overlay-Plan enthält zu wenig belegte Handlung: {len(planned)} gesamt, je Folge {dict(counts)}")
    return sorted(
        planned,
        key=lambda cue: (episode_order.get(str(cue.get("anchor_episode_id") or ""), 9999), int(cue.get("anchor_insert_ms", 0))),
    )


def run_pipeline(project_id: str, run_id: str) -> None:
    try:
        update_run(run_id, status="running", message="Kapitel und Serienfolgen werden zugeordnet")
        project = STATE["projects"][project_id]
        mapping = []
        transcript = []
        transcript_words = []
        episodes = fetch_plex_episodes(str(project["binding"]["series_id"]))
        if not episodes:
            raise RuntimeError("Plex liefert für die gewählte Serie keine verwendbaren Episoden")
        for episode in episodes:
            select_episode_audio(episode, project.get("audio_language", ""))
        episode_by_id = {episode["id"]: episode for episode in episodes}
        existing_mapping = project.get("mapping") or []
        chapter_ids = {str(chapter["id"]) for chapter in project["chapters"]}
        reuse_mapping = bool(existing_mapping) and all(
            str(item.get("chapter_id") or "") in chapter_ids
            and str(item.get("episode_id") or "") in episode_by_id
            for item in existing_mapping
        )
        if reuse_mapping:
            # Die Zuordnung wird nicht bei jedem Lauf neu recherchiert: Jede
            # Neurecherche liefert ein leicht anderes Folgenfenster, entwertet
            # damit alle Szenenausrichtungs-Checkpoints (~1,5 h Agentenzeit)
            # und verschiebt die Buchrand-Prüfung. Eine frische Recherche gibt
            # es über DELETE /projects/{id}/mapping vor dem nächsten Lauf.
            mapping = json.loads(json.dumps(existing_mapping))
            research = project.get("mapping_research")
            update_run(run_id, message="Bestehende Kapitelzuordnung wird übernommen", completed_units=len(project["chapters"]))
        else:
            research = research_episode_mapping(project, episodes)
        research_by_chapter = {item["chapter_id"]: item for item in (research or {}).get("mappings", [])}
        for index, chapter in enumerate(project["chapters"]):
            if reuse_mapping:
                break
            researched = research_by_chapter.get(chapter["id"])
            if researched:
                selected_ids = researched["episode_ids"]
                sources = researched.get("sources", [])
                evidence = researched.get("reasoning", "")
                if sources:
                    evidence += " Quellen: " + "; ".join(f"{source['title']} ({source['url']})" for source in sources)
                mapping_type = "codex_research"
                confidence = float(researched.get("confidence", 0))
                conflicts = researched.get("conflicts", [])
            else:
                selected_ids = [episodes[min(index, len(episodes) - 1)]["id"]]
                sources = []
                evidence = "Fallback nach Plex-Reihenfolge; keine Codex-Recherche konfiguriert."
                mapping_type = "plex_order_fallback"
                confidence = 0.35
                conflicts = ["Semantische Zuordnung nicht recherchiert"]
            for episode_id in selected_ids:
                episode = episode_by_id.get(str(episode_id))
                if not episode:
                    raise RuntimeError(f"Codex-Mapping referenziert unbekannte Plex-Episode {episode_id}")
                mapping.append({"id": new_id("map"), "chapter_id": chapter["id"], "chapter_title": chapter["title"], "episode_id": episode["id"], "season": episode["season"], "episode": episode["episode"], "episode_title": episode["title"], "mapping_type": mapping_type, "confidence": confidence, "evidence": evidence.strip(), "research_sources": sources, "conflicts": conflicts, "review_state": "needs_review"})
            update_run(run_id, completed_units=index + 1)
            time.sleep(0.08)
        with LOCK:
            project["mapping"] = mapping
            project["mapping_research"] = research
            project["stage"] = 3
            project["status"] = "mapping_review"
            save_state()
        mapped_episode_ids = unique_episode_ids(mapping)
        update_run(
            run_id,
            stage="detecting_intros",
            message="Intros, Commercial-Bumper, Abspänne und Episodenvorschauen werden erkannt",
            completed_units=0,
            total_units=len(mapped_episode_ids),
        )
        intro_audio = {}
        for intro_index, episode_id in enumerate(mapped_episode_ids):
            episode = episode_by_id[episode_id]
            intro_audio[episode_id] = extract_episode_audio(
                episode,
                ARTIFACTS / project_id / "source-audio" / f"episode-{episode_id}.flac",
            )
            update_run(run_id, completed_units=intro_index + 1)
        intro_detection = detect_project_intros(project, intro_audio)
        commercial_bumper_detection = detect_project_commercial_bumpers(project, intro_audio)
        ending_detection = detect_project_endings(project, intro_audio)
        with LOCK:
            project["intro_detection"] = intro_detection
            project["commercial_bumper_detection"] = commercial_bumper_detection
            project["ending_detection"] = ending_detection
            save_state()
        update_run(
            run_id,
            stage="transcribing",
            message="Plex-Audiospuren werden mit Vocarium transkribiert",
            completed_units=0,
            total_units=len(project["chapters"]),
        )
        cursor = 0
        media_assets = []
        for index, chapter in enumerate(project["chapters"]):
            chapter_mappings = [item for item in mapping if item["chapter_id"] == chapter["id"]]
            for mapped in chapter_mappings:
                episode = next(item for item in episodes if item["id"] == mapped["episode_id"])
                audio_path = extract_episode_audio(episode, ARTIFACTS / project_id / "source-audio" / f"episode-{episode['id']}.flac")
                duration_ms = max(episode["duration_ms"], 1)
                chunk_transcripts = transcribe_with_vocarium(audio_path, project.get("audio_language", ""), duration_ms)
                duration = round(duration_ms * SAMPLE_RATE / 1000)
                for chunk in chunk_transcripts:
                    for segment_index, segment in enumerate(chunk["segments"]):
                        episode_start_ms = chunk["start_ms"] + segment["start_ms"]
                        episode_end_ms = min(duration_ms, chunk["start_ms"] + segment["end_ms"])
                        start_sample = cursor + round(episode_start_ms * SAMPLE_RATE / 1000)
                        end_sample = min(cursor + duration, cursor + round(episode_end_ms * SAMPLE_RATE / 1000))
                        transcript.append({"id": new_id("ts"), "chapter_id": chapter["id"], "chapter_title": chapter["title"], "episode_id": episode["id"], "episode": episode["episode"], "episode_title": episode["title"], "chunk_index": chunk["index"], "segment_index": segment_index, "episode_start_ms": episode_start_ms, "episode_end_ms": episode_end_ms, "start_sample": start_sample, "end_sample": end_sample, "start_ms": round(start_sample * 1000 / SAMPLE_RATE), "end_ms": round(end_sample * 1000 / SAMPLE_RATE), "text": segment["text"], "language": chunk["language"], "timestamp_source": chunk["timestamp_source"], "transcription_source": "vocarium_whisper", "confidence": 0.92 if chunk["timestamp_source"] == "whisper_segments" else 0.5})
                    for word_index, word in enumerate(chunk["words"]):
                        episode_start_ms = chunk["start_ms"] + word["start_ms"]
                        episode_end_ms = min(duration_ms, chunk["start_ms"] + word["end_ms"])
                        transcript_words.append({"id": new_id("tw"), "chapter_id": chapter["id"], "episode_id": episode["id"], "chunk_index": chunk["index"], "word_index": word_index, "episode_start_ms": episode_start_ms, "episode_end_ms": episode_end_ms, "start_sample": cursor + round(episode_start_ms * SAMPLE_RATE / 1000), "end_sample": cursor + round(episode_end_ms * SAMPLE_RATE / 1000), "text": word["text"], "language": chunk["language"], "timestamp_source": chunk["timestamp_source"]})
                detected_languages = sorted({chunk["language"] for chunk in chunk_transcripts})
                timestamp_sources = sorted({chunk["timestamp_source"] for chunk in chunk_transcripts})
                media_assets.append({"chapter_id": chapter["id"], "episode_id": episode["id"], "season": episode["season"], "episode": episode["episode"], "title": episode["title"], "duration_ms": duration_ms, "audio_streams": episode["audio_streams"], "selected_audio": episode["selected_audio"], "media_count": episode["media_count"], "part_count": episode["part_count"], "derived_audio": str(audio_path.relative_to(ARTIFACTS)), "asr_chunk_count": len(chunk_transcripts), "timestamp_segment_count": sum(len(chunk["segments"]) for chunk in chunk_transcripts), "timestamp_word_count": sum(len(chunk["words"]) for chunk in chunk_transcripts), "detected_languages": detected_languages, "timestamp_sources": timestamp_sources})
                cursor += duration
            with LOCK:
                project["transcript"] = list(transcript)
                project["transcript_words"] = list(transcript_words)
                project["media_assets"] = list(media_assets)
                save_state()
            update_run(run_id, completed_units=index + 1)
        with LOCK:
            project["transcript"] = transcript
            project["transcript_words"] = transcript_words
            project["media_assets"] = media_assets
            project["stage"] = 4
            project["status"] = "aligning"
            save_state()
        reconcile_project_subtitles(project_id, run_id, project.get("audio_language", "de"))
        if STATE["runs"][run_id].get("status") == "failed":
            raise RuntimeError(str(STATE["runs"][run_id].get("error") or "Untertitelabgleich fehlgeschlagen"))
        density = str(project.get("narration_density") or "audio_drama")
        rewrite_narration(project_id, run_id, density)
    except Exception as error:
        update_run(run_id, status="failed", error=str(error), message="Pipeline fehlgeschlagen")


def realign_project(project_id: str, run_id: str) -> None:
    try:
        project = STATE["projects"][project_id]
        update_run(run_id, status="running", stage="transcribing", message="Whisper transkribiert und liefert Wortzeitmarken", completed_units=0)
        episodes = fetch_plex_episodes(str(project["binding"]["series_id"]))
        episode_by_id = {episode["id"]: episode for episode in episodes}
        for episode in episodes:
            select_episode_audio(episode, project.get("audio_language", ""))
        mapped_episode_ids = unique_episode_ids(project.get("mapping", []))
        update_run(
            run_id,
            stage="detecting_intros",
            message="Intros, Commercial-Bumper, Abspänne und Episodenvorschauen werden erkannt",
            completed_units=0,
            total_units=len(mapped_episode_ids),
        )
        intro_audio = {}
        for intro_index, episode_id in enumerate(mapped_episode_ids):
            episode = episode_by_id.get(episode_id)
            if not episode:
                raise RuntimeError(f"Plex-Episode {episode_id} ist nicht mehr verfügbar")
            intro_audio[episode_id] = extract_episode_audio(
                episode,
                ARTIFACTS / project_id / "source-audio" / f"episode-{episode_id}.flac",
            )
            update_run(run_id, completed_units=intro_index + 1)
        intro_detection = detect_project_intros(project, intro_audio)
        commercial_bumper_detection = detect_project_commercial_bumpers(project, intro_audio)
        ending_detection = detect_project_endings(project, intro_audio)
        with LOCK:
            project["intro_detection"] = intro_detection
            project["commercial_bumper_detection"] = commercial_bumper_detection
            project["ending_detection"] = ending_detection
            save_state()
        update_run(
            run_id,
            stage="transcribing",
            message="Whisper transkribiert und liefert Wortzeitmarken",
            completed_units=0,
            total_units=len(project["chapters"]),
        )
        transcript: list[dict[str, Any]] = []
        transcript_words: list[dict[str, Any]] = []
        media_assets: list[dict[str, Any]] = []
        cursor = 0
        for index, chapter in enumerate(project["chapters"]):
            chapter_mappings = [item for item in project["mapping"] if item["chapter_id"] == chapter["id"]]
            for mapped in chapter_mappings:
                episode = episode_by_id.get(str(mapped["episode_id"]))
                if not episode:
                    raise RuntimeError(f"Plex-Episode {mapped['episode_id']} ist nicht mehr verfügbar")
                audio_path = extract_episode_audio(episode, ARTIFACTS / project_id / "source-audio" / f"episode-{episode['id']}.flac")
                duration_ms = max(episode["duration_ms"], 1)
                chunks = transcribe_with_vocarium(audio_path, project.get("audio_language", ""), duration_ms)
                duration_samples = round(duration_ms * SAMPLE_RATE / 1000)
                for chunk in chunks:
                    for segment_index, segment in enumerate(chunk["segments"]):
                        episode_start_ms = chunk["start_ms"] + segment["start_ms"]
                        episode_end_ms = min(duration_ms, chunk["start_ms"] + segment["end_ms"])
                        transcript.append({"id": new_id("ts"), "chapter_id": chapter["id"], "chapter_title": chapter["title"], "episode_id": episode["id"], "episode": episode["episode"], "episode_title": episode["title"], "chunk_index": chunk["index"], "segment_index": segment_index, "episode_start_ms": episode_start_ms, "episode_end_ms": episode_end_ms, "start_sample": cursor + round(episode_start_ms * SAMPLE_RATE / 1000), "end_sample": min(cursor + duration_samples, cursor + round(episode_end_ms * SAMPLE_RATE / 1000)), "start_ms": round((cursor + round(episode_start_ms * SAMPLE_RATE / 1000)) * 1000 / SAMPLE_RATE), "end_ms": round((cursor + round(episode_end_ms * SAMPLE_RATE / 1000)) * 1000 / SAMPLE_RATE), "text": segment["text"], "language": chunk["language"], "timestamp_source": chunk["timestamp_source"], "transcription_source": "vocarium_whisper", "confidence": 0.92 if chunk["timestamp_source"] == "whisper_segments" else 0.5})
                    for word_index, word in enumerate(chunk["words"]):
                        episode_start_ms = chunk["start_ms"] + word["start_ms"]
                        episode_end_ms = min(duration_ms, chunk["start_ms"] + word["end_ms"])
                        transcript_words.append({"id": new_id("tw"), "chapter_id": chapter["id"], "episode_id": episode["id"], "chunk_index": chunk["index"], "word_index": word_index, "episode_start_ms": episode_start_ms, "episode_end_ms": episode_end_ms, "start_sample": cursor + round(episode_start_ms * SAMPLE_RATE / 1000), "end_sample": cursor + round(episode_end_ms * SAMPLE_RATE / 1000), "text": word["text"], "language": chunk["language"], "timestamp_source": chunk["timestamp_source"]})
                media_assets.append({"chapter_id": chapter["id"], "episode_id": episode["id"], "season": episode["season"], "episode": episode["episode"], "title": episode["title"], "duration_ms": duration_ms, "audio_streams": episode["audio_streams"], "selected_audio": episode["selected_audio"], "media_count": episode["media_count"], "part_count": episode["part_count"], "derived_audio": str(audio_path.relative_to(ARTIFACTS)), "asr_chunk_count": len(chunks), "timestamp_segment_count": sum(len(chunk["segments"]) for chunk in chunks), "timestamp_word_count": sum(len(chunk["words"]) for chunk in chunks), "detected_languages": sorted({chunk["language"] for chunk in chunks}), "timestamp_sources": sorted({chunk["timestamp_source"] for chunk in chunks})})
                cursor += duration_samples
            with LOCK:
                project["transcript"] = list(transcript)
                project["transcript_words"] = list(transcript_words)
                project["media_assets"] = list(media_assets)
                save_state()
            update_run(run_id, completed_units=index + 1)
        with LOCK:
            project["transcript"] = transcript
            project["transcript_words"] = transcript_words
            project["media_assets"] = media_assets
            save_state()
        reconcile_project_subtitles(project_id, run_id, project.get("audio_language", "de"))
        if STATE["runs"][run_id].get("status") == "failed":
            raise RuntimeError(str(STATE["runs"][run_id].get("error") or "Untertitelabgleich fehlgeschlagen"))
        density = str(project.get("narration_density") or "audio_drama")
        rewrite_narration(project_id, run_id, density)
    except Exception as error:
        update_run(run_id, status="failed", error=str(error), message="Forced Alignment fehlgeschlagen")


def reconcile_project_subtitles(project_id: str, run_id: str, language: str) -> None:
    try:
        project = STATE["projects"][project_id]
        episode_ids = unique_episode_ids(project.get("mapping", []))
        update_run(run_id, status="running", stage="reconciling_subtitles", message="ASR wird mit vollständigen Plex-Untertiteln abgeglichen", completed_units=0, total_units=len(episode_ids))
        episodes = fetch_plex_episodes(str(project["binding"]["series_id"]))
        episode_by_id = {str(episode["id"]): episode for episode in episodes}
        transcript = json.loads(json.dumps(project.get("transcript", [])))
        subtitle_cues: list[dict[str, Any]] = []
        subtitle_assets: list[dict[str, Any]] = []
        project_dir = ARTIFACTS / project_id / "source-subtitles"
        for index, episode_id in enumerate(episode_ids):
            episode = episode_by_id.get(episode_id)
            if not episode:
                subtitle_assets.append({"episode_id": episode_id, "status": "missing_episode", "cue_count": 0})
                update_run(run_id, completed_units=index + 1)
                continue
            selected = select_episode_subtitle(episode, language)
            if not selected:
                subtitle_assets.append({"episode_id": episode_id, "episode_title": episode["title"], "status": "unavailable", "cue_count": 0})
                update_run(run_id, completed_units=index + 1)
                continue
            destination = project_dir / f"episode-{episode_id}-s{int(selected['index'])}.srt"
            extracted = extract_episode_subtitles(episode, destination, language)
            if not extracted:
                subtitle_assets.append({"episode_id": episode_id, "episode_title": episode["title"], "status": "unavailable", "cue_count": 0})
                update_run(run_id, completed_units=index + 1)
                continue
            subtitle_path, selected = extracted
            parsed = parse_srt(subtitle_path.read_text("utf-8", errors="replace"))
            for cue in parsed:
                cue.update({"episode_id": episode_id, "episode_title": episode["title"], "language": selected.get("language") or language, "source": "plex_embedded_srt"})
            subtitle_cues.extend(parsed)
            for segment in transcript:
                if str(segment.get("episode_id") or "") != episode_id:
                    continue
                match = transcript_subtitle_match(segment, parsed)
                segment.update(match)
                segment["reconciled_text"] = match.get("subtitle_text") if match.get("subtitle_match_status") == "matched" else segment.get("text", "")
            subtitle_assets.append({
                "episode_id": episode_id,
                "episode_title": episode["title"],
                "status": "matched" if parsed else "empty",
                "cue_count": len(parsed),
                "language": selected.get("language") or language,
                "language_code": selected.get("language_code"),
                "codec": selected.get("codec"),
                "stream_id": selected.get("id"),
                "stream_index": selected.get("index"),
                "forced": bool(selected.get("forced")),
                "title": selected.get("title"),
                "storage_name": str(subtitle_path.relative_to(ARTIFACTS)),
            })
            update_run(run_id, completed_units=index + 1)

        reconciled_transcript = build_reconciled_transcript(project, subtitle_cues)
        canonical_by_episode: dict[str, list[dict[str, Any]]] = {}
        for item in reconciled_transcript:
            canonical_by_episode.setdefault(str(item.get("episode_id") or ""), []).append(item)
        cues = json.loads(json.dumps(project.get("cues", [])))
        for cue in cues:
            episode_items = canonical_by_episode.get(str(cue.get("anchor_episode_id") or ""), [])
            if episode_items:
                anchor_ms = int(cue.get("anchor_episode_start_ms", 0))
                anchor = min(episode_items, key=lambda item: abs(int(item.get("episode_start_ms", 0)) - anchor_ms))
                cue["anchor_subtitle_cue_id"] = anchor.get("subtitle_cue_id")
                cue["anchor_text"] = anchor["text"]
                cue["anchor_text_source"] = "plex_srt_reconciled_with_whisper_words"
                cue["subtitle_match_score"] = anchor.get("subtitle_match_score")
        summary = {
            "episode_count": len(episode_ids),
            "subtitle_episode_count": sum(1 for item in subtitle_assets if item.get("status") == "matched"),
            "subtitle_cue_count": len(subtitle_cues),
            "segment_count": len(reconciled_transcript),
            "matched_segment_count": sum(1 for item in reconciled_transcript if item.get("subtitle_match_status") == "matched"),
            "consistent_segment_count": sum(1 for item in reconciled_transcript if item.get("subtitle_text_status") == "consistent"),
            "review_segment_count": sum(1 for item in reconciled_transcript if item.get("subtitle_text_status") == "different"),
            "missing_segment_count": sum(1 for item in reconciled_transcript if item.get("subtitle_match_status") == "missing"),
            "asr_segment_count": len(transcript),
            "asr_word_count": len(project.get("transcript_words", [])),
            "language": language,
            "source": "plex_embedded_srt",
            "updated_at": now(),
        }
        with LOCK:
            project["transcript"] = transcript
            project["reconciled_transcript"] = reconciled_transcript
            project["subtitle_cues"] = subtitle_cues
            project["subtitle_assets"] = subtitle_assets
            project["subtitle_reconciliation"] = summary
            project["cues"] = cues
            invalidate_automatic_quality(project)
            project["updated_at"] = now()
            warnings = [warning for warning in project.get("warnings", []) if "Untertitel" not in warning]
            warnings.append(
                f"Plex-Untertitel bilden die Lesefassung: {summary['matched_segment_count']} von {summary['segment_count']} SRT-Zeilen besitzen direkt zeitgleiche Whisper-Wörter; der ASR-Rohbefund bleibt getrennt erhalten."
            )
            project["warnings"] = warnings
            save_state()
        update_run(run_id, status="succeeded", stage="completed", message="Untertitelabgleich ist fertig", completed_units=len(episode_ids))
    except Exception as error:
        update_run(run_id, status="failed", error=str(error), message="Untertitelabgleich fehlgeschlagen")


REPAIR_LOCK_VERSION = 4


def chronological_alignments(
    items: list[dict[str, Any]],
    chunk_payload: dict[str, Any],
    chunk_episode_ids: list[str],
) -> list[dict[str, Any]]:
    """Sortiert Alignments (Alias-IDs) wie der Runner: Folge, Anker-Start, before_anchor zuerst, Cue-Reihenfolge."""
    segment_position = {
        str(item.get("id") or ""): (
            chunk_episode_ids.index(str(item.get("episode_id") or ""))
            if str(item.get("episode_id") or "") in chunk_episode_ids
            else 0,
            int(item.get("start_ms") or 0),
        )
        for item in chunk_payload.get("segments", [])
    }
    cue_position = {str(item.get("id") or ""): index for index, item in enumerate(chunk_payload.get("cues", []))}
    return sorted(
        items,
        key=lambda item: (
            segment_position.get(str(item.get("anchor_segment_id") or ""), (0, 0)),
            0 if str(item.get("placement") or "") == "before_anchor" else 1,
            cue_position.get(str(item.get("cue_id") or ""), 9999),
        ),
    )


def enforce_locked_alignments(
    result: dict[str, Any],
    chunk_payload: dict[str, Any],
    chunk_episode_ids: list[str],
) -> int:
    """Gesperrte Cues kommen wortgleich zurueck -- egal, was der Agent daraus gemacht hat.

    Ein Reparaturlauf darf nur die angeforderten Cues veraendern. Wurde ein
    gesperrter Cue umgeschrieben oder weggelassen (Band 1: fuenf Folgen komplett
    neu geschrieben, weil Sperren in der Agenten-Retry-Kette verloren gingen),
    wird die gesperrte Fassung wiederhergestellt. Rueckgabe: Anzahl Korrekturen.
    """
    locked_by_alias = {
        str(item.get("cue_id") or ""): item
        for item in chunk_payload.get("_locked_alignments", [])
        if isinstance(item, dict) and item.get("cue_id")
    }
    if not locked_by_alias:
        return 0
    answered: dict[str, dict[str, Any]] = {}
    for item in result.get("alignments", []):
        if isinstance(item, dict) and item.get("cue_id"):
            answered[str(item["cue_id"])] = item
    restored = 0
    for alias, locked in locked_by_alias.items():
        current = answered.get(alias)
        if current != locked:
            answered[alias] = json.loads(json.dumps(locked))
            restored += 1
    result["alignments"] = chronological_alignments(list(answered.values()), chunk_payload, chunk_episode_ids)
    locked_intros = [intro for intro in chunk_payload.get("_locked_character_introductions", [])
                    if isinstance(intro, dict) and intro.get("cue_id")]
    locked_names = {str(intro.get("research_name") or "").casefold() for intro in locked_intros}
    result["character_introductions"] = [
        intro for intro in result.get("character_introductions", [])
        if str(intro.get("research_name") or "").casefold() not in locked_names
        and str(intro.get("cue_id") or "") not in locked_by_alias
    ] + json.loads(json.dumps(locked_intros))
    return restored


def locked_alignments_for_repair(
    project: dict[str, Any],
    raw_cues: list[dict[str, Any]],
    repair_cue_ids: set[str],
    omit_cue_ids: set[str] | None = None,
) -> tuple[set[str], dict[str, Any], list[dict[str, Any]]]:
    """Ordnet die bestehenden Projekt-Cues den frisch erzeugten Rohcues zu.

    Rohcue-IDs sind je Lauf zufaellig; stabil sind Romanfragmente und
    Ankersegment. Zurueck kommen die Rohcue-IDs der zu reparierenden Cues,
    gesperrte Alignments je Rohcue-ID und die gesperrten Einfuehrungen.
    """
    identities = character_identity_keys(project, project.get("episode_context_research") or {})

    def key_of(cue: dict[str, Any]) -> tuple[str, str, str]:
        fragments = ",".join(sorted(str(value) for value in cue.get("source_fragment_ids", [])))
        name = "".join(normalized_character_evidence(cue.get("introduction_research_name")).split())
        return (fragments, str(cue.get("anchor_episode_id") or ""), identities.get(name, name))

    raw_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for raw in raw_cues:
        raw_by_key.setdefault(key_of(raw), []).append(raw)
    repair_raw_ids: set[str] = set()
    locked: dict[str, Any] = {}
    intro_cue_map: dict[str, str] = {}
    used_raw_ids: set[str] = set()
    for cue in project.get("cues", []):
        if str(cue.get("id") or "") in (omit_cue_ids or set()):
            continue
        candidates = [item for item in raw_by_key.get(key_of(cue), []) if str(item["id"]) not in used_raw_ids]
        raw = next((item for item in candidates if str(item.get("anchor_segment_id")) == str(cue.get("anchor_segment_id"))), None) or (candidates[0] if candidates else None)
        if raw is None:
            raw = json.loads(json.dumps(cue))
            raw["id"] = new_id("cue")
            raw_cues.append(raw)
        raw_id = str(raw["id"])
        used_raw_ids.add(raw_id)
        if str(cue.get("id")) in repair_cue_ids:
            repair_raw_ids.add(raw_id)
            continue
        locked[raw_id] = {
            "cue_id": raw_id,
            "narration_text": str(cue.get("text") or ""),
            "purpose": str(cue.get("narrative_purpose") or "visual_action"),
            "introduced_characters": list(cue.get("introduced_characters") or []),
            "information_gain": str(cue.get("information_gain") or ""),
            "source_detail_coverage": float(cue.get("source_detail_coverage") or 0),
            "source_detail_reference_count": int(cue.get("source_detail_reference_count") if cue.get("source_detail_reference_count") is not None else MIN_SOURCE_DETAIL_REFERENCE_TOKENS),
            "preserved_concrete_detail_count": int(cue.get("preserved_concrete_detail_count") or 0),
            # Defaults muessen dem Alignment-Schema entsprechen, sonst faellt der
            # gesperrte Cue bei der Agenten-Validierung durch.
            "native_audio_relation": (
                str(cue.get("native_audio_relation"))
                if cue.get("native_audio_relation") in {"complements_existing_audio", "no_relevant_existing_narration"}
                else "complements_existing_audio"
            ),
            # Sichtbare Handlungen brauchen laut Vertrag mindestens ein Bilddetail;
            # aeltere Cues tragen das Feld nicht immer, dann gilt der erste Satz.
            "retained_visual_details": list(cue.get("retained_visual_details") or [])
            or [str(cue.get("text") or "").split(". ")[0][:240]],
            "beat_type": (
                str(cue.get("beat_type"))
                if cue.get("beat_type") in {"scene_setup", "pre_action", "action_sync", "reaction", "dialogue_bridge", "scene_close"}
                else "action_sync"
            ),
            "audio_strategy": (
                str(cue.get("audio_strategy"))
                if cue.get("audio_strategy") in {"prefer_ambience_overlay", "pause_at_scene_boundary"}
                else "pause_at_scene_boundary"
            ),
            "target_duration_ms": int(cue.get("target_duration_ms") or cue.get("estimated_duration_ms") or 8000),
            "anchor_segment_id": str(cue.get("anchor_segment_id") or ""),
            "placement": str(cue.get("alignment_placement")) if cue.get("alignment_placement") in {"before_anchor", "after_anchor"} else "after_anchor",
            "confidence": max(0.82, float(cue.get("confidence") or 0.9)),
            "reasoning": str(cue.get("alignment_reasoning") or "unveraendert uebernommen"),
        }
        intro_cue_map[str(cue.get("id"))] = raw_id
    locked_intros = [
        {**intro, "cue_id": intro_cue_map[str(intro.get("cue_id"))]}
        for intro in project.get("character_introductions", [])
        if str(intro.get("cue_id")) in intro_cue_map
    ]
    return repair_raw_ids, locked, locked_intros


def timeline_gap_repair_candidates(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Create source-backed editorial tasks inside actual gaps, never generic filler."""
    timeline = dict(project.get("timeline") or {})
    clips = [c for c in timeline.get("clips", []) if c.get("track_id") == "trk_episode"]
    cues = {str(c.get("id")): c for c in project.get("cues", [])}
    fragments = {str(f["id"]): (ch, f) for ch in project.get("chapters", []) for f in ch.get("fragments", [])}
    fragment_order = list(fragments)
    fragment_positions = {fid: index for index, fid in enumerate(fragment_order)}
    segments = project.get("reconciled_transcript") or project.get("transcript", [])
    # An explicitly reviewed book edge can exclude the following book before
    # the next render. Its source evidence and anchor are stored on the cue.
    for cue in cues.values():
        if not cue.get("verified_book_boundary_anchor"):
            continue
        source_ms = int(cue.get("anchor_episode_start_ms") or 0) + BOOK_BOUNDARY_CONTEXT_MS
        for clip in clips:
            if str(clip.get("episode_id")) == str(cue.get("anchor_episode_id")) and int(clip["source_start_sample"]) <= source_ms * SAMPLE_RATE // 1000 <= int(clip["source_end_sample"]):
                timeline["duration_samples"] = min(int(timeline["duration_samples"]), int(clip["timeline_start_sample"]) + source_ms * SAMPLE_RATE // 1000 - int(clip["source_start_sample"]))
    maximum = max(30_000, int(narration_rules(project)["narration_max_gap_ms"]))
    measured = rendered_narration_gap_measurements(timeline, maximum)
    result = []
    occupied = {str(c.get("anchor_segment_id")) for c in cues.values()}
    picture_candidates = {str(c["anchor_segment_id"]): c for c in apply_reviewed_scene_evidence(project, [])}
    timed_segments = []
    for segment in segments:
        if not transcript_item_outside_intros(project, segment):
            continue
        source_sample = int(segment.get("episode_start_ms") or 0) * SAMPLE_RATE // 1000
        for clip in clips:
            if str(clip.get("episode_id")) == str(segment.get("episode_id")) and int(clip["source_start_sample"]) <= source_sample < int(clip["source_end_sample"]):
                position = round((int(clip["timeline_start_sample"]) + source_sample - int(clip["source_start_sample"])) * 1000 / SAMPLE_RATE)
                timed_segments.append((position, segment))
                break
    for gap in measured["gaps"]:
        cursor = gap["start_ms"]
        anchors = []
        while gap["end_ms"] - cursor > maximum - 15000:
            choices = [(position, segment) for position, segment in timed_segments
                       if cursor + 10000 < position <= cursor + maximum - 20000
                       and position < gap["end_ms"] and str(segment["id"]) not in occupied]
            if not choices:
                raise RuntimeError(f"Comment-gap repair has no evidenced subtitle anchor in output {cursor}–{cursor + maximum - 20000} ms")
            reviewed_choices = [(position, segment) for position, segment in choices
                                if str(segment["id"]) in picture_candidates]
            position, anchor = max(reviewed_choices or choices, key=lambda item: item[0])
            occupied.add(str(anchor["id"]))
            anchors.append(anchor)
            cursor = position
        for anchor in anchors:
            episode_id = str(anchor["episode_id"])
            source_ms = int(anchor["episode_start_ms"])
            if str(anchor["id"]) in picture_candidates:
                # Reuse the separate, stable picture task instead of fabricating
                # novel support for an anime-only scene or duplicating its cue.
                result.append({**picture_candidates[str(anchor["id"])],
                               "coverage_repair_neighbours": [str(gap[key]) for key in
                                   ("before_cue_id", "after_cue_id") if gap.get(key)]})
                continue
            neighbours = [cues.get(str(gap.get(key)), {}) for key in ("before_cue_id", "after_cue_id")]
            neighbours = [c for c in neighbours if str(c.get("anchor_episode_id")) == episode_id and c.get("source_fragment_ids") and not c.get("verified_book_boundary_anchor")]
            if not neighbours:
                neighbours = sorted([c for c in cues.values() if str(c.get("anchor_episode_id")) == episode_id and c.get("source_fragment_ids")],
                                    key=lambda c: abs(int(c.get("anchor_episode_start_ms") or 0) - source_ms))[:2]
            ids = list(dict.fromkeys(str(fid) for c in neighbours for fid in c.get("source_fragment_ids", []) if str(fid) in fragments))
            if not ids:
                # Folgen ohne eigene Passagen (Band 13, S1E115: Kapitel 151 ist
                # ihr zwar zugeordnet, alle Passagen landeten aber in der
                # Nachbarfolge) haben keine Nachbar-Cues mit Belegen. Dann
                # zaehlt die Kapitelzuordnung als Beleg: die Fragmente der
                # zugeordneten Kapitel liefern den Rohtext fuer die Luecke.
                mapped_chapters = {str(m.get("chapter_id")) for m in project.get("mapping", []) if str(m.get("episode_id")) == episode_id}
                ids = [fid for fid in fragment_order if str(fragments[fid][0].get("id")) in mapped_chapters]
            if not ids:
                # Anime-eigene Handlung ohne Romankapitel: die Luecke bleibt
                # offen, statt den ganzen Reparaturlauf abzubrechen (bis
                # 2026-09-17 ein harter Fehler, der jede weitere Reparatur des
                # Projekts verhinderte).
                note = f"Abdeckungslücke ohne Romanbeleg übersprungen: Folge {episode_id} bei {source_ms // 60000}:{(source_ms // 1000) % 60:02d}"
                warnings = project.setdefault("warnings", [])
                if note not in warnings:
                    warnings.append(note)
                continue
            # Retained neighbours may straddle source paragraphs omitted from
            # the first script. Those intervening actions are precisely what
            # the gap repair needs, not just the opening/closing statements.
            positions = [fragment_positions[fid] for fid in ids]
            ids = fragment_order[min(positions):max(positions) + 1]
            source = "\n".join(str(fragments[fid][1].get("text") or "") for fid in ids)
            if anchor.get("visual_scene_anchor") and anchor.get("visual_evidence"):
                source += "\nBildbeleg aus der Originalfolge: " + str(anchor["visual_evidence"])
            chapter = fragments[ids[0]][0]
            anchor_ms = int(anchor["episode_start_ms"])
            result.append({"id": new_id("cue"), "chapter_id": chapter["id"], "chapter_title": chapter["title"],
                           "text": source[:1100], "editorial_source_context": source, "source_fragment_ids": ids,
                           "anchor_episode_id": episode_id, "anchor_segment_id": anchor["id"],
                           "anchor_episode_start_ms": anchor_ms, "anchor_insert_ms": anchor_ms,
                           "anchor_text": str(anchor.get("reconciled_text") or anchor.get("text") or ""),
                           "confidence": 0.9, "estimated_duration_ms": 10000, "revision": 1,
                           "coverage_repair_window": {"start_ms": source_ms - 3000, "end_ms": source_ms + 3000},
                           "coverage_repair_neighbours": [str(c.get("id")) for c in neighbours]})
    return result


def coverage_gap_repair_ids(
    project: dict[str, Any],
    targets: dict[str, dict[str, Any]],
    locked: dict[str, Any],
) -> set[str]:
    """Waehlt Rohcues, die zu lange Strecken ohne Kommentar schliessen.

    Die gesperrten Alignments bilden das bestehende Raster; jede Luecke ueber
    der Hoechstlaenge wird von links greedy mit dem jeweils letzten noch
    erreichbaren Kandidaten gefuellt. Unterschreitet eine Folge danach noch
    die Mindestzahl, kommen die Kandidaten mit dem groessten Abstand zu allen
    bereits gesetzten Kommentaren hinzu.
    """
    rules = narration_rules(project)
    max_gap_ms = max(30_000, int(rules["narration_max_gap_ms"]))
    # Der Rohcue-Anker ist nur ein Vorschlag; gesetzt wird ein Kommentar an dem
    # Segment, das die Ausrichtung gewaehlt hat. Erst das ergibt das echte Raster.
    segment_start_ms = {
        str(item.get("id") or ""): int(item.get("episode_start_ms") or 0)
        for item in (project.get("reconciled_transcript") or project.get("transcript", []))
    }
    rendered_starts = {
        str(cue.get("anchor_segment_id") or ""): round(int(cue["placement_sample_in_episode"]) * 1000 / SAMPLE_RATE)
        for cue in project.get("cues", []) if cue.get("placement_sample_in_episode") is not None
    }
    fragments = source_content_fragments(project)
    boundary_segments = {
        edge: {str(cue.get("anchor_segment_id") or "") for cue in project.get("cues", [])
               if str(fragment.get("id") or "") in cue.get("source_fragment_ids", [])}
        for edge, fragment in ([('start', fragments[0]), ('end', fragments[-1])] if fragments else [])
    }
    chosen: set[str] = set()
    for episode_id, target in targets.items():
        anchors = {str(cue_id): int(ms) for cue_id, ms in dict(target.get("candidate_anchors") or {}).items()}
        if not anchors:
            continue
        coverage_ids = set(anchors if target.get("coverage_candidate_ids") is None else target["coverage_candidate_ids"])
        locked_segments = {cue_id: str((locked.get(cue_id) or {}).get("anchor_segment_id") or "")
                           for cue_id in anchors if cue_id in locked}
        positions = {cue_id: rendered_starts.get(segment_id, segment_start_ms.get(segment_id, anchors[cue_id]))
                     for cue_id, segment_id in locked_segments.items()}
        rendered_sources = {segment_start_ms[segment_id]: positions[cue_id]
                            for cue_id, segment_id in locked_segments.items() if segment_id in segment_start_ms}
        anchors = {cue_id: rendered_sources.get(home, home) for cue_id, home in anchors.items()}
        book_edge = str(target.get("book_edge_episode") or "none")
        start_locked = book_edge in {"start", "both"} and bool(set(locked_segments.values()) & boundary_segments.get("start", set()))
        end_locked = book_edge in {"end", "both"} and bool(set(locked_segments.values()) & boundary_segments.get("end", set()))
        # A selected cue can itself be the missing source beat when its current
        # anchor drifted out of the gap. Let the planner explicitly unlock it;
        # otherwise the runner requests a repair that engine locks forbid.
        while True:
            placed = sorted(positions.values())
            inner = placed[1:] if start_locked and len(placed) > 1 else placed
            if end_locked and len(inner) > 1:
                inner = inner[:-1]
            edges = [
                *([] if start_locked else [min([*anchors.values(), *positions.values()])]),
                *inner,
                *([] if end_locked else [max([*anchors.values(), *positions.values()])]),
            ]
            candidate = None
            for left, right in zip(edges, edges[1:]):
                if right - left <= max_gap_ms:
                    continue
                inside = sorted((ms, cue_id) for cue_id, ms in anchors.items()
                                if left < ms < right and cue_id not in chosen and cue_id in coverage_ids)
                if inside:
                    reachable = [item for item in inside if item[0] - left <= max_gap_ms]
                    free_reachable = [item for item in reachable if item[1] not in locked]
                    candidate = (free_reachable[-1] if free_reachable else
                                 reachable[-1] if reachable else inside[0])
                    break
            if candidate is None:
                break
            ms, cue_id = candidate
            chosen.add(cue_id)
            positions[cue_id] = ms
        needed = int(target.get("min_cues") or 0)
        remaining = sorted((ms, cue_id) for cue_id, ms in anchors.items() if cue_id not in positions)
        while len(positions) < needed and remaining:
            best = max(remaining, key=lambda item: min((abs(item[0] - value) for value in positions.values()), default=10**12))
            remaining.remove(best)
            chosen.add(best[1])
            positions[best[1]] = best[0]
    return chosen


def rewrite_narration(project_id: str, run_id: str, density: str, repair_cue_ids: set[str] | None = None, repair_reason: str = "", repair_coverage: bool = False, repair_introductions: bool = False) -> None:
    project = STATE["projects"][project_id]
    script_saved = False
    previous = {
        "cues": json.loads(json.dumps(project.get("cues", []))),
        "semantic_cues": json.loads(json.dumps(project.get("semantic_cues", []))),
        "narration_density": project.get("narration_density"),
        "narration_density_contract": json.loads(json.dumps(project.get("narration_density_contract"))),
        "narration_name_lexicon": json.loads(json.dumps(project.get("narration_name_lexicon", []))),
        "narration_rules": json.loads(json.dumps(project.get("narration_rules", default_narration_rules()))),
        "narration_editor_version": project.get("narration_editor_version"),
        "episode_context_research": json.loads(json.dumps(project.get("episode_context_research"))),
        "narration_script_research": json.loads(json.dumps(project.get("narration_script_research"))),
        "character_introductions": json.loads(json.dumps(project.get("character_introductions", []))),
        "timeline": json.loads(json.dumps(project.get("timeline"))),
        "artifacts": json.loads(json.dumps(project.get("artifacts", []))),
        "stage": project.get("stage"),
        "status": project.get("status"),
        "warnings": list(project.get("warnings", [])),
    }
    try:
        episode_ids = unique_episode_ids(project.get("mapping", []))
        cached_audio = {
            episode_id: ARTIFACTS / project_id / "source-audio" / f"episode-{episode_id}.flac"
            for episode_id in episode_ids
        }
        if episode_ids and all(path.exists() for path in cached_audio.values()):
            update_run(
                run_id,
                status="running",
                stage="detecting_intros",
                message="Commercial-Bumper, Abspann und Episodenvorschau werden vor dem Skripting ausgeschlossen",
            )
            commercial_bumper_detection = detect_project_commercial_bumpers(project, cached_audio)
            ending_detection = detect_project_endings(project, cached_audio)
            with LOCK:
                project["commercial_bumper_detection"] = commercial_bumper_detection
                project["ending_detection"] = ending_detection
                project["updated_at"] = now()
                save_state()
        update_run(run_id, status="running", stage="writing", message="Handlung wird in regelmäßige Erzählerpassagen gegliedert")
        fresh_scene_alignment_required = narration_requires_fresh_scene_alignment(project)
        semantic_cues = json.loads(json.dumps(project.get("semantic_cues", [])))
        if density != str(project.get("narration_density") or "detailed"):
            semantic_cues = []
            fresh_scene_alignment_required = True
        if repair_cue_ids or repair_coverage or repair_introductions:
            # Reparaturlauf: Rohcues frisch erzeugen, aber nur die beanstandeten
            # neu schreiben lassen; der Rest bleibt gesperrt.
            semantic_cues = []
            fresh_scene_alignment_required = True
        scene_alignment_execution = None
        episode_context_research = json.loads(json.dumps(project.get("episode_context_research") or {}))
        narration_script_research = json.loads(json.dumps(project.get("narration_script_research") or {}))
        if fresh_scene_alignment_required:
            semantic_cues = []
            update_run(
                run_id,
                message="Szenenskript ist veraltet oder unzureichend belegt; es wird vollständig redaktionell erneuert",
            )
        if not semantic_cues and len(project.get("cues", [])) >= 70 and not fresh_scene_alignment_required:
            semantic_cues = json.loads(json.dumps(project["cues"]))
        if semantic_cues:
            update_run(run_id, message=f"{len(semantic_cues)} belegte Handlungspassagen werden auf Musik- und Bildpausen verteilt")
            cues = replan_semantic_cues_for_overlay(project, semantic_cues)
        else:
            cues = generate_narration_cues(project, density)
            if not cues:
                raise RuntimeError("Aus Romanfragmenten und Transkript konnten keine Erzähler-Cues erzeugt werden")
            if episode_context_research_is_current(project, episode_context_research):
                update_run(
                    run_id,
                    stage="writing",
                    message=f"Belegter Online-Kontext für {len(mapped_episode_descriptors(project))} Folgen wird wiederverwendet",
                )
            else:
                context_episode_total = len(mapped_episode_descriptors(project))
                update_run(
                    run_id,
                    stage="researching_context",
                    message=f"Codex recherchiert den belegten Kontext für {context_episode_total} Folgen",
                    # Eigene Zaehlung: sonst blieben Einheiten und Prozentsatz
                    # auf dem Endstand der Zuordnung stehen und meldeten 100 %,
                    # waehrend die Recherche noch lief oder bereits scheiterte.
                    completed_units=0,
                    total_units=max(context_episode_total, 1),
                )

                def store_episode_context(snapshot: dict[str, Any]) -> None:
                    with LOCK:
                        project["episode_context_research"] = json.loads(json.dumps(snapshot))
                        project["updated_at"] = now()
                        save_state()
                    update_run(
                        run_id,
                        stage="researching_context",
                        completed_units=len(snapshot.get("episode_contexts") or []),
                    )

                episode_context_research = research_episode_context(
                    project,
                    progress=lambda message: update_run(run_id, stage="researching_context", message=message),
                    persist=store_episode_context,
                )
                store_episode_context(episode_context_research)
            episode_context_research = normalize_character_introduction_contexts(project, episode_context_research)
            cues = add_dedicated_character_introduction_candidates(
                project,
                cues,
                episode_context_research,
                force=repair_introductions,
                preserve_existing=bool(repair_cue_ids or repair_coverage or repair_introductions),
            )
            cues = apply_reviewed_scene_evidence(project, cues)
            update_run(
                run_id,
                stage="writing",
                message=f"{len(cues)} Rohpassagen werden auf Informationswert, Figurenführung und rechtzeitiges Timing redigiert",
            )
            raw_candidate_counts = Counter(
                str(cue.get("anchor_episode_id") or "") for cue in cues
            )
            raw_coverage_targets = narration_coverage_targets(project, cues)
            repair_scope = None
            if repair_cue_ids or repair_coverage or repair_introductions:
                omitted = obsolete_introduction_candidate_ids(project, episode_context_research) & set(repair_cue_ids or set())
                repair_raw_ids, locked, locked_intros = locked_alignments_for_repair(
                    project, cues, set(repair_cue_ids or set()), omit_cue_ids=omitted,
                )
                if repair_introductions:
                    repair_raw_ids.update(str(cue["id"]) for cue in cues
                                          if cue.get("character_introduction_candidate") and str(cue["id"]) not in locked)
                if repair_coverage:
                    gap_candidates = timeline_gap_repair_candidates(project)
                    existing_candidate_ids = {str(cue["id"]) for cue in cues}
                    cues.extend(cue for cue in gap_candidates if str(cue["id"]) not in existing_candidate_ids)
                    repair_raw_ids.update(str(cue["id"]) for cue in gap_candidates)
                raw_candidate_counts = Counter(str(cue.get("anchor_episode_id") or "") for cue in cues)
                raw_coverage_targets = narration_coverage_targets(project, cues)
                gap_ids: set[str] = set()
                if repair_coverage:
                    gap_ids = coverage_gap_repair_ids(project, raw_coverage_targets, locked) - repair_raw_ids
                    repair_raw_ids |= gap_ids
                    for cue_id in gap_ids:
                        locked.pop(cue_id, None)
                    if gap_ids:
                        repair_reason = (
                            f"{repair_reason} | {len(gap_ids)} zusätzliche oder umplatzierte Cues schließen zu lange "
                            "Kommentarlücken bzw. füllen die Mindestzahl je Folge auf; sie sind verpflichtend "
                            "und dürfen nicht weggelassen werden."
                        )[:1500]
                if not repair_raw_ids and not omitted:
                    raise RuntimeError("Reparaturlauf: keiner der beanstandeten Cues ließ sich den frischen Rohcues zuordnen")
                update_run(run_id, message=f"Reparaturlauf: {len(repair_raw_ids)} Cues werden neu geschrieben ({len(gap_ids)} für Kommentarlücken), {len(locked)} bleiben erhalten; {len(omitted)} veraltete Einführungskandidaten entfallen")
                repair_scope = {"cue_ids": repair_raw_ids, "locked": locked, "locked_introductions": locked_intros, "reason": repair_reason}
            scene_alignment = research_scene_alignment(
                project,
                cues,
                episode_context_research,
                density,
                repair=repair_scope,
                progress=lambda message, completed, total: update_run(
                    run_id, stage="writing", message=message,
                    completed_units=completed, total_units=total,
                ),
            )
            narration_script_research = json.loads(json.dumps(scene_alignment))
            scene_alignment_execution = scene_alignment.get("execution")
            discovered_names = scene_alignment.get("name_aliases", [])
            project["narration_name_lexicon"] = merge_narration_name_lexicon(
                project.get("narration_name_lexicon", []),
                discovered_names,
            )
            cues = apply_researched_scene_alignment(project, cues, scene_alignment)
            for cue in cues:
                cue["text"] = canonicalize_narration_names(
                    str(cue.get("text") or ""),
                    project["narration_name_lexicon"],
                )
            semantic_cues = json.loads(json.dumps(cues))
            narration_density_contract = {
                "editorial_contract_version": NARRATION_EDITOR_VERSION,
                "density": density,
                "candidate_count": sum(raw_candidate_counts.values()),
                "candidate_counts": dict(raw_candidate_counts),
                "coverage_targets": json.loads(json.dumps(raw_coverage_targets)),
                "selected_count": len(cues),
                "selected_counts": dict(
                    Counter(str(cue.get("anchor_episode_id") or "") for cue in cues)
                ),
            }
            boundary_plan = book_boundary_plan({
                **project,
                "cues": cues,
                "semantic_cues": semantic_cues,
            })
            boundary_project = {
                **project,
                "cues": cues,
                "semantic_cues": semantic_cues,
                "narration_editor_version": NARRATION_EDITOR_VERSION,
                "quality_report": None,
            }
            if (
                boundary_plan.get("status") == "review"
                or narration_requires_fresh_scene_alignment(boundary_project)
            ):
                detail = (
                    str(boundary_plan.get("detail") or "Buchanfang oder Buchende ist nicht belegt")
                    if boundary_plan.get("status") == "review"
                    else "Buchanfang oder Buchende unterschreitet die Grounding-Mindestkonfidenz"
                )
                raise RuntimeError(detail)
        with LOCK:
            project["cues"] = cues
            project["semantic_cues"] = semantic_cues
            if scene_alignment_execution:
                project["scene_alignment_execution"] = scene_alignment_execution
            project["episode_context_research"] = episode_context_research
            project["narration_script_research"] = narration_script_research
            project["character_introductions"] = narration_script_research.get("character_introductions", [])
            project["narration_editor_version"] = NARRATION_EDITOR_VERSION
            project["narration_rules"] = narration_rules(project)
            project["narration_density"] = density
            if "narration_density_contract" in locals():
                project["narration_density_contract"] = narration_density_contract
            project["timeline_revision"] = max(int(project.get("timeline_revision", 0)), int((project.get("timeline") or {}).get("revision", 0)))
            project["timeline"] = None
            project["audio_stale"] = bool(project.get("artifacts"))
            invalidate_automatic_quality(project)
            project["stage"] = 5
            project["status"] = "script_review"
            project["updated_at"] = now()
            project["warnings"] = [
                "Codex hat pro Folge belegten Online-Kontext recherchiert und die Roman-Rohpassagen auf Figurenführung, Informationswert und Timing redigiert. Lokale Plex-Untertitel und Forced-Aligner-Zeitstempel bleiben die alleinigen Audioanker."
            ]
            for episode_id in scene_alignment.get("uncovered_episode_ids") or []:
                label = next(
                    (
                        f"S{int(item.get('season') or 0)}E{int(item.get('episode') or 0):02d} „{item.get('episode_title') or ''}“"
                        for item in project.get("mapping", [])
                        if str(item.get("episode_id") or "") == str(episode_id)
                    ),
                    str(episode_id),
                )
                project["warnings"].append(
                    f"Folge {label} bleibt ohne Erzählpassagen: Keine Roman-Rohpassage wurde ihr zugeordnet "
                    "(das Kapitel läuft in der Nachbarfolge oder die Folge ist Anime-eigene Handlung). "
                    "Bei Bedarf die Kapitelzuordnung prüfen."
                )
            save_state()
        script_saved = True
        update_run(run_id, total_units=len(cues), completed_units=0, message=f"{len(cues)} Erzählerpassagen werden vertont")
        render_project(project_id, run_id)
    except Exception as error:
        if script_saved:
            # The script survived its own gates and is persisted; a render
            # failure must not throw away an hour of agent work. The project
            # stays in script_review and can be rendered again.
            update_run(run_id, status="failed", error=str(error), message="Render fehlgeschlagen; das Erzähler-Skript bleibt erhalten")
            return
        with LOCK:
            reusable_episode_context = json.loads(json.dumps(episode_context_research))
            project.update(previous)
            if episode_context_research_is_current(project, reusable_episode_context):
                project["episode_context_research"] = reusable_episode_context
            project["updated_at"] = now()
            save_state()
        update_run(run_id, status="failed", error=str(error), message="Erzähler-Skript konnte nicht erneuert werden")


def render_only(project_id: str, run_id: str) -> None:
    try:
        update_run(run_id, status="running")
        render_project(project_id, run_id)
    except Exception as error:
        update_run(run_id, status="failed", error=str(error), message="Render fehlgeschlagen")


def concise_narration(text: str, title: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    chosen = " ".join(sentences[:2]).strip()
    if len(chosen) > 420:
        chosen = chosen[:417].rsplit(" ", 1)[0] + "…"
    return chosen or f"Die Handlung von {title} beginnt."


def next_timeline_revision(project: dict[str, Any]) -> int:
    return max(int(project.get("timeline_revision", 0)), int((project.get("timeline") or {}).get("revision", 0))) + 1


def inspect_delivery_audio(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.exists():
        return {"status": "unavailable", "error": "FFprobe oder Delivery-Datei fehlt"}
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_name,sample_rate,channels",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        payload = json.loads(result.stdout)
        stream = next(
            (item for item in payload.get("streams", []) if item.get("sample_rate")),
            (payload.get("streams") or [{}])[0],
        )
        return {
            "status": "inspected",
            "codec": str(stream.get("codec_name") or ""),
            "sample_rate": int(stream.get("sample_rate") or 0),
            "channels": int(stream.get("channels") or 0),
            "duration_ms": round(float((payload.get("format") or {}).get("duration") or 0) * 1000),
        }
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as error:
        return {"status": "unavailable", "error": str(error)[:300]}


def _quality_check(
    check_id: str,
    label: str,
    passed: bool,
    detail: str,
    *,
    actual: Any = None,
    expected: Any = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": "passed" if passed else "failed",
        "detail": detail,
        "actual": actual,
        "expected": expected,
    }


def _source_coverage_result(project: dict[str, Any], timeline: dict[str, Any]) -> tuple[bool, str]:
    expected_episode_ids = unique_episode_ids(project.get("mapping", []))
    clips_by_episode: dict[str, list[tuple[int, int]]] = {}
    for clip in timeline.get("clips", []):
        episode_id = str(clip.get("episode_id") or "")
        if not episode_id or clip.get("track_id") != "trk_episode":
            continue
        clips_by_episode.setdefault(episode_id, []).append(
            (int(clip.get("source_start_sample", 0)), int(clip.get("source_end_sample", 0)))
        )
    if set(clips_by_episode) != set(expected_episode_ids):
        return False, f"Erwartet {len(expected_episode_ids)} Folgen, Timeline enthält {len(clips_by_episode)}"
    source_lengths = {
        str(episode_id): int(samples)
        for episode_id, samples in dict(timeline.get("source_episode_samples") or {}).items()
    }
    excluded_by_episode = dict(timeline.get("excluded_source_intervals") or {})
    intro_duration_samples = 0
    commercial_bumper_duration_samples = 0
    ending_duration_samples = 0
    boundary_duration_samples = 0
    for episode_id in expected_episode_ids:
        intervals = sorted(clips_by_episode.get(episode_id, []))
        if not intervals:
            return False, f"Folge {episode_id} besitzt keine Quellclips"
        merged = []
        for start, end in intervals:
            if end <= start:
                return False, f"Folge {episode_id} enthält einen leeren oder rückwärts laufenden Quellclip"
            if merged and start < merged[-1][1]:
                return False, f"Folge {episode_id} enthält bei Sample {start} doppelte Quellabdeckung"
            if merged and start == merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        expected_end = source_lengths.get(episode_id, merged[-1][1])
        exclusions = [
            (int(item.get("start_sample", 0)), int(item.get("end_sample", 0)))
            for item in excluded_by_episode.get(episode_id, [])
        ]
        allowed = source_ranges_after_exclusions(0, expected_end, exclusions)
        if merged != allowed:
            return False, f"Folge {episode_id} weicht außerhalb der belegten Introintervalle von der Quellspur ab"
        for item in excluded_by_episode.get(episode_id, []):
            duration = int(item.get("end_sample", 0)) - int(item.get("start_sample", 0))
            if str(item.get("reason") or "").startswith("book_boundary_"):
                boundary_duration_samples += max(0, duration)
            elif item.get("reason") == "ending_and_next_episode_preview":
                ending_duration_samples += max(0, duration)
            elif item.get("reason") == "repeated_commercial_bumper":
                commercial_bumper_duration_samples += max(0, duration)
            else:
                intro_duration_samples += max(0, duration)
    removed_seconds = intro_duration_samples / SAMPLE_RATE
    commercial_bumper_seconds = commercial_bumper_duration_samples / SAMPLE_RATE
    ending_seconds = ending_duration_samples / SAMPLE_RATE
    boundary_seconds = boundary_duration_samples / SAMPLE_RATE
    return True, (
        f"{len(expected_episode_ids)} eindeutige Folgen außerhalb von "
        f"{removed_seconds:.1f} s belegter Introspuren, "
        f"{commercial_bumper_seconds:.1f} s Commercial-Bumper, "
        f"{ending_seconds:.1f} s Abspann/Vorschau "
        f"und {boundary_seconds:.1f} s "
        "belegter Buchränder lückenlos und jeweils genau einmal"
    )


def _book_boundary_result(project: dict[str, Any], timeline: dict[str, Any]) -> tuple[bool, str]:
    durations = {
        str(episode_id): round(int(samples) * 1000 / SAMPLE_RATE)
        for episode_id, samples in dict(timeline.get("source_episode_samples") or {}).items()
    }
    plan = book_boundary_plan(project, durations)
    if plan["status"] == "not_applicable":
        return True, str(plan["detail"])
    if plan["status"] != "passed":
        return False, str(plan["detail"])
    expected = {
        (
            episode_id,
            round(int(item["start_ms"]) * SAMPLE_RATE / 1000),
            round(int(item["end_ms"]) * SAMPLE_RATE / 1000),
            str(item["reason"]),
        )
        for episode_id, items in plan["exclusions"].items()
        for item in items
    }
    actual = {
        (
            str(episode_id),
            int(item.get("start_sample", 0)),
            int(item.get("end_sample", 0)),
            str(item.get("reason") or ""),
        )
        for episode_id, items in dict(timeline.get("excluded_source_intervals") or {}).items()
        for item in items
        if str(item.get("reason") or "").startswith("book_boundary_")
    }
    return expected == actual, (
        str(plan["detail"])
        if expected == actual
        else f"{len(actual)} von {len(expected)} belegten Buchrand-Schnitten stimmen exakt"
    )


def _intro_removal_result(project: dict[str, Any], timeline: dict[str, Any]) -> tuple[bool, str]:
    episode_count = len(unique_episode_ids(project.get("mapping", [])))
    detection = project.get("intro_detection") or {}
    if episode_count < 3:
        return True, "Weniger als drei Folgen; Wiederholungserkennung nicht anwendbar"
    occurrences = list(detection.get("occurrences") or [])
    required = int(detection.get("required_occurrences") or max(3, math.ceil(episode_count * INTRO_MIN_EPISODE_RATIO)))
    if detection.get("status") != "detected" or len(occurrences) < required:
        return False, f"Keine belastbare wiederkehrende Introspur in mindestens {required} von {episode_count} Folgen erkannt"

    exclusions_by_episode = dict(timeline.get("excluded_source_intervals") or {})
    clips_by_episode: dict[str, list[tuple[int, int]]] = {}
    for clip in timeline.get("clips", []):
        if clip.get("track_id") != "trk_episode" or not clip.get("episode_id"):
            continue
        clips_by_episode.setdefault(str(clip["episode_id"]), []).append(
            (int(clip.get("source_start_sample", 0)), int(clip.get("source_end_sample", 0)))
        )
    invalid = []
    for occurrence in occurrences:
        episode_id = str(occurrence.get("episode_id") or "")
        expected_start = round(int(occurrence.get("start_ms", 0)) * SAMPLE_RATE / 1000)
        expected_end = round(int(occurrence.get("end_ms", 0)) * SAMPLE_RATE / 1000)
        exclusions = [
            (int(item.get("start_sample", 0)), int(item.get("end_sample", 0)))
            for item in exclusions_by_episode.get(episode_id, [])
        ]
        exact = any(abs(start - expected_start) <= 1 and abs(end - expected_end) <= 1 for start, end in exclusions)
        overlaps_source = any(start < expected_end and end > expected_start for start, end in clips_by_episode.get(episode_id, []))
        if not exact or overlaps_source:
            invalid.append(episode_id)
    removed_ms = sum(int(item.get("duration_ms", 0)) for item in occurrences)
    return not invalid, (
        f"{len(occurrences)} wiederkehrende Introspuren ({removed_ms / 1000:.1f} s) vollständig entfernt"
        if not invalid
        else f"{len(invalid)} von {len(occurrences)} erkannten Introspuren sind nicht exakt aus der Timeline ausgeschlossen"
    )


def _ending_removal_result(project: dict[str, Any], timeline: dict[str, Any]) -> tuple[bool, str]:
    expected_episode_ids = set(unique_episode_ids(project.get("mapping", [])))
    episode_count = len(expected_episode_ids)
    detection = project.get("ending_detection") or {}
    if episode_count < 3:
        return True, "Weniger als drei Folgen; Endsequenz-Erkennung nicht anwendbar"
    occurrences = list(detection.get("occurrences") or [])
    required = int(
        detection.get("required_occurrences")
        or max(3, math.ceil(episode_count * INTRO_MIN_EPISODE_RATIO))
    )
    occurrence_episode_ids = {str(item.get("episode_id") or "") for item in occurrences}
    if (
        detection.get("status") != "detected"
        or len(occurrences) < required
        or occurrence_episode_ids != expected_episode_ids
    ):
        return False, (
            f"Abspann-/Vorschau-Endblock nur in {len(occurrence_episode_ids)} von "
            f"{episode_count} Folgen belastbar erkannt"
        )
    exclusions_by_episode = dict(timeline.get("excluded_source_intervals") or {})
    clips_by_episode: dict[str, list[tuple[int, int]]] = {}
    for clip in timeline.get("clips", []):
        if clip.get("track_id") == "trk_episode" and clip.get("episode_id"):
            clips_by_episode.setdefault(str(clip["episode_id"]), []).append(
                (int(clip.get("source_start_sample", 0)), int(clip.get("source_end_sample", 0)))
            )
    invalid = []
    for occurrence in occurrences:
        episode_id = str(occurrence.get("episode_id") or "")
        expected_start = round(int(occurrence.get("start_ms", 0)) * SAMPLE_RATE / 1000)
        expected_end = round(int(occurrence.get("end_ms", 0)) * SAMPLE_RATE / 1000)
        exact = any(
            item.get("reason") == "ending_and_next_episode_preview"
            and abs(int(item.get("start_sample", 0)) - expected_start) <= 1
            and abs(int(item.get("end_sample", 0)) - expected_end) <= 1
            for item in exclusions_by_episode.get(episode_id, [])
        )
        overlaps_source = any(
            start < expected_end and end > expected_start
            for start, end in clips_by_episode.get(episode_id, [])
        )
        if not exact or overlaps_source:
            invalid.append(episode_id)
    removed_ms = sum(int(item.get("duration_ms", 0)) for item in occurrences)
    return not invalid, (
        f"{len(occurrences)} Abspann-/Vorschau-Endblöcke ({removed_ms / 1000:.1f} s) vollständig entfernt"
        if not invalid
        else f"{len(invalid)} von {len(occurrences)} Endblöcken sind nicht exakt aus der Timeline ausgeschlossen"
    )


def _commercial_bumper_removal_result(
    project: dict[str, Any],
    timeline: dict[str, Any],
) -> tuple[bool, str]:
    expected_episode_ids = set(unique_episode_ids(project.get("mapping", [])))
    episode_count = len(expected_episode_ids)
    detection = project.get("commercial_bumper_detection") or {}
    if episode_count < 3:
        return True, "Weniger als drei Folgen; Commercial-Bumper-Erkennung nicht anwendbar"
    occurrences = list(detection.get("occurrences") or [])
    occurrence_episode_ids = {str(item.get("episode_id") or "") for item in occurrences}
    if detection.get("status") != "detected" or occurrence_episode_ids != expected_episode_ids:
        return False, (
            f"Commercial-Bumper nur in {len(occurrence_episode_ids)} von "
            f"{episode_count} Folgen belastbar erkannt"
        )
    exclusions_by_episode = dict(timeline.get("excluded_source_intervals") or {})
    clips_by_episode: dict[str, list[tuple[int, int]]] = {}
    for clip in timeline.get("clips", []):
        if clip.get("track_id") == "trk_episode" and clip.get("episode_id"):
            clips_by_episode.setdefault(str(clip["episode_id"]), []).append(
                (int(clip.get("source_start_sample", 0)), int(clip.get("source_end_sample", 0)))
            )
    invalid = []
    for occurrence in occurrences:
        episode_id = str(occurrence.get("episode_id") or "")
        expected_start = round(int(occurrence.get("start_ms", 0)) * SAMPLE_RATE / 1000)
        expected_end = round(int(occurrence.get("end_ms", 0)) * SAMPLE_RATE / 1000)
        exact = any(
            item.get("reason") == "repeated_commercial_bumper"
            and abs(int(item.get("start_sample", 0)) - expected_start) <= 1
            and abs(int(item.get("end_sample", 0)) - expected_end) <= 1
            for item in exclusions_by_episode.get(episode_id, [])
        )
        overlaps_source = any(
            start < expected_end and end > expected_start
            for start, end in clips_by_episode.get(episode_id, [])
        )
        if not exact or overlaps_source:
            invalid.append(episode_id)
    removed_ms = sum(int(item.get("duration_ms", 0)) for item in occurrences)
    return not invalid, (
        f"{len(occurrences)} Commercial-Bumper ({removed_ms / 1000:.1f} s) vollständig entfernt"
        if not invalid
        else (
            f"{len(invalid)} von {len(occurrences)} Commercial-Bumpern sind "
            "nicht exakt aus der Timeline ausgeschlossen"
        )
    )


def _overlay_safety_result(project: dict[str, Any]) -> tuple[bool, str]:
    speech_by_episode: dict[str, list[tuple[int, int]]] = {}
    for item in [*(project.get("reconciled_transcript") or []), *project.get("transcript_words", [])]:
        if not transcript_item_has_spoken_content(item):
            continue
        episode_id = str(item.get("episode_id") or "")
        start = int(item.get("episode_start_ms", item.get("start_ms", 0))) - 250
        end = int(item.get("episode_end_ms", item.get("end_ms", start))) + 250
        if episode_id and end > start:
            speech_by_episode.setdefault(episode_id, []).append((start, end))
    overlay_policies = {"overlay_speech_free", "overlay_intro_music", "overlay_opening_gap"}
    overlays = [cue for cue in project.get("cues", []) if cue.get("placement_policy") in overlay_policies]
    unsafe = []
    for cue in overlays:
        episode_id = str(cue.get("anchor_episode_id") or "")
        placement_sample = cue.get("placement_sample_in_episode")
        if placement_sample is None:
            unsafe.append(str(cue.get("id") or "unbekannt"))
            continue
        start = round(int(placement_sample) * 1000 / SAMPLE_RATE)
        end = start + int(cue.get("measured_duration_ms", 0))
        gap_start = int(cue.get("safe_gap_start_ms", start))
        gap_end = int(cue.get("safe_gap_end_ms", end))
        if (
            start < gap_start
            or end > gap_end
            or any(speech_start < end and speech_end > start for speech_start, speech_end in speech_by_episode.get(episode_id, []))
        ):
            unsafe.append(str(cue.get("id") or "unbekannt"))
    return not unsafe, (
        f"{len(overlays)} Overlays vollständig innerhalb belegter Sprechpausen"
        if not unsafe
        else f"{len(unsafe)} von {len(overlays)} Overlays verletzen ihre Sprechpause"
    )


def _narration_anchor_proximity_result(project: dict[str, Any]) -> tuple[bool, str]:
    rules = narration_rules(project)
    maximum_ms = int(rules["max_anchor_distance_ms"])
    segments = {
        str(item.get("id") or ""): item
        for item in project.get("reconciled_transcript") or project.get("transcript", [])
    }
    checked = 0
    invalid = []
    ignored_policies = {"replace_existing_narration", "overlay_intro_music", "overlay_opening_gap"}
    for cue in project.get("cues", []):
        policy = str(cue.get("placement_policy") or "")
        if policy in ignored_policies:
            continue
        anchor = segments.get(str(cue.get("anchor_segment_id") or ""))
        placement_sample = cue.get("placement_sample_in_episode")
        if not anchor or placement_sample is None:
            invalid.append(f"{cue.get('id')}: Zeitanker fehlt")
            continue
        checked += 1
        boundary_ms = aligned_scene_boundary_ms(cue, anchor)
        audible_start_ms = round(int(placement_sample) * 1000 / SAMPLE_RATE)
        audible_end_ms = audible_start_ms + int(cue.get("measured_duration_ms") or 0)
        before = cue.get("alignment_placement") == "before_anchor"
        if policy == ALIGNED_SCENE_INSERT_POLICY:
            distance_ms = abs(audible_start_ms - boundary_ms)
        elif before:
            distance_ms = boundary_ms - audible_end_ms
        else:
            distance_ms = audible_start_ms - boundary_ms
        if distance_ms < 0 or distance_ms > maximum_ms:
            invalid.append(
                f"{cue.get('id')}: {abs(distance_ms)} ms "
                f"{'auf der falschen Seite' if distance_ms < 0 else 'vom Szenenanker entfernt'}"
            )
    return not invalid, (
        f"{checked} Kommentare höchstens {maximum_ms / 1000:.0f} s vom belegten Szenenanker entfernt"
        if not invalid
        else f"{len(invalid)} unpassende Kommentarpositionen: {'; '.join(invalid[:6])}"
    )


def rendered_narration_gap_measurements(timeline: dict[str, Any], max_gap_ms: int) -> dict[str, Any]:
    """Measure actual uncommented output, including transitions and retained book edges.

    Source candidates are planning hints and cannot exempt any audible interval.
    Merge overlapping narration clips and measure from their ends, not starts.
    """
    duration = max(0, int(timeline.get("duration_samples") or 0))
    intervals = []
    for clip in timeline.get("clips", []):
        if clip.get("track_id") != "trk_narrator":
            continue
        start = int(clip.get("timeline_start_sample") or 0)
        length = int(clip.get("source_end_sample") or 0) - int(clip.get("source_start_sample") or 0)
        end = min(duration, start + max(0, length))
        start = max(0, start)
        if end > start:
            intervals.append((start, end, str(clip.get("cue_id") or "")))
    cursor = 0
    previous = None
    longest = 0
    gaps = []
    threshold = max_gap_ms * SAMPLE_RATE // 1000
    for start, end, cue_id in [*sorted(intervals), (duration, duration, None)]:
        length = max(0, start - cursor)
        longest = max(longest, length)
        if length > threshold:
            gaps.append({
                "start_ms": round(cursor * 1000 / SAMPLE_RATE),
                "end_ms": round(start * 1000 / SAMPLE_RATE),
                "duration_ms": round(length * 1000 / SAMPLE_RATE),
                "before_cue_id": previous,
                "after_cue_id": cue_id,
            })
        if end > cursor:
            cursor = end
            previous = cue_id
    return {"measured_on": "rendered_timeline", "max_gap_ms": max_gap_ms,
            "longest_gap_ms": round(longest * 1000 / SAMPLE_RATE), "gaps": gaps}


def _narration_coverage_result(
    project: dict[str, Any], timeline: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if str(project.get("narration_density") or "") != "audio_drama":
        return True, "Lückenprüfung gilt für Hörspiel-Kommentare"
    rendered = timeline if timeline is not None else project.get("timeline")
    if rendered is None:
        # Script planning has no final cut/insert positions. It must not serve
        # as the release gate: build_automatic_quality_report passes its timeline.
        return _planned_narration_coverage_result(project)
    if not int(rendered.get("duration_samples") or 0):
        return False, "Tatsächliche Kommentarabstände mangels fertiger Timeline nicht prüfbar"
    maximum = max(30_000, int(narration_rules(project)["narration_max_gap_ms"]))
    measured = rendered_narration_gap_measurements(rendered, maximum)
    gaps = measured["gaps"]
    if not gaps:
        return True, f"Längste tatsächliche Kommentarpause {measured['longest_gap_ms'] / 1000:.3f} s bei höchstens {maximum / 1000:.0f} s"
    def timestamp(ms: int) -> str:
        seconds = ms // 1000
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    examples = "; ".join(
        f"{timestamp(gap['start_ms'])}–{timestamp(gap['end_ms'])}: {gap['duration_ms'] / 1000:.3f} s"
        for gap in sorted(gaps, key=lambda item: -item["duration_ms"])[:6]
    )
    return False, f"{len(gaps)} tatsächliche Kommentarpausen über {maximum / 1000:.0f} s; längste {measured['longest_gap_ms'] / 1000:.3f} s. {examples}"


def _planned_narration_coverage_result(project: dict[str, Any]) -> tuple[bool, str]:
    """Fail when the listener is left without a comment for too long."""
    rules = narration_rules(project)
    contract = project.get("narration_density_contract") or {}
    targets = dict(contract.get("coverage_targets") or {})
    if str(project.get("narration_density") or "") != "audio_drama" or not targets:
        return True, "Lückenprüfung gilt nur für belegte Hörspiel-Kandidaten"
    max_gap_ms = max(30_000, int(rules["narration_max_gap_ms"]))
    placements_by_episode: dict[str, list[int]] = {}
    rendered_source_positions: dict[tuple[str, int], int] = {}
    synthetic_ids = {str(cue.get("id") or "") for cue in project.get("cues", []) if cue.get("character_introduction_candidate")}
    for cue in project.get("cues", []):
        episode_id = str(cue.get("anchor_episode_id") or "")
        if not episode_id:
            continue
        placement_sample = cue.get("placement_sample_in_episode")
        placement_ms = (
            round(int(placement_sample) * 1000 / SAMPLE_RATE)
            if placement_sample is not None
            else int(cue.get("anchor_episode_start_ms") or cue.get("anchor_insert_ms") or 0)
        )
        placements_by_episode.setdefault(episode_id, []).append(placement_ms)
        rendered_source_positions[(episode_id, int(cue.get("anchor_episode_start_ms") or 0))] = placement_ms
    violations: list[str] = []
    largest_gap_ms = 0
    for episode_id, target in sorted(targets.items()):
        coverage_ids = target.get("coverage_candidate_ids")
        anchors = {
            str(cue_id): int(value)
            for cue_id, value in dict(target.get("candidate_anchors") or {}).items()
            if (str(cue_id) in coverage_ids if coverage_ids is not None else str(cue_id) not in synthetic_ids)
        }
        if not anchors:
            continue
        # Match the runner: a selected beat anchored away from its source
        # window can leave the same gap as an omitted beat.
        # A cue anchored at its proposed source beat still covers that beat
        # after the renderer moves it to a speech-safe boundary/overlay pause.
        # Only genuinely displaced selected beats keep a separate home anchor.
        source_positions = [rendered_source_positions.get((episode_id, home), home) for home in anchors.values()]
        placements = sorted(placements_by_episode.get(episode_id, []))
        book_edge = str(target.get("book_edge_episode") or "none")
        lead = [] if book_edge in {"start", "both"} else [min(anchors.values())]
        tail = [] if book_edge in {"end", "both"} else [max(anchors.values())]
        inner = placements[1:] if book_edge in {"start", "both"} and len(placements) > 1 else placements
        if book_edge in {"end", "both"} and len(inner) > 1:
            inner = inner[:-1]
        edges = [*lead, *inner, *tail] or [min(anchors.values()), max(anchors.values())]
        for left, right in zip(edges, edges[1:]):
            gap_ms = right - left
            if gap_ms > largest_gap_ms and any(left < value < right for value in source_positions):
                largest_gap_ms = gap_ms
            if gap_ms <= max_gap_ms:
                continue
            if any(left < value < right for value in source_positions):
                violations.append(
                    f"Folge {episode_id}: {gap_ms / 1000:.0f} s ohne Kommentar "
                    f"ab {left / 1000:.0f} s"
                )
    return not violations, (
        f"Längste unkommentierte Strecke {largest_gap_ms / 1000:.0f} s "
        f"bei höchstens {max_gap_ms / 1000:.0f} s"
        if not violations
        else f"{len(violations)} zu lange Kommentarlücken: {'; '.join(violations[:6])}"
    )


def _forced_insert_spacing_result(project: dict[str, Any]) -> tuple[bool, str]:
    minimum = int(narration_rules(project)["min_forced_insert_spacing_ms"])
    by_episode: dict[str, list[int]] = {}
    for cue in project.get("cues", []):
        if cue.get("placement_policy") != "insert_for_dense_action":
            continue
        episode_id = str(cue.get("anchor_episode_id") or "")
        placement_sample = cue.get("placement_sample_in_episode")
        insertion = (
            round(int(placement_sample) * 1000 / SAMPLE_RATE)
            if placement_sample is not None
            else int(cue.get("anchor_insert_ms", 0))
        )
        by_episode.setdefault(episode_id, []).append(insertion)
    gaps = [
        right - left
        for insertions in by_episode.values()
        for left, right in zip(sorted(insertions), sorted(insertions)[1:])
    ]
    shortest = min(gaps, default=None)
    passed = shortest is None or shortest >= minimum
    detail = (
        f"{sum(len(items) for items in by_episode.values())} Zeiteinschübe, Mindestabstand {minimum // 1000} s"
        if passed
        else f"Kürzester Abstand {shortest / 1000:.1f} s statt mindestens {minimum / 1000:.0f} s"
    )
    return passed, detail


def _aligned_scene_insert_result(project: dict[str, Any]) -> tuple[bool, str]:
    segments = {
        str(item.get("id") or ""): item
        for item in (project.get("reconciled_transcript") or project.get("transcript", []))
    }
    speech = merged_speech_intervals(project)
    insertions = [
        cue
        for cue in project.get("cues", [])
        if cue.get("placement_policy") == ALIGNED_SCENE_INSERT_POLICY
    ]
    invalid = []
    for cue in insertions:
        anchor = segments.get(str(cue.get("anchor_segment_id") or ""))
        if not anchor or str(anchor.get("episode_id") or "") != str(cue.get("anchor_episode_id") or ""):
            invalid.append(str(cue.get("id") or "unbekannt"))
            continue
        boundary_ms = aligned_scene_boundary_ms(cue, anchor)
        # A boundary inside a running utterance is rendered at the nearer
        # utterance edge so the splice never clips a word.  The evaluator
        # replays that deterministic shift; anything else stays a defect.
        snapped_ms = speech_safe_insert_ms(
            boundary_ms, speech.get(str(cue.get("anchor_episode_id") or ""), [])
        )
        expected_samples = {
            round(boundary_ms * SAMPLE_RATE / 1000),
            round(snapped_ms * SAMPLE_RATE / 1000),
        }
        placement_sample = cue.get("placement_sample_in_episode")
        actual_sample = (
            int(placement_sample)
            if placement_sample is not None
            else round(int(cue.get("anchor_insert_ms", 0)) * SAMPLE_RATE / 1000)
        )
        if all(abs(actual_sample - expected) > 1 for expected in expected_samples):
            invalid.append(str(cue.get("id") or "unbekannt"))
    return not invalid, (
        f"{len(insertions)} Einfügungen liegen an belegten Szenengrenzen oder deren sprechfreier Kante"
        if not invalid
        else f"{len(invalid)} von {len(insertions)} Einfügungen liegen nicht an ihrer belegten Szenengrenze"
    )


def _character_introduction_timing_result(
    project: dict[str, Any],
    timeline: dict[str, Any],
) -> tuple[bool, str]:
    rules = narration_rules(project)

    def name_key(value: Any) -> str:
        return identity_keys.get("".join(normalized_character_evidence(value).split()), "".join(normalized_character_evidence(value).split()))

    research = project.get("episode_context_research") or {}
    identity_keys = character_identity_keys(project, research)
    contexts = normalize_character_introduction_contexts(project, research).get("episode_contexts") or []
    required = {
        name_key(character.get("name")): {
            "display_name": str(character.get("name") or "").strip(),
            "episode_id": str(context.get("episode_id") or ""),
        }
        for context in contexts
        for character in context.get("character_introductions", [])
        if character.get("introduction_required")
    }
    if not required:
        return True, "Keine verpflichtenden Figuren-Einführungen für diesen Folgenbereich"

    introductions_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in project.get("character_introductions", []):
        if isinstance(item, dict):
            introductions_by_name.setdefault(name_key(item.get("research_name")), []).append(item)

    cues = [cue for cue in project.get("cues", []) if isinstance(cue, dict)]
    cue_counts = Counter(str(cue.get("id") or "") for cue in cues)
    cue_by_id = {str(cue.get("id") or ""): cue for cue in cues}
    segments = {
        str(item.get("id") or ""): item
        for item in (project.get("reconciled_transcript") or project.get("transcript", []))
    }
    narrator_clips_by_cue: dict[str, list[dict[str, Any]]] = {}
    for clip in timeline.get("clips", []):
        if clip.get("track_id") == "trk_narrator" and clip.get("cue_id"):
            narrator_clips_by_cue.setdefault(str(clip["cue_id"]), []).append(clip)

    content_fragments = source_content_fragments(project)
    first_fragment_id = str(content_fragments[0].get("id") or "") if content_fragments else ""
    episode_order = {
        episode_id: index
        for index, episode_id in enumerate(unique_episode_ids(project.get("mapping", [])))
    }
    start_boundary_cue = min(
        (
            cue
            for cue in cues
            if first_fragment_id
            and first_fragment_id in {str(value) for value in cue.get("source_fragment_ids", [])}
        ),
        key=lambda cue: (
            episode_order.get(str(cue.get("anchor_episode_id") or ""), 9999),
            int(cue.get("anchor_episode_start_ms") or 0),
        ),
        default=None,
    )
    start_boundary_episode_id = str((start_boundary_cue or {}).get("anchor_episode_id") or "")
    book_start_ms = int((start_boundary_cue or {}).get("anchor_episode_start_ms") or 0)
    if (
        (start_boundary_cue or {}).get("placement_policy") == ALIGNED_SCENE_INSERT_POLICY
        and (start_boundary_cue or {}).get("alignment_placement") == "before_anchor"
    ):
        # Compare source coordinates after the same deterministic speech-safe
        # snap used by the renderer, not against the unsnapped subtitle time.
        book_start_ms = speech_safe_insert_ms(
            book_start_ms, merged_speech_intervals(project).get(start_boundary_episode_id, [])
        )
    start_boundary_sample = round(
        book_start_ms * SAMPLE_RATE / 1000
    )
    excluded_by_episode = dict(timeline.get("excluded_source_intervals") or {})

    errors = []
    valid_cue_ids: set[str] = set()
    start_episode_names = 0
    for research_name, requirement in required.items():
        display_name = str(requirement["display_name"] or research_name)
        mappings = introductions_by_name.get(research_name, [])
        reasons = []
        if len(mappings) != 1:
            reasons.append(f"{len(mappings)} formale Zuordnungen statt genau einer")
            errors.append(f"{display_name}: {', '.join(reasons)}")
            continue

        introduction = mappings[0]
        cue_id = str(introduction.get("cue_id") or "")
        cue = cue_by_id.get(cue_id)
        spoken_name = str(introduction.get("spoken_name") or "").strip()
        required_episode_id = str(requirement["episode_id"])
        episode_subtitles = " ".join(
            str(row.get("reconciled_text") or row.get("text") or "")
            for row in (project.get("reconciled_transcript") or [])
            if str(row.get("episode_id") or "") == required_episode_id
        )
        if not spoken_name_matches_research_character(
            display_name,
            spoken_name,
            project.get("narration_name_lexicon", []),
        ) and not character_name_evidenced(episode_subtitles, spoken_name):
            # narration_rules declare the target subtitles as the name source, so
            # a name the dub actually speaks beats a web-research romanisation
            # ("Meister Lampe" over "Karottenmonster", "Krillin" over "Kuririn").
            reasons.append(
                f"gesprochener Name {spoken_name or '(leer)'} gehört nicht zu {display_name} "
                "und kommt auch nicht in den Ziel-Untertiteln vor"
            )
        if cue_counts.get(cue_id, 0) != 1 or not cue:
            reasons.append("zugeordneter Cue fehlt im finalen Skript")
        if str(introduction.get("episode_id") or "") != required_episode_id:
            reasons.append("formale Erstfolge stimmt nicht")
        if cue:
            if cue.get("narrative_purpose") != "character_introduction":
                reasons.append("Cue ist keine Figuren-Einführung")
            if cue.get("alignment_placement") != "before_anchor":
                reasons.append("Cue ist nicht als before_anchor deklariert")
            if str(cue.get("anchor_episode_id") or "") != required_episode_id:
                reasons.append("Szenenanker liegt nicht in der Erstfolge")
            if not spoken_name or not re.search(
                rf"(?<!\w){re.escape(spoken_name)}(?:s)?(?!\w)",
                str(cue.get("text") or ""),
                flags=re.IGNORECASE,
            ):
                reasons.append(f"gesprochener Name {spoken_name or '(leer)'} fehlt")

        anchor = segments.get(str((cue or {}).get("anchor_segment_id") or ""))
        if not anchor or str(anchor.get("episode_id") or "") != required_episode_id:
            reasons.append("belegter Untertitelanker fehlt")
        placement_value = (cue or {}).get("placement_sample_in_episode")
        placement_sample = int(placement_value) if placement_value is not None else None
        policy = str((cue or {}).get("placement_policy") or "")
        clips = narrator_clips_by_cue.get(cue_id, [])
        if len(clips) != 1:
            reasons.append(f"{len(clips)} finale Timeline-Clips statt genau eines")
            clip = None
        else:
            clip = clips[0]
            expected_timeline_start = (cue or {}).get("timeline_start_sample")
            if (
                expected_timeline_start is None
                or int(clip.get("timeline_start_sample", -1)) != int(expected_timeline_start)
            ):
                reasons.append("Timeline-Position stimmt nicht mit dem finalen Cue überein")
            if str(clip.get("strategy") or "") != policy:
                reasons.append("Timeline-Strategie stimmt nicht mit dem finalen Cue überein")

        clip_duration = (
            int((clip or {}).get("source_end_sample", 0))
            - int((clip or {}).get("source_start_sample", 0))
        )
        if placement_sample is None:
            reasons.append("hörbare Quellposition fehlt")
        elif anchor:
            boundary_ms = aligned_scene_boundary_ms(cue or {}, anchor)
            boundary_sample = round(boundary_ms * SAMPLE_RATE / 1000)
            if policy == ALIGNED_SCENE_INSERT_POLICY:
                snapped_sample = round(
                    speech_safe_insert_ms(
                        boundary_ms,
                        merged_speech_intervals(project).get(required_episode_id, []),
                    )
                    * SAMPLE_RATE
                    / 1000
                )
                if (
                    abs(placement_sample - boundary_sample) > 1
                    and abs(placement_sample - snapped_sample) > 1
                ):
                    reasons.append("Einfügung liegt nicht exakt an der Dialoggrenze")
            elif policy == "overlay_speech_free":
                if clip_duration <= 0:
                    reasons.append("hörbare Clipdauer fehlt")
                elif placement_sample + clip_duration > boundary_sample + 1:
                    late_ms = round(
                        (placement_sample + clip_duration - boundary_sample) * 1000 / SAMPLE_RATE
                    )
                    reasons.append(f"Overlay endet {late_ms} ms nach dem Erstanker")
                else:
                    lead_ms = round(
                        (boundary_sample - placement_sample - clip_duration) * 1000 / SAMPLE_RATE
                    )
                    maximum_lead_ms = int(rules["character_introduction_max_lead_ms"])
                    if lead_ms > maximum_lead_ms:
                        reasons.append(
                            f"Overlay endet {lead_ms} ms vor dem Erstanker "
                            f"(erlaubt höchstens {maximum_lead_ms} ms)"
                        )
            elif policy == "overlay_opening_gap":
                gap_start_sample = round(
                    int((cue or {}).get("safe_gap_start_ms", 0)) * SAMPLE_RATE / 1000
                )
                gap_end_sample = round(
                    int((cue or {}).get("safe_gap_end_ms", 0)) * SAMPLE_RATE / 1000
                )
                if cue is not start_boundary_cue:
                    reasons.append("Öffnungs-Overlay ist nicht der belegte Buchanfang")
                elif clip_duration <= 0:
                    reasons.append("hörbare Clipdauer fehlt")
                elif (
                    placement_sample < gap_start_sample
                    or placement_sample + clip_duration > gap_end_sample + 1
                ):
                    reasons.append("Öffnungs-Overlay liegt nicht vollständig in seiner Sprechpause")
                else:
                    lead_ms = round(
                        (gap_end_sample - placement_sample - clip_duration)
                        * 1000
                        / SAMPLE_RATE
                    )
                    maximum_lead_ms = int(rules["character_introduction_max_lead_ms"])
                    if lead_ms > maximum_lead_ms:
                        reasons.append(
                            f"Öffnungs-Overlay endet {lead_ms} ms vor dem Ende der Sprechpause "
                            f"(erlaubt höchstens {maximum_lead_ms} ms)"
                        )
            else:
                reasons.append(f"unzulässige Timing-Strategie {policy or '(leer)'}")

            if (
                required_episode_id == start_boundary_episode_id
                and placement_sample < start_boundary_sample
            ):
                early_ms = round((start_boundary_sample - placement_sample) * 1000 / SAMPLE_RATE)
                reasons.append(f"Einführung beginnt {early_ms} ms vor dem Buchanfang")
            if required_episode_id == start_boundary_episode_id:
                start_episode_names += 1

            exclusions = excluded_by_episode.get(required_episode_id, [])
            if policy in {"overlay_speech_free", "overlay_opening_gap"}:
                overlaps_exclusion = any(
                    int(item.get("start_sample", 0)) < placement_sample + max(0, clip_duration)
                    and int(item.get("end_sample", 0)) > placement_sample
                    for item in exclusions
                )
            else:
                overlaps_exclusion = any(
                    int(item.get("start_sample", 0)) <= placement_sample
                    < int(item.get("end_sample", 0))
                    for item in exclusions
                )
            if overlaps_exclusion:
                reasons.append("Einführung liegt in einem ausgeschlossenen Intro- oder Buchrand")

        if reasons:
            errors.append(f"{display_name}: {', '.join(dict.fromkeys(reasons))}")
        else:
            valid_cue_ids.add(cue_id)

    if errors:
        return False, "; ".join(errors)[:1500]
    return True, (
        f"{len(required)} verpflichtende Figuren in {len(valid_cue_ids)} finalen Clips · "
        f"{start_episode_names} Figuren der Startfolge hinter dem Buchanfang · "
        "alle unmittelbar vor ihrem ersten belegten Dialog"
    )


def cue_editorial_metadata_complete(cue: dict[str, Any]) -> bool:
    return (
        str(cue.get("narrative_purpose") or "") in {
            "character_introduction",
            "scene_transition",
            "visual_action",
            "internal_motivation",
            "offscreen_context",
            "continuity_bridge",
            "foreshadowing",
            "book_boundary",
        }
        and bool(str(cue.get("information_gain") or "").strip())
    )


def cue_novelistic_metadata_complete(cue: dict[str, Any], minimum_source_coverage: float) -> bool:
    return (
        (
            str(cue.get("narrative_purpose") or "")
            not in (VISUAL_SOURCE_COVERAGE_PURPOSES - {"character_introduction"})
            or int(
                cue.get("source_detail_reference_count")
                if cue.get("source_detail_reference_count") is not None
                else MIN_SOURCE_DETAIL_REFERENCE_TOKENS
            ) < MIN_SOURCE_DETAIL_REFERENCE_TOKENS
            # Mirror the editor: audio drama is judged on preserved
            # picture words, so the ratio alone must not reject a cue
            # the contract deliberately accepted.
            or int(cue.get("preserved_concrete_detail_count") or 0) >= 1
            or float(cue.get("source_detail_coverage") or 0) >= minimum_source_coverage
        )
        and str(cue.get("native_audio_relation") or "") in {
            "complements_existing_audio",
            "no_relevant_existing_narration",
        }
        and (
            str(cue.get("narrative_purpose") or "")
            not in {"character_introduction", "visual_action"}
            or bool(cue.get("retained_visual_details"))
        )
    )


def cue_audio_drama_contract_complete(cue: dict[str, Any]) -> bool:
    minimum_duration_ms = AUDIO_DRAMA_MIN_DURATION_MS.get(str(cue.get("narrative_purpose") or ""), 3_500)
    if cue.get("audio_strategy") == "prefer_ambience_overlay" and cue.get("placement_policy") in {
        "overlay_speech_free", "overlay_intro_music", "overlay_opening_gap",
    }:
        # Match the runner's eight-word allowance only when the rendered cue
        # actually rides a pause. A converted scene interruption gets no waiver.
        if len(str(cue.get("text") or "").split()) < 8:
            return False
        minimum_duration_ms = min(minimum_duration_ms, 3_600)
    return (
        str(cue.get("beat_type") or "") in AUDIO_DRAMA_BEAT_TYPES
        and str(cue.get("audio_strategy") or "") in AUDIO_DRAMA_STRATEGIES
        and minimum_duration_ms
        <= int(cue.get("target_duration_ms") or 0)
        <= AUDIO_DRAMA_DURATION_LIMITS_MS.get(
            str(cue.get("narrative_purpose") or ""),
            16_000,
        )
        and int(
            cue.get("measured_duration_ms")
            or cue.get("estimated_duration_ms")
            or 0
        )
        <= AUDIO_DRAMA_DURATION_LIMITS_MS.get(
            str(cue.get("narrative_purpose") or ""),
            16_000,
        ) + 2_000
    )


def _editorial_quality_result(
    project: dict[str, Any], introduction_timing_ok: bool,
) -> tuple[bool, str]:
    rules = narration_rules(project)

    def name_key(value: Any) -> str:
        return identity_keys.get("".join(normalized_character_evidence(value).split()), "".join(normalized_character_evidence(value).split()))

    identity_keys = character_identity_keys(project, project.get("episode_context_research") or {})
    expected_episode_ids = set(unique_episode_ids(project.get("mapping", [])))
    research = normalize_character_introduction_contexts(project, project.get("episode_context_research") or {})
    contexts = research.get("episode_contexts") if isinstance(research.get("episode_contexts"), list) else []
    context_episode_ids = {str(item.get("episode_id") or "") for item in contexts}
    sourced_contexts = all(
        isinstance(item.get("sources"), list)
        and bool(item["sources"])
        and all(str(source.get("url") or "").startswith(("https://", "http://")) for source in item["sources"])
        for item in contexts
    )
    cues = project.get("cues", [])
    editorial_metadata_complete = bool(cues) and all(cue_editorial_metadata_complete(cue) for cue in cues)
    counts = Counter(str(cue.get("anchor_episode_id") or "") for cue in cues)
    density = str(project.get("narration_density") or "compact")
    density_contract = project.get("narration_density_contract") or {}
    novelistic_contract_required = (
        int(density_contract.get("editorial_contract_version") or 0)
        >= NARRATION_EDITOR_VERSION
    )
    visual_context_complete = all(
        not character.get("introduction_required")
        or bool(str(character.get("visual_description") or "").strip())
        for context in contexts
        for character in context.get("character_introductions", [])
    )
    minimum_source_coverage = float(
        rules["audio_drama_source_coverage_min"]
        if str(project.get("narration_density") or "") == "audio_drama"
        else rules["detailed_source_coverage_min"]
    )
    repetition_violations = (
        native_audio_repetition_violations(project, cues)
        if novelistic_contract_required
        else []
    )
    novelistic_metadata_complete = (
        not novelistic_contract_required
        or (
            visual_context_complete
            and bool(cues)
            and all(cue_novelistic_metadata_complete(cue, minimum_source_coverage) for cue in cues)
            and not repetition_violations
        )
    )
    audio_drama_contract_required = (
        density == "audio_drama"
        and int(density_contract.get("editorial_contract_version") or 0)
        >= NARRATION_EDITOR_VERSION
    )
    audio_drama_contract_complete = (
        not audio_drama_contract_required
        or all(cue_audio_drama_contract_complete(cue) for cue in cues)
    )
    candidate_counts = {
        str(episode_id): int(count)
        for episode_id, count in dict(density_contract.get("candidate_counts") or {}).items()
    }
    if density == "detailed" and candidate_counts:
        density_ok = (
            str(density_contract.get("density") or "") == "detailed"
            and set(candidate_counts) == expected_episode_ids
            and len(cues) == sum(candidate_counts.values())
            and int(density_contract.get("selected_count") or 0) == len(cues)
        )
        density_detail = (
            f"Detailliert: {len(cues)} von {sum(candidate_counts.values())} "
            "belegten Rohpassagen erhalten"
        )
    elif density == "audio_drama" and candidate_counts:
        contract_targets = dict(density_contract.get("coverage_targets") or {})
        thin_episodes = []
        for episode_id in expected_episode_ids:
            candidate_count = candidate_counts.get(episode_id, 0)
            target = contract_targets.get(episode_id) or {}
            minimum = min(candidate_count, max(4, int(target.get("min_cues") or 4)))
            selected = counts.get(episode_id, 0)
            if not minimum <= selected <= candidate_count:
                thin_episodes.append(f"{episode_id}: {selected} statt {minimum}–{candidate_count}")
        density_ok = (
            str(density_contract.get("density") or "") == "audio_drama"
            and set(candidate_counts) == expected_episode_ids
            and not thin_episodes
            and int(density_contract.get("selected_count") or 0) == len(cues)
        )
        density_detail = (
            f"Hörspiel: {len(cues)} Szenenbeats aus "
            f"{sum(candidate_counts.values())} belegten Kandidaten"
            if not thin_episodes
            else "Zu wenige Kommentare je Folge: " + "; ".join(thin_episodes[:6])
        )
    else:
        maximum = 18 if density == "balanced" else 12
        density_ok = bool(expected_episode_ids) and all(
            1 <= counts.get(episode_id, 0) <= maximum
            for episode_id in expected_episode_ids
        )
        density_detail = f"{density}: 1–{maximum} Cues je Folge"
    required_names = {
        name_key(character.get("name"))
        for context in contexts
        for character in context.get("character_introductions", [])
        if character.get("introduction_required")
    }
    # The release gate supplies the complete rendered introduction verdict.
    # A second partial check disagreed on subtitle-backed dub names (Kuririn /
    # Krillin) and ignored the renderer's speech-safe placement adjustment.
    passed = bool(
        int(project.get("narration_editor_version") or 0) >= NARRATION_EDITOR_VERSION
        and context_episode_ids == expected_episode_ids
        and sourced_contexts
        and editorial_metadata_complete
        and novelistic_metadata_complete
        and audio_drama_contract_complete
        and density_ok
        and introduction_timing_ok
    )
    if passed:
        editorial_result = (
            "Hörspiel-Beats ergänzen Dialog und Klangbett mit geprüfter Sprechdauer"
            if density == "audio_drama"
            else "Romanhandlung ergänzt den vorhandenen Serienerzähler"
        )
        return True, (
            f"{len(contexts)} Folgen online belegt · {len(cues)} redigierte Cues · "
            f"{len(required_names)} verpflichtende Figuren rechtzeitig eingeführt · "
            + editorial_result
        )
    problems = []
    if context_episode_ids != expected_episode_ids or not sourced_contexts:
        problems.append("Episodenkontext unvollständig")
    if not editorial_metadata_complete:
        problems.append("Zweck/Informationsgewinn fehlt")
    if not novelistic_metadata_complete:
        if repetition_violations:
            first = repetition_violations[0]
            problems.append(
                f"Erzählertext wiederholt Serienaudio bei Cue {first['cue_id']}: "
                f"„{first['text']}“"
            )
        else:
            problems.append(
                "Romanhandlung, sichtbare Details oder Ergänzung des Serienerzählers unvollständig"
            )
    if not audio_drama_contract_complete:
        problems.append("Hörspiel-Beat, Audio-Strategie oder Sprechdauer verletzt den Vertrag")
    if not density_ok:
        problems.append(f"Cue-Dichte verletzt Vertrag ({density_detail})")
    if not introduction_timing_ok:
        problems.append("Figuren-Einführungen fehlen oder sind zu spät")
    return False, "; ".join(problems) or "Redaktioneller Codex-Pass fehlt"


def grounded_character_introduction_cue_ids(project: dict[str, Any]) -> set[str]:
    def name_key(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    researched = {
        (name_key(character.get("name")), str(context.get("episode_id") or ""))
        for context in (project.get("episode_context_research") or {}).get(
            "episode_contexts", []
        )
        if isinstance(context.get("sources"), list) and bool(context["sources"])
        for character in context.get("character_introductions", [])
        if character.get("introduction_required")
        and bool(str(character.get("visual_description") or "").strip())
    }
    return {
        str(introduction.get("cue_id") or "")
        for introduction in project.get("character_introductions", [])
        if isinstance(introduction, dict)
        and (
            name_key(introduction.get("research_name")),
            str(introduction.get("episode_id") or ""),
        ) in researched
        and str(introduction.get("cue_id") or "")
    }


def build_automatic_quality_report(
    project: dict[str, Any],
    timeline: dict[str, Any],
    artifact: dict[str, Any],
) -> dict[str, Any]:
    rules = narration_rules(project)
    expected_episode_ids = set(unique_episode_ids(project.get("mapping", [])))
    source_coverage_ok, source_coverage_detail = _source_coverage_result(project, timeline)
    boundary_ok, boundary_detail = _book_boundary_result(project, timeline)
    intro_removal_ok, intro_removal_detail = _intro_removal_result(project, timeline)
    commercial_bumper_ok, commercial_bumper_detail = _commercial_bumper_removal_result(
        project,
        timeline,
    )
    ending_removal_ok, ending_removal_detail = _ending_removal_result(project, timeline)
    overlay_safety_ok, overlay_safety_detail = _overlay_safety_result(project)
    anchor_proximity_ok, anchor_proximity_detail = _narration_anchor_proximity_result(project)
    narration_coverage_ok, narration_coverage_detail = _narration_coverage_result(project, timeline)
    forced_spacing_ok, forced_spacing_detail = _forced_insert_spacing_result(project)
    aligned_insert_ok, aligned_insert_detail = _aligned_scene_insert_result(project)
    introduction_timing_ok, introduction_timing_detail = _character_introduction_timing_result(
        project,
        timeline,
    )
    editorial_quality_ok, editorial_quality_detail = _editorial_quality_result(
        project, introduction_timing_ok,
    )
    narrator_clips = [
        clip for clip in timeline.get("clips", [])
        if clip.get("track_id") == "trk_narrator" and clip.get("cue_id")
    ]
    narrator_counts = Counter(str(clip.get("cue_id")) for clip in narrator_clips)
    cue_ids = {str(cue.get("id")) for cue in project.get("cues", [])}
    all_cues_rendered = bool(cue_ids) and set(narrator_counts) == cue_ids and all(
        narrator_counts[cue_id] == 1 for cue_id in cue_ids
    )
    omitted_count = sum(
        1 for cue in project.get("cues", [])
        if str(cue.get("render_status") or "").startswith("omitted_")
    )
    all_cues_rendered = all_cues_rendered and omitted_count == 0

    ungrounded_ids = ungrounded_narration_cue_ids(project)
    ungrounded = [cue for cue in project.get("cues", []) if str(cue.get("id") or "") in ungrounded_ids]
    transcript_episode_ids = {
        str(item.get("episode_id") or "") for item in project.get("transcript", [])
        if item.get("episode_id")
    }
    word_episode_ids = {
        str(item.get("episode_id") or "") for item in project.get("transcript_words", [])
        if item.get("episode_id")
    }
    transcript = project.get("transcript", [])
    subtitle_summary = project.get("subtitle_reconciliation") or {}
    evidence_complete = (
        bool(transcript)
        and expected_episode_ids <= transcript_episode_ids
        and expected_episode_ids <= word_episode_ids
        and all(item.get("timestamp_source") == "whisper_segments" for item in transcript)
        and int(subtitle_summary.get("episode_count", 0)) == len(expected_episode_ids)
        and int(subtitle_summary.get("subtitle_episode_count", 0)) == len(expected_episode_ids)
        and int(subtitle_summary.get("segment_count", 0)) > 0
    )

    validation = timeline.get("validation") or {}
    collisions = int(validation.get("dialog_collisions", narration_speech_cut_collisions(project)))
    replaced_ms = int(validation.get("replaced_source_narration_ms", 0))
    audio = artifact.get("audio") or {}
    expected_duration_ms = round(int(timeline.get("duration_samples", 0)) * 1000 / SAMPLE_RATE)
    actual_duration_ms = int(audio.get("duration_ms") or 0)
    technical_delivery_ok = (
        int(artifact.get("bytes") or 0) > 0
        and len(str(artifact.get("sha256") or "")) == 64
        and audio.get("status") == "inspected"
        and audio.get("codec") == "aac"
        and int(audio.get("sample_rate") or 0) == SAMPLE_RATE
        and int(audio.get("channels") or 0) == 1
        and abs(actual_duration_ms - expected_duration_ms) <= 250
    )

    checks = [
        _quality_check(
            "timestamp_evidence",
            "Zeit- und Untertitelbelege vollständig",
            evidence_complete,
            (
                f"{len(expected_episode_ids)} Folgen mit Forced Alignment, Wörtern und vollständiger Ziel-SRT"
                if evidence_complete
                else "Mindestens einer Folge fehlen Forced-Aligner-Wörter oder vollständige Ziel-Untertitel"
            ),
        ),
        _quality_check(
            "narration_grounding",
            "Erzählertext belegt",
            not ungrounded and bool(project.get("cues")),
            (
                f"{len(project.get('cues', []))} Cues mit Romanfragment, Szenenanker und Mindestkonfidenz"
                if not ungrounded
                else f"{len(ungrounded)} Cues ohne ausreichenden Beleg"
            ),
            actual=len(project.get("cues", [])) - len(ungrounded),
            expected=len(project.get("cues", [])),
        ),
        _quality_check(
            "narration_editorial_quality",
            "Erzählertext kontextstiftend und rechtzeitig",
            editorial_quality_ok,
            editorial_quality_detail,
        ),
        _quality_check(
            "character_introduction_timing",
            "Figuren hörbar vor ihrem Erstauftritt",
            introduction_timing_ok,
            introduction_timing_detail,
        ),
        _quality_check(
            "source_coverage",
            "Quellhandlung vollständig",
            source_coverage_ok,
            source_coverage_detail,
        ),
        _quality_check(
            "book_boundary_coverage",
            "Buchanfang und Buchende belegt",
            boundary_ok,
            boundary_detail,
        ),
        _quality_check(
            "repeated_intro_removal",
            "Wiederkehrende Introspur entfernt",
            intro_removal_ok,
            intro_removal_detail,
        ),
        _quality_check(
            "ending_preview_removal",
            "Abspann und Episodenvorschau entfernt",
            ending_removal_ok,
            ending_removal_detail,
        ),
        _quality_check(
            "commercial_bumper_removal",
            "Commercial-Bumper entfernt",
            commercial_bumper_ok,
            commercial_bumper_detail,
        ),
        _quality_check(
            "narration_anchor_proximity",
            "Kommentare szenennah platziert",
            anchor_proximity_ok,
            anchor_proximity_detail,
        ),
        _quality_check(
            "narration_coverage",
            "Keine langen Strecken ohne Kommentar",
            narration_coverage_ok,
            narration_coverage_detail,
            actual=rendered_narration_gap_measurements(
                timeline, max(30_000, int(rules["narration_max_gap_ms"])),
            ) if str(project.get("narration_density") or "") == "audio_drama" else None,
        ),
        _quality_check(
            "all_cues_rendered",
            "Ausgewählte Cues genau einmal gerendert",
            all_cues_rendered,
            (
                f"{len(cue_ids)} von {len(cue_ids)} Cues in der Timeline"
                if all_cues_rendered
                else f"{len(narrator_counts)} Timeline-Cues, {omitted_count} ausgelassen"
            ),
            actual=len(narrator_counts),
            expected=len(cue_ids),
        ),
        _quality_check(
            "dialog_safety",
            "Keine Dialogkollision",
            collisions == 0 and overlay_safety_ok,
            f"{collisions} Schnittkollisionen · {overlay_safety_detail}",
            actual=collisions,
            expected=0,
        ),
        _quality_check(
            "source_narrator_preserved",
            "Serienerzähler erhalten",
            replaced_ms == 0 and bool(rules["preserve_existing_narrator"]),
            f"{replaced_ms} ms vorhandene Erzählerspur ersetzt",
            actual=replaced_ms,
            expected=0,
        ),
        _quality_check(
            "timeline_insert_integrity",
            "Timeline-Erweiterungen szenengenau",
            forced_spacing_ok and aligned_insert_ok,
            f"{aligned_insert_detail} · {forced_spacing_detail}",
        ),
        _quality_check(
            "delivery_technical",
            "Delivery technisch konsistent",
            technical_delivery_ok,
            (
                f"{audio.get('codec', 'unbekannt').upper()} · {audio.get('sample_rate', 0)} Hz · "
                f"{audio.get('channels', 0)} Kanal · Laufzeitabweichung {abs(actual_duration_ms - expected_duration_ms)} ms"
            ),
        ),
    ]
    for check in checks:
        if check["status"] == "failed":
            check["cue_ids"] = sorted(quality_repair_targets({
                **project, "quality_report": {"checks": [check]},
            })[0])
            if check["cue_ids"] and (check.get("actual") is None or check["id"] == "narration_grounding"):
                check["actual"] = {"cue_ids": check["cue_ids"]}
    passed_count = sum(1 for check in checks if check["status"] == "passed")
    release_ready = passed_count == len(checks)
    return {
        "version": 1,
        "evaluator": AUTOMATIC_QUALITY_EVALUATOR,
        "generated_at": now(),
        "timeline_revision": int(timeline.get("revision", 0)),
        "artifact_sha256": str(artifact.get("sha256") or ""),
        "status": "passed" if release_ready else "blocked",
        "release_ready": release_ready,
        "score": round(passed_count / max(len(checks), 1), 4),
        "passed_checks": passed_count,
        "total_checks": len(checks),
        "human_review": "optional_calibration",
        "summary": (
            "Automatische Freigabe bestanden; ein Testhörer ist nur noch optional zur Kalibrierung."
            if release_ready
            else f"Automatische Freigabe blockiert: {len(checks) - passed_count} Qualitätskriterien sind offen."
        ),
        "checks": checks,
    }


def sync_narration_review(project: dict[str, Any]) -> None:
    review = {**default_narration_review(), **dict(project.get("narration_review") or {})}
    review["mode"] = "calibration"
    review["required"] = False
    report = project.get("quality_report") or {}
    review["automatic_status"] = report.get("status", "not_run")
    revision = int((project.get("timeline") or {}).get("revision", 0))
    artifact_sha = str((project.get("artifacts") or [{}])[0].get("sha256") or "")
    matching_rounds = [
        item for item in review.get("rounds", [])
        if int(item.get("timeline_revision", -1)) == revision
        and str(item.get("artifact_sha256") or "") == artifact_sha
    ]
    latest = matching_rounds[-1] if matching_rounds else None
    accepted = bool(
        latest
        and latest.get("verdict") == "passed"
        and float(latest.get("score") or 0) >= float(narration_rules(project)["test_listener_min_score"])
    )
    mismatch = bool(report.get("release_ready") and latest and not accepted)
    if not report:
        status = "automatic_pending"
    elif not report.get("release_ready"):
        status = "automatic_blocked"
    elif mismatch:
        status = "calibration_gap"
    elif accepted:
        status = "calibrated"
    else:
        status = "calibration_optional"
    guidance = []
    if mismatch and latest:
        guidance = [*latest.get("findings", [])]
        if latest.get("notes"):
            guidance.append(str(latest["notes"]))
    elif report and not report.get("release_ready"):
        guidance = [
            str(check.get("label"))
            for check in report.get("checks", [])
            if check.get("status") == "failed"
        ]
    review.update(
        {
            "status": status,
            "score": latest.get("score") if latest else None,
            "one_shot_ready": bool(report.get("release_ready") and not mismatch),
            "calibration_mismatch": mismatch,
            "one_shot_guidance": guidance,
        }
    )
    project["narration_review"] = review


def invalidate_automatic_quality(project: dict[str, Any]) -> None:
    project["quality_report"] = None
    review = {**default_narration_review(), **dict(project.get("narration_review") or {})}
    review.update(
        {
            "status": "automatic_pending",
            "automatic_status": "not_run",
            "score": None,
            "one_shot_ready": False,
            "calibration_mismatch": False,
            "one_shot_guidance": [],
        }
    )
    project["narration_review"] = review


def ensure_project_quality_state(project: dict[str, Any]) -> bool:
    before = json.dumps(
        {
            "narration_rules": project.get("narration_rules"),
            "narration_review": project.get("narration_review"),
            "quality_report": project.get("quality_report"),
            "artifacts": project.get("artifacts"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    project["narration_rules"] = narration_rules(project)
    project["narration_review"] = {
        **default_narration_review(),
        **dict(project.get("narration_review") or {}),
    }
    timeline = project.get("timeline")
    artifacts = project.get("artifacts") or []
    if timeline and artifacts:
        artifact = artifacts[0]
        report = project.get("quality_report") or {}
        audio_was_updated = False
        evaluator_is_current = (
            report.get("evaluator") == AUTOMATIC_QUALITY_EVALUATOR
            or (
                report.get("evaluator") == LEGACY_AUTOMATIC_QUALITY_EVALUATOR
                and not (timeline.get("validation") or {}).get("book_boundary_status")
            )
        )
        report_is_current = (
            evaluator_is_current
            and int(report.get("timeline_revision", -1)) == int(timeline.get("revision", 0))
            and str(report.get("artifact_sha256") or "") == str(artifact.get("sha256") or "")
        )
        if (artifact.get("audio") or {}).get("status") != "inspected":
            storage_name = str(artifact.get("storage_name") or "")
            if storage_name:
                inspected = inspect_delivery_audio(ARTIFACTS / storage_name)
                audio_was_updated = inspected != artifact.get("audio")
                artifact["audio"] = inspected
        if not report_is_current or audio_was_updated:
            project["quality_report"] = build_automatic_quality_report(project, timeline, artifact)
        sync_narration_review(project)
    else:
        invalidate_automatic_quality(project)
    after = json.dumps(
        {
            "narration_rules": project.get("narration_rules"),
            "narration_review": project.get("narration_review"),
            "quality_report": project.get("quality_report"),
            "artifacts": project.get("artifacts"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return before != after


def narration_speech_cut_collisions(project: dict[str, Any]) -> int:
    speech_by_episode: dict[str, list[tuple[int, int]]] = {}
    for item in [*(project.get("reconciled_transcript") or []), *project.get("transcript_words", [])]:
        if not transcript_item_has_spoken_content(item):
            continue
        episode_id = str(item.get("episode_id") or "")
        start = int(item.get("episode_start_ms", item.get("start_ms", 0)))
        end = int(item.get("episode_end_ms", item.get("end_ms", start)))
        if episode_id and end > start:
            speech_by_episode.setdefault(episode_id, []).append((start - 250, end + 250))
    collisions = 0
    for cue in project.get("cues", []):
        policy = cue.get("placement_policy")
        if policy not in {"after_aligned_speech_gap", "insert_for_dense_action", "overlay_speech_free"}:
            continue
        episode_id = str(cue.get("anchor_episode_id") or "")
        insertion = int(cue.get("anchor_insert_ms", 0))
        gap_start = int(cue.get("safe_gap_start_ms", insertion))
        gap_end = int(cue.get("safe_gap_end_ms", insertion))
        if policy == "overlay_speech_free" and cue.get("placement_sample_in_episode") is not None:
            overlay_start = round(int(cue["placement_sample_in_episode"]) * 1000 / SAMPLE_RATE)
            overlay_end = overlay_start + int(cue.get("measured_duration_ms", 0))
            if (
                overlay_start < gap_start
                or overlay_end > gap_end
                or any(start < overlay_end and end > overlay_start for start, end in speech_by_episode.get(episode_id, []))
            ):
                collisions += 1
        elif not gap_start < insertion < gap_end or any(start <= insertion <= end for start, end in speech_by_episode.get(episode_id, [])):
            collisions += 1
    return collisions


def render_project(project_id: str, run_id: str) -> None:
    project = STATE["projects"][project_id]
    rules = narration_rules(project)
    update_run(run_id, stage="synthesizing", message="Erzählstimme und Timeline werden gerendert", completed_units=0, total_units=len(project["cues"]))
    project_dir = ARTIFACTS / project_id
    for old in project.get("artifacts", []):
        old_path = ARTIFACTS / old["storage_name"]
        archived = project_dir / f"{old['id']}.m4a"
        if old_path.exists() and old_path != archived:
            shutil.copy2(old_path, archived)
            with LOCK:
                old["storage_name"] = str(archived.relative_to(ARTIFACTS))
                save_state()
    # A run owns its scratch files. Persisted narration audio is shared only
    # after an atomic rename, so a restart or duplicate legacy worker cannot
    # expose half-written WAV data to the next render.
    work_dir = project_dir / "work" / run_id
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    wav_files = []
    clips = []
    timeline_cursor = 0
    narration_audio: dict[str, tuple[Path, int]] = {}
    episode_lookup: dict[str, dict[str, Any]] = {}
    episode_audio: dict[str, Path] = {}
    source_episode_samples: dict[str, int] = {}
    if project.get("binding") and project.get("mapping"):
        episode_lookup = {item["id"]: item for item in fetch_plex_episodes(str(project["binding"]["series_id"]))}
        for episode in episode_lookup.values():
            select_episode_audio(episode, project.get("audio_language", ""))
        episode_ids = unique_episode_ids(project.get("mapping", []))
        update_run(
            run_id,
            stage="detecting_intros",
            message="Intros, Commercial-Bumper, Abspänne und Episodenvorschauen werden erkannt",
            completed_units=0,
            total_units=len(episode_ids),
        )
        for episode_index, episode_id in enumerate(episode_ids):
            episode = episode_lookup.get(episode_id)
            if not episode:
                raise RuntimeError(f"Plex-Episode {episode_id} ist nicht mehr verfügbar")
            audio_index = int((episode.get("selected_audio") or {}).get("index", 0))
            episode_wav = extract_episode_delivery_audio(
                episode,
                project_dir / "source-audio" / f"episode-{episode['id']}-a{audio_index}-48k.wav",
            )
            episode_audio[episode_id] = episode_wav
            source_episode_samples[episode_id] = wav_duration_samples(episode_wav)
            update_run(run_id, completed_units=episode_index + 1)
        intro_detection = detect_project_intros(project, episode_audio)
        commercial_bumper_detection = detect_project_commercial_bumpers(project, episode_audio)
        ending_detection = detect_project_endings(project, episode_audio)
        with LOCK:
            project["intro_detection"] = intro_detection
            project["commercial_bumper_detection"] = commercial_bumper_detection
            project["ending_detection"] = ending_detection
            reanchored_cues = reanchor_cues_outside_intros(project)
            # Jede Einfuegung startet wieder exakt an ihrer belegten Szenengrenze;
            # nur so stimmt der Renderer mit der Qualitaetspruefung ueberein,
            # die Grenze plus einmaliges Ruecken erwartet.
            realign_scene_inserts(project)
            # Runs after the re-anchoring so a cue moved out of an intro is
            # checked against the speech it lands next to, not the old one.
            snapped_inserts = snap_inserts_out_of_speech(project)
            project["updated_at"] = now()
            save_state()
        update_run(
            run_id,
            stage="synthesizing",
            message=(
                f"{intro_detection.get('occurrence_count', 0)} Intros und "
                f"{commercial_bumper_detection.get('occurrence_count', 0)} Commercial-Bumper und "
                f"{ending_detection.get('occurrence_count', 0)} Endblöcke erkannt; "
                f"{reanchored_cues} Erzähleranker angepasst; "
                f"{snapped_inserts} Schnitte aus laufender Rede gerückt; "
                f"Erzählstimme wird gerendert"
            ),
            completed_units=0,
            total_units=len(project["cues"]),
        )
    for index, cue in enumerate(project["cues"]):
        wav_path = work_dir / f"cue-{index:03d}.wav"
        binding = project.get("binding") or {}
        voice = binding.get("narrator_voice") or os.getenv("TTS_VOICE", "default")
        language = binding.get("audio_language") or project.get("audio_language", "de")
        narration_key = hashlib.sha256(f"{voice}\0{language}\0{cue['text']}".encode()).hexdigest()
        narration_cache = project_dir / "narration-cache" / f"{narration_key}.wav"
        narration_cache.parent.mkdir(parents=True, exist_ok=True)
        if not narration_cache.exists():
            pending_cache = narration_cache.with_name(f".{narration_key}.{run_id}.pending.wav")
            pending_cache.unlink(missing_ok=True)
            try:
                synthesize_speech(cue["text"], pending_cache, voice=voice, language=language)
                if narration_cache.exists():
                    pending_cache.unlink(missing_ok=True)
                else:
                    pending_cache.replace(narration_cache)
            finally:
                pending_cache.unlink(missing_ok=True)
        shutil.copyfile(narration_cache, wav_path)
        # Lautheit an die Folge angleichen, in der der Kommentar sitzt.
        episode_path = episode_audio.get(str(cue.get("anchor_episode_id") or ""))
        episode_lufs = measure_integrated_loudness(episode_path) if episode_path else None
        target_lufs = (episode_lufs if episode_lufs is not None else NARRATION_LOUDNESS_FALLBACK_LUFS) + NARRATION_LOUDNESS_OFFSET_DB
        cue["loudness_gain_db"] = round(match_narration_loudness(wav_path, target_lufs), 2)
        cue["loudness_target_lufs"] = round(target_lufs, 2)
        duration_samples = wav_duration_samples(wav_path)
        maximum_duration_samples = duration_samples
        if cue.get("placement_policy") in {
            "overlay_speech_free",
            "overlay_intro_music",
            "overlay_opening_gap",
        }:
            gap_start_sample = round(
                int(cue.get("safe_gap_start_ms", cue.get("anchor_insert_ms", 0)))
                * SAMPLE_RATE
                / 1000
            )
            gap_end_sample = round(
                int(cue.get("safe_gap_end_ms", cue.get("anchor_insert_ms", 0)))
                * SAMPLE_RATE
                / 1000
            )
            maximum_duration_samples = min(
                maximum_duration_samples,
                gap_end_sample - gap_start_sample,
            )
        if str(project.get("narration_density") or "") == "audio_drama":
            contract_limit_ms = AUDIO_DRAMA_DURATION_LIMITS_MS.get(
                str(cue.get("narrative_purpose") or ""),
                12_000,
            ) + 2_000
            maximum_duration_samples = min(
                maximum_duration_samples,
                round(contract_limit_ms * SAMPLE_RATE / 1000),
            )
        try:
            duration_samples, time_fit_rate = fit_narration_to_duration(
                wav_path,
                maximum_duration_samples,
            )
        except RuntimeError as fit_error:
            # A comment that will not fit its pause even after the pitch-safe
            # stretch is not a render failure: the repair below turns it into an
            # exact scene-boundary insert, which needs no pause at all.
            if cue.get("placement_policy") not in {
                "overlay_speech_free",
                "overlay_intro_music",
                "overlay_opening_gap",
            }:
                # Ein zu langer Clip soll den Render nicht mehr kippen: der
                # Cue wird auf das erlaubte Maximum gestrafft und laeuft ueber
                # seine Klangflaeche hinaus; das Qualitaetsgate meldet es als
                # Warnung. Vorher scheiterte die ganze Folge an einem Satz.
                warning = f"Cue {cue.get('id')}: {fit_error}; Clip wurde auf {NARRATION_MAX_TIME_FIT:.2f}x gestrafft und ragt ueber die Klangflaeche hinaus"
                with LOCK:
                    warnings_list = project.setdefault("warnings", [])
                    if warning not in warnings_list:
                        warnings_list.append(warning)
                duration_samples, time_fit_rate = fit_narration_to_duration(
                    wav_path,
                    max(1, round(wav_duration_samples(wav_path) / NARRATION_MAX_TIME_FIT)),
                )
                cue["time_fit_overrun"] = True
            else:
                duration_samples = wav_duration_samples(wav_path)
                time_fit_rate = 1.0
        cue["time_fit_rate"] = round(time_fit_rate, 6)
        cue["measured_duration_ms"] = round(duration_samples * 1000 / SAMPLE_RATE)
        narration_audio[cue["id"]] = (wav_path, duration_samples)
        update_run(run_id, completed_units=index + 1)

    anchor_segments = {
        str(item.get("id") or ""): item
        for item in (project.get("reconciled_transcript") or project.get("transcript", []))
    }
    overlay_policies = {"overlay_speech_free", "overlay_intro_music", "overlay_opening_gap"}
    for cue in project["cues"]:
        if cue.get("placement_policy") not in overlay_policies:
            continue
        _, duration_samples = narration_audio[cue["id"]]
        anchor = anchor_segments.get(str(cue.get("anchor_segment_id") or ""))
        if cue.get("placement_policy") == "overlay_speech_free":
            if not anchor:
                raise RuntimeError(f"Erzähler-Cue {cue.get('id')} besitzt keinen belegten Szenenanker")
            if enforce_character_introduction_sync(cue, anchor, rules):
                continue
        gap_start_sample = round(int(cue.get("safe_gap_start_ms", 0)) * SAMPLE_RATE / 1000)
        gap_end_sample = round(int(cue.get("safe_gap_end_ms", 0)) * SAMPLE_RATE / 1000)
        if gap_end_sample - gap_start_sample >= duration_samples:
            continue
        if not anchor:
            raise RuntimeError(f"Erzähler-Cue {cue.get('id')} besitzt keinen belegten Szenenanker")
        apply_aligned_scene_insert(cue, anchor)
    # Eben umgewandelte Overlays sitzen sonst mitten in einer Aeusserung.
    snap_inserts_out_of_speech(project)

    silence_before = work_dir / "narration-gap-before.wav"
    silence_after = work_dir / "narration-gap-after.wav"
    make_silence(silence_before, int(0.4 * SAMPLE_RATE))
    make_silence(silence_after, int(0.75 * SAMPLE_RATE))
    cadence_silences: dict[int, Path] = {
        400: silence_before,
        750: silence_after,
    }

    def cadence_silence(duration_ms: int) -> Path:
        if duration_ms not in cadence_silences:
            path = work_dir / f"narration-gap-{duration_ms}ms.wav"
            make_silence(path, round(duration_ms * SAMPLE_RATE / 1000))
            cadence_silences[duration_ms] = path
        return cadence_silences[duration_ms]

    rendered_cues: set[str] = set()
    excluded_source_intervals: dict[str, list[dict[str, Any]]] = {}
    intro_intervals = intro_intervals_by_episode(project)
    commercial_bumper_intervals = commercial_bumper_intervals_by_episode(project)
    ending_intervals = ending_intervals_by_episode(project)
    boundary_plan = book_boundary_plan(
        project,
        {
            episode_id: round(samples * 1000 / SAMPLE_RATE)
            for episode_id, samples in source_episode_samples.items()
        },
    )
    for episode_id in unique_episode_ids(project.get("mapping", [])):
        episode = episode_lookup.get(episode_id)
        if not episode:
            continue
        episode_wav = episode_audio.get(episode_id)
        if not episode_wav:
            audio_index = int((episode.get("selected_audio") or {}).get("index", 0))
            episode_wav = extract_episode_delivery_audio(
                episode,
                project_dir / "source-audio" / f"episode-{episode['id']}-a{audio_index}-48k.wav",
            )
        episode_samples = source_episode_samples.get(episode_id) or wav_duration_samples(episode_wav)
        source_episode_samples[episode_id] = episode_samples
        intro_exclusions = [
            (
                max(0, round(start_ms * SAMPLE_RATE / 1000)),
                min(episode_samples, round(end_ms * SAMPLE_RATE / 1000)),
            )
            for start_ms, end_ms in intro_intervals.get(episode_id, [])
        ]
        occurrence_by_interval = {
            (int(item.get("start_ms", 0)), int(item.get("end_ms", 0))): item
            for item in (project.get("intro_detection") or {}).get("occurrences", [])
            if str(item.get("episode_id") or "") == episode_id
        }
        exclusion_records = [
            {
                "start_sample": start_sample,
                "end_sample": end_sample,
                "reason": "repeated_intro_fingerprint",
                "confidence": float(
                    occurrence_by_interval.get(
                        (
                            round(start_sample * 1000 / SAMPLE_RATE),
                            round(end_sample * 1000 / SAMPLE_RATE),
                        ),
                        {},
                    ).get("confidence", (project.get("intro_detection") or {}).get("confidence", 0))
                ),
            }
            for start_sample, end_sample in intro_exclusions
            if end_sample > start_sample
        ]
        commercial_occurrence_by_interval = {
            (int(item.get("start_ms", 0)), int(item.get("end_ms", 0))): item
            for item in (project.get("commercial_bumper_detection") or {}).get("occurrences", [])
            if str(item.get("episode_id") or "") == episode_id
        }
        exclusion_records.extend(
            {
                "start_sample": max(0, round(start_ms * SAMPLE_RATE / 1000)),
                "end_sample": min(episode_samples, round(end_ms * SAMPLE_RATE / 1000)),
                "reason": "repeated_commercial_bumper",
                "confidence": float(
                    commercial_occurrence_by_interval.get((start_ms, end_ms), {}).get(
                        "confidence",
                        (project.get("commercial_bumper_detection") or {}).get("confidence", 0),
                    )
                ),
            }
            for start_ms, end_ms in commercial_bumper_intervals.get(episode_id, [])
            if end_ms > start_ms
        )
        ending_occurrence_by_interval = {
            (int(item.get("start_ms", 0)), int(item.get("end_ms", 0))): item
            for item in (project.get("ending_detection") or {}).get("occurrences", [])
            if str(item.get("episode_id") or "") == episode_id
        }
        exclusion_records.extend(
            {
                "start_sample": max(0, round(start_ms * SAMPLE_RATE / 1000)),
                "end_sample": min(episode_samples, round(end_ms * SAMPLE_RATE / 1000)),
                "reason": "ending_and_next_episode_preview",
                "confidence": float(
                    ending_occurrence_by_interval.get((start_ms, end_ms), {}).get(
                        "confidence",
                        (project.get("ending_detection") or {}).get("confidence", 0),
                    )
                ),
            }
            for start_ms, end_ms in ending_intervals.get(episode_id, [])
            if end_ms > start_ms
        )
        exclusion_records.extend(
            {
                "start_sample": max(0, round(int(item["start_ms"]) * SAMPLE_RATE / 1000)),
                "end_sample": min(episode_samples, round(int(item["end_ms"]) * SAMPLE_RATE / 1000)),
                "reason": str(item["reason"]),
                "confidence": float(item.get("confidence") or 0),
            }
            for item in boundary_plan.get("exclusions", {}).get(episode_id, [])
            if int(item["end_ms"]) > int(item["start_ms"])
        )
        exclusion_records.sort(key=lambda item: (int(item["start_sample"]), int(item["end_sample"])))
        excluded_source_intervals[episode_id] = exclusion_records
        episode_exclusions = [
            (int(item["start_sample"]), int(item["end_sample"]))
            for item in exclusion_records
        ]
        source_piece_index = 0

        def append_source(start_sample: int, end_sample: int, strategy: str) -> int:
            nonlocal timeline_cursor, source_piece_index
            appended = 0
            for included_start, included_end in source_ranges_after_exclusions(
                start_sample,
                end_sample,
                episode_exclusions,
            ):
                source_slice = work_dir / f"episode-{episode_id}-source-{source_piece_index:04d}.wav"
                source_piece_index += 1
                slice_wav(episode_wav, source_slice, included_start, included_end)
                wav_files.append(source_slice)
                clips.append(
                    {
                        "clip_id": new_id("clip"),
                        "episode_id": episode_id,
                        "track_id": "trk_episode",
                        "timeline_start_sample": timeline_cursor,
                        "source_start_sample": included_start,
                        "source_end_sample": included_end,
                        "strategy": strategy,
                        "gain_millidb": 0,
                    }
                )
                duration = included_end - included_start
                timeline_cursor += duration
                appended += duration
            return appended

        episode_cues = sorted(
            [
                cue for cue in project["cues"]
                if str(cue.get("anchor_episode_id") or "") == episode_id
                and cue.get("placement_policy") in {
                    "before_aligned_dialog",
                    "after_aligned_speech_gap",
                    "insert_for_dense_action",
                    "overlay_speech_free",
                    "overlay_intro_music",
                    "overlay_opening_gap",
                    "replace_existing_narration",
                    ALIGNED_SCENE_INSERT_POLICY,
                }
            ],
            key=lambda cue: int(cue.get("anchor_insert_ms", 0)),
        )
        source_cursor = 0
        for cue_position, cue in enumerate(episode_cues):
            wav_path, duration_samples = narration_audio[cue["id"]]
            if cue.get("placement_policy") in {"overlay_speech_free", "overlay_intro_music", "overlay_opening_gap"}:
                gap_start_sample = max(
                    source_cursor,
                    round(int(cue.get("safe_gap_start_ms", cue.get("anchor_insert_ms", 0))) * SAMPLE_RATE / 1000),
                )
                gap_end_sample = min(
                    episode_samples,
                    round(int(cue.get("safe_gap_end_ms", cue.get("anchor_insert_ms", 0))) * SAMPLE_RATE / 1000),
                )
                if gap_end_sample - gap_start_sample < duration_samples:
                    raise RuntimeError(f"Sprachpause für Erzähler-Cue {cue.get('id')} ist nach TTS zu kurz")
                overlay_start = closest_overlay_start_sample(
                    cue,
                    gap_start_sample,
                    gap_end_sample,
                    duration_samples,
                )
                overlay_end = overlay_start + duration_samples
                if any(start < overlay_end and end > overlay_start for start, end in episode_exclusions):
                    raise RuntimeError(f"Erzähler-Cue {cue.get('id')} überlappt eine entfernte Introspur")
                if overlay_start > source_cursor:
                    append_source(source_cursor, overlay_start, "aligned_segment")
                overlay_wav = work_dir / f"episode-{episode_id}-overlay-{cue_position:03d}.wav"
                mix_narration_over_source(episode_wav, wav_path, overlay_wav, overlay_start, overlay_end)
                wav_files.append(overlay_wav)
                cue["timeline_start_sample"] = timeline_cursor
                cue["placement_sample_in_episode"] = overlay_start
                cue["render_status"] = "overlaid_on_source"
                clips.append({"clip_id": new_id("clip"), "episode_id": episode_id, "track_id": "trk_episode", "timeline_start_sample": timeline_cursor, "source_start_sample": overlay_start, "source_end_sample": overlay_end, "strategy": "sidechain_ducked_ambience_overlay", "gain_millidb": -9000})
                clips.append({"clip_id": new_id("clip"), "cue_id": cue["id"], "anchor_segment_id": cue.get("anchor_segment_id"), "track_id": "trk_narrator", "timeline_start_sample": timeline_cursor, "source_start_sample": 0, "source_end_sample": duration_samples, "strategy": cue.get("placement_policy"), "gain_millidb": 0})
                timeline_cursor += duration_samples
                source_cursor = overlay_end
                rendered_cues.add(cue["id"])
                continue
            split_sample = max(source_cursor, min(episode_samples, round(int(cue.get("anchor_insert_ms", 0)) * SAMPLE_RATE / 1000)))
            resume_sample = split_sample
            if cue.get("placement_policy") == "replace_existing_narration":
                resume_sample = max(
                    split_sample,
                    min(episode_samples, round(int(cue.get("replacement_end_ms", cue.get("anchor_insert_ms", 0))) * SAMPLE_RATE / 1000)),
                )
            if split_sample > source_cursor:
                append_source(source_cursor, split_sample, "aligned_segment")
            cadence_before_ms, cadence_after_ms = narration_cadence_ms(cue)
            cadence_before_samples = round(cadence_before_ms * SAMPLE_RATE / 1000)
            cadence_after_samples = round(cadence_after_ms * SAMPLE_RATE / 1000)
            cue["cadence_before_ms"] = cadence_before_ms
            cue["cadence_after_ms"] = cadence_after_ms
            wav_files.append(cadence_silence(cadence_before_ms))
            timeline_cursor += cadence_before_samples
            cue["timeline_start_sample"] = timeline_cursor
            cue["placement_sample_in_episode"] = split_sample
            cue["render_status"] = "inserted_into_timeline"
            apply_edge_fades(wav_path, NARRATION_EDGE_FADE_MS, NARRATION_EDGE_FADE_MS)
            wav_files.extend([wav_path, cadence_silence(cadence_after_ms)])
            clips.append({"clip_id": new_id("clip"), "cue_id": cue["id"], "anchor_segment_id": cue.get("anchor_segment_id"), "track_id": "trk_narrator", "timeline_start_sample": timeline_cursor, "source_start_sample": 0, "source_end_sample": duration_samples, "strategy": cue.get("placement_policy"), "gain_millidb": 0})
            timeline_cursor += duration_samples + cadence_after_samples
            source_cursor = resume_sample
            rendered_cues.add(cue["id"])
        if source_cursor < episode_samples:
            append_source(source_cursor, episode_samples, "episode_sequence")
            wav_files.append(silence_after)
            timeline_cursor += int(0.75 * SAMPLE_RATE)

    for cue in project["cues"]:
        if cue["id"] in rendered_cues:
            continue
        cue["render_status"] = "omitted_unplaced"
    output_wav = project_dir / "preview.wav"
    concat_wavs(wav_files, output_wav)
    delivery = project_dir / "audiobook.m4a"
    encode_delivery(output_wav, delivery)
    sha = hashlib.sha256(delivery.read_bytes()).hexdigest()
    timeline_revision = next_timeline_revision(project)
    speech_cut_collisions = narration_speech_cut_collisions(project)
    replaced_narration_ms = sum(
        max(0, int(cue.get("replacement_end_ms", 0)) - int(cue.get("replacement_start_ms", 0)))
        for cue in project.get("cues", [])
        if cue.get("placement_policy") == "replace_existing_narration"
    )
    overlay_count = sum(1 for cue in project.get("cues", []) if cue.get("render_status") == "overlaid_on_source")
    forced_insert_count = sum(1 for cue in project.get("cues", []) if cue.get("placement_policy") == "insert_for_dense_action")
    aligned_scene_insert_count = sum(
        1
        for cue in project.get("cues", [])
        if cue.get("placement_policy") == ALIGNED_SCENE_INSERT_POLICY
    )
    omitted_count = sum(1 for cue in project.get("cues", []) if str(cue.get("render_status") or "").startswith("omitted_"))
    removed_intro_count = sum(
        1
        for items in excluded_source_intervals.values()
        for item in items
        if item.get("reason") == "repeated_intro_fingerprint"
    )
    removed_intro_samples = sum(
        int(item["end_sample"]) - int(item["start_sample"])
        for items in excluded_source_intervals.values()
        for item in items
        if item.get("reason") == "repeated_intro_fingerprint"
    )
    removed_ending_count = sum(
        1
        for items in excluded_source_intervals.values()
        for item in items
        if item.get("reason") == "ending_and_next_episode_preview"
    )
    removed_ending_samples = sum(
        int(item["end_sample"]) - int(item["start_sample"])
        for items in excluded_source_intervals.values()
        for item in items
        if item.get("reason") == "ending_and_next_episode_preview"
    )
    removed_commercial_bumper_count = sum(
        1
        for items in excluded_source_intervals.values()
        for item in items
        if item.get("reason") == "repeated_commercial_bumper"
    )
    removed_commercial_bumper_samples = sum(
        int(item["end_sample"]) - int(item["start_sample"])
        for items in excluded_source_intervals.values()
        for item in items
        if item.get("reason") == "repeated_commercial_bumper"
    )
    removed_boundary_count = sum(
        1
        for items in excluded_source_intervals.values()
        for item in items
        if str(item.get("reason") or "").startswith("book_boundary_")
    )
    removed_boundary_samples = sum(
        int(item["end_sample"]) - int(item["start_sample"])
        for items in excluded_source_intervals.values()
        for item in items
        if str(item.get("reason") or "").startswith("book_boundary_")
    )
    timeline = {
        "schema_version": "1.0.0",
        "revision": timeline_revision,
        "timebase": {"sample_rate": SAMPLE_RATE, "intervals": "half_open"},
        "duration_samples": timeline_cursor,
        "source_episode_samples": source_episode_samples,
        "excluded_source_intervals": excluded_source_intervals,
        "tracks": [{"track_id": "trk_narrator", "role": "narrator", "mix_order": 0}, {"track_id": "trk_episode", "role": "source_episode", "mix_order": 1}],
        "clips": clips,
        "validation": {
            "status": "valid" if speech_cut_collisions == 0 else "review",
            "dialog_collisions": speech_cut_collisions,
            "replaced_source_narration_ms": replaced_narration_ms,
            "speech_free_overlay_count": overlay_count,
            "dense_action_insert_count": forced_insert_count,
            "aligned_scene_insert_count": aligned_scene_insert_count,
            "omitted_narration_count": omitted_count,
            "removed_intro_count": removed_intro_count,
            "removed_intro_duration_ms": round(removed_intro_samples * 1000 / SAMPLE_RATE),
            "removed_ending_count": removed_ending_count,
            "removed_ending_duration_ms": round(removed_ending_samples * 1000 / SAMPLE_RATE),
            "removed_commercial_bumper_count": removed_commercial_bumper_count,
            "removed_commercial_bumper_duration_ms": round(
                removed_commercial_bumper_samples * 1000 / SAMPLE_RATE
            ),
            "book_boundary_status": str(boundary_plan.get("status") or "not_applicable"),
            "book_boundary_exclusion_count": removed_boundary_count,
            "book_boundary_exclusion_duration_ms": round(removed_boundary_samples * 1000 / SAMPLE_RATE),
            "book_boundary_detail": str(boundary_plan.get("detail") or ""),
            "intro_detection_status": str((project.get("intro_detection") or {}).get("status") or "not_run"),
            "commercial_bumper_detection_status": str(
                (project.get("commercial_bumper_detection") or {}).get("status") or "not_run"
            ),
            "ending_detection_status": str((project.get("ending_detection") or {}).get("status") or "not_run"),
            "note": (
                f"{removed_intro_count} wiederkehrende Introspuren wurden per Audio-Fingerabdruck entfernt. "
                f"{removed_commercial_bumper_count} Commercial-Bumper wurden per Midroll-Cluster entfernt. "
                f"{removed_ending_count} Abspann-/Vorschau-Endblöcke wurden vollständig entfernt. "
                f"{removed_boundary_count} serienfremde Randbereiche wurden an belegten Buchgrenzen entfernt. "
                "Erzähler-Cues nutzen zuerst dialogfreie Musik-/Bildpassagen. Wo diese fehlen, pausiert die "
                "Quellfolge samplegenau an der belegten Szenengrenze und setzt danach lückenlos fort."
            ),
        },
    }
    artifact = {
        "id": new_id("art"),
        "role": "delivery",
        "filename": f"{safe_filename(project['title'])}.m4a",
        "storage_name": f"{project_id}/audiobook.m4a",
        "mime_type": "audio/mp4",
        "bytes": delivery.stat().st_size,
        "sha256": sha,
        "audio": inspect_delivery_audio(delivery),
        "created_at": now(),
    }
    immutable_delivery = project_dir / f"{artifact['id']}.m4a"
    shutil.copy2(delivery, immutable_delivery)
    artifact["storage_name"] = str(immutable_delivery.relative_to(ARTIFACTS))
    quality_report = build_automatic_quality_report(project, timeline, artifact)
    timeline["validation"]["automatic_gate_status"] = quality_report["status"]
    timeline["validation"]["automatic_release_ready"] = quality_report["release_ready"]
    manifest = project_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "project_id": project_id,
                "source_sha256": project["source"]["sha256"],
                "intro_detection": project.get("intro_detection"),
                "commercial_bumper_detection": project.get("commercial_bumper_detection"),
                "timeline": timeline,
                "artifact": artifact,
                "quality_report": quality_report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    with LOCK:
        project["timeline"] = timeline
        project["timeline_revision"] = timeline_revision
        project.setdefault("audio_history", []).extend(project.get("artifacts", []))
        project["artifacts"] = [artifact]
        project["audio_stale"] = False
        project["quality_report"] = quality_report
        sync_narration_review(project)
        project["stage"] = 7
        project["status"] = "completed"
        project["updated_at"] = now()
        save_state()
    repair_failed = bool(STATE["runs"].get(run_id, {}).get("quality_repair")) and not quality_report["release_ready"]
    unresolved = [str(check.get("label") or check["id"]) for check in quality_report["checks"] if check["status"] == "failed"]
    update_run(
        run_id,
        status="failed" if repair_failed else "succeeded",
        stage="completed",
        message=("Reparatur unvollständig: " + "; ".join(unresolved)
                 if repair_failed else "Render und automatische Qualitätsprüfung sind fertig"),
        error="Quality repair did not pass: " + "; ".join(unresolved) if repair_failed else None,
        completed_units=len(project["cues"]),
    )


def synthesize_speech(text: str, output: Path, voice: str, language: str) -> None:
    """Rendert eine Erzähler-Zeile auf OmniVoice bzw. Kikiri.

    Szenenklang fiel bei einem TTS-Fehler still auf ``espeak-ng`` und zuletzt
    auf einen 220-Hz-Sinuston zurück. Beides landete unbemerkt in der fertigen
    Abmischung — ein Piepton im Hörspiel ist schlimmer als ein sichtbar
    fehlgeschlagener Lauf. Den Wechsel OmniVoice -> Kikiri leistet Vocarium
    ohnehin über die Stimmenzuordnung, deshalb bricht der Render hier hart ab.
    """
    del language  # Beide Engines sind über die Stimme selbst sprachgebunden.
    voices = engine_voice_map()
    engine = voices.get(voice)
    if engine is None and voices:
        lowered = voice.casefold()
        engine = next((value for key, value in voices.items() if key.casefold() == lowered), None)
    if engine is None:
        raise RuntimeError(f"Stimme {voice!r} ist weder bei OmniVoice noch bei Kikiri bekannt")
    try:
        from podcast.tags import sanitize as sanitize_tags

        spoken = sanitize_tags(text, engine=engine)
    except Exception:
        spoken = text
    spoken = (spoken or "").strip()
    if not spoken:
        raise RuntimeError("Die Erzähler-Zeile enthält nach der Tag-Bereinigung keinen sprechbaren Text")
    bases = engine_base_urls()
    if engine == "omnivoice":
        endpoint = f"{bases['omnivoice']}/v1/audio/speech"
        payload = {"model": "omnivoice", "input": spoken, "voice": voice, "response_format": "wav"}
    else:
        endpoint = f"{bases['kikiri']}/v1/audio/speech"
        payload = {"model": voice, "input": spoken, "voice": "default", "response_format": "wav"}
    ffmpeg = shutil.which("ffmpeg")
    provider_output = output.with_suffix(".provider.wav")
    try:
        with httpx.stream("POST", endpoint, json=payload, timeout=3600) as response:
            response.raise_for_status()
            with provider_output.open("wb") as target:
                for chunk in response.iter_bytes():
                    target.write(chunk)
        if not ffmpeg:
            shutil.move(provider_output, output)
            return
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(provider_output), "-ar", str(SAMPLE_RATE), "-ac", "1", str(output)],
            check=True,
            timeout=300,
        )
        provider_output.unlink(missing_ok=True)
    except Exception as error:
        provider_output.unlink(missing_ok=True)
        raise RuntimeError(f"{engine} konnte die Stimme {voice!r} nicht rendern: {error}") from error


def make_silence(path: Path, samples: int) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(b"\x00\x00" * samples)


# Erzaehlerclips kommen von OmniVoice nahe Vollaussteuerung, Serienton liegt
# typischerweise 15-20 dB darunter. Ohne Angleichung ist der Erzaehler "viel
# lauter als der Rest". Ziel: integrierte Lautheit der jeweiligen Folge plus
# ein kleiner Vorsprung, damit der Kommentar traegt, aber nicht schreit.
NARRATION_LOUDNESS_OFFSET_DB = float(os.getenv("NARRATION_LOUDNESS_OFFSET_DB", "1.5"))
NARRATION_LOUDNESS_FALLBACK_LUFS = float(os.getenv("NARRATION_LOUDNESS_FALLBACK_LUFS", "-20"))
_LOUDNESS_CACHE: dict[str, float] = {}


def measure_integrated_loudness(path: Path) -> float | None:
    """EBU R128 integrierte Lautheit in LUFS via ffmpeg, None wenn nicht messbar."""
    key = f"{path}:{path.stat().st_mtime_ns if path.exists() else 0}"
    if key in _LOUDNESS_CACHE:
        return _LOUDNESS_CACHE[key]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not path.exists():
        return None
    try:
        result = subprocess.run(
            [ffmpeg, "-nostats", "-i", str(path), "-map", "0:a:0", "-af", "ebur128=peak=none", "-f", "null", "-"],
            capture_output=True, text=True, timeout=900,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    match = None
    for candidate in re.finditer(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", result.stderr):
        match = candidate
    if not match:
        return None
    value = float(match.group(1))
    if value < -70:
        return None
    _LOUDNESS_CACHE[key] = value
    return value


def match_narration_loudness(wav_path: Path, target_lufs: float) -> float:
    """Zieht den Clip auf target_lufs (tonhoehenneutral, mit Limiter); gibt die Aenderung in dB zurueck."""
    measured = measure_integrated_loudness(wav_path)
    if measured is None:
        return 0.0
    gain_db = max(-30.0, min(12.0, target_lufs - measured))
    if abs(gain_db) < 0.3:
        return 0.0
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return 0.0
    adjusted = wav_path.with_name(wav_path.stem + ".loud.wav")
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(wav_path), "-af", f"volume={gain_db:.2f}dB,alimiter=limit=0.95:attack=5:release=50",
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(adjusted)],
        check=True, timeout=180,
    )
    adjusted.replace(wav_path)
    return gain_db


def wav_duration_samples(path: Path) -> int:
    with wave.open(str(path), "rb") as source:
        return round(source.getnframes() * SAMPLE_RATE / source.getframerate())


# OmniVoice spricht deutsche Erzaehlpassagen langsamer, als der Skript-
# Schaetzer (390 ms/Wort) annimmt; 1.10x reichte fuer viele Clips nicht.
# atempo bleibt tonhoehenerhaltend, bis ~1.25x ist die Straffung unauffaellig.
NARRATION_MAX_TIME_FIT = float(os.getenv("NARRATION_MAX_TIME_FIT", "1.25"))


def fit_narration_to_duration(
    narration_path: Path,
    maximum_samples: int,
    maximum_rate: float | None = None,
) -> tuple[int, float]:
    """Pitch-preservingly fit a small TTS overrun to an editorial audio budget."""
    if maximum_rate is None:
        maximum_rate = NARRATION_MAX_TIME_FIT
    duration_samples = wav_duration_samples(narration_path)
    if duration_samples <= maximum_samples:
        return duration_samples, 1.0
    if maximum_samples <= 0:
        raise RuntimeError("Sprachfreie Klangfläche hat keine nutzbare Dauer")
    required_rate = duration_samples / maximum_samples
    if required_rate > maximum_rate:
        raise RuntimeError(
            f"Erzählerclip benötigt {required_rate:.3f}x Time-Fit; erlaubt sind höchstens {maximum_rate:.2f}x"
        )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg fehlt für das Erzähler-Time-Fit")
    applied_rate = min(maximum_rate, required_rate * 1.002)
    fitted_path = narration_path.with_name(f".{narration_path.stem}.time-fit.wav")
    fitted_path.unlink(missing_ok=True)
    try:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-i",
                str(narration_path),
                "-filter:a",
                f"atempo={applied_rate:.6f}",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(fitted_path),
            ],
            check=True,
            timeout=180,
        )
        fitted_samples = wav_duration_samples(fitted_path)
        if fitted_samples > maximum_samples:
            raise RuntimeError("Erzähler-Time-Fit überschreitet weiterhin die Klangfläche")
        fitted_path.replace(narration_path)
        return fitted_samples, applied_rate
    finally:
        fitted_path.unlink(missing_ok=True)


SOURCE_EDGE_FADE_MS = 40
NARRATION_EDGE_FADE_MS = 25


def ramp_pcm_frames(frames: bytes, fade_in_samples: int, fade_out_samples: int) -> bytes:
    """Linear ramps on the outer samples so a splice does not click.

    A sample-accurate cut leaves a step in the waveform, which is exactly the
    hard edit heard around inserted narration.
    """
    if not frames or (fade_in_samples <= 0 and fade_out_samples <= 0):
        return frames
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    total = len(samples)
    if total < 4:
        return frames
    limit = total // 2
    fade_in = max(0, min(fade_in_samples, limit))
    fade_out = max(0, min(fade_out_samples, limit))
    for index in range(fade_in):
        samples[index] = int(samples[index] * index / fade_in)
    for offset in range(fade_out):
        index = total - 1 - offset
        samples[index] = int(samples[index] * offset / fade_out)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def apply_edge_fades(path: Path, fade_in_ms: int, fade_out_ms: int) -> None:
    with wave.open(str(path), "rb") as source:
        if source.getframerate() != SAMPLE_RATE or source.getnchannels() != 1 or source.getsampwidth() != 2:
            return
        frames = source.readframes(source.getnframes())
    faded = ramp_pcm_frames(
        frames,
        round(fade_in_ms * SAMPLE_RATE / 1000),
        round(fade_out_ms * SAMPLE_RATE / 1000),
    )
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(faded)


def slice_wav(source_path: Path, target_path: Path, start_sample: int, end_sample: int) -> None:
    with wave.open(str(source_path), "rb") as source:
        if source.getframerate() != SAMPLE_RATE or source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise RuntimeError("Timeline-Schnitt erwartet Mono-PCM mit 48 kHz und 16 Bit")
        start = max(0, min(start_sample, source.getnframes()))
        end = max(start, min(end_sample, source.getnframes()))
        source.setpos(start)
        frames = source.readframes(end - start)
    edge_samples = round(SOURCE_EDGE_FADE_MS * SAMPLE_RATE / 1000)
    frames = ramp_pcm_frames(frames, edge_samples, edge_samples)
    with wave.open(str(target_path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(frames)


def mix_narration_over_source(
    source_path: Path,
    narration_path: Path,
    target_path: Path,
    start_sample: int,
    end_sample: int,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg fehlt für das Erzähler-Overlay")
    duration_samples = max(1, end_sample - start_sample)
    filter_graph = (
        f"[0:a]atrim=start_sample={start_sample}:end_sample={end_sample},"
        "asetpts=PTS-STARTPTS,apad[background];"
        f"[1:a]atrim=start_sample=0:end_sample={duration_samples},"
        "asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.08,"
        f"afade=t=out:st={max(0, (duration_samples / SAMPLE_RATE) - 0.14):.6f}:d=0.14,"
        # Keep the input branches alive until the explicit output sample limit.
        # Finite branch EOF can terminate this graph before queued source audio
        # is drained. Pad before mixing, then trim the mix to the original window;
        # padding an already truncated mix would silently lose native audio.
        "apad,asplit=2[voice_key][voice];"
        "[background][voice_key]sidechaincompress=threshold=0.035:ratio=8:"
        "attack=15:release=180:makeup=1[ducked];"
        "[ducked][voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"atrim=end_sample={duration_samples}[mix]"
    )
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error", "-i", str(source_path), "-i", str(narration_path),
            "-filter_complex", filter_graph, "-map", "[mix]", "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-c:a", "pcm_s16le", str(target_path),
        ],
        check=True,
        timeout=180,
    )


def concat_wavs(paths: list[Path], output: Path) -> None:
    with wave.open(str(output), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        for path in paths:
            with wave.open(str(path), "rb") as source:
                if source.getframerate() == SAMPLE_RATE and source.getnchannels() == 1 and source.getsampwidth() == 2:
                    target.writeframes(source.readframes(source.getnframes()))


def delivery_timeout_seconds(duration_samples: int) -> int:
    # Allow at least 20x realtime encoding plus startup/finalization headroom.
    return max(180, math.ceil(duration_samples / SAMPLE_RATE / 20) + 60)


def encode_delivery(source: Path, target: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        timeout = delivery_timeout_seconds(wav_duration_samples(source))
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(source), "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)], check=True, timeout=timeout)
    else:
        shutil.copyfile(source, target)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-").lower()
    return cleaned[:80] or "hoerspiel"


# ══════════════════════════════════════════════════════════════════════════
# Anbindung an Vocarium
# ══════════════════════════════════════════════════════════════════════════

_LEGACY_SOURCE_TOKENS = {
    "vocarium_qwen3_asr": "vocarium_whisper",
    "qwen3_forced_aligner": "whisper_segments",
    "plex_srt_reconciled_with_qwen3_words": "plex_srt_reconciled_with_whisper_words",
}
_SOURCE_FIELDS = ("transcription_source", "timestamp_source", "anchor_text_source")


def _rewrite_source_tokens(container: Any) -> int:
    """Ersetzt alte Qwen-Provenienz-Marker in einem Dict rekursiv."""
    if not isinstance(container, dict):
        return 0
    touched = 0
    for field in _SOURCE_FIELDS:
        value = container.get(field)
        if isinstance(value, str) and value in _LEGACY_SOURCE_TOKENS:
            container[field] = _LEGACY_SOURCE_TOKENS[value]
            touched += 1
    return touched


def migrate_transcription_source_tokens() -> None:
    """Zieht bestehende Projekte auf die Whisper-Benennung nach.

    Die Qualitätstore vergleichen ``timestamp_source`` gegen feste Zeichenketten
    (0.92 Konfidenz für echte Wortzeitmarken, 0.5 für Chunk-Grenzen). Ohne diese
    Migration würden importierte Szenenklang-Projekte auf den strengen Pfad
    fallen und Läufe grundlos blockieren.
    """
    touched = 0
    with LOCK:
        for project in STATE.get("projects", {}).values():
            touched += _rewrite_source_tokens(project)
            for key in ("transcript", "transcript_words", "cues", "semantic_cues"):
                value = project.get(key)
                if isinstance(value, dict):
                    touched += _rewrite_source_tokens(value)
                elif isinstance(value, list):
                    for entry in value:
                        touched += _rewrite_source_tokens(entry)
    if touched:
        logger.info("Hörspiele: %d Provenienz-Marker auf Whisper umgestellt", touched)
        save_state()


def set_data_root(root: Path) -> None:
    """Datenwurzel nachziehen, falls sie erst beim Mounten feststeht."""
    global ROOT, UPLOADS, ARTIFACTS
    ROOT = Path(root)
    UPLOADS = ROOT / "uploads"
    ARTIFACTS = ROOT / "artifacts"


def configure(
    *,
    get_current_user: Callable[..., Any],
    store: StateStore,
    tts_bridge: Any = None,
    gpu_submit: Callable[..., Any] | None = None,
    data_root: Path | None = None,
) -> APIRouter:
    """Verdrahtet die Engine mit Vocarium und liefert den Router.

    Szenenklang lud den Zustand und startete Wiederaufnahme sowie Migration
    beim Import des Moduls. In Vocarium existiert zu diesem Zeitpunkt weder
    die Datenbank noch die Benutzerauflösung; das übernimmt :func:`start`.
    """
    if data_root is not None:
        set_data_root(data_root)
    _DEPS["get_current_user"] = get_current_user
    _DEPS["store"] = store
    _DEPS["tts_bridge"] = tts_bridge
    _DEPS["gpu_submit"] = gpu_submit
    return router


def start() -> None:
    """Zustand hydrieren, sobald die Datenbank steht.

    Getrennt von :func:`configure`, weil der Router beim Import von ``main``
    gemountet wird, ``init_db()`` aber erst im Startup-Ereignis läuft.
    """
    with LOCK:
        STATE.clear()
        STATE.update(load_state())
    migrate_transcription_source_tokens()
    migrate_persisted_transcription_markers()
    recover_interrupted_runs()
    with LOCK:
        projects = len(STATE.get("projects", {}))
        runs = len(STATE.get("runs", {}))
    logger.info("Hörspiele bereit: %d Projekte, %d Läufe, Daten unter %s", projects, runs, ROOT)
