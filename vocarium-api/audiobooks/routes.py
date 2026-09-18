"""Audiobooks area: upload → chapters → generation → gated playback.

The Canto rebuild's core invariants, honoured here:
  1. Playback never triggers TTS — a chapter is listenable only once every
     segment file exists (the cache is the authority, counters are derived).
  2. Segmentation is persisted at import (``segments.json``) and the chunker
     version is part of the audio path, so a pipeline change can never silently
     invalidate cached audio.
  3. Audio is normalised with the exact ffmpeg chain the spec settled on —
     EBU R128 −16 LUFS, soft-onset padding, deliberately no ``silenceremove``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import hashlib
import os
from zoneinfo import ZoneInfo
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from .text_pipeline import CHUNKER_VERSION, chunk_text, extract_epub_cover, parse_document, split_chapters_txt
from .text_pipeline import Chapter as PipelineChapter
from .semantics import build_book_embeddings, semantic_chapter_split, semantic_search

logger = logging.getLogger(__name__)

NORMALIZE_FILTER = "adelay=40:all=1,apad=pad_dur=0.08,loudnorm=I=-16:TP=-1.5:LRA=11"

BUSINESS_TZ = ZoneInfo(os.environ.get("BUSINESS_HOURS_TZ", "Europe/Berlin"))


def is_out_of_business_hours() -> bool:
    """Off-Hours-Fenster für Priorität ≥ 8 (Canto §4.2, aber TZ-fest)."""
    start = int(os.environ.get("BUSINESS_HOURS_START", "9"))
    end = int(os.environ.get("BUSINESS_HOURS_END", "18"))
    days = {int(d) for d in os.environ.get("BUSINESS_HOURS_DAYS", "1,2,3,4,5").split(",")}
    now = datetime.now(BUSINESS_TZ)
    dow = now.isoweekday() % 7  # 0 = Sonntag, wie im Canto-Original
    return dow not in days or now.hour < start or now.hour >= end


def _voice_token(voice_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", voice_id)[:128]


def create_audiobooks_router(
    *,
    get_current_user: Callable[[Request], dict],
    db_getter: Callable[[], sqlite3.Connection],
    tts_bridge: Any,
    data_dir: Path,
) -> APIRouter:
    router = APIRouter(prefix="/api/audiobooks", tags=["audiobooks"])
    root = data_dir / "audiobooks"
    root.mkdir(parents=True, exist_ok=True)
    # generation jobs: (book_id, voice_token) -> state dict
    jobs: dict[tuple[str, str], dict] = {}

    def book_dir(book_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9-]{8,36}", book_id):
            raise HTTPException(400, "Invalid book id")
        return root / book_id

    def load_segments(book_id: str) -> list[dict]:
        path = book_dir(book_id) / "segments.json"
        if not path.is_file():
            raise HTTPException(404, "Buch nicht gefunden")
        return json.loads(path.read_text(encoding="utf-8"))

    def get_book(book_id: str, user_id: int) -> dict:
        row = db_getter().execute(
            "SELECT id, title, author, format, voice_id, total_chapters, created_at, is_hidden, chunker_version "
            "FROM ab_books WHERE id=? AND user_id=?",
            (book_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Buch nicht gefunden")
        progress = db_getter().execute(
            "SELECT chapter_index, segment_index, updated_at, completed FROM ab_progress WHERE book_id=? AND user_id=?",
            (book_id, user_id),
        ).fetchone()
        return {
            "progress": {"chapterIndex": progress[0], "segmentIndex": progress[1], "updatedAt": progress[2], "completed": bool(progress[3])} if progress else None,
            "id": row[0], "title": row[1], "author": row[2], "format": row[3],
            "voice_id": row[4], "total_chapters": row[5], "created_at": row[6],
            "is_hidden": bool(row[7]),
            "chunker_version": row[8] or "v1",
            "has_cover": cover_path(book_id) is not None,
        }

    def cover_path(book_id: str) -> Path | None:
        for ext in ("jpg", "png", "webp", "gif"):
            p = book_dir(book_id) / f"cover.{ext}"
            if p.is_file():
                return p
        return None

    # ── Aussprache-Regeln (Canto §5.7 — dort nie angewendet, hier schon) ────
    # Angewendet auf den Text, der zur TTS geht; Anzeige und Segmentierung
    # bleiben Original. Der Regel-Hash ist Teil des Cache-Pfads, damit eine
    # Regeländerung nie stilles, veraltetes Audio ausliefert.

    def get_rules(user_id: int) -> list[tuple[str, str]]:
        rows = db_getter().execute(
            "SELECT original, replacement FROM ab_pronunciation_rules "
            "WHERE user_id=? ORDER BY LENGTH(original) DESC, created_at",
            (user_id,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def rules_hash(user_id: int) -> str:
        rules = get_rules(user_id)
        if not rules:
            return ""
        digest = hashlib.sha1(
            "\x1f".join(f"{o}\x1e{r}" for o, r in sorted(rules)).encode("utf-8"),
        ).hexdigest()[:8]
        return digest

    def apply_rules(text: str, rules: list[tuple[str, str]]) -> str:
        for original, replacement in rules:
            text = re.sub(
                rf"(?<!\w){re.escape(original)}(?!\w)", replacement.replace("\\", "\\\\"), text,
            )
        return text

    store_root = root / "store"
    store_root.mkdir(parents=True, exist_ok=True)

    def store_dir(sid: str) -> Path:
        if not re.fullmatch(r"[a-f0-9-]{8,36}", sid):
            raise HTTPException(400, "Invalid store id")
        return store_root / sid

    def _store_link(book_id: str) -> str | None:
        row = db_getter().execute(
            "SELECT store_book_id FROM ab_books WHERE id=?", (book_id,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def audio_dir(book_id: str, voice_id: str, rhash: str = "") -> Path:
        """Audio-Wurzel; Bücher aus dem Store teilen ihren Cache über den
        Store-Ordner — wer zuerst generiert, generiert für alle.

        Die Chunker-Version kommt aus dem Buch selbst (beim Import
        festgeschrieben): Alt-Bücher behalten so ihre v1-Segmentierung UND
        ihren v1-Audio-Cache, neue Importe laufen unter der aktuellen Version.
        """
        token = _voice_token(voice_id) + (f"+r{rhash}" if rhash else "")
        row = db_getter().execute(
            "SELECT store_book_id, chunker_version FROM ab_books WHERE id=?", (book_id,),
        ).fetchone()
        sid = row[0] if row and row[0] else None
        version = (row[1] if row and row[1] else None) or CHUNKER_VERSION
        base = store_dir(sid) if sid else book_dir(book_id)
        return base / "audio" / version / token

    def chapter_stats(book_id: str, voice_id: str, chapters: list[dict], segments: list[dict], rhash: str = "") -> list[dict]:
        adir = audio_dir(book_id, voice_id, rhash)
        cached = {p.stem for p in adir.glob("*.mp3")} if adir.is_dir() else set()
        stats = []
        for chapter in chapters:
            ci = chapter["index"]
            seg_ids = [s["index"] for s in segments if s["chapterIndex"] == ci]
            done = sum(1 for si in seg_ids if f"{ci}_{si}" in cached)
            stats.append({
                "index": ci,
                "title": chapter["title"],
                "totalSegments": len(seg_ids),
                "cachedSegments": done,
                "complete": bool(seg_ids) and done == len(seg_ids),
            })
        return stats

    # Ein Render-Pfad für Batch-Generierung UND Live-Streaming: Synthese →
    # Normalisierung → atomares Rename in den regulären Cache. Der Suffix im
    # Tmp-Namen verhindert Kollisionen zwischen parallelen Erzeugern.
    async def render_segment(
        segment: dict, voice_id: str, user_id: int, adir: Path,
        rules: list[tuple[str, str]], tmp_suffix: str = "",
    ) -> Path:
        ci, si = segment["chapterIndex"], segment["index"]
        target = adir / f"{ci}_{si}.mp3"
        if target.exists():
            return target
        adir.mkdir(parents=True, exist_ok=True)
        wav_tmp = adir / f".{ci}_{si}{tmp_suffix}.wav"
        await tts_bridge.synthesize_to_file(
            apply_rules(segment["text"], rules), voice_id, wav_tmp, "wav", user_id=user_id,
        )
        mp3_tmp = adir / f".{ci}_{si}{tmp_suffix}.tmp.mp3"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(wav_tmp),
            "-af", NORMALIZE_FILTER, "-ar", "24000",
            "-codec:a", "libmp3lame", "-qscale:a", "4",
            "-loglevel", "error", str(mp3_tmp),
        )
        code = await proc.wait()
        wav_tmp.unlink(missing_ok=True)
        if code != 0 or not mp3_tmp.exists():
            raise RuntimeError(f"ffmpeg failed for segment {si}")
        mp3_tmp.rename(target)
        return target

    async def generate_worker(book_id: str, voice_id: str, user_id: int, chapter_index: int | None) -> None:
        key = (book_id, _voice_token(voice_id))
        state = jobs[key]
        segments = load_segments(book_id)
        todo = [
            s for s in segments
            if (chapter_index is None or s["chapterIndex"] == chapter_index)
        ]
        rules = get_rules(user_id)
        adir = audio_dir(book_id, voice_id, rules_hash(user_id))
        try:
            for segment in todo:
                if state.get("cancel"):
                    state["status"] = "cancelled"
                    return
                await render_segment(segment, voice_id, user_id, adir, rules)
                state["done"] += 1
                state["updated"] = time.time()
            state["status"] = "completed"
        except Exception as exc:
            logger.exception("Audiobook generation failed (%s, %s)", book_id, voice_id)
            state["status"] = "failed"
            state["error"] = str(exc)[:300]

    # ── Bibliothek ───────────────────────────────────────────────────────────

    @router.get("")
    async def list_books(request: Request):
        user = get_current_user(request)
        db = db_getter()
        rows = db.execute(
            "SELECT id, title, author, format, voice_id, total_chapters, created_at, is_hidden "
            "FROM ab_books WHERE user_id=? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        progress = {
            r[0]: {"chapterIndex": r[1], "segmentIndex": r[2], "updatedAt": r[3], "completed": bool(r[4])}
            for r in db.execute(
                "SELECT book_id, chapter_index, segment_index, updated_at, completed FROM ab_progress WHERE user_id=?",
                (user["id"],),
            ).fetchall()
        }
        memberships: dict[str, list[str]] = {}
        for cid, bid in db.execute(
            "SELECT cb.collection_id, cb.book_id FROM ab_collection_books cb "
            "JOIN ab_collections c ON c.id = cb.collection_id WHERE c.user_id=?",
            (user["id"],),
        ).fetchall():
            memberships.setdefault(bid, []).append(cid)
        return {"books": [
            {
                "id": r[0], "title": r[1], "author": r[2], "format": r[3],
                "voice_id": r[4], "total_chapters": r[5], "created_at": r[6],
                "is_hidden": bool(r[7]),
                "has_cover": cover_path(r[0]) is not None,
                "collectionIds": memberships.get(r[0], []),
                "progress": progress.get(r[0]),
            }
            for r in rows
        ]}

    # ── Sammlungen (vor /{book_id} registriert — Pfad-Priorität) ────────────

    @router.get("/collections")
    async def list_collections(request: Request):
        user = get_current_user(request)
        rows = db_getter().execute(
            "SELECT id, name, color FROM ab_collections WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
        return {"collections": [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]}

    @router.post("/collections", status_code=201)
    async def create_collection(body: dict, request: Request):
        user = get_current_user(request)
        name = str(body.get("name", "")).strip()[:100]
        color = str(body.get("color", "#c59f5f")).strip()
        if not name:
            raise HTTPException(400, "Name fehlt")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise HTTPException(400, "Ungültige Farbe")
        cid = str(uuid.uuid4())[:8]
        db = db_getter()
        db.execute(
            "INSERT INTO ab_collections (id, user_id, name, color, created_at) VALUES (?,?,?,?,?)",
            (cid, user["id"], name, color, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        db.commit()
        return {"id": cid, "name": name, "color": color}

    def get_collection(cid: str, user_id: int) -> None:
        row = db_getter().execute(
            "SELECT id FROM ab_collections WHERE id=? AND user_id=?", (cid, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Sammlung nicht gefunden")

    @router.patch("/collections/{cid}")
    async def update_collection(cid: str, body: dict, request: Request):
        user = get_current_user(request)
        get_collection(cid, user["id"])
        fields, values = [], []
        if str(body.get("name", "")).strip():
            fields.append("name=?"); values.append(str(body["name"]).strip()[:100])
        if body.get("color"):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(body["color"])):
                raise HTTPException(400, "Ungültige Farbe")
            fields.append("color=?"); values.append(str(body["color"]))
        if not fields:
            raise HTTPException(400, "Nichts zu ändern")
        db = db_getter()
        db.execute(
            f"UPDATE ab_collections SET {', '.join(fields)} WHERE id=? AND user_id=?",
            values + [cid, user["id"]],
        )
        db.commit()
        return {"ok": True}

    @router.delete("/collections/{cid}")
    async def delete_collection(cid: str, request: Request):
        user = get_current_user(request)
        get_collection(cid, user["id"])
        db = db_getter()
        db.execute("DELETE FROM ab_collection_books WHERE collection_id=?", (cid,))
        db.execute("DELETE FROM ab_collections WHERE id=? AND user_id=?", (cid, user["id"]))
        db.commit()
        return {"ok": True}

    @router.post("/collections/{cid}/books", status_code=201)
    async def add_to_collection(cid: str, body: dict, request: Request):
        user = get_current_user(request)
        get_collection(cid, user["id"])
        book_id = str(body.get("bookId", ""))
        get_book(book_id, user["id"])
        db = db_getter()
        db.execute(
            "INSERT OR IGNORE INTO ab_collection_books (collection_id, book_id) VALUES (?,?)",
            (cid, book_id),
        )
        db.commit()
        return {"ok": True}

    @router.delete("/collections/{cid}/books/{book_id}")
    async def remove_from_collection(cid: str, book_id: str, request: Request):
        user = get_current_user(request)
        get_collection(cid, user["id"])
        db = db_getter()
        db.execute(
            "DELETE FROM ab_collection_books WHERE collection_id=? AND book_id=?",
            (cid, book_id),
        )
        db.commit()
        return {"ok": True}

    # ── Persistente Generierungs-Queue (vor /{book_id} registriert) ─────────
    # Überlebt Neustarts: pending-Jobs bleiben in SQLite, processing wird beim
    # Startup zurück auf pending gesetzt; das Segment-Skipping des Workers
    # macht die Wiederaufnahme idempotent. Priorität ≥ 8 läuft nur außerhalb
    # der Geschäftszeiten (Europe/Berlin).

    def _queue_rows(user_id: int) -> list[dict]:
        rows = db_getter().execute(
            "SELECT q.id, q.book_id, q.voice_id, q.chapter_index, q.priority, q.status, "
            "q.done, q.total, q.error, q.created_at, q.started_at, q.finished_at, b.title "
            "FROM ab_generation_queue q LEFT JOIN ab_books b ON b.id = q.book_id "
            "WHERE q.user_id=? ORDER BY "
            "CASE q.status WHEN 'processing' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
            "q.priority, q.created_at",
            (user_id,),
        ).fetchall()
        out = []
        for r in rows:
            entry = {
                "id": r[0], "book_id": r[1], "voice_id": r[2], "chapter": r[3],
                "priority": r[4], "status": r[5], "done": r[6], "total": r[7],
                "error": r[8], "created_at": r[9], "started_at": r[10],
                "finished_at": r[11], "title": r[12] or r[1],
            }
            if r[5] == "processing":
                live = jobs.get((r[1], _voice_token(r[2])))
                if live:
                    entry["done"], entry["total"] = live["done"], live["total"]
            out.append(entry)
        return out

    @router.get("/queue")
    async def list_queue(request: Request):
        user = get_current_user(request)
        return {"jobs": _queue_rows(user["id"]), "offHours": is_out_of_business_hours()}

    @router.post("/queue", status_code=201)
    async def enqueue(body: dict, request: Request):
        user = get_current_user(request)
        book_id = str(body.get("book_id", ""))
        book = get_book(book_id, user["id"])
        voice_id = (body.get("voice_id") or book["voice_id"] or "").strip()
        if not voice_id:
            raise HTTPException(400, "voice_id fehlt")
        chapter = body.get("chapter")
        priority = max(1, min(10, int(body.get("priority", 5))))
        db = db_getter()
        dup = db.execute(
            "SELECT id FROM ab_generation_queue WHERE user_id=? AND book_id=? AND voice_id=? "
            "AND status IN ('pending','processing') AND chapter_index IS ?",
            (user["id"], book_id, voice_id, int(chapter) if chapter is not None else None),
        ).fetchone()
        if dup:
            raise HTTPException(409, "Job steht bereits in der Warteschlange")
        qid = str(uuid.uuid4())[:8]
        segments = load_segments(book_id)
        total = sum(1 for s in segments if chapter is None or s["chapterIndex"] == int(chapter))
        db.execute(
            "INSERT INTO ab_generation_queue (id, user_id, book_id, voice_id, chapter_index, "
            "priority, status, total, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (qid, user["id"], book_id, voice_id,
             int(chapter) if chapter is not None else None,
             priority, "pending", total, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        db.commit()
        return {"id": qid, "priority": priority}

    @router.delete("/queue/{qid}")
    async def cancel_queue_job(qid: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        row = db.execute(
            "SELECT book_id, voice_id, status FROM ab_generation_queue WHERE id=? AND user_id=?",
            (qid, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Job nicht gefunden")
        if row[2] == "processing":
            live = jobs.get((row[0], _voice_token(row[1])))
            if live:
                live["cancel"] = True
        db.execute(
            "UPDATE ab_generation_queue SET status='cancelled', finished_at=? WHERE id=? AND user_id=?",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ"), qid, user["id"]),
        )
        db.commit()
        return {"ok": True}

    @router.get("/queue/events")
    async def queue_events(request: Request):
        user = get_current_user(request)

        async def gen():
            while not await request.is_disconnected():
                payload = json.dumps({
                    "jobs": _queue_rows(user["id"]),
                    "offHours": is_out_of_business_hours(),
                })
                yield f"data: {payload}\n\n"
                await asyncio.sleep(1.5)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    # ── Anzeige-Einstellungen (serverseitig, geräteübergreifend) ────────────

    PREF_KEYS = {"fontSize", "lineHeight", "serif", "fontFamily", "theme", "bionic"}

    @router.get("/prefs")
    async def get_prefs(request: Request):
        user = get_current_user(request)
        row = db_getter().execute(
            "SELECT prefs FROM ab_user_prefs WHERE user_id=?", (user["id"],),
        ).fetchone()
        try:
            return json.loads(row[0]) if row else {}
        except json.JSONDecodeError:
            return {}

    @router.put("/prefs")
    async def put_prefs(body: dict, request: Request):
        user = get_current_user(request)
        clean = {k: v for k, v in body.items() if k in PREF_KEYS}
        db = db_getter()
        db.execute(
            "INSERT INTO ab_user_prefs (user_id, prefs, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET prefs=excluded.prefs, updated_at=excluded.updated_at",
            (user["id"], json.dumps(clean), time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        db.commit()
        return {"ok": True}

    # ── Store: geteiltes Regal mit gemeinsamem Audio-Cache ──────────────────
    # Veröffentlichen kopiert Inhalt + Cover und verlinkt vorhandenes Audio
    # per Hardlink (gleiche Volume, kein Platzverbrauch). Hinzugefügte Bücher
    # tragen store_book_id; audio_dir() leitet ihren Cache auf den Store um.

    @router.get("/store")
    async def list_store(request: Request):
        user = get_current_user(request)
        rows = db_getter().execute(
            "SELECT s.id, s.title, s.author, s.genre, s.format, s.total_chapters, s.created_at, "
            "s.added_by, u.username, "
            "(SELECT ROUND(AVG(rating),1) FROM ab_store_ratings r WHERE r.store_book_id = s.id), "
            "(SELECT COUNT(*) FROM ab_store_ratings r WHERE r.store_book_id = s.id), "
            "(SELECT rating FROM ab_store_ratings r WHERE r.store_book_id = s.id AND r.user_id = ?) "
            "FROM ab_store_books s LEFT JOIN users u ON u.id = s.added_by "
            "ORDER BY s.created_at DESC",
            (user["id"],),
        ).fetchall()
        mine = {
            r[0] for r in db_getter().execute(
                "SELECT store_book_id FROM ab_books WHERE user_id=? AND store_book_id IS NOT NULL",
                (user["id"],),
            ).fetchall()
        }
        return {"books": [
            {
                "id": r[0], "title": r[1], "author": r[2], "genre": r[3], "format": r[4],
                "total_chapters": r[5], "created_at": r[6], "mine": r[7] == user["id"],
                "added_by": r[8] or "?", "inLibrary": r[0] in mine,
                "has_cover": any((store_dir(r[0]) / f"cover.{e}").is_file() for e in ("jpg", "png", "webp", "gif")),
                "avgRating": r[9] or 0, "ratingCount": r[10], "userRating": r[11],
            }
            for r in rows
        ]}

    @router.post("/store/publish", status_code=201)
    async def publish_to_store(body: dict, request: Request):
        user = get_current_user(request)
        book_id = str(body.get("book_id", ""))
        book = get_book(book_id, user["id"])
        genre = str(body.get("genre", "")).strip()[:100]
        sid = str(uuid.uuid4())[:8]
        sdir = store_dir(sid)
        sdir.mkdir(parents=True)
        bdir = book_dir(book_id)
        for name in ("chapters.json", "segments.json", "content.json"):
            shutil.copy2(bdir / name, sdir / name)
        cover = cover_path(book_id)
        if cover:
            shutil.copy2(cover, sdir / cover.name)
        # Vorhandenes Audio hardlinken — sofort hörbar für alle
        src_audio = bdir / "audio"
        if src_audio.is_dir():
            for mp3 in src_audio.glob("*/*/*.mp3"):
                rel = mp3.relative_to(src_audio)
                target = sdir / "audio" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(mp3, target)
                except OSError:
                    shutil.copy2(mp3, target)
        db = db_getter()
        src_version = db.execute(
            "SELECT chunker_version FROM ab_books WHERE id=?", (book_id,),
        ).fetchone()
        book_version = (src_version[0] if src_version and src_version[0] else None) or "v1"
        db.execute(
            "INSERT INTO ab_store_books (id, title, author, genre, format, total_chapters, added_by, created_at, chunker_version) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, book["title"], book["author"], genre, book["format"],
             book["total_chapters"], user["id"], time.strftime("%Y-%m-%dT%H:%M:%SZ"), book_version),
        )
        # Eigenes Buch mit dem Store verknüpfen, damit auch der Publisher den
        # geteilten Cache nutzt (das alte Audio ist per Hardlink identisch).
        db.execute("UPDATE ab_books SET store_book_id=? WHERE id=?", (sid, book_id))
        db.commit()
        return {"id": sid}

    @router.post("/store/{sid}/add", status_code=201)
    async def add_from_store(sid: str, request: Request):
        user = get_current_user(request)
        row = db_getter().execute(
            "SELECT title, author, format, total_chapters, chunker_version FROM ab_store_books WHERE id=?", (sid,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Store-Buch nicht gefunden")
        dup = db_getter().execute(
            "SELECT id FROM ab_books WHERE user_id=? AND store_book_id=?", (user["id"], sid),
        ).fetchone()
        if dup:
            raise HTTPException(409, "Bereits in deiner Bibliothek")
        book_id = str(uuid.uuid4())[:8]
        bdir = book_dir(book_id)
        bdir.mkdir(parents=True)
        sdir = store_dir(sid)
        for name in ("chapters.json", "segments.json", "content.json"):
            shutil.copy2(sdir / name, bdir / name)
        for ext in ("jpg", "png", "webp", "gif"):
            if (sdir / f"cover.{ext}").is_file():
                shutil.copy2(sdir / f"cover.{ext}", bdir / f"cover.{ext}")
                break
        db = db_getter()
        db.execute(
            "INSERT INTO ab_books (id, user_id, title, author, format, voice_id, total_chapters, created_at, store_book_id, chunker_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (book_id, user["id"], row[0], row[1], row[2], None, row[3],
             time.strftime("%Y-%m-%dT%H:%M:%SZ"), sid, (row[4] or "v1")),
        )
        db.commit()
        return {"id": book_id}

    @router.delete("/store/{sid}")
    async def delete_store_book(sid: str, request: Request):
        user = get_current_user(request)
        row = db_getter().execute(
            "SELECT added_by FROM ab_store_books WHERE id=?", (sid,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Store-Buch nicht gefunden")
        if row[0] != user["id"]:
            raise HTTPException(403, "Nur wer veröffentlicht hat, kann löschen")
        linked = db_getter().execute(
            "SELECT COUNT(*) FROM ab_books WHERE store_book_id=? AND user_id != ?", (sid, user["id"]),
        ).fetchone()[0]
        if linked:
            raise HTTPException(409, f"{linked} andere Bibliothek(en) nutzen dieses Buch noch")
        db = db_getter()
        db.execute("UPDATE ab_books SET store_book_id=NULL WHERE store_book_id=?", (sid,))
        db.execute("DELETE FROM ab_store_books WHERE id=?", (sid,))
        db.commit()
        shutil.rmtree(store_dir(sid), ignore_errors=True)
        return {"ok": True}

    @router.patch("/store/{sid}")
    async def edit_store_book(sid: str, body: dict, request: Request):
        user = get_current_user(request)
        row = db_getter().execute(
            "SELECT added_by FROM ab_store_books WHERE id=?", (sid,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Store-Buch nicht gefunden")
        if row[0] != user["id"]:
            raise HTTPException(403, "Nur wer veröffentlicht hat, kann bearbeiten")
        fields, values = [], []
        for key, col, limit in (("title", "title", 300), ("author", "author", 300), ("genre", "genre", 100)):
            if key in body:
                fields.append(f"{col}=?")
                values.append(str(body[key]).strip()[:limit])
        if not fields:
            raise HTTPException(400, "Nichts zu ändern")
        db = db_getter()
        db.execute(
            f"UPDATE ab_store_books SET {', '.join(fields)} WHERE id=?", values + [sid],
        )
        db.commit()
        return {"ok": True}

    @router.post("/store/{sid}/rating", status_code=201)
    async def rate_store_book(sid: str, body: dict, request: Request):
        user = get_current_user(request)
        if not db_getter().execute("SELECT 1 FROM ab_store_books WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Store-Buch nicht gefunden")
        rating = int(body.get("rating", 0))
        if not 1 <= rating <= 5:
            raise HTTPException(400, "Bewertung muss 1–5 sein")
        db = db_getter()
        db.execute(
            "INSERT INTO ab_store_ratings (store_book_id, user_id, rating, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(store_book_id, user_id) DO UPDATE SET rating=excluded.rating, created_at=excluded.created_at",
            (sid, user["id"], rating, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        db.commit()
        avg, count = db.execute(
            "SELECT AVG(rating), COUNT(*) FROM ab_store_ratings WHERE store_book_id=?", (sid,),
        ).fetchone()
        return {"avgRating": round(avg or 0, 1), "ratingCount": count, "userRating": rating}

    @router.get("/store/{sid}/cover")
    async def store_cover(sid: str, request: Request):
        get_current_user(request)
        for ext in ("jpg", "png", "webp", "gif"):
            p = store_dir(sid) / f"cover.{ext}"
            if p.is_file():
                media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
                return FileResponse(p, media_type=media[ext], headers={"Cache-Control": "private, max-age=86400"})
        raise HTTPException(404, "Kein Cover")

    # ── Ambience: Hintergrundklänge fürs Hören ──────────────────────────────
    # Pro Nutzer hochgeladene Loops (Regen, Kamin, …); der Reader spielt sie
    # mit Crossfade-Looping unabhängig von der Sprachspur.

    ambience_root = data_dir / "ambience"
    ambience_root.mkdir(parents=True, exist_ok=True)

    def _ambience_row(aid: str, user_id: int) -> tuple:
        row = db_getter().execute(
            "SELECT id, name, filename FROM ab_ambience WHERE id=? AND user_id=?",
            (aid, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Klang nicht gefunden")
        return row

    @router.get("/ambience")
    async def list_ambience(request: Request):
        user = get_current_user(request)
        rows = db_getter().execute(
            "SELECT id, name, filename, created_at FROM ab_ambience WHERE user_id=? ORDER BY name",
            (user["id"],),
        ).fetchall()
        return {"sounds": [
            {"id": r[0], "name": r[1], "filename": r[2], "created_at": r[3]} for r in rows
        ]}

    @router.post("/ambience", status_code=201)
    async def upload_ambience(request: Request, file: UploadFile = File(...), name: str = Form("")):
        user = get_current_user(request)
        ext = (file.filename or "").rsplit(".", 1)[-1].lower()
        if ext not in ("mp3", "ogg", "wav", "m4a", "flac"):
            raise HTTPException(400, "Nur MP3, OGG, WAV, M4A oder FLAC")
        data = await file.read()
        if len(data) > 100 * 1024 * 1024:
            raise HTTPException(413, "Datei größer als 100 MB")
        aid = str(uuid.uuid4())[:8]
        filename = f"{aid}.{ext}"
        (ambience_root / filename).write_bytes(data)
        display = (name.strip() or (file.filename or "Klang").rsplit(".", 1)[0])[:100]
        db = db_getter()
        db.execute(
            "INSERT INTO ab_ambience (id, user_id, name, filename, created_at) VALUES (?,?,?,?,?)",
            (aid, user["id"], display, filename, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        db.commit()
        return {"id": aid, "name": display}

    @router.delete("/ambience/{aid}")
    async def delete_ambience(aid: str, request: Request):
        user = get_current_user(request)
        row = _ambience_row(aid, user["id"])
        (ambience_root / row[2]).unlink(missing_ok=True)
        db = db_getter()
        db.execute("DELETE FROM ab_ambience WHERE id=? AND user_id=?", (aid, user["id"]))
        db.commit()
        return {"ok": True}

    @router.get("/ambience/{aid}/audio")
    async def serve_ambience(aid: str, request: Request):
        user = get_current_user(request)
        row = _ambience_row(aid, user["id"])
        path = ambience_root / row[2]
        if not path.is_file():
            raise HTTPException(404, "Datei fehlt")
        media = {"mp3": "audio/mpeg", "ogg": "audio/ogg", "wav": "audio/wav",
                 "m4a": "audio/mp4", "flac": "audio/flac"}
        return FileResponse(path, media_type=media.get(path.suffix.lstrip("."), "audio/mpeg"))

    # ── Hörsitzungen & Statistiken ──────────────────────────────────────────

    @router.post("/listening-sessions", status_code=201)
    async def start_session(body: dict, request: Request):
        user = get_current_user(request)
        book_id = str(body.get("bookId", ""))
        get_book(book_id, user["id"])
        sid = str(uuid.uuid4())[:12]
        db = db_getter()
        db.execute(
            "INSERT INTO ab_listening_sessions (id, user_id, book_id, started_at) VALUES (?,?,?,?)",
            (sid, user["id"], book_id, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        db.commit()
        return {"id": sid}

    @router.patch("/listening-sessions")
    async def update_session(body: dict, request: Request):
        user = get_current_user(request)
        sid = str(body.get("sessionId", ""))
        duration = max(0, min(86_400_000, int(body.get("durationMs", 0))))
        segments = max(0, min(100_000, int(body.get("segmentsPlayed", 0))))
        db = db_getter()
        cur = db.execute(
            "UPDATE ab_listening_sessions SET duration_ms=?, segments_played=?, ended_at=? "
            "WHERE id=? AND user_id=?",
            (duration, segments, time.strftime("%Y-%m-%dT%H:%M:%SZ"), sid, user["id"]),
        )
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Session nicht gefunden")
        return {"ok": True}

    @router.get("/stats")
    async def user_stats(request: Request):
        user = get_current_user(request)
        db = db_getter()
        uid = user["id"]
        total_books = db.execute("SELECT COUNT(*) FROM ab_books WHERE user_id=?", (uid,)).fetchone()[0]
        started = db.execute(
            "SELECT COUNT(DISTINCT book_id) FROM ab_progress WHERE user_id=?", (uid,),
        ).fetchone()[0]
        completed = db.execute(
            "SELECT COUNT(*) FROM ab_progress p JOIN ab_books b ON b.id = p.book_id "
            "WHERE p.user_id=? AND b.user_id=p.user_id AND p.completed=1",
            (uid,),
        ).fetchone()[0]
        bookmarks = db.execute("SELECT COUNT(*) FROM ab_bookmarks WHERE user_id=?", (uid,)).fetchone()[0]
        listening_ms, sessions = db.execute(
            "SELECT COALESCE(SUM(duration_ms),0), COUNT(*) FROM ab_listening_sessions WHERE user_id=?",
            (uid,),
        ).fetchone()
        seg_played = db.execute(
            "SELECT COALESCE(SUM(segments_played),0) FROM ab_listening_sessions WHERE user_id=?",
            (uid,),
        ).fetchone()[0]
        formats = [
            {"format": r[0], "count": r[1]}
            for r in db.execute(
                "SELECT format, COUNT(*) FROM ab_books WHERE user_id=? GROUP BY format", (uid,),
            ).fetchall()
        ]
        cached_segments = 0
        for (bid,) in db.execute("SELECT id FROM ab_books WHERE user_id=?", (uid,)).fetchall():
            adir = root / bid / "audio"
            if adir.is_dir():
                cached_segments += sum(1 for _ in adir.glob("*/*/*.mp3"))

        # Streak: zusammenhängende Tage mit Hörsitzung, rückwärts ab heute (Europe/Berlin)
        days = {
            r[0][:10] for r in db.execute(
                "SELECT started_at FROM ab_listening_sessions WHERE user_id=?", (uid,),
            ).fetchall() if r[0]
        }
        streak = 0
        from datetime import timedelta
        cursor_day = datetime.now(BUSINESS_TZ).date()
        while cursor_day.isoformat() in days:
            streak += 1
            cursor_day -= timedelta(days=1)

        # Tages-Breakdown der letzten 14 Tage
        breakdown = [
            {"date": r[0], "ms": r[1]} for r in db.execute(
                "SELECT substr(started_at,1,10) d, COALESCE(SUM(duration_ms),0) "
                "FROM ab_listening_sessions WHERE user_id=? GROUP BY d ORDER BY d DESC LIMIT 14",
                (uid,),
            ).fetchall()
        ]

        defs = [
            ("first_book", "Erstes Buch", "Ein Hörbuch hochgeladen", "📕", total_books >= 1),
            ("bookworm", "Bücherwurm", "5 Hörbücher in der Bibliothek", "📚", total_books >= 5),
            ("bibliophile", "Bibliophil", "20 Hörbücher in der Bibliothek", "🏛️", total_books >= 20),
            ("first_listen", "Erster Ton", "Ein Buch angefangen", "🎧", started >= 1),
            ("completionist", "Durchgehört", "Ein Buch beendet", "🏁", completed >= 1),
            ("marathon", "Marathon", "5 Bücher beendet", "🏅", completed >= 5),
            ("hour_listener", "Stundenhörer", "1 Stunde gehört", "⏱️", listening_ms >= 3_600_000),
            ("day_listener", "Taghörer", "24 Stunden gehört", "🌙", listening_ms >= 86_400_000),
            ("bookmark_lover", "Merker", "10 Lesezeichen gesetzt", "⚑", bookmarks >= 10),
            ("multi_format", "Formatoffen", "3 verschiedene Formate", "🗂️", len(formats) >= 3),
            ("audio_factory", "Audiofabrik", "500 Segmente vertont", "🏭", cached_segments >= 500),
            ("streak_week", "Wochenserie", "7 Tage am Stück gehört", "🔥", streak >= 7),
        ]
        return {
            "stats": {
                "totalBooks": total_books, "booksStarted": started, "booksCompleted": completed,
                "totalAudioSegments": cached_segments, "totalListeningMs": listening_ms,
                "totalBookmarks": bookmarks, "sessions": sessions,
                "segmentsPlayed": seg_played, "formats": formats, "streak": streak,
                "dailyBreakdown": breakdown,
            },
            "achievements": [
                {"id": i, "title": t, "description": d, "icon": ic, "unlocked": u}
                for i, t, d, ic, u in defs
            ],
        }

    # ── Aussprache-CRUD ─────────────────────────────────────────────────────

    @router.get("/pronunciation")
    async def list_pronunciation(request: Request):
        user = get_current_user(request)
        rows = db_getter().execute(
            "SELECT id, original, replacement, language, created_at "
            "FROM ab_pronunciation_rules WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
        return {"rules": [
            {"id": r[0], "original": r[1], "replacement": r[2], "language": r[3], "created_at": r[4]}
            for r in rows
        ]}

    @router.post("/pronunciation", status_code=201)
    async def add_pronunciation(body: dict, request: Request):
        user = get_current_user(request)
        original = str(body.get("original", "")).strip()[:200]
        replacement = str(body.get("replacement", "")).strip()[:200]
        if not original or not replacement:
            raise HTTPException(400, "original und replacement sind Pflicht")
        rid = str(uuid.uuid4())[:8]
        db = db_getter()
        db.execute(
            "INSERT INTO ab_pronunciation_rules (id, user_id, original, replacement, language, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (rid, user["id"], original, replacement,
             str(body.get("language", "German"))[:40], time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        db.commit()
        return {"id": rid}

    @router.patch("/pronunciation/{rid}")
    async def edit_pronunciation(rid: str, body: dict, request: Request):
        user = get_current_user(request)
        fields, values = [], []
        for key, limit in (("original", 200), ("replacement", 200), ("language", 40)):
            if str(body.get(key, "")).strip():
                fields.append(f"{key}=?")
                values.append(str(body[key]).strip()[:limit])
        if not fields:
            raise HTTPException(400, "Nichts zu ändern")
        db = db_getter()
        cur = db.execute(
            f"UPDATE ab_pronunciation_rules SET {', '.join(fields)} WHERE id=? AND user_id=?",
            values + [rid, user["id"]],
        )
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Regel nicht gefunden")
        return {"ok": True}

    @router.delete("/pronunciation/{rid}")
    async def delete_pronunciation(rid: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        db.execute("DELETE FROM ab_pronunciation_rules WHERE id=? AND user_id=?", (rid, user["id"]))
        db.commit()
        return {"ok": True}

    @router.post("/pronunciation/preview")
    async def preview_pronunciation(body: dict, request: Request):
        user = get_current_user(request)
        text = str(body.get("text", ""))[:2000]
        return {"result": apply_rules(text, get_rules(user["id"]))}

    async def _run_queue_job(row: tuple) -> None:
        qid, user_id, book_id, voice_id, chapter_index = row
        db = db_getter()
        key = (book_id, _voice_token(voice_id))
        if jobs.get(key, {}).get("status") == "running":
            return  # Vordergrund-Generierung läuft — Job bleibt pending
        segments = load_segments(book_id)
        todo = [s for s in segments if chapter_index is None or s["chapterIndex"] == chapter_index]
        db.execute(
            "UPDATE ab_generation_queue SET status='processing', started_at=?, total=? WHERE id=?",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ"), len(todo), qid),
        )
        db.commit()
        jobs[key] = {
            "status": "running", "voice_id": voice_id, "chapter": chapter_index,
            "done": 0, "total": len(todo), "error": "", "updated": time.time(),
        }
        await generate_worker(book_id, voice_id, user_id, chapter_index)
        state = jobs.get(key, {})
        final = {"completed": "complete", "failed": "error", "cancelled": "cancelled"}.get(
            state.get("status", ""), "error",
        )
        db.execute(
            "UPDATE ab_generation_queue SET status=?, done=?, error=?, finished_at=? WHERE id=?",
            (final, state.get("done", 0), state.get("error", ""),
             time.strftime("%Y-%m-%dT%H:%M:%SZ"), qid),
        )
        db.commit()

    async def queue_loop() -> None:
        recovered = False
        while True:
            try:
                db = db_getter()
                if not recovered:
                    # Crash-Recovery erst hier: beim Startup-Hook ist init_db()
                    # noch nicht gelaufen.
                    db.execute(
                        "UPDATE ab_generation_queue SET status='pending' WHERE status='processing'",
                    )
                    db.commit()
                    recovered = True
                busy = db.execute(
                    "SELECT COUNT(*) FROM ab_generation_queue WHERE status='processing'",
                ).fetchone()[0]
                if not busy:
                    clause = "" if is_out_of_business_hours() else " AND priority < 8"
                    row = db.execute(
                        "SELECT id, user_id, book_id, voice_id, chapter_index "
                        f"FROM ab_generation_queue WHERE status='pending'{clause} "
                        "ORDER BY priority, created_at LIMIT 1",
                    ).fetchone()
                    if row:
                        await _run_queue_job(row)
            except Exception:
                logger.exception("Queue-Loop-Tick fehlgeschlagen")
            await asyncio.sleep(5)

    @router.on_event("startup")
    async def _startup_queue() -> None:
        asyncio.create_task(queue_loop())

    @router.post("", status_code=201)
    async def upload_book(
        request: Request,
        file: UploadFile = File(...),
        title: str = Form(""),
        author: str = Form(""),
        voice_id: str = Form(""),
    ):
        user = get_current_user(request)
        fmt = (file.filename or "").rsplit(".", 1)[-1].lower()
        data = await file.read()
        try:
            parsed = await asyncio.to_thread(parse_document, data, fmt)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        # Ein-Kapitel-Ergebnis (z. B. EPUB mit einem einzigen Spine-Dokument):
        # erst die Kapitelmarken-Regex auf dem Volltext versuchen — die griff
        # bisher nur im TXT-/DOCX-Pfad.
        if len(parsed.chapters) == 1:
            resplit = split_chapters_txt(parsed.chapters[0].content)
            if len(resplit) >= 2:
                parsed.chapters = resplit
                logger.info("Regex-Nachsplit: %d Kapitel", len(resplit))

        # Semantischer Fallback: kein einziger Kapitelmarker erkannt (ein
        # Riesenkapitel) oder nur mechanische PDF-Blöcke → Kapitelgrenzen
        # über Themenwechsel via Embeddings. Degradiert leise ohne Backend.
        needs_semantic = (
            len(parsed.chapters) == 1
            or all(c.title.startswith("Abschnitt ") for c in parsed.chapters)
        )
        if needs_semantic:
            full_text = "\n\n".join(c.content for c in parsed.chapters)
            try:
                semantic = await semantic_chapter_split(full_text)
            except Exception:
                logger.exception("Semantischer Kapitel-Split fehlgeschlagen")
                semantic = None
            if semantic:
                parsed.chapters = [PipelineChapter(title=t, content=c) for t, c in semantic]
                logger.info("Semantischer Split: %d Kapitel", len(semantic))

        book_id = str(uuid.uuid4())[:8]
        bdir = root / book_id
        bdir.mkdir(parents=True)
        try:
            chapters = [
                {"index": i, "title": c.title} for i, c in enumerate(parsed.chapters)
            ]
            segments: list[dict] = []
            texts: dict[int, str] = {}
            for i, chapter in enumerate(parsed.chapters):
                texts[i] = chapter.content
                segments.extend(s.as_dict() for s in chunk_text(chapter.content, i))
            if not segments:
                raise HTTPException(400, "Dokument enthält keinen vertonbaren Text")
            (bdir / "chapters.json").write_text(json.dumps(chapters, ensure_ascii=False), encoding="utf-8")
            (bdir / "segments.json").write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
            (bdir / "content.json").write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
            if fmt == "epub":
                cover = await asyncio.to_thread(extract_epub_cover, data)
                if cover:
                    (bdir / f"cover.{cover[1]}").write_bytes(cover[0])

            final_title = (title.strip() or parsed.title or (file.filename or "Unbenannt").rsplit(".", 1)[0])[:300]
            db = db_getter()
            db.execute(
                "INSERT INTO ab_books (id, user_id, title, author, format, voice_id, total_chapters, created_at, chunker_version) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    book_id, user["id"], final_title,
                    (author.strip() or parsed.author)[:300], fmt,
                    voice_id.strip() or None, len(chapters),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ"), CHUNKER_VERSION,
                ),
            )
            db.commit()
        except HTTPException:
            shutil.rmtree(bdir, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(bdir, ignore_errors=True)
            raise
        # Segment-Embeddings im Hintergrund — Basis der buchweiten Suche.
        asyncio.create_task(build_book_embeddings(bdir, CHUNKER_VERSION, segments))
        return get_book(book_id, user["id"]) | {"segments": len(segments)}

    @router.get("/{book_id}")
    async def book_detail(book_id: str, request: Request, voice: str = ""):
        user = get_current_user(request)
        book = get_book(book_id, user["id"])
        chapters = json.loads((book_dir(book_id) / "chapters.json").read_text(encoding="utf-8"))
        segments = load_segments(book_id)
        voice_id = voice.strip() or book["voice_id"] or ""
        key = (book_id, _voice_token(voice_id)) if voice_id else None
        job = jobs.get(key) if key else None
        return {
            **book,
            "chapters": chapter_stats(book_id, voice_id, chapters, segments, rules_hash(user["id"])) if voice_id else [
                {"index": c["index"], "title": c["title"], "totalSegments": 0, "cachedSegments": 0, "complete": False}
                for c in chapters
            ],
            "generation": (
                {k: v for k, v in job.items() if k != "cancel"} if job else None
            ),
        }

    @router.patch("/{book_id}")
    async def update_book(book_id: str, body: dict, request: Request):
        user = get_current_user(request)
        get_book(book_id, user["id"])
        fields, values = [], []
        if "title" in body and str(body["title"]).strip():
            fields.append("title=?"); values.append(str(body["title"]).strip()[:300])
        if "voice_id" in body:
            fields.append("voice_id=?"); values.append((body["voice_id"] or "").strip()[:128] or None)
        if "is_hidden" in body:
            fields.append("is_hidden=?"); values.append(1 if body["is_hidden"] else 0)
        if "author" in body:
            fields.append("author=?"); values.append(str(body["author"]).strip()[:300])
        if not fields:
            raise HTTPException(400, "Nichts zu ändern")
        values += [book_id, user["id"]]
        db = db_getter()
        db.execute(f"UPDATE ab_books SET {', '.join(fields)} WHERE id=? AND user_id=?", values)
        db.commit()
        return get_book(book_id, user["id"])

    @router.delete("/{book_id}")
    async def delete_book(book_id: str, request: Request):
        user = get_current_user(request)
        get_book(book_id, user["id"])
        for key in [k for k in jobs if k[0] == book_id]:
            jobs[key]["cancel"] = True
        shutil.rmtree(book_dir(book_id), ignore_errors=True)
        db = db_getter()
        db.execute("DELETE FROM ab_books WHERE id=? AND user_id=?", (book_id, user["id"]))
        db.execute("DELETE FROM ab_progress WHERE book_id=? AND user_id=?", (book_id, user["id"]))
        db.execute("DELETE FROM ab_bookmarks WHERE book_id=? AND user_id=?", (book_id, user["id"]))
        db.execute("DELETE FROM ab_collection_books WHERE book_id=?", (book_id,))
        db.commit()
        return {"ok": True}

    # ── Cover ───────────────────────────────────────────────────────────────

    @router.get("/{book_id}/cover")
    async def serve_cover(book_id: str, request: Request):
        user = get_current_user(request)
        get_book(book_id, user["id"])
        path = cover_path(book_id)
        if not path:
            raise HTTPException(404, "Kein Cover")
        media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
        return FileResponse(
            path, media_type=media.get(path.suffix.lstrip("."), "image/jpeg"),
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @router.post("/{book_id}/cover")
    async def upload_cover(book_id: str, request: Request, cover: UploadFile = File(...)):
        user = get_current_user(request)
        get_book(book_id, user["id"])
        ext = (cover.filename or "").rsplit(".", 1)[-1].lower()
        if ext == "jpeg":
            ext = "jpg"
        if ext not in ("jpg", "png", "webp", "gif"):
            raise HTTPException(400, "Nur JPG, PNG, WebP oder GIF")
        data = await cover.read()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "Cover größer als 10 MB")
        old = cover_path(book_id)
        if old:
            old.unlink(missing_ok=True)
        (book_dir(book_id) / f"cover.{ext}").write_bytes(data)
        return {"ok": True}

    # ── Inhalt & Fortschritt ────────────────────────────────────────────────

    @router.get("/{book_id}/content")
    async def chapter_content(book_id: str, request: Request, chapter: int = 0):
        user = get_current_user(request)
        get_book(book_id, user["id"])
        chapters = json.loads((book_dir(book_id) / "chapters.json").read_text(encoding="utf-8"))
        if not 0 <= chapter < len(chapters):
            raise HTTPException(404, "Kapitel nicht gefunden")
        segments = [s for s in load_segments(book_id) if s["chapterIndex"] == chapter]
        return {
            "chapter": chapters[chapter],
            "segments": segments,
            "totalChapters": len(chapters),
        }

    @router.post("/{book_id}/progress")
    async def save_progress(book_id: str, body: dict, request: Request):
        user = get_current_user(request)
        get_book(book_id, user["id"])
        try:
            chapter = int(body.get("chapterIndex", 0))
            segment = int(body.get("segmentIndex", 0))
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid listening position")
        segments = await asyncio.to_thread(load_segments, book_id)
        if not any(s["chapterIndex"] == chapter and s["index"] == segment for s in segments):
            raise HTTPException(400, "Listening position does not exist")
        last = segments[-1] if segments else None
        completed = body.get("completed") is True and last is not None and last["chapterIndex"] == chapter and last["index"] == segment
        db = db_getter()
        db.execute(
            "INSERT INTO ab_progress (book_id, user_id, chapter_index, segment_index, updated_at, completed) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(book_id, user_id) DO UPDATE SET "
            "chapter_index=excluded.chapter_index, segment_index=excluded.segment_index, updated_at=excluded.updated_at, completed=excluded.completed",
            (book_id, user["id"], chapter, segment, time.strftime("%Y-%m-%dT%H:%M:%SZ"), int(completed)),
        )
        db.commit()
        return {"ok": True}

    @router.get("/{book_id}/offline-voices")
    async def offline_voices(book_id: str, request: Request):
        """Alle Stimmen mit Cache-Stand für dieses Buch — Basis für den
        Offline-Download-Dialog und die Stimmen-Readiness im Reader.
        Scannt die Voice-Token-Ordner unter dem (ggf. Store-)Audio-Verzeichnis."""
        user = get_current_user(request)
        get_book(book_id, user["id"])
        segments = load_segments(book_id)
        total = len(segments)
        rhash = rules_hash(user["id"])
        suffix = f"+r{rhash}" if rhash else ""
        sid = _store_link(book_id)
        vrow = db_getter().execute(
            "SELECT chunker_version FROM ab_books WHERE id=?", (book_id,),
        ).fetchone()
        book_version = (vrow[0] if vrow and vrow[0] else None) or CHUNKER_VERSION
        base = (store_dir(sid) if sid else book_dir(book_id)) / "audio" / book_version
        voices = []
        if base.is_dir():
            for vdir in sorted(base.iterdir()):
                if not vdir.is_dir():
                    continue
                token = vdir.name
                # Nur Einträge, die zum Regel-Hash des Nutzers passen
                if suffix:
                    if not token.endswith(suffix):
                        continue
                    voice_id = token[: -len(suffix)]
                elif "+r" in token:
                    continue
                else:
                    voice_id = token
                cached = sum(1 for _ in vdir.glob("*.mp3"))
                if cached:
                    voices.append({
                        "voice_id": voice_id,
                        "cachedSegments": cached,
                        "totalSegments": total,
                        "complete": cached >= total,
                    })
        return {"voices": voices, "totalSegments": total}

    @router.get("/{book_id}/search")
    async def search_book(book_id: str, request: Request, q: str = ""):
        """Semantische buchweite Suche über die beim Import gebauten
        Segment-Embeddings. Fehlen sie (noch), wird nachgebaut und
        vorerst leer geantwortet."""
        user = get_current_user(request)
        book = get_book(book_id, user["id"])
        query = q.strip()
        if len(query) < 3:
            return {"results": [], "ready": True}
        segments = load_segments(book_id)
        version = book.get("chunker_version") or "v1"
        results = await semantic_search(book_dir(book_id), version, segments, query)
        if results is None:
            asyncio.create_task(build_book_embeddings(book_dir(book_id), version, segments))
            return {"results": [], "ready": False}
        return {"results": results, "ready": True}

    @router.get("/{book_id}/bookmarks")
    async def list_bookmarks(book_id: str, request: Request):
        user = get_current_user(request)
        get_book(book_id, user["id"])
        rows = db_getter().execute(
            "SELECT id, chapter_index, segment_index, note, created_at FROM ab_bookmarks "
            "WHERE book_id=? AND user_id=? ORDER BY chapter_index, segment_index",
            (book_id, user["id"]),
        ).fetchall()
        return {"bookmarks": [
            {"id": r[0], "chapterIndex": r[1], "segmentIndex": r[2], "note": r[3], "created_at": r[4]}
            for r in rows
        ]}

    @router.post("/{book_id}/bookmarks", status_code=201)
    async def add_bookmark(book_id: str, body: dict, request: Request):
        user = get_current_user(request)
        get_book(book_id, user["id"])
        bm_id = str(uuid.uuid4())[:8]
        db = db_getter()
        db.execute(
            "INSERT INTO ab_bookmarks (id, book_id, user_id, chapter_index, segment_index, note, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (bm_id, book_id, user["id"], int(body.get("chapterIndex", 0)),
             int(body.get("segmentIndex", 0)), str(body.get("note", ""))[:300],
             time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        db.commit()
        return {"id": bm_id}

    @router.delete("/{book_id}/bookmarks/{bm_id}")
    async def delete_bookmark(book_id: str, bm_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        db.execute("DELETE FROM ab_bookmarks WHERE id=? AND book_id=? AND user_id=?",
                   (bm_id, book_id, user["id"]))
        db.commit()
        return {"ok": True}

    # ── Generierung & Playback ──────────────────────────────────────────────

    @router.post("/{book_id}/generate")
    async def generate(book_id: str, body: dict, request: Request):
        user = get_current_user(request)
        book = get_book(book_id, user["id"])
        voice_id = (body.get("voice_id") or book["voice_id"] or "").strip()
        if not voice_id:
            raise HTTPException(400, "voice_id fehlt — Buch hat keine Standardstimme")
        chapter = body.get("chapter")
        chapter_index = int(chapter) if chapter is not None else None

        key = (book_id, _voice_token(voice_id))
        existing = jobs.get(key)
        if existing and existing["status"] == "running":
            raise HTTPException(409, "Für dieses Buch und diese Stimme läuft bereits eine Generierung")

        segments = load_segments(book_id)
        todo = [s for s in segments if chapter_index is None or s["chapterIndex"] == chapter_index]
        if not todo:
            raise HTTPException(404, "Kapitel nicht gefunden")
        jobs[key] = {
            "status": "running", "voice_id": voice_id, "chapter": chapter_index,
            "done": 0, "total": len(todo), "error": "", "updated": time.time(),
        }
        asyncio.create_task(generate_worker(book_id, voice_id, user["id"], chapter_index))
        return jobs[key] | {"book_id": book_id}

    @router.post("/{book_id}/generate/cancel")
    async def cancel_generation(book_id: str, body: dict, request: Request):
        user = get_current_user(request)
        book = get_book(book_id, user["id"])
        voice_id = (body.get("voice_id") or book["voice_id"] or "").strip()
        key = (book_id, _voice_token(voice_id))
        if key not in jobs or jobs[key]["status"] != "running":
            raise HTTPException(409, "Keine laufende Generierung")
        jobs[key]["cancel"] = True
        return {"ok": True}

    # ── Export (M4B/MP3 mit Kapitelmarken) ──────────────────────────────────
    # Bewusst cache-only: der Export stößt nie TTS an (Canto-Rebuild-Findung #8
    # war ein unbegrenzter Live-TTS-Pfad). Fehlende Kapitel ⇒ 409.

    export_jobs: dict[tuple[str, str, str], dict] = {}

    def _ffmeta_escape(value: str) -> str:
        value = value.replace("\\", "\\\\").replace("\n", " ")
        for ch in "=;#":
            value = value.replace(ch, f"\\{ch}")
        return value

    async def _probe_duration_ms(path: Path) -> int:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        try:
            return int(float(out.decode().strip()) * 1000)
        except ValueError:
            return 0

    async def export_worker(book_id: str, voice_id: str, fmt: str, title: str, chapters: list[dict], segments: list[dict], rhash: str = "") -> None:
        key = (book_id, _voice_token(voice_id), fmt)
        state = export_jobs[key]
        adir = audio_dir(book_id, voice_id, rhash)
        edir = book_dir(book_id) / "exports"
        tmp = edir / f".{_voice_token(voice_id)}-{fmt}-tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            # Kapitel zu Einzeldateien concat-kopieren (segmente sind homogene MP3s)
            chapter_files: list[tuple[Path, str]] = []
            for chapter in chapters:
                ci = chapter["index"]
                seg_ids = [s["index"] for s in segments if s["chapterIndex"] == ci]
                listing = tmp / f"ch{ci}.txt"
                listing.write_text(
                    "".join(f"file '{(adir / f'{ci}_{si}.mp3').as_posix()}'\n" for si in seg_ids),
                    encoding="utf-8",
                )
                out = tmp / f"ch{ci}.mp3"
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-loglevel", "error", str(out),
                )
                if await proc.wait() != 0:
                    raise RuntimeError(f"Kapitel {ci} konnte nicht zusammengefügt werden")
                chapter_files.append((out, chapter["title"]))
                state["done"] += 1
                state["updated"] = time.time()

            full_listing = tmp / "book.txt"
            full_listing.write_text(
                "".join(f"file '{p.as_posix()}'\n" for p, _ in chapter_files), encoding="utf-8",
            )
            edir.mkdir(parents=True, exist_ok=True)
            target = edir / f"{_voice_token(voice_id)}.{fmt}"

            if fmt == "mp3":
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(full_listing),
                    "-c", "copy", "-loglevel", "error", str(target),
                )
                if await proc.wait() != 0:
                    raise RuntimeError("MP3-Export fehlgeschlagen")
            else:
                # FFMETADATA1 mit [CHAPTER]-Marken aus den Kapiteldauern
                meta_lines = [";FFMETADATA1", f"title={_ffmeta_escape(title)}"]
                cursor = 0
                for path, chapter_title in chapter_files:
                    dur = await _probe_duration_ms(path)
                    meta_lines += [
                        "[CHAPTER]", "TIMEBASE=1/1000",
                        f"START={cursor}", f"END={cursor + dur}",
                        f"title={_ffmeta_escape(chapter_title)}",
                    ]
                    cursor += dur
                meta = tmp / "meta.txt"
                meta.write_text("\n".join(meta_lines) + "\n", encoding="utf-8")
                cover = cover_path(book_id)
                cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(full_listing),
                       "-f", "ffmetadata", "-i", str(meta)]
                if cover:
                    cmd += ["-i", str(cover), "-map", "0:a", "-map", "2:v",
                            "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
                else:
                    cmd += ["-map", "0:a"]
                cmd += ["-map_metadata", "1", "-map_chapters", "1",
                        "-c:a", "aac", "-b:a", "128k",
                        "-f", "mp4", "-loglevel", "warning", str(target)]
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stderr=asyncio.subprocess.PIPE,
                )
                _, err = await proc.communicate()
                if err:
                    logger.warning("m4b export ffmpeg: %s", err.decode(errors="replace")[-2000:])
                if proc.returncode != 0:
                    raise RuntimeError("M4B-Export fehlgeschlagen")

            state["status"] = "completed"
            state["size"] = target.stat().st_size
        except Exception as exc:
            logger.exception("Audiobook export failed (%s, %s, %s)", book_id, voice_id, fmt)
            state["status"] = "failed"
            state["error"] = str(exc)[:300]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @router.post("/{book_id}/export")
    async def start_export(book_id: str, body: dict, request: Request):
        user = get_current_user(request)
        book = get_book(book_id, user["id"])
        voice_id = (body.get("voice_id") or book["voice_id"] or "").strip()
        fmt = str(body.get("format", "m4b")).lower()
        if fmt not in ("m4b", "mp3"):
            raise HTTPException(400, "Format muss m4b oder mp3 sein")
        if not voice_id:
            raise HTTPException(400, "voice_id fehlt")
        chapters = json.loads((book_dir(book_id) / "chapters.json").read_text(encoding="utf-8"))
        segments = load_segments(book_id)
        rhash = rules_hash(user["id"])
        stats = chapter_stats(book_id, voice_id, chapters, segments, rhash)
        missing = [s["index"] for s in stats if not s["complete"]]
        if missing:
            raise HTTPException(409, f"Kapitel noch nicht generiert: {missing[:10]}")
        key = (book_id, _voice_token(voice_id), fmt)
        existing = export_jobs.get(key)
        if existing and existing["status"] == "running":
            raise HTTPException(409, "Export läuft bereits")
        export_jobs[key] = {
            "status": "running", "voice_id": voice_id, "format": fmt,
            "done": 0, "total": len(chapters), "error": "", "updated": time.time(),
        }
        asyncio.create_task(export_worker(book_id, voice_id, fmt, book["title"], chapters, segments, rhash))
        return export_jobs[key]

    @router.get("/{book_id}/export/status")
    async def export_status(book_id: str, request: Request, voice: str = "", format: str = "m4b"):
        user = get_current_user(request)
        book = get_book(book_id, user["id"])
        voice_id = voice.strip() or book["voice_id"] or ""
        job = export_jobs.get((book_id, _voice_token(voice_id), format))
        target = book_dir(book_id) / "exports" / f"{_voice_token(voice_id)}.{format}"
        return {
            "job": job,
            "ready": target.is_file(),
            "size": target.stat().st_size if target.is_file() else 0,
            "codeVersion": 2,
        }

    @router.get("/{book_id}/export/download")
    async def export_download(book_id: str, request: Request, voice: str = "", format: str = "m4b"):
        user = get_current_user(request)
        book = get_book(book_id, user["id"])
        voice_id = voice.strip() or book["voice_id"] or ""
        target = book_dir(book_id) / "exports" / f"{_voice_token(voice_id)}.{format}"
        if not target.is_file():
            raise HTTPException(404, "Kein Export vorhanden")
        safe_title = re.sub(r'[\r\n"]', "", book["title"])[:120] or "Hoerbuch"
        return FileResponse(
            target,
            media_type="audio/mp4" if format == "m4b" else "audio/mpeg",
            headers={"Content-Disposition": f'attachment; filename="{safe_title}.{format}"'},
        )

    @router.get("/{book_id}/audio/{voice_id}/{chapter_index}/{segment_index}")
    async def serve_segment(
        book_id: str, voice_id: str, chapter_index: int, segment_index: int, request: Request
    ):
        """Cache-only playback: a missing file is a 404, never a generation."""
        user = get_current_user(request)
        get_book(book_id, user["id"])
        path = audio_dir(book_id, voice_id, rules_hash(user["id"])) / f"{chapter_index}_{segment_index}.mp3"
        if not path.is_file():
            raise HTTPException(404, "Segment nicht generiert")
        return FileResponse(path, media_type="audio/mpeg")

    # ── Live-Streaming: Segmente just-in-time generieren ────────────────────
    # OmniVoice/Kikiri sind schnell genug (RTF « 1), um beim Hören knapp vor
    # dem Abspielkopf zu generieren. Erzeugtes Audio landet im REGULÄREN
    # Cache — einmal gehört ist dauerhaft generiert, Export/Offline
    # funktionieren danach ohne weiteres Zutun. Pro (Buch, Stimme) wird
    # serialisiert, damit parallele Prefetches die Engine nicht fluten und
    # kein Segment doppelt synthetisiert wird.

    live_locks: dict[tuple[str, str], asyncio.Lock] = {}

    @router.get("/{book_id}/audio-live/{voice_id}/{chapter_index}/{segment_index}")
    async def serve_segment_live(
        book_id: str, voice_id: str, chapter_index: int, segment_index: int, request: Request
    ):
        user = get_current_user(request)
        get_book(book_id, user["id"])
        rhash = rules_hash(user["id"])
        adir = audio_dir(book_id, voice_id, rhash)
        path = adir / f"{chapter_index}_{segment_index}.mp3"
        if path.is_file():
            return FileResponse(path, media_type="audio/mpeg")

        segment = next(
            (s for s in load_segments(book_id)
             if s["chapterIndex"] == chapter_index and s["index"] == segment_index),
            None,
        )
        if segment is None:
            raise HTTPException(404, "Segment nicht gefunden")

        lock = live_locks.setdefault((book_id, _voice_token(voice_id)), asyncio.Lock())
        async with lock:
            if not path.is_file():
                try:
                    await render_segment(
                        segment, voice_id, user["id"], adir, get_rules(user["id"]),
                        tmp_suffix=".live",
                    )
                except Exception as exc:
                    logger.exception("Live-Segment fehlgeschlagen (%s, %s, %s_%s)",
                                     book_id, voice_id, chapter_index, segment_index)
                    raise HTTPException(502, f"Generierung fehlgeschlagen: {str(exc)[:200]}")
        return FileResponse(path, media_type="audio/mpeg")

    return router
