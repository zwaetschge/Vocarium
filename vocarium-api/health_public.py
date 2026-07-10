"""Redacted public health payloads for the unauthenticated readiness route."""

from __future__ import annotations

from typing import Any


def _voice_count(value: object) -> int:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    if isinstance(value, int):
        return max(0, value)
    return 0


def _decision_summary(decision: dict[str, Any]) -> dict[str, Any]:
    selected = decision.get("selected") or {}
    reasons = decision.get("reasons") or selected.get("reasons") or []
    return {
        "service_type": decision.get("service_type"),
        "target_gpu": decision.get("target_gpu"),
        "estimated_need_mib": decision.get("estimated_need_mib"),
        "allowed": bool(decision.get("allowed", False)),
        "reasons": [str(reason) for reason in reasons],
    }


def build_public_health(
    tts_health: dict[str, Any],
    gpu_resources: dict[str, Any],
) -> dict[str, Any]:
    tts = {
        "status": tts_health.get("status", "unreachable"),
        "current_model": tts_health.get("current_model"),
        "model_loaded": bool(tts_health.get("model_loaded", False)),
        "active_requests": int(tts_health.get("active_requests") or 0),
        "voices_loaded": _voice_count(tts_health.get("voices_loaded")),
    }
    gpu: dict[str, Any] = {
        "enabled": bool(gpu_resources.get("enabled", False)),
        "available": bool(gpu_resources.get("available", False)),
        "service_gpus": dict(gpu_resources.get("service_gpus") or {}),
    }
    decisions = gpu_resources.get("decisions") or {}
    if isinstance(decisions, dict):
        gpu["decisions"] = {
            str(name): _decision_summary(value)
            for name, value in decisions.items()
            if isinstance(value, dict)
        }
    if gpu_resources.get("error"):
        gpu["error"] = "resource monitor unavailable"
    return {"api": "ok", "tts": tts, "gpu_resources": gpu}
