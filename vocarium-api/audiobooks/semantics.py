"""Embedding-gestützte Semantik für den Hörbücher-Bereich.

Zwei Aufgaben:
  1. ``semantic_chapter_split`` — Kapitelgrenzen für Bücher ohne erkennbare
     Kapitelmarken: Absätze werden eingebettet, Grenzen liegen in den Tälern
     der Kosinus-Ähnlichkeit benachbarter Fenster (Themenwechsel).
  2. Segment-Embeddings pro Buch (Hintergrund-Task beim Import) plus
     ``semantic_search`` für die buchweite Suche im Reader.

Backend: jede OpenAI-kompatible Embedding-API; Default ist der lokale
llama.cpp-Server (Compose-Service ``embeddings``) mit bge-m3 als GGUF auf der
CPU -- 1024 Dim., mehrsprachig, also formgleich mit dem früheren Ollama-Backend:
bereits geschriebene Embedding-Caches bleiben gültig. Alles degradiert leise:
ohne erreichbares Embedding-Backend bleibt es beim bisherigen Verhalten.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

EMBED_URL = (os.environ.get("EMBEDDING_API_URL") or "http://embeddings:8080/v1").rstrip("/")
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL") or "bge-m3"
BATCH = 48

# Semantic-Split-Parameter
MIN_CHAPTER_CHARS = 4000     # keine Mini-Kapitel
TARGET_CHAPTERS_PER_100K = 8  # grobe Zieldichte


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Batch-Embeddings; None wenn das Backend nicht erreichbar ist."""
    if not texts:
        return []
    out: list[list[float]] = []
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for i in range(0, len(texts), BATCH):
                chunk = [t[:2000] for t in texts[i:i + BATCH]]
                resp = await client.post(
                    f"{EMBED_URL}/embeddings",
                    json={"model": EMBED_MODEL, "input": chunk},
                )
                resp.raise_for_status()
                data = resp.json()["data"]
                out.extend(e["embedding"] for e in data)
    except Exception as exc:
        logger.warning("Embedding-Backend nicht verfügbar (%s): %s", EMBED_URL, exc)
        return None
    return out


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _mean(vecs: list[list[float]]) -> list[float]:
    n = len(vecs)
    return [sum(v[i] for v in vecs) / n for i in range(len(vecs[0]))]


async def semantic_chapter_split(text: str) -> list[tuple[str, str]] | None:
    """Kapitelgrenzen über Themenwechsel. Liefert [(titel, inhalt)] oder None,
    wenn Embeddings fehlen oder das Buch zu klein/homogen ist."""
    import re

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) < 12 or len(text) < 2 * MIN_CHAPTER_CHARS:
        return None

    vecs = await embed_texts(paragraphs)
    if vecs is None:
        return None

    # Ähnlichkeit zwischen dem Fenster davor und danach an jeder Absatzgrenze
    window = 3
    sims: list[tuple[int, float]] = []
    for i in range(window, len(paragraphs) - window + 1):
        before = _mean(vecs[i - window:i])
        after = _mean(vecs[i:i + window])
        sims.append((i, _cos(before, after)))
    if not sims:
        return None

    # Zielanzahl Grenzen; die tiefsten Täler gewinnen, mit Mindestabstand
    target = max(1, min(40, round(len(text) / 100_000 * TARGET_CHAPTERS_PER_100K)))
    char_pos = []
    pos = 0
    for p in paragraphs:
        char_pos.append(pos)
        pos += len(p) + 2

    def pos_of(i: int) -> int:
        return char_pos[i] if i < len(char_pos) else len(text)

    candidates = sorted(sims, key=lambda s: s[1])
    boundaries: list[int] = []
    for idx, _sim in candidates:
        if len(boundaries) >= target:
            break
        # Mindest-Kapitellänge zu allen bereits gewählten Grenzen + Rändern
        anchors = [0, len(paragraphs)] + boundaries
        if all(abs(pos_of(idx) - pos_of(a)) >= MIN_CHAPTER_CHARS for a in anchors):
            boundaries.append(idx)
    if not boundaries:
        return None
    boundaries.sort()

    chapters: list[tuple[str, str]] = []
    starts = [0] + boundaries
    ends = boundaries + [len(paragraphs)]
    for n, (s, e) in enumerate(zip(starts, ends), 1):
        chunk = paragraphs[s:e]
        if not chunk:
            continue
        # Titel: erste kurze Zeile ohne Satzende, sonst Anfangsworte
        first = chunk[0].split("\n")[0].strip()
        if len(first) <= 70 and not first.endswith((".", "!", "?", "…", ",")):
            title = f"{n}. {first}"
        else:
            words = " ".join(chunk[0].split()[:6])
            title = f"{n}. {words}…"
        chapters.append((title[:120], "\n\n".join(chunk)))
    return chapters if len(chapters) >= 2 else None


# ── Buchweite Suche ─────────────────────────────────────────────────────────

def _emb_path(book_dir: Path, version: str) -> Path:
    return book_dir / f"embeddings-{version}.json"


async def build_book_embeddings(book_dir: Path, version: str, segments: list[dict]) -> None:
    """Hintergrund-Task nach dem Import: Segment-Embeddings persistieren."""
    path = _emb_path(book_dir, version)
    if path.is_file():
        return
    texts = [s["text"] for s in segments]
    vecs = await embed_texts(texts)
    if vecs is None:
        return
    # Kompakt runden — für Kosinus-Ranking reicht das locker
    payload = [[round(x, 4) for x in v] for v in vecs]
    path.write_text(json.dumps(payload), encoding="utf-8")
    logger.info("Buch-Embeddings gebaut: %s (%d Segmente)", path.name, len(payload))


async def semantic_search(book_dir: Path, version: str, segments: list[dict], query: str, k: int = 8) -> list[dict] | None:
    """Top-k-Segmente zur Anfrage; None wenn (noch) keine Embeddings da sind."""
    path = _emb_path(book_dir, version)
    if not path.is_file():
        return None
    try:
        vecs = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if len(vecs) != len(segments):
        return None
    qv = await embed_texts([query])
    if not qv:
        return None
    scored = sorted(
        ((_cos(qv[0], v), s) for v, s in zip(vecs, segments)),
        key=lambda x: -x[0],
    )
    results: list[dict] = []
    seen: set[str] = set()
    for score, s in scored:
        if score <= 0.3 or len(results) >= k:
            break
        key = s["text"][:80]
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "chapterIndex": s["chapterIndex"], "index": s["index"],
            "text": s["text"][:160], "score": round(score, 3),
        })
    return results
