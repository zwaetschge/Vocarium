"""Bereichs-Voreinstellungen pro Nutzer.

Ein schmaler Key-Value-Speicher, damit jeder Tab im Einstellungs-Hub seine
Voreinstellungen sichern kann, ohne dass für jeden Schalter eine eigene Tabelle
entsteht. Nur bekannte Schlüssel werden gespeichert — sonst wächst hier
stillschweigend beliebiger Client-Zustand mit.

Liegt bewusst neben `database.py` statt in `main.py`, weil die Podcast-Routen
dieselben Defaults brauchen und nicht auf `main` zeigen dürfen.
"""

from __future__ import annotations

import json
import sqlite3
import time

SETTINGS_NAMESPACES: dict[str, dict[str, object]] = {
    "podcast": {
        "format": "dialog",
        "duration": "medium",
        "audio_format": "mp3",
        "disfluency_level": 2,
        "language": "German",
    },
    "lab": {
        "speech_voice": "",
        "speech_format": "mp3",
        "speech_speed": 1.0,
    },
    "general": {
        "area_default": "audiobooks",
    },
}


def defaults(namespace: str) -> dict:
    return dict(SETTINGS_NAMESPACES.get(namespace, {}))


def read(db: sqlite3.Connection, user_id: int, namespace: str) -> dict:
    """Gespeicherte Werte über die Defaults gelegt.

    Fehlt die Tabelle oder ist der Eintrag kaputt, gelten die Defaults — eine
    unlesbare Einstellung darf keine Seite lahmlegen.
    """
    merged = defaults(namespace)
    if not merged:
        return {}
    try:
        row = db.execute(
            "SELECT prefs FROM user_settings WHERE user_id=? AND namespace=?",
            (user_id, namespace),
        ).fetchone()
    except sqlite3.Error:
        return merged
    if row and row[0]:
        try:
            stored = json.loads(row[0])
        except (TypeError, ValueError):
            stored = {}
        if isinstance(stored, dict):
            merged.update({k: v for k, v in stored.items() if k in merged})
    return merged


def write(db: sqlite3.Connection, user_id: int, namespace: str, body: dict) -> dict:
    allowed = SETTINGS_NAMESPACES.get(namespace, {})
    merged = read(db, user_id, namespace)
    merged.update({k: v for k, v in (body or {}).items() if k in allowed})
    db.execute(
        "INSERT INTO user_settings (user_id, namespace, prefs, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, namespace) DO UPDATE SET "
        "prefs=excluded.prefs, updated_at=excluded.updated_at",
        (user_id, namespace, json.dumps(merged), time.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    db.commit()
    return merged
