#!/usr/bin/env python3
"""Small TTS load test for Vocarium's Qwen3-TTS replicas.

Default mode hits the TTS containers directly, round-robin across
``TTS_URLS`` or ``TTS_URL``. This verifies model load, inference latency, and
whether both replicas can work concurrently when the GPU guard allows it.

Examples:
    python3 scripts/tts-loadtest.py --requests 4 --concurrency 2
    TTS_URLS=http://localhost:8201,http://localhost:8202 python3 scripts/tts-loadtest.py
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class Result:
    index: int
    url: str
    status: int
    seconds: float
    bytes_read: int
    generation_time: float | None
    error: str | None = None


def _request_json(method: str, url: str, body: Any | None = None, timeout: float = 20.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _gpu_guard_allows(api_url: str) -> tuple[bool, str]:
    try:
        _status, data = _request_json("GET", f"{api_url.rstrip('/')}/api/resources/status", timeout=10)
    except Exception as exc:
        return False, f"GPU guard status unavailable: {exc}"
    if not data.get("enabled", True):
        return True, "GPU guard disabled"
    decisions = data.get("decisions") or {}
    tts_decisions = [
        item for key, item in decisions.items()
        if key in {"tts", "tts_extra"}
    ]
    if not tts_decisions:
        return True, "no TTS decision reported"
    blocked = [d for d in tts_decisions if not d.get("allowed")]
    if blocked:
        reasons = []
        for decision in blocked:
            selected = decision.get("selected") or {}
            reasons.extend(selected.get("reasons") or decision.get("reasons") or [])
        return False, "; ".join(str(r) for r in reasons) or "TTS GPU blocked"
    return True, "TTS GPUs allowed"


def _one_request(index: int, url: str, text: str, timeout: float) -> Result:
    payload = {
        "text": text,
        "speaker": "Vivian",
        "language": "German",
        "response_format": "wav",
    }
    req = urllib.request.Request(
        f"{url.rstrip('/')}/v1/audio/speech/custom",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            elapsed = time.perf_counter() - start
            gen_raw = resp.headers.get("X-Generation-Time") or resp.headers.get("x-generation-time")
            return Result(
                index=index,
                url=url,
                status=resp.status,
                seconds=elapsed,
                bytes_read=len(body),
                generation_time=float(gen_raw) if gen_raw else None,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return Result(
            index=index,
            url=url,
            status=exc.code,
            seconds=time.perf_counter() - start,
            bytes_read=len(body),
            generation_time=None,
            error=body.decode("utf-8", "replace")[:300],
        )
    except Exception as exc:
        return Result(
            index=index,
            url=url,
            status=0,
            seconds=time.perf_counter() - start,
            bytes_read=0,
            generation_time=None,
            error=str(exc),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=os.environ.get("API_URL", "http://localhost:8280"))
    parser.add_argument("--requests", type=int, default=int(os.environ.get("TTS_LOAD_REQUESTS", "4")))
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("TTS_LOAD_CONCURRENCY", "2")))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("TTS_LOAD_TIMEOUT", "3600")))
    parser.add_argument("--force", action="store_true", help="run even when GPU guard reports TTS blocked")
    parser.add_argument(
        "--text",
        default=os.environ.get(
            "TTS_LOAD_TEXT",
            "Dies ist ein kurzer Vocarium Lasttest fuer die Sprachsynthese.",
        ),
    )
    parser.add_argument(
        "--urls",
        default=os.environ.get("TTS_URLS") or os.environ.get("TTS_URL", "http://localhost:8201"),
        help="comma-separated direct TTS URLs",
    )
    args = parser.parse_args()

    urls = [u.strip().rstrip("/") for u in args.urls.split(",") if u.strip()]
    if not urls:
        print("No TTS URLs configured", file=sys.stderr)
        return 2
    if args.requests < 1 or args.concurrency < 1:
        print("--requests and --concurrency must be >= 1", file=sys.stderr)
        return 2

    allowed, reason = _gpu_guard_allows(args.api_url)
    if not allowed and not args.force:
        print(f"Skipping TTS load test: {reason}")
        return 0
    print(f"GPU guard: {reason}")
    print(f"Requests: {args.requests}, concurrency: {args.concurrency}, URLs: {', '.join(urls)}")

    started = time.perf_counter()
    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(_one_request, i, urls[i % len(urls)], args.text, args.timeout)
            for i in range(args.requests)
        ]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            status = result.status if result.status else "ERR"
            print(
                f"#{result.index + 1} {result.url} status={status} "
                f"time={result.seconds:.2f}s bytes={result.bytes_read}"
            )
            if result.error:
                print(f"  error: {result.error}")

    total = time.perf_counter() - started
    ok = [r for r in results if 200 <= r.status < 300 and r.bytes_read > 0]
    failures = [r for r in results if r not in ok]
    times = [r.seconds for r in ok]
    print()
    print(f"Completed in {total:.2f}s: ok={len(ok)} failed={len(failures)}")
    if times:
        print(
            "Latency seconds: "
            f"min={min(times):.2f} median={statistics.median(times):.2f} max={max(times):.2f}"
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
