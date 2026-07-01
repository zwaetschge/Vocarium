"""Shared helpers for podcast generation."""

from __future__ import annotations

import math
import re
import secrets
import time
from typing import TypeVar


WORDS_PER_MINUTE = 140
_WORD_SPLIT = re.compile(r"\s+")
_FILENAME_INVALID = re.compile(r'[<>:"/\\|?*]')
_FILENAME_SPACE = re.compile(r"\s+")


def generate_id(prefix: str = "") -> str:
    """Short unique id: ``<prefix>-<timestamp_ms>-<rand6>``."""
    ts_ms = int(time.time() * 1000)
    suffix = secrets.token_hex(3)
    return f"{prefix}-{ts_ms}-{suffix}" if prefix else f"{ts_ms}-{suffix}"


def count_words(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return sum(1 for w in _WORD_SPLIT.split(stripped) if w)


def estimate_speaking_duration(words: int) -> float:
    """Return estimated spoken duration in seconds at ~140 wpm (ceiling)."""
    return math.ceil((words / WORDS_PER_MINUTE) * 60)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("Embeddings must have the same length")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom == 0:
        return 0.0
    return dot / denom


T = TypeVar("T")


def top_k_by_similarity(
    items: list[T], query_embedding: list[float], k: int
) -> list[T]:
    """Rank ``items`` (each must have ``embedding``) by cosine similarity and
    return the top ``k``. Accepts objects with an ``embedding`` attribute or
    ``embedding`` dict key."""

    def _get_embedding(item) -> list[float]:
        if isinstance(item, dict):
            return item["embedding"]
        return item.embedding

    scored = [
        (cosine_similarity(_get_embedding(item), query_embedding), item)
        for item in items
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:k]]


def chunk_text(text: str, max_chars: int = 2000, overlap: int = 200) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunk_end = end
        if end < len(text):
            last_period = text.rfind(".", start, end)
            last_question = text.rfind("?", start, end)
            last_exclam = text.rfind("!", start, end)
            last_nl = text.rfind("\n", start, end)
            break_point = max(last_period, last_question, last_exclam, last_nl)
            if break_point > start + max_chars // 2:
                chunk_end = break_point + 1
        chunks.append(text[start:chunk_end].strip())
        if chunk_end >= len(text):
            break
        if chunk_end <= start:
            start = chunk_end + 1
            continue
        effective_overlap = min(max(overlap, 0), chunk_end - start - 1)
        start = chunk_end - effective_overlap
    return [c for c in chunks if c]


def sanitize_filename(name: str) -> str:
    cleaned = _FILENAME_INVALID.sub("_", name)
    cleaned = _FILENAME_SPACE.sub("_", cleaned)
    return cleaned[:255]


def format_duration(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
