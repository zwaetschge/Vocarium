"""Small authorization helpers without FastAPI or database dependencies."""

from __future__ import annotations


def is_admin_username(username: str, configured: str) -> bool:
    allowed = {
        item.strip().casefold()
        for item in (configured or "").split(",")
        if item.strip()
    }
    value = (username or "").strip().casefold()
    return bool(value) and value in allowed
