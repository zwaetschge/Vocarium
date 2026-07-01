"""Artifact cleanup helpers for generated local Vocarium files."""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any


def cleanup_artifacts(
    db: sqlite3.Connection,
    data_dir: Path,
    voices_dir: Path,
    *,
    max_age_hours: float = 24.0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Find and optionally delete local artifacts no longer referenced by DB.

    The cleanup is intentionally conservative:
    - podcast final files are kept when referenced by ``podcasts.audio_path``;
    - per-podcast segment directories are kept while the podcast row exists;
    - uploaded source files are kept while a source row references them;
    - voice directories are kept while a voice row references the ID;
    - TTS cache/temp files are age-based only.
    """
    cutoff = time.time() - max_age_hours * 3600
    data_dir = Path(data_dir)
    voices_dir = Path(voices_dir)
    audio_root = Path(os.environ.get("PODCAST_AUDIO_PATH", data_dir / "podcast_audio"))
    upload_root = Path(os.environ.get("PODCAST_UPLOAD_PATH", data_dir / "podcast_uploads"))

    podcast_ids = {
        row[0]
        for row in db.execute("SELECT id FROM podcasts").fetchall()
        if row[0]
    }
    referenced_audio = {
        _safe_resolve(Path(row[0]))
        for row in db.execute(
            "SELECT audio_path FROM podcasts WHERE audio_path IS NOT NULL AND audio_path != ''"
        ).fetchall()
        if row[0]
    }
    referenced_uploads = {
        _safe_resolve(Path(row[0]))
        for row in db.execute(
            "SELECT content FROM podcast_sources WHERE type='file' "
            "AND content IS NOT NULL AND content != ''"
        ).fetchall()
        if row[0]
    }
    voice_ids = {
        row[0]
        for row in db.execute("SELECT id FROM voices").fetchall()
        if row[0]
    }

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "max_age_hours": max_age_hours,
        "deleted": [],
        "candidates": [],
        "skipped": [],
        "stale_music_tasks": 0,
    }

    def consider(path: Path, category: str, reason: str) -> None:
        resolved = _safe_resolve(path)
        if resolved is None:
            return
        if not _older_than(resolved, cutoff):
            report["skipped"].append(
                {"path": str(resolved), "category": category, "reason": "too_new"}
            )
            return
        item = {"path": str(resolved), "category": category, "reason": reason}
        report["candidates"].append(item)
        if dry_run:
            return
        if _delete_path(resolved):
            report["deleted"].append(item)

    # Podcast final files and per-project segment directories.
    if audio_root.exists():
        for entry in audio_root.iterdir():
            if entry.name == "temp":
                _consider_old_children(entry, "podcast_temp", cutoff, dry_run, report)
                continue
            if entry.name == "tts_cache":
                _consider_old_children(entry, "tts_cache", cutoff, dry_run, report)
                continue
            resolved = _safe_resolve(entry)
            if resolved is None:
                continue
            if entry.is_file():
                if resolved not in referenced_audio:
                    consider(entry, "podcast_audio", "not_referenced_by_podcast")
                continue
            if entry.is_dir() and entry.name not in podcast_ids:
                consider(entry, "podcast_project_dir", "podcast_row_missing")

    # Uploaded source files.
    if upload_root.exists():
        for entry in upload_root.iterdir():
            resolved = _safe_resolve(entry)
            if resolved is None:
                continue
            if entry.is_file() and resolved not in referenced_uploads:
                consider(entry, "podcast_upload", "not_referenced_by_source")

    # Voice directories that no longer have a DB row.
    if voices_dir.exists():
        for entry in voices_dir.iterdir():
            if entry.is_dir() and entry.name not in voice_ids:
                consider(entry, "voice_dir", "voice_row_missing")

    # Music files live in the ACE-Step service, but stale noncompleted DB task
    # references can still be cleaned.
    interval = f"-{max_age_hours} hours"
    stale_music = db.execute(
        "SELECT task_id FROM music_tasks "
        "WHERE status IN ('submitted', 'running', 'failed') "
        "AND datetime(updated_at) < datetime('now', ?)",
        (interval,),
    ).fetchall()
    report["stale_music_tasks"] = len(stale_music)
    if stale_music and not dry_run:
        db.execute(
            "DELETE FROM music_tasks "
            "WHERE status IN ('submitted', 'running', 'failed') "
            "AND datetime(updated_at) < datetime('now', ?)",
            (interval,),
        )
        db.commit()

    return report


def _consider_old_children(
    root: Path,
    category: str,
    cutoff: float,
    dry_run: bool,
    report: dict[str, Any],
) -> None:
    if not root.exists():
        return
    for entry in root.iterdir():
        resolved = _safe_resolve(entry)
        if resolved is None:
            continue
        if not _older_than(resolved, cutoff):
            report["skipped"].append(
                {"path": str(resolved), "category": category, "reason": "too_new"}
            )
            continue
        item = {"path": str(resolved), "category": category, "reason": "age"}
        report["candidates"].append(item)
        if not dry_run and _delete_path(resolved):
            report["deleted"].append(item)


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def _older_than(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except OSError:
        return False


def _delete_path(path: Path) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError:
        return False
