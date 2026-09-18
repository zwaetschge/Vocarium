"""Router-Fassade für den Hörspiele-Bereich.

Die Fachlogik liegt unverändert in :mod:`hoerspiele.engine`; hier wird sie nur
mit den Vocarium-Diensten verdrahtet, damit das Einhängen genauso aussieht wie
bei Podcasts und Hörbüchern.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request

from . import engine
from .store import StateStore

#: Vom Gateway im Startup-Ereignis aufgerufen, sobald ``init_db()`` durch ist.
start = engine.start


def create_hoerspiele_router(
    *,
    get_current_user: Callable[[Request], dict],
    db_getter: Callable[[], sqlite3.Connection],
    tts_bridge: Any = None,
    gpu_submit: Callable[..., Any] | None = None,
    data_dir: Path | None = None,
) -> APIRouter:
    root = Path(os.getenv("HOERSPIELE_DATA_DIR") or "") if os.getenv("HOERSPIELE_DATA_DIR") else None
    if root is None and data_dir is not None:
        root = Path(data_dir) / "hoerspiele"
    return engine.configure(
        get_current_user=get_current_user,
        store=StateStore(db_getter),
        tts_bridge=tts_bridge,
        gpu_submit=gpu_submit,
        data_root=root,
    )
