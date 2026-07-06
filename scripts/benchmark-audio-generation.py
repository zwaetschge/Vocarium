#!/usr/bin/env python3
"""Benchmark Vocarium music/SFX generation without surprising first-load downloads."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MODEL_WEIGHT_HINTS = {
    "music": [
        {
            "name": "Qwen3 Embedding",
            "approx_size": "1.19 GB",
            "purpose": "ACE-Step text conditioning",
        },
        {
            "name": "acestep-v15-turbo",
            "approx_size": "4.79 GB",
            "purpose": "ACE-Step diffusion model",
        },
        {
            "name": "acestep-5Hz-lm",
            "approx_size": "up to 3.71 GB",
            "purpose": "ACE-Step lyrics/prompt language model",
        },
    ],
    "sfx": [
        {
            "name": "mmaudio_large_44k_v2.pth",
            "approx_size": "4.12 GB",
            "purpose": "MMAudio large 44 kHz SFX generation model",
        }
    ],
}


@dataclass
class ProbeResult:
    kind: str
    health: dict[str, Any]
    generated: bool
    elapsed_seconds: float | None = None
    bytes_received: int | None = None
    output_path: str | None = None
    error: str | None = None


def _request(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    user: str = "api",
    timeout: int = 30,
) -> tuple[int, bytes, str]:
    data = None
    headers = {"Remote-User": user}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def _json_request(api_url: str, path: str, *, user: str) -> dict[str, Any]:
    status, body, _ = _request("GET", f"{api_url}{path}", user=user, timeout=10)
    if status >= 400:
        raise RuntimeError(body.decode(errors="replace"))
    return json.loads(body)


def _is_loaded(kind: str, health: dict[str, Any]) -> bool:
    if kind == "music":
        return bool(health.get("backend_running"))
    return bool(health.get("model_loaded"))


def _weight_hints(kind: str, health: dict[str, Any]) -> list[dict[str, Any]]:
    first_load = health.get("first_load")
    if isinstance(first_load, dict):
        hints = first_load.get("model_weight_hints")
        if isinstance(hints, list):
            return hints
    return MODEL_WEIGHT_HINTS[kind]


def _generate_music(api_url: str, duration: int, user: str) -> tuple[int, bytes]:
    payload = {
        "engine": "acestep",
        "prompt": "Kurzer deutscher Benchmark: ruhige elektronische Musik, klarer Beat.",
        "lyrics": "",
        "audio_duration": max(10, duration),
        "instrumental": True,
        "loopable": True,
        "audio_format": "wav",
        "batch_size": 1,
        "seed": 1234,
    }
    status, body, _ = _request(
        "POST",
        f"{api_url}/api/music/generate",
        payload=payload,
        user=user,
        timeout=900,
    )
    return status, body


def _generate_sfx(api_url: str, duration: int, user: str) -> tuple[int, bytes]:
    payload = {
        "engine": "mmaudio",
        "prompt": "Kurzer deutscher Benchmark: sanftes Glockenspiel in einem kleinen Raum.",
        "duration": min(max(1, duration), 30),
        "negative_prompt": "distortion, clipping",
        "no_speech": True,
        "no_music": False,
        "seed": 1234,
    }
    status, body, _ = _request(
        "POST",
        f"{api_url}/api/sfx/generate",
        payload=payload,
        user=user,
        timeout=900,
    )
    return status, body


def probe_kind(args: argparse.Namespace, kind: str) -> ProbeResult:
    health_path = "/api/music/health" if kind == "music" else "/api/sfx/health"
    health = _json_request(args.api_url, health_path, user=args.user)
    if args.preload and not _is_loaded(kind, health) and not args.force_generate:
        return ProbeResult(
            kind=kind,
            health=health,
            generated=False,
            error=(
                "first-load generation not started; pass --force-generate to "
                "download/load model weights"
            ),
        )
    if not args.force_generate:
        return ProbeResult(kind=kind, health=health, generated=False)

    start = time.perf_counter()
    status, body = (
        _generate_music(args.api_url, args.duration, args.user)
        if kind == "music"
        else _generate_sfx(args.api_url, args.duration, args.user)
    )
    elapsed = time.perf_counter() - start
    if status >= 400:
        return ProbeResult(
            kind=kind,
            health=health,
            generated=True,
            elapsed_seconds=elapsed,
            error=body.decode(errors="replace"),
        )

    output_path = None
    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"vocarium-{kind}-benchmark.bin"
        output.write_bytes(body)
        output_path = str(output)
    return ProbeResult(
        kind=kind,
        health=health,
        generated=True,
        elapsed_seconds=elapsed,
        bytes_received=len(body),
        output_path=output_path,
    )


def render_human(results: list[ProbeResult]) -> None:
    for result in results:
        print(f"{result.kind}:")
        loaded = _is_loaded(result.kind, result.health)
        print(f"  loaded: {loaded}")
        if not loaded:
            print("  first-load model weights:")
            for hint in _weight_hints(result.kind, result.health):
                print(
                    "   - "
                    f"{hint.get('name')}: {hint.get('approx_size')} "
                    f"({hint.get('purpose')})"
                )
        if result.elapsed_seconds is not None:
            print(f"  elapsed_seconds: {result.elapsed_seconds:.2f}")
        if result.bytes_received is not None:
            print(f"  bytes_received: {result.bytes_received}")
        if result.output_path:
            print(f"  output_path: {result.output_path}")
        if result.error:
            print(f"  note: {result.error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8280")
    parser.add_argument("--user", default="api")
    parser.add_argument("--kind", choices=["music", "sfx", "both"], default="both")
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--preload", action="store_true")
    parser.add_argument("--force-generate", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out-dir")
    args = parser.parse_args()
    args.api_url = args.api_url.rstrip("/")

    kinds = ["music", "sfx"] if args.kind == "both" else [args.kind]
    results = [probe_kind(args, kind) for kind in kinds]
    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2, ensure_ascii=False))
    else:
        render_human(results)
    return 1 if any(r.error and r.generated for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
