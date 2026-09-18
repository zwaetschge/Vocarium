"""SQLite database for Vocarium voice metadata, benchmarks, and podcast jobs."""

import os
import json
import sqlite3
import struct
import time
import uuid
from pathlib import Path

from metrics import observe

_db: sqlite3.Connection | None = None
_db_path: Path | None = None

# Speaker presets of the retired Qwen engine. They are kept so existing rows
# stay resolvable; with QWEN_TTS_ENABLED=false they are hidden everywhere and
# legacy hosts were remapped to OmniVoice voices.
_BUILTIN_SPEAKERS = [
    ("Vivian",   "female", "German"),
    ("Serena",   "female", "German"),
    ("Uncle_Fu", "male",   "Chinese"),
    ("Dylan",    "male",   "German"),
    ("Eric",     "male",   "German"),
    ("Ryan",     "male",   "German"),
    ("Aiden",    "male",   "German"),
    ("Ono_Anna", "female", "Japanese"),
    ("Sohee",    "female", "Korean"),
]


class InstrumentedConnection(sqlite3.Connection):
    """sqlite3 connection that records coarse query timings."""

    def execute(self, sql, parameters=(), /):  # type: ignore[no-untyped-def]
        start = time.perf_counter()
        try:
            return super().execute(sql, parameters)
        finally:
            observe(
                "vocarium_db_query_seconds",
                time.perf_counter() - start,
                {"operation": _sql_operation(sql)},
            )

    def executemany(self, sql, parameters, /):  # type: ignore[no-untyped-def]
        start = time.perf_counter()
        try:
            return super().executemany(sql, parameters)
        finally:
            observe(
                "vocarium_db_query_seconds",
                time.perf_counter() - start,
                {"operation": _sql_operation(sql)},
            )

    def executescript(self, sql_script, /):  # type: ignore[no-untyped-def]
        start = time.perf_counter()
        try:
            return super().executescript(sql_script)
        finally:
            observe(
                "vocarium_db_query_seconds",
                time.perf_counter() - start,
                {"operation": "script"},
            )


def _sql_operation(sql: str) -> str:
    for token in (sql or "").lstrip().split():
        return token.strip().lower()[:32] or "unknown"
    return "unknown"


def _connect_database(
    db_path: Path,
    *,
    row_factory=None,  # type: ignore[no-untyped-def]
    isolation_level: str | None = "",
) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        factory=InstrumentedConnection,
        isolation_level=isolation_level,
    )
    try:
        connection.row_factory = row_factory
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except Exception:
        connection.close()
        raise


def init_db(db_path: Path):
    global _db, _db_path
    _db_path = Path(db_path)
    _db = _connect_database(_db_path)

    _db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS voices (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            name TEXT NOT NULL,
            language TEXT DEFAULT 'German',
            source TEXT NOT NULL DEFAULT 'clone',
            design_prompt TEXT DEFAULT '',
            ref_text TEXT DEFAULT '',
            speaker TEXT DEFAULT '',
            instruct TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS benchmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            voice_id TEXT,
            model_id TEXT,
            text TEXT,
            audio_duration REAL,
            generation_time REAL,
            rtf REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS hosts (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            name TEXT NOT NULL,
            personality TEXT NOT NULL DEFAULT '',
            speaking_style TEXT NOT NULL DEFAULT '',
            persona_tagline TEXT NOT NULL DEFAULT '',
            persona_humour TEXT NOT NULL DEFAULT '',
            persona_warmth TEXT NOT NULL DEFAULT '',
            persona_script_length TEXT NOT NULL DEFAULT 'medium',
            voice_id TEXT,
            role TEXT NOT NULL DEFAULT 'host',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_hosts_user_id ON hosts(user_id);
        CREATE INDEX IF NOT EXISTS idx_hosts_role ON hosts(role);

        CREATE TABLE IF NOT EXISTS llm_providers (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            temperature REAL NOT NULL DEFAULT 0.8,
            max_tokens INTEGER NOT NULL DEFAULT 16384,
            is_active INTEGER NOT NULL DEFAULT 0,
            provider_type TEXT NOT NULL DEFAULT 'openai',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_llm_providers_user_id ON llm_providers(user_id);
        CREATE INDEX IF NOT EXISTS idx_llm_providers_active ON llm_providers(user_id, is_active);

        CREATE TABLE IF NOT EXISTS podcasts (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            topic TEXT NOT NULL DEFAULT '',
            format TEXT NOT NULL DEFAULT 'dialog',
            disfluency_level INTEGER NOT NULL DEFAULT 2,
            duration TEXT NOT NULL DEFAULT 'medium',
            language TEXT NOT NULL DEFAULT 'de',
            status TEXT NOT NULL DEFAULT 'draft',
            error_message TEXT,
            script_json TEXT,
            hosts_json TEXT,
            sources_json TEXT,
            audio_path TEXT,
            audio_duration REAL NOT NULL DEFAULT 0,
            audio_format TEXT NOT NULL DEFAULT 'mp3',
            audio_size INTEGER NOT NULL DEFAULT 0,
            audio_sha256 TEXT NOT NULL DEFAULT '',
            total_words INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_podcasts_user_id ON podcasts(user_id);
        CREATE INDEX IF NOT EXISTS idx_podcasts_status ON podcasts(status);
        CREATE INDEX IF NOT EXISTS idx_podcasts_created_at ON podcasts(created_at DESC);

        CREATE TABLE IF NOT EXISTS podcast_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            podcast_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            message TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (podcast_id) REFERENCES podcasts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_podcast_jobs_podcast_id ON podcast_jobs(podcast_id);
        CREATE INDEX IF NOT EXISTS idx_podcast_jobs_created_at ON podcast_jobs(created_at DESC);

        CREATE TABLE IF NOT EXISTS ab_books (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            format TEXT NOT NULL,
            voice_id TEXT,
            total_chapters INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ab_bookmarks (
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            chapter_index INTEGER NOT NULL,
            segment_index INTEGER NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS library_links (
            user_id INTEGER NOT NULL,
            book_id TEXT NOT NULL REFERENCES ab_books(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL,
            PRIMARY KEY (user_id, book_id, project_id)
        );
        CREATE TABLE IF NOT EXISTS ab_progress (
            book_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            chapter_index INTEGER DEFAULT 0,
            segment_index INTEGER DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (book_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS ab_collections (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT DEFAULT '#c59f5f',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ab_collection_books (
            collection_id TEXT NOT NULL,
            book_id TEXT NOT NULL,
            PRIMARY KEY (collection_id, book_id)
        );
        CREATE TABLE IF NOT EXISTS ab_generation_queue (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            book_id TEXT NOT NULL,
            voice_id TEXT NOT NULL,
            chapter_index INTEGER,
            priority INTEGER NOT NULL DEFAULT 5,
            status TEXT NOT NULL DEFAULT 'pending',
            done INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            error TEXT DEFAULT '',
            created_at TEXT,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ab_queue_status ON ab_generation_queue(status, priority, created_at);
        CREATE TABLE IF NOT EXISTS ab_user_prefs (
            user_id INTEGER PRIMARY KEY,
            prefs TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER NOT NULL,
            namespace TEXT NOT NULL,
            prefs TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT,
            PRIMARY KEY (user_id, namespace)
        );
        CREATE TABLE IF NOT EXISTS ab_store_ratings (
            store_book_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            created_at TEXT,
            PRIMARY KEY (store_book_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS ab_ambience (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            filename TEXT NOT NULL,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ab_store_books (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            genre TEXT DEFAULT '',
            format TEXT NOT NULL,
            total_chapters INTEGER DEFAULT 0,
            added_by INTEGER NOT NULL,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ab_listening_sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            book_id TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            segments_played INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ab_sessions_user ON ab_listening_sessions(user_id, started_at);
        CREATE TABLE IF NOT EXISTS ab_pronunciation_rules (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            original TEXT NOT NULL,
            replacement TEXT NOT NULL,
            language TEXT DEFAULT 'German',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS podcast_sources (
            id TEXT PRIMARY KEY,
            podcast_id TEXT NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT,
            url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            FOREIGN KEY (podcast_id) REFERENCES podcasts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_podcast_sources_podcast_id ON podcast_sources(podcast_id);

        CREATE TABLE IF NOT EXISTS podcast_chunks (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            podcast_id TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding TEXT,
            embedding_blob BLOB,
            position INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES podcast_sources(id) ON DELETE CASCADE,
            FOREIGN KEY (podcast_id) REFERENCES podcasts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_podcast_chunks_source_id ON podcast_chunks(source_id);
        CREATE INDEX IF NOT EXISTS idx_podcast_chunks_podcast_id ON podcast_chunks(podcast_id);

        CREATE TABLE IF NOT EXISTS music_tasks (
            task_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'submitted',
            file_paths TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_music_tasks_user_id ON music_tasks(user_id);
        CREATE INDEX IF NOT EXISTS idx_music_tasks_updated_at ON music_tasks(updated_at DESC);

        CREATE TABLE IF NOT EXISTS music_task_files (
            task_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (task_id, path),
            FOREIGN KEY (task_id) REFERENCES music_tasks(task_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_music_task_files_user_path ON music_task_files(user_id, path);
        CREATE INDEX IF NOT EXISTS idx_music_task_files_user_task ON music_task_files(user_id, task_id);

        CREATE TABLE IF NOT EXISTS gpu_queue_jobs (
            job_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL DEFAULT 'default',
            user_id INTEGER,
            request_id TEXT,
            service_type TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            position INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            last_error TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 1,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL,
            heartbeat_at REAL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_gpu_queue_jobs_status_updated
            ON gpu_queue_jobs(status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_gpu_queue_jobs_service_updated
            ON gpu_queue_jobs(service_type, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_gpu_queue_jobs_worker_status
            ON gpu_queue_jobs(worker_id, status, updated_at DESC);
    """)

    _ensure_hosts_foreign_key_rules()

    # Migration: add user_id / speaker / instruct columns if missing (existing installs)
    cols = {row[1] for row in _db.execute("PRAGMA table_info(voices)").fetchall()}
    if "user_id" not in cols:
        _db.execute("ALTER TABLE voices ADD COLUMN user_id INTEGER REFERENCES users(id)")
    if "speaker" not in cols:
        _db.execute("ALTER TABLE voices ADD COLUMN speaker TEXT DEFAULT ''")
    if "instruct" not in cols:
        _db.execute("ALTER TABLE voices ADD COLUMN instruct TEXT DEFAULT ''")

    benchmark_cols = {
        row[1] for row in _db.execute("PRAGMA table_info(benchmarks)").fetchall()
    }
    if "user_id" not in benchmark_cols:
        _db.execute("ALTER TABLE benchmarks ADD COLUMN user_id INTEGER REFERENCES users(id)")
    _db.execute("CREATE INDEX IF NOT EXISTS idx_benchmarks_user_id ON benchmarks(user_id)")
    _db.execute("CREATE INDEX IF NOT EXISTS idx_benchmarks_user_created_at_desc ON benchmarks(user_id, created_at DESC)")
    podcast_cols = {
        row[1] for row in _db.execute("PRAGMA table_info(podcasts)").fetchall()
    }
    if "audio_size" not in podcast_cols:
        _db.execute("ALTER TABLE podcasts ADD COLUMN audio_size INTEGER NOT NULL DEFAULT 0")
    if "audio_sha256" not in podcast_cols:
        _db.execute("ALTER TABLE podcasts ADD COLUMN audio_sha256 TEXT NOT NULL DEFAULT ''")
    podcast_cols = {row[1] for row in _db.execute("PRAGMA table_info(podcasts)").fetchall()}
    if "audio_revision" not in podcast_cols:
        _db.execute("ALTER TABLE podcasts ADD COLUMN audio_revision TEXT NOT NULL DEFAULT ''")
    progress_cols = {row[1] for row in _db.execute("PRAGMA table_info(ab_progress)").fetchall()}
    if "completed" not in progress_cols:
        _db.execute("ALTER TABLE ab_progress ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
    ab_cols = {row[1] for row in _db.execute("PRAGMA table_info(ab_books)").fetchall()}
    if "is_hidden" not in ab_cols:
        _db.execute("ALTER TABLE ab_books ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0")
    if "store_book_id" not in ab_cols:
        _db.execute("ALTER TABLE ab_books ADD COLUMN store_book_id TEXT")
    if "chunker_version" not in ab_cols:
        # Bestand wurde mit v1 segmentiert — Cache-Pfade bleiben gültig.
        _db.execute("ALTER TABLE ab_books ADD COLUMN chunker_version TEXT NOT NULL DEFAULT 'v1'")
    store_cols = {row[1] for row in _db.execute("PRAGMA table_info(ab_store_books)").fetchall()}
    if "chunker_version" not in store_cols:
        _db.execute("ALTER TABLE ab_store_books ADD COLUMN chunker_version TEXT NOT NULL DEFAULT 'v1'")
    queue_cols = {
        row[1] for row in _db.execute("PRAGMA table_info(gpu_queue_jobs)").fetchall()
    }
    if "worker_id" not in queue_cols:
        _db.execute("ALTER TABLE gpu_queue_jobs ADD COLUMN worker_id TEXT NOT NULL DEFAULT 'default'")
    if "user_id" not in queue_cols:
        _db.execute("ALTER TABLE gpu_queue_jobs ADD COLUMN user_id INTEGER")
    if "request_id" not in queue_cols:
        _db.execute("ALTER TABLE gpu_queue_jobs ADD COLUMN request_id TEXT")
    if "last_error" not in queue_cols:
        _db.execute("ALTER TABLE gpu_queue_jobs ADD COLUMN last_error TEXT")
    if "attempt_count" not in queue_cols:
        _db.execute("ALTER TABLE gpu_queue_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
    if "max_attempts" not in queue_cols:
        _db.execute("ALTER TABLE gpu_queue_jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 1")
    if "cancel_requested" not in queue_cols:
        _db.execute("ALTER TABLE gpu_queue_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
    if "heartbeat_at" not in queue_cols:
        _db.execute("ALTER TABLE gpu_queue_jobs ADD COLUMN heartbeat_at REAL")
    _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_gpu_queue_jobs_worker_status "
        "ON gpu_queue_jobs(worker_id, status, updated_at DESC)"
    )

    host_cols = {
        row[1] for row in _db.execute("PRAGMA table_info(hosts)").fetchall()
    }
    if "persona_tagline" not in host_cols:
        _db.execute("ALTER TABLE hosts ADD COLUMN persona_tagline TEXT NOT NULL DEFAULT ''")
    if "persona_humour" not in host_cols:
        _db.execute("ALTER TABLE hosts ADD COLUMN persona_humour TEXT NOT NULL DEFAULT ''")
    if "persona_warmth" not in host_cols:
        _db.execute("ALTER TABLE hosts ADD COLUMN persona_warmth TEXT NOT NULL DEFAULT ''")
    if "persona_script_length" not in host_cols:
        _db.execute("ALTER TABLE hosts ADD COLUMN persona_script_length TEXT NOT NULL DEFAULT 'medium'")
    _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_gpu_queue_jobs_user_status "
        "ON gpu_queue_jobs(user_id, status, updated_at DESC)"
    )
    _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_gpu_queue_jobs_request_id "
        "ON gpu_queue_jobs(request_id)"
    )
    _ensure_performance_schema()

    # Migration: llm_providers table (added 2026-04-22)
    tables = {row[0] for row in _db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "llm_providers" not in tables:
        _db.executescript("""
            CREATE TABLE llm_providers (
                id TEXT PRIMARY KEY,
                user_id INTEGER,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                temperature REAL NOT NULL DEFAULT 0.8,
                max_tokens INTEGER NOT NULL DEFAULT 16384,
                is_active INTEGER NOT NULL DEFAULT 0,
                provider_type TEXT NOT NULL DEFAULT 'openai',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE INDEX idx_llm_providers_user_id ON llm_providers(user_id);
            CREATE INDEX idx_llm_providers_active ON llm_providers(user_id, is_active);
        """)
        # Seed a placeholder provider from env for all existing users.
        # If LLM_API_URL is unset, no row is inserted — users configure via UI.
        default_base_url = os.environ.get("LLM_API_URL", "")
        default_model = os.environ.get("LLM_MODEL", "")
        default_api_key = os.environ.get("LLM_API_KEY", "")
        default_temp = os.environ.get("LLM_TEMPERATURE", "0.8")
        default_max = os.environ.get("LLM_MAX_TOKENS", "16384")
        if default_base_url:
            for (uid,) in _db.execute("SELECT id FROM users").fetchall():
                _db.execute(
                    "INSERT OR IGNORE INTO llm_providers (id, user_id, name, base_url, api_key, model, temperature, max_tokens, is_active, provider_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'openai')",
                    (f"default-{uid}", uid, "Default", default_base_url, default_api_key, default_model, default_temp, default_max),
                )
        _db.commit()
    else:
        # Ensure every user has at least one provider (env fallback) — but
        # only if an env-configured URL exists; otherwise leave empty so the
        # user is prompted to configure one in Settings.
        default_base_url = os.environ.get("LLM_API_URL", "")
        default_model = os.environ.get("LLM_MODEL", "")
        default_api_key = os.environ.get("LLM_API_KEY", "")
        default_temp = os.environ.get("LLM_TEMPERATURE", "0.8")
        default_max = os.environ.get("LLM_MAX_TOKENS", "16384")
        if default_base_url:
            for (uid,) in _db.execute("SELECT id FROM users").fetchall():
                has_active = _db.execute(
                    "SELECT 1 FROM llm_providers WHERE user_id=? AND is_active=1 LIMIT 1",
                    (uid,),
                ).fetchone()
                if not has_active:
                    _db.execute(
                        "INSERT OR IGNORE INTO llm_providers (id, user_id, name, base_url, api_key, model, temperature, max_tokens, is_active, provider_type) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'openai')",
                        (f"default-{uid}", uid, "Default", default_base_url, default_api_key, default_model, default_temp, default_max),
                    )
        _db.commit()

    _db.commit()


def _ensure_hosts_foreign_key_rules() -> None:
    """Rebuild legacy hosts tables so voice_id is a plain engine voice id.

    Voices used to be rows in `voices`, so hosts.voice_id carried a foreign key.
    Since the OmniVoice/Kikiri migration a podcast voice is named by the engine
    and has no row anywhere, which made every host with a working voice
    unsavable. The user FK stays: hosts still belong to a user.
    """
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    by_column = {row[3]: row for row in _db.execute("PRAGMA foreign_key_list(hosts)").fetchall()}
    user_fk = by_column.get("user_id")
    if "voice_id" not in by_column and user_fk and user_fk[6].upper() == "CASCADE":
        return

    # Carry over every column the old table happens to have; anything the
    # canonical schema added later falls back to its default.
    canonical = [
        "id", "user_id", "name", "personality", "speaking_style",
        "persona_tagline", "persona_humour", "persona_warmth",
        "persona_script_length", "voice_id", "role", "created_at", "updated_at",
    ]
    existing = {row[1] for row in _db.execute("PRAGMA table_info(hosts)").fetchall()}
    shared = ", ".join(c for c in canonical if c in existing)

    _db.commit()
    _db.execute("PRAGMA foreign_keys=OFF")
    try:
        _db.executescript(f"""
            ALTER TABLE hosts RENAME TO hosts_legacy;

            CREATE TABLE hosts (
                id TEXT PRIMARY KEY,
                user_id INTEGER,
                name TEXT NOT NULL,
                personality TEXT NOT NULL DEFAULT '',
                speaking_style TEXT NOT NULL DEFAULT '',
                persona_tagline TEXT NOT NULL DEFAULT '',
                persona_humour TEXT NOT NULL DEFAULT '',
                persona_warmth TEXT NOT NULL DEFAULT '',
                persona_script_length TEXT NOT NULL DEFAULT 'medium',
                voice_id TEXT,
                role TEXT NOT NULL DEFAULT 'host',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            INSERT INTO hosts ({shared}) SELECT {shared} FROM hosts_legacy;

            DROP TABLE hosts_legacy;
            CREATE INDEX IF NOT EXISTS idx_hosts_user_id ON hosts(user_id);
            CREATE INDEX IF NOT EXISTS idx_hosts_role ON hosts(role);
            CREATE INDEX IF NOT EXISTS idx_hosts_user_name_nocase ON hosts(user_id, name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_hosts_user_created_at ON hosts(user_id, created_at);
        """)
    finally:
        _db.execute("PRAGMA foreign_keys=ON")
    _db.commit()


def _ensure_performance_schema() -> None:
    """Add low-risk indexes and additive columns used by hot-path queries."""
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    chunk_cols = {row[1] for row in _db.execute("PRAGMA table_info(podcast_chunks)").fetchall()}
    if "embedding_blob" not in chunk_cols:
        _db.execute("ALTER TABLE podcast_chunks ADD COLUMN embedding_blob BLOB")

    _db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_voices_user_source_created_at
            ON voices(user_id, source, created_at);
        CREATE INDEX IF NOT EXISTS idx_voices_user_created_at_desc
            ON voices(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_benchmarks_user_created_at_desc
            ON benchmarks(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_hosts_user_name_nocase
            ON hosts(user_id, name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_hosts_user_created_at
            ON hosts(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_podcasts_user_created_at_desc
            ON podcasts(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_podcast_chunks_podcast_position
            ON podcast_chunks(podcast_id, position);
        CREATE INDEX IF NOT EXISTS idx_podcast_sources_podcast_created_at
            ON podcast_sources(podcast_id, created_at);
        CREATE TABLE IF NOT EXISTS music_task_files (
            task_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (task_id, path),
            FOREIGN KEY (task_id) REFERENCES music_tasks(task_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_music_task_files_user_path
            ON music_task_files(user_id, path);
        CREATE INDEX IF NOT EXISTS idx_music_task_files_user_task
            ON music_task_files(user_id, task_id);
    """)
    _backfill_music_task_files()
    _backfill_podcast_embedding_blobs()
    _db.commit()


def _backfill_music_task_files() -> None:
    """Populate normalized music output paths from legacy JSON rows."""
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    rows = _db.execute(
        "SELECT task_id, user_id, file_paths, created_at FROM music_tasks "
        "WHERE file_paths IS NOT NULL AND file_paths != '[]'"
    ).fetchall()
    for task_id, user_id, paths_json, created_at in rows:
        try:
            paths = json.loads(paths_json or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(paths, list):
            continue
        for path in paths:
            if not isinstance(path, str) or not path:
                continue
            _db.execute(
                "INSERT OR IGNORE INTO music_task_files (task_id, user_id, path, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, user_id, path, created_at),
            )


def _backfill_podcast_embedding_blobs() -> None:
    """Convert legacy JSON embeddings to compact float32 blobs where missing."""
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    rows = _db.execute(
        "SELECT id, embedding FROM podcast_chunks "
        "WHERE embedding_blob IS NULL AND embedding IS NOT NULL AND embedding != ''"
    ).fetchall()
    for chunk_id, embedding_json in rows:
        try:
            values = json.loads(embedding_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(values, list) or not values:
            continue
        try:
            blob = struct.pack(f"<{len(values)}f", *[float(v) for v in values])
        except (TypeError, ValueError, struct.error):
            continue
        _db.execute(
            "UPDATE podcast_chunks SET embedding_blob=? WHERE id=?",
            (blob, chunk_id),
        )


def _seed_prebuilt_custom_voices(
    db: sqlite3.Connection, user_id: int
) -> int:
    """Insert one saved custom-voice preset per built-in speaker for a user.
    Idempotent: skips speakers that already exist as saved voices for this user.
    Returns the number of rows inserted."""
    existing = {
        row[0]
        for row in db.execute(
            "SELECT speaker FROM voices WHERE user_id=? AND source='custom'",
            (user_id,),
        ).fetchall()
    }
    inserted = 0
    for speaker_id, _gender, language in _BUILTIN_SPEAKERS:
        if speaker_id in existing:
            continue
        name = speaker_id.replace("_", " ")
        instruct = _BUILTIN_SPEAKER_INSTRUCTS.get(speaker_id, "")
        db.execute(
            "INSERT INTO voices (id, user_id, name, language, source, speaker, instruct, created_at) "
            "VALUES (?, ?, ?, ?, 'custom', ?, ?, datetime('now'))",
            (str(uuid.uuid4()), user_id, name, language, speaker_id, instruct),
        )
        inserted += 1
    return inserted


def seed_prebuilt_custom_voices(user_id: int) -> int:
    """Seed built-in custom voices and commit any inserted presets."""
    db = get_db()
    inserted = _seed_prebuilt_custom_voices(db, user_id)
    if inserted:
        db.commit()
    return inserted


# Default instruct per speaker (emotion / delivery style)
_BUILTIN_SPEAKER_INSTRUCTS: dict[str, str] = {
    "Vivian": "warm, energetic, clear articulation, slightly faster pace, approachable",
    "Serena": "calm, articulate, thoughtful, measured pace, warm undertone",
    "Uncle_Fu": "wise, gentle, storytelling, warm grandfatherly tone, unhurried",
    "Dylan": "curious, dynamic, bright, clear articulation, modern and relatable",
    "Eric": "professional, measured, authoritative, classic delivery, warm undertone",
    "Ryan": "friendly, conversational, easy-going, natural pacing, relatable",
    "Aiden": "bold, confident, strong presence, assertive, direct",
    "Ono_Anna": "polite, precise, elegant, gentle, clear, graceful",
    "Sohee": "bright, cheerful, expressive, energetic, warm, engaging",
}

# Die 9 Qwen-Sprecher wurden früher automatisch als Podcast-Hosts angelegt.
# Seit dem Host-Hub wählt der Nutzer aus 40 Vorlagen (podcast/host_presets.py),
# und Qwen ist stillgelegt — ein auto-geseedeter Host zeigt also auf eine Stimme,
# die keine Engine mehr kennt, und lässt `POST /api/podcasts` mit 400 scheitern.
# Deshalb wird nichts mehr geseedet; Altbestände werden einmalig zurückgezogen.
_LEGACY_QWEN_HOST_SPEAKERS = tuple(name for name, _gender, _lang in _BUILTIN_SPEAKERS)


def _retire_legacy_qwen_hosts(db: sqlite3.Connection, user_id: int) -> int:
    """Remove auto-seeded Qwen hosts that still point at a retired Qwen voice.

    Conservative on purpose: only rows whose id matches the old seeder's pattern
    AND whose voice is still one of this user's Qwen ``source='custom'`` voices
    are removed. A host the user has since re-pointed at an OmniVoice/Kikiri
    voice is a real host now and stays. Podcasts snapshot their cast into
    ``hosts_json``, so removing the row cannot break an existing podcast.
    """
    legacy_ids = [
        f"host-{speaker.lower()}-{user_id}" for speaker in _LEGACY_QWEN_HOST_SPEAKERS
    ]
    if not legacy_ids:
        return 0
    placeholders = ",".join("?" * len(legacy_ids))
    cursor = db.execute(
        f"DELETE FROM hosts WHERE user_id=? AND id IN ({placeholders}) "
        "AND voice_id IN (SELECT id FROM voices WHERE user_id=? AND source='custom')",
        (user_id, *legacy_ids, user_id),
    )
    return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# LLM Provider helpers
# ---------------------------------------------------------------------------

def get_active_llm_provider(user_id: int) -> dict | None:
    """Return the active LLM provider for a user, or None."""
    db = get_db()
    row = db.execute(
        "SELECT id, name, base_url, api_key, model, temperature, max_tokens, provider_type "
        "FROM llm_providers WHERE user_id=? AND is_active=1 ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "base_url": row[2], "api_key": row[3],
        "model": row[4], "temperature": row[5], "max_tokens": row[6],
        "provider_type": row[7],
    }


def get_or_create_user(username: str) -> dict:
    """Get or auto-create a user by username. Returns dict with id, username, display_name.
    On first creation, seeds the 9 built-in speakers as saved custom voices and creates
    preset podcast hosts for each."""
    db = get_db()
    row = db.execute("SELECT id, username, display_name, created_at FROM users WHERE username=?", (username,)).fetchone()
    if row:
        return {"id": row[0], "username": row[1], "display_name": row[2] or row[1], "created_at": row[3]}
    owned_db = _open_user_creation_connection(db)
    try:
        owned_db.execute("BEGIN IMMEDIATE")
        row = owned_db.execute(
            "SELECT id, username, display_name, created_at FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if row:
            owned_db.commit()
            return {
                "id": row[0],
                "username": row[1],
                "display_name": row[2] or row[1],
                "created_at": row[3],
            }
        cursor = owned_db.execute(
            "INSERT OR IGNORE INTO users (username, display_name) VALUES (?, ?)",
            (username, username),
        )
        created = cursor.rowcount == 1
        row = owned_db.execute(
            "SELECT id, username, display_name, created_at FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if row is None:
            raise RuntimeError("User insert completed without a readable row")
        if created:
            _seed_prebuilt_custom_voices(owned_db, row[0])
        owned_db.commit()
    except Exception:
        owned_db.rollback()
        raise
    finally:
        owned_db.close()
    return {
        "id": row[0],
        "username": row[1],
        "display_name": row[2] or row[1],
        "created_at": row[3],
    }


def _open_user_creation_connection(
    main_db: sqlite3.Connection,
) -> sqlite3.Connection:
    if _db_path is None or str(_db_path) == ":memory:":
        raise RuntimeError(
            "User creation requires an initialized file-backed database"
        )
    return _connect_database(
        _db_path,
        row_factory=main_db.row_factory,
        isolation_level=main_db.isolation_level,
    )


def retire_legacy_qwen_hosts_for_all_users() -> int:
    """Retire auto-seeded Qwen hosts for every user. Called once at startup."""
    db = get_db()
    users = db.execute("SELECT id FROM users").fetchall()
    removed = 0
    for (uid,) in users:
        removed += _retire_legacy_qwen_hosts(db, uid)
    if removed:
        db.commit()
    return removed


def upsert_gpu_queue_job(job: dict) -> None:
    """Persist the latest status for an in-memory GPU queue job."""
    db = get_db()
    db.execute(
        "INSERT INTO gpu_queue_jobs "
        "(job_id, worker_id, user_id, request_id, service_type, description, status, "
        " position, error, last_error, attempt_count, max_attempts, cancel_requested, "
        " created_at, started_at, finished_at, heartbeat_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "worker_id=excluded.worker_id, "
        "user_id=excluded.user_id, "
        "request_id=excluded.request_id, "
        "service_type=excluded.service_type, "
        "description=excluded.description, "
        "status=CASE "
        "WHEN gpu_queue_jobs.cancel_requested=1 AND excluded.status IN ('queued', 'running') "
        "THEN 'cancel_requested' ELSE excluded.status END, "
        "position=excluded.position, "
        "error=CASE "
        "WHEN gpu_queue_jobs.cancel_requested=1 AND excluded.status IN ('queued', 'running') "
        "THEN COALESCE(gpu_queue_jobs.error, 'cancel requested') ELSE excluded.error END, "
        "last_error=excluded.last_error, "
        "attempt_count=excluded.attempt_count, "
        "max_attempts=excluded.max_attempts, "
        "cancel_requested=CASE "
        "WHEN gpu_queue_jobs.cancel_requested=1 THEN 1 ELSE excluded.cancel_requested END, "
        "created_at=excluded.created_at, "
        "started_at=excluded.started_at, "
        "finished_at=excluded.finished_at, "
        "heartbeat_at=excluded.heartbeat_at, "
        "updated_at=excluded.updated_at",
        (
            job["job_id"],
            job.get("worker_id") or "default",
            job.get("user_id"),
            job.get("request_id"),
            job["service_type"],
            job.get("description") or "",
            job["status"],
            int(job.get("position") or 0),
            job.get("error"),
            job.get("last_error"),
            int(job.get("attempt_count") or 0),
            int(job.get("max_attempts") or 1),
            1 if job.get("cancel_requested") else 0,
            float(job["created_at"]),
            job.get("started_at"),
            job.get("finished_at"),
            job.get("heartbeat_at"),
            float(job.get("updated_at") or time.time()),
        ),
    )
    db.commit()


def mark_interrupted_gpu_jobs(
    reason: str = "API restarted",
    *,
    worker_id: str | None = None,
) -> int:
    """Mark stale queued/running persisted GPU jobs as interrupted."""
    db = get_db()
    now = time.time()
    if worker_id:
        cursor = db.execute(
            "UPDATE gpu_queue_jobs SET status='interrupted', error=?, "
            "finished_at=COALESCE(finished_at, ?), updated_at=? "
            "WHERE worker_id=? AND status IN ('queued', 'running')",
            (reason, now, now, worker_id),
        )
    else:
        cursor = db.execute(
            "UPDATE gpu_queue_jobs SET status='interrupted', error=?, "
            "finished_at=COALESCE(finished_at, ?), updated_at=? "
            "WHERE status IN ('queued', 'running')",
            (reason, now, now),
        )
    db.commit()
    return cursor.rowcount


def request_cancel_gpu_queue_job(job_id: str, *, user_id: int | None = None) -> dict | None:
    """Persist a cancellation request for a queued/running job.

    The in-process queue consumes this through its cancel checker. For jobs on
    a different API replica, this is intentionally cooperative: queued jobs are
    skipped before execution, running jobs are marked cancelled after the active
    work returns.
    """
    db = get_db()
    existing = get_gpu_queue_job(job_id, user_id=user_id)
    if not existing:
        return None
    if existing["status"] in ("completed", "failed", "cancelled"):
        return existing
    now = time.time()
    db.execute(
        "UPDATE gpu_queue_jobs SET cancel_requested=1, status='cancel_requested', "
        "error=COALESCE(error, 'cancel requested'), updated_at=? "
        "WHERE job_id=?",
        (now, job_id),
    )
    db.commit()
    return get_gpu_queue_job(job_id, user_id=user_id)


def is_gpu_queue_cancel_requested(job_id: str) -> bool:
    row = get_db().execute(
        "SELECT cancel_requested, status FROM gpu_queue_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not row:
        return False
    return bool(row[0]) or row[1] == "cancel_requested"


def gpu_queue_quota_decision(user_id: int, service_type: str) -> dict:
    """Return whether a user may enqueue another GPU job."""
    max_active = _env_int("GPU_QUEUE_MAX_ACTIVE_PER_USER", 8)
    max_queued = _env_int("GPU_QUEUE_MAX_QUEUED_PER_USER", 6)
    if max_active <= 0 and max_queued <= 0:
        return {"allowed": True}

    rows = get_db().execute(
        "SELECT status, COUNT(*) FROM gpu_queue_jobs "
        "WHERE user_id=? AND status IN ('queued', 'running', 'cancel_requested') "
        "GROUP BY status",
        (user_id,),
    ).fetchall()
    counts = {row[0]: int(row[1]) for row in rows}
    queued = counts.get("queued", 0)
    running = counts.get("running", 0) + counts.get("cancel_requested", 0)
    active = queued + running

    if max_active > 0 and active >= max_active:
        return {
            "allowed": False,
            "message": (
                f"GPU queue quota exceeded: {active}/{max_active} active jobs "
                f"for user {user_id}"
            ),
            "user_id": user_id,
            "service_type": service_type,
            "active": active,
            "queued": queued,
            "running": running,
            "max_active": max_active,
            "max_queued": max_queued,
        }
    if max_queued > 0 and queued >= max_queued:
        return {
            "allowed": False,
            "message": (
                f"GPU queue quota exceeded: {queued}/{max_queued} queued jobs "
                f"for user {user_id}"
            ),
            "user_id": user_id,
            "service_type": service_type,
            "active": active,
            "queued": queued,
            "running": running,
            "max_active": max_active,
            "max_queued": max_queued,
        }
    return {
        "allowed": True,
        "user_id": user_id,
        "service_type": service_type,
        "active": active,
        "queued": queued,
        "running": running,
        "max_active": max_active,
        "max_queued": max_queued,
    }


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def get_gpu_queue_job(job_id: str, *, user_id: int | None = None) -> dict | None:
    params: tuple = (job_id,)
    where = "WHERE job_id=?"
    if user_id is not None:
        where += " AND user_id=?"
        params = (job_id, user_id)
    row = get_db().execute(
        "SELECT job_id, worker_id, user_id, request_id, service_type, description, "
        "status, position, error, last_error, attempt_count, max_attempts, "
        "cancel_requested, created_at, started_at, finished_at, heartbeat_at, updated_at "
        f"FROM gpu_queue_jobs {where}",
        params,
    ).fetchone()
    if not row:
        return None
    return _gpu_queue_job_row(row)


def list_gpu_queue_jobs(limit: int = 50, *, user_id: int | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    params: tuple = (limit,)
    where = ""
    if user_id is not None:
        where = "WHERE user_id=?"
        params = (user_id, limit)
    rows = get_db().execute(
        "SELECT job_id, worker_id, user_id, request_id, service_type, description, "
        "status, position, error, last_error, attempt_count, max_attempts, "
        "cancel_requested, created_at, started_at, finished_at, heartbeat_at, updated_at "
        f"FROM gpu_queue_jobs {where} ORDER BY updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [_gpu_queue_job_row(row) for row in rows]


def _gpu_queue_job_row(row) -> dict:  # type: ignore[no-untyped-def]
    return {
        "job_id": row[0],
        "worker_id": row[1],
        "user_id": row[2],
        "request_id": row[3],
        "service_type": row[4],
        "description": row[5],
        "status": row[6],
        "position": row[7],
        "error": row[8],
        "last_error": row[9],
        "attempt_count": row[10],
        "max_attempts": row[11],
        "cancel_requested": bool(row[12]),
        "created_at": row[13],
        "started_at": row[14],
        "finished_at": row[15],
        "heartbeat_at": row[16],
        "updated_at": row[17],
    }


def get_db() -> sqlite3.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db
