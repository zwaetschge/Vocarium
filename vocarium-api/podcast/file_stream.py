"""Bounded synchronous file iterators for Starlette StreamingResponse."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def parse_single_range(header: str, file_size: int) -> tuple[int, int]:
    if file_size <= 0:
        raise ValueError("empty file")
    match = _RANGE_RE.fullmatch((header or "").strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise ValueError("invalid byte range")
    start_text, end_text = match.groups()
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        return max(0, file_size - suffix), file_size - 1
    start = int(start_text)
    end = int(end_text) if end_text else file_size - 1
    end = min(end, file_size - 1)
    if start >= file_size or start > end:
        raise ValueError("range not satisfiable")
    return start, end


def iter_file_range(
    path: Path,
    start: int,
    end: int,
    chunk_size: int = 64 * 1024,
) -> Iterator[bytes]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    remaining = end - start + 1
    with Path(path).open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
