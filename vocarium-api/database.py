"""SQLite database for Vocarium voice metadata, benchmarks, and podcast jobs."""

import os
import sqlite3
import uuid
import datetime
from pathlib import Path

_db: sqlite3.Connection | None = None

# Mirrors BUILTIN_SPEAKERS in qwen3-tts/server.py. Every user gets one saved
# custom-voice preset per speaker on first creation so hosts/podcasts work
# without forcing a trip through the Custom page first.
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


def init_db(db_path: Path):
    global _db
    _db = sqlite3.connect(str(db_path), check_same_thread=False)
    _db.execute("PRAGMA journal_mode=WAL")
    _db.execute("PRAGMA foreign_keys=ON")

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
            voice_id TEXT,
            role TEXT NOT NULL DEFAULT 'host',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (voice_id) REFERENCES voices(id)
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
            position INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES podcast_sources(id) ON DELETE CASCADE,
            FOREIGN KEY (podcast_id) REFERENCES podcasts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_podcast_chunks_source_id ON podcast_chunks(source_id);
        CREATE INDEX IF NOT EXISTS idx_podcast_chunks_podcast_id ON podcast_chunks(podcast_id);
    """)

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


def seed_prebuilt_custom_voices(user_id: int) -> int:
    """Insert one saved custom-voice preset per built-in speaker for a user.
    Idempotent: skips speakers that already exist as saved voices for this user.
    Returns the number of rows inserted."""
    db = get_db()
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

# Personality profiles for the 9 built-in speakers when auto-created as podcast hosts
_BUILTIN_HOST_PROFILES: dict[str, tuple[str, str, str]] = {
    # (personality, speaking_style, role)
    "Vivian":   ("Energetic and warm podcast host who brings enthusiasm to every topic.", "Energetic, warm, clear articulation", "host"),
    "Serena":   ("Calm, articulate, and insightful. Ideal for thoughtful discussions and deep dives.", "Calm, articulate, thoughtful", "host"),
    "Uncle_Fu": ("Wise, grandfatherly figure with a gentle, storytelling tone. Brings cultural depth.", "Gentle, storytelling, wise", "expert"),
    "Dylan":    ("Young, curious, and dynamic. Great for tech, innovation, and trending topics.", "Young, curious, dynamic", "host"),
    "Eric":     ("Professional and measured. Classic news-anchor delivery with authority.", "Professional, measured, authoritative", "expert"),
    "Ryan":     ("Friendly and relatable. Conversational and easy-going with broad appeal.", "Friendly, relatable, conversational", "host"),
    "Aiden":    ("Bold and confident. Strong presence, perfect for debates and challenging takes.", "Bold, confident, strong presence", "expert"),
    "Ono_Anna": ("Polite, precise, and elegant. Embodies Japanese cultural grace and clarity.", "Polite, precise, elegant", "expert"),
    "Sohee":    ("Bright, cheerful, and expressive. Brings Korean pop-culture energy and charm.", "Bright, cheerful, expressive", "host"),
}


def seed_prebuilt_hosts(user_id: int) -> int:
    """Create a preset podcast host for every custom-voice speaker a user owns.
    Idempotent: skips if a host with the same name already exists for this user.
    Returns the number of hosts inserted."""
    db = get_db()
    # Find this user's custom voices that correspond to built-in speakers
    rows = db.execute(
        "SELECT id, name, speaker FROM voices WHERE user_id=? AND source='custom'",
        (user_id,),
    ).fetchall()

    # Build set of existing host names for idempotency
    existing_host_names = {
        row[0]
        for row in db.execute(
            "SELECT name FROM hosts WHERE user_id=?", (user_id,)
        ).fetchall()
    }

    inserted = 0
    now = datetime.datetime.utcnow().isoformat()
    for voice_id, voice_name, speaker_id in rows:
        profile = _BUILTIN_HOST_PROFILES.get(speaker_id)
        if not profile:
            continue
        if voice_name in existing_host_names:
            # Ensure the existing host is linked to this voice_id
            db.execute(
                "UPDATE hosts SET voice_id=? WHERE user_id=? AND name=? AND voice_id IS NULL",
                (voice_id, user_id, voice_name),
            )
            continue
        personality, speaking_style, role = profile
        host_id = f"host-{speaker_id.lower()}-{user_id}"
        db.execute(
            "INSERT INTO hosts (id, user_id, name, personality, speaking_style, voice_id, role, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (host_id, user_id, voice_name, personality, speaking_style, voice_id, role, now, now),
        )
        inserted += 1

    if inserted or db.total_changes > 0:
        db.commit()
    return inserted


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
    db.execute("INSERT INTO users (username, display_name) VALUES (?, ?)", (username, username))
    db.commit()
    row = db.execute("SELECT id, username, display_name, created_at FROM users WHERE username=?", (username,)).fetchone()
    seed_prebuilt_custom_voices(row[0])
    seed_prebuilt_hosts(row[0])
    return {"id": row[0], "username": row[1], "display_name": row[2] or row[1], "created_at": row[3]}


def backfill_hosts_for_all_users() -> int:
    """Backfill preset hosts for all existing users who don't have them yet.
    Called once at startup."""
    db = get_db()
    users = db.execute("SELECT id FROM users").fetchall()
    total_inserted = 0
    for (uid,) in users:
        total_inserted += seed_prebuilt_hosts(uid)
    return total_inserted


def get_db() -> sqlite3.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db
