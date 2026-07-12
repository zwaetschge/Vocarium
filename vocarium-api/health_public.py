"""Redacted public health payloads for the unauthenticated readiness route."""

from __future__ import annotations

from typing import Any


_PUBLIC_TTS_STATUSES = frozenset({"ok", "unreachable"})
_PUBLIC_TTS_MODELS = frozenset({"1.7b-base", "1.7b-design", "1.7b-custom"})
_PUBLIC_SERVICE_TYPES = ("tts", "tts_extra", "asr", "music", "sfx")
_MAX_PUBLIC_COUNT = 10_000
_MAX_PUBLIC_GPU_INDEX = 255
_MAX_PUBLIC_MEMORY_MIB = 1_048_576


def _plain_dict(value: object) -> dict[str, Any]:
    # Upstream JSON objects decode to plain dictionaries. Reject subclasses and
    # other mapping-like objects so public health never invokes attacker-defined
    # conversion or lookup methods.
    if type(value) is dict:
        return value
    return {}


def _bounded_nonnegative_int(value: object, maximum: int) -> int:
    if type(value) is not int:
        return 0
    return min(max(value, 0), maximum)


def _gpu_index(value: object) -> int | None:
    if type(value) is int:
        index = value
    elif (
        type(value) is str
        and 1 <= len(value) <= 3
        and value.isascii()
        and value.isdecimal()
    ):
        index = int(value)
    else:
        return None
    if 0 <= index <= _MAX_PUBLIC_GPU_INDEX:
        return index
    return None


def _voice_count(value: object) -> int:
    if type(value) in (list, tuple, set, frozenset, dict):
        return min(len(value), _MAX_PUBLIC_COUNT)
    if type(value) is int:
        return _bounded_nonnegative_int(value, _MAX_PUBLIC_COUNT)
    return 0


def _decision_summary(
    service_type: str,
    decision: object,
) -> dict[str, Any]:
    source = _plain_dict(decision)
    allowed = source.get("allowed") is True
    return {
        "service_type": service_type,
        "target_gpu": _gpu_index(source.get("target_gpu")),
        "estimated_need_mib": _bounded_nonnegative_int(
            source.get("estimated_need_mib"),
            _MAX_PUBLIC_MEMORY_MIB,
        ),
        "allowed": allowed,
        "reasons": [] if allowed else ["resource_unavailable"],
    }


def build_public_health(
    tts_health: dict[str, Any],
    gpu_resources: dict[str, Any],
) -> dict[str, Any]:
    tts_source = _plain_dict(tts_health)
    gpu_source = _plain_dict(gpu_resources)
    status = tts_source.get("status")
    current_model = tts_source.get("current_model")
    tts = {
        "status": (
            status
            if type(status) is str and status in _PUBLIC_TTS_STATUSES
            else "unreachable"
        ),
        "current_model": (
            current_model
            if type(current_model) is str
            and current_model in _PUBLIC_TTS_MODELS
            else None
        ),
        "model_loaded": tts_source.get("model_loaded") is True,
        "active_requests": _bounded_nonnegative_int(
            tts_source.get("active_requests"),
            _MAX_PUBLIC_COUNT,
        ),
        "voices_loaded": _voice_count(tts_source.get("voices_loaded")),
    }
    service_gpus_source = _plain_dict(gpu_source.get("service_gpus"))
    decisions_source = _plain_dict(gpu_source.get("decisions"))
    gpu: dict[str, Any] = {
        "enabled": gpu_source.get("enabled") is True,
        "available": gpu_source.get("available") is True,
        "service_gpus": {
            service_type: _gpu_index(service_gpus_source.get(service_type))
            for service_type in _PUBLIC_SERVICE_TYPES
            if service_type in service_gpus_source
        },
        "decisions": {
            service_type: _decision_summary(
                service_type,
                decisions_source.get(service_type),
            )
            for service_type in _PUBLIC_SERVICE_TYPES
            if service_type in decisions_source
        },
    }
    if "error" in gpu_source:
        gpu["error"] = "resource monitor unavailable"
    return {"api": "ok", "tts": tts, "gpu_resources": gpu}
