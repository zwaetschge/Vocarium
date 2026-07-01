#!/usr/bin/env python3
"""Vocarium Docker-stack smoke test.

The script uses only the Python standard library so it can run on a fresh host:

    python3 scripts/e2e-smoke.py

Environment overrides:
    API_URL=http://localhost:8280
    UI_URL=http://localhost:3100
    TTS_URL=http://localhost:8201
    REMOTE_USER=vocarium-smoke
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def request(
    method: str,
    url: str,
    *,
    json_body: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> Response:
    data = None
    req_headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(), dict(resp.headers.items()))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read(), dict(exc.headers.items()))


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def wait_for_api(api_url: str, headers: dict[str, str], timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = request("GET", f"{api_url}/api/health", headers=headers, timeout=5)
        if resp.status == 200:
            return
        time.sleep(2)
    raise AssertionError(f"API did not become healthy within {timeout_s:.0f}s")


def check_ui(ui_url: str) -> None:
    resp = request("GET", ui_url, timeout=10)
    expect(resp.status == 200, f"UI index returned HTTP {resp.status}")
    html = resp.text()
    expect('id="root"' in html, "UI index does not contain React root")
    assets = re.findall(r'src="([^"]+/assets/[^"]+\.js)"', html)
    if assets:
        asset_url = urllib.parse.urljoin(ui_url, assets[0])
        asset_resp = request("GET", asset_url, timeout=10)
        expect(asset_resp.status == 200, f"UI JS asset returned HTTP {asset_resp.status}")


def check_api_contracts(api_url: str, headers: dict[str, str]) -> str:
    auth = request("GET", f"{api_url}/api/auth/me", headers=headers)
    expect(auth.status == 200, f"auth/me returned HTTP {auth.status}")
    expect(auth.json()["user"]["username"], "auth/me returned no username")

    health = request("GET", f"{api_url}/api/health", headers=headers, timeout=30)
    expect(health.status == 200, f"health returned HTTP {health.status}")
    expect(health.json().get("api") == "ok", "health did not report api=ok")

    queue = request("GET", f"{api_url}/api/queue/status", headers=headers)
    expect(queue.status == 200, f"queue status returned HTTP {queue.status}")
    expect("queue_length" in queue.json(), "queue status missing queue_length")

    jobs = request("GET", f"{api_url}/api/queue/jobs?limit=5", headers=headers)
    expect(jobs.status == 200, f"queue jobs returned HTTP {jobs.status}")
    expect("jobs" in jobs.json(), "queue jobs missing jobs list")

    resources = request("GET", f"{api_url}/api/resources/status", headers=headers)
    expect(resources.status == 200, f"resources returned HTTP {resources.status}")

    metrics = request("GET", f"{api_url}/api/metrics", headers=headers)
    expect(metrics.status == 200, f"metrics returned HTTP {metrics.status}")
    expect("vocarium_http_requests_total" in metrics.text(), "metrics missing HTTP counter")

    voices = request("GET", f"{api_url}/api/voices", headers=headers)
    expect(voices.status == 200, f"voices returned HTTP {voices.status}")
    data = voices.json()
    expect(isinstance(data.get("voices"), list), "voices response is not a list")

    music_404 = request(
        "GET",
        f"{api_url}/api/music/audio?path={urllib.parse.quote('missing/file.wav')}",
        headers=headers,
    )
    expect(music_404.status == 404, f"unknown music path should 404, got {music_404.status}")

    cleanup = request(
        "POST",
        f"{api_url}/api/admin/artifacts/cleanup?dry_run=true&max_age_hours=0",
        headers=headers,
    )
    expect(cleanup.status == 200, f"artifact cleanup dry-run returned HTTP {cleanup.status}")
    expect(cleanup.json().get("dry_run") is True, "artifact cleanup did not stay dry-run")

    podcast = request(
        "POST",
        f"{api_url}/api/podcasts",
        headers=headers,
        json_body={
            "topic": "Smoke test podcast",
            "format": "dialog",
            "duration": "short",
            "language": "de",
            "host_ids": [],
        },
    )
    expect(podcast.status == 200, f"create podcast returned HTTP {podcast.status}: {podcast.text()}")
    podcast_id = podcast.json()["id"]

    source = request(
        "POST",
        f"{api_url}/api/podcasts/{podcast_id}/sources/text",
        headers=headers,
        json_body={
            "title": "Smoke source",
            "content": "Vocarium smoke test source content. " * 8,
        },
    )
    expect(source.status == 200, f"add text source returned HTTP {source.status}: {source.text()}")
    source_id = source.json()["id"]

    deadline = time.time() + 20
    while time.time() < deadline:
        current = request(
            "GET",
            f"{api_url}/api/podcasts/{podcast_id}/sources/{source_id}",
            headers=headers,
        )
        expect(current.status == 200, f"get source returned HTTP {current.status}")
        status = current.json().get("status")
        if status in {"processed", "failed"}:
            break
        time.sleep(1)
    expect(status in {"processed", "failed"}, "source processing did not finish or fail")

    delete = request("DELETE", f"{api_url}/api/podcasts/{podcast_id}", headers=headers)
    expect(delete.status == 200, f"delete podcast returned HTTP {delete.status}")
    return podcast_id


def check_tts_validation(tts_url: str) -> None:
    resp = request(
        "POST",
        f"{tts_url}/v1/audio/speech/custom",
        json_body={"text": "", "speaker": ""},
        timeout=20,
    )
    expect(resp.status in {400, 422}, f"TTS validation returned HTTP {resp.status}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=os.environ.get("API_URL", "http://localhost:8280"))
    parser.add_argument("--ui-url", default=os.environ.get("UI_URL", "http://localhost:3100"))
    parser.add_argument("--tts-url", default=os.environ.get("TTS_URL", "http://localhost:8201"))
    parser.add_argument("--remote-user", default=os.environ.get("REMOTE_USER", "vocarium-smoke"))
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--skip-ui", action="store_true")
    parser.add_argument("--skip-tts-validation", action="store_true")
    args = parser.parse_args()

    headers = {"Remote-User": args.remote_user}
    wait_for_api(args.api_url.rstrip("/"), headers, args.timeout)
    if not args.skip_ui:
        check_ui(args.ui_url.rstrip("/") + "/")
    check_api_contracts(args.api_url.rstrip("/"), headers)
    if not args.skip_tts_validation:
        check_tts_validation(args.tts_url.rstrip("/"))

    print("E2E smoke passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"E2E smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
