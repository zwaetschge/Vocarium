"""Persistenz für Hörspiele.

Szenenklang hielt den kompletten Zustand in *einer* ``state.json``. Die Datei
war in der Praxis 144 MB groß (sechs Projekte, ~12 MB Worttimings je Projekt)
und wurde bei **jedem** ``save_state()`` vollständig neu serialisiert und
ersetzt — auch innerhalb der Transkriptionsschleife, also einmal pro Kapitel.

Hier liegt derselbe Zustand in SQLite, eine Zeile je Projekt bzw. Lauf. Die
In-Memory-Struktur bleibt identisch (``{"projects": {...}, "runs": {...}, ...}``),
damit die portierte Engine unverändert darauf arbeitet; geschrieben wird aber
nur, was sich seit dem letzten Speichern wirklich geändert hat. Ein Lauf, der
nur ``completed_units`` hochzählt, schreibt damit eine Zeile von wenigen
Kilobyte statt 144 MB.

Jede Zeile trägt ``user_id``: Hörspiele sind wie der Rest von Vocarium
mandantengetrennt.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Callable

SETTINGS_KEYS = ("codex_settings", "agent_settings", "integrations")


class StateStore:
    def __init__(self, db_getter: Callable[[], sqlite3.Connection]) -> None:
        self._db_getter = db_getter
        self._lock = threading.Lock()
        # Zuletzt geschriebene Serialisierung je Zeile — der Dirty-Vergleich.
        self._written: dict[tuple[str, str], str] = {}
        # Das Schema entsteht beim ersten Zugriff: Beim Mounten des Routers ist
        # ``init_db()`` noch nicht gelaufen.
        self._schema_ready = False

    # ── Schema ──────────────────────────────────────────────────────────────
    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        db = self._db_getter()
        with self._lock:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS hs_projects (
                    id          TEXT PRIMARY KEY,
                    user_id     INTEGER NOT NULL,
                    updated_at  TEXT,
                    payload     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_hs_projects_user ON hs_projects(user_id);
                CREATE TABLE IF NOT EXISTS hs_runs (
                    id          TEXT PRIMARY KEY,
                    project_id  TEXT,
                    user_id     INTEGER NOT NULL,
                    updated_at  TEXT,
                    payload     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_hs_runs_user ON hs_runs(user_id);
                CREATE INDEX IF NOT EXISTS idx_hs_runs_project ON hs_runs(project_id);
                CREATE TABLE IF NOT EXISTS hs_settings (
                    key     TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                """
            )
            db.commit()
        self._schema_ready = True

    # ── Laden ───────────────────────────────────────────────────────────────
    def load(self, base: dict[str, Any]) -> dict[str, Any]:
        self._ensure_schema()
        db = self._db_getter()
        state = dict(base)
        state["projects"] = {}
        state["runs"] = {}
        with self._lock:
            for row in db.execute("SELECT id, payload FROM hs_projects").fetchall():
                try:
                    state["projects"][row[0]] = json.loads(row[1])
                except json.JSONDecodeError:
                    continue
                self._written[("project", row[0])] = row[1]
            for row in db.execute("SELECT id, payload FROM hs_runs").fetchall():
                try:
                    state["runs"][row[0]] = json.loads(row[1])
                except json.JSONDecodeError:
                    continue
                self._written[("run", row[0])] = row[1]
            for row in db.execute("SELECT key, payload FROM hs_settings").fetchall():
                if row[0] in SETTINGS_KEYS:
                    try:
                        state[row[0]] = json.loads(row[1])
                    except json.JSONDecodeError:
                        continue
                    self._written[("setting", row[0])] = row[1]
        return state

    # ── Speichern ───────────────────────────────────────────────────────────
    def save(self, state: dict[str, Any]) -> None:
        """Schreibt nur geänderte Zeilen. Aufrufkompatibel zu ``save_state()``."""
        self._ensure_schema()
        projects = state.get("projects") or {}
        runs = state.get("runs") or {}
        db = self._db_getter()
        with self._lock:
            for project_id, project in projects.items():
                payload = json.dumps(project, ensure_ascii=False)
                if self._written.get(("project", project_id)) == payload:
                    continue
                db.execute(
                    "INSERT INTO hs_projects (id, user_id, updated_at, payload) VALUES (?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id, "
                    "updated_at=excluded.updated_at, payload=excluded.payload",
                    (
                        project_id,
                        int(project.get("owner_user_id") or 0),
                        str(project.get("updated_at") or ""),
                        payload,
                    ),
                )
                self._written[("project", project_id)] = payload

            for run_id, run in runs.items():
                payload = json.dumps(run, ensure_ascii=False)
                if self._written.get(("run", run_id)) == payload:
                    continue
                db.execute(
                    "INSERT INTO hs_runs (id, project_id, user_id, updated_at, payload) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, "
                    "user_id=excluded.user_id, updated_at=excluded.updated_at, payload=excluded.payload",
                    (
                        run_id,
                        str(run.get("project_id") or ""),
                        int(run.get("owner_user_id") or 0),
                        str(run.get("updated_at") or ""),
                        payload,
                    ),
                )
                self._written[("run", run_id)] = payload

            for key in SETTINGS_KEYS:
                if key not in state:
                    continue
                payload = json.dumps(state[key], ensure_ascii=False)
                if self._written.get(("setting", key)) == payload:
                    continue
                db.execute(
                    "INSERT INTO hs_settings (key, payload) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                    (key, payload),
                )
                self._written[("setting", key)] = payload

            # Zeilen, deren Objekt im Speicher gelöscht wurde, verschwinden auch
            # aus der Datenbank — sonst tauchte ein gelöschtes Projekt beim
            # nächsten Start wieder auf.
            for kind, table, live in (
                ("project", "hs_projects", projects),
                ("run", "hs_runs", runs),
            ):
                stale = [
                    key[1]
                    for key in list(self._written)
                    if key[0] == kind and key[1] not in live
                ]
                for row_id in stale:
                    db.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
                    self._written.pop((kind, row_id), None)

            db.commit()
