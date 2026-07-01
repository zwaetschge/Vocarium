"""Per-request context shared across API helpers.

FastAPI dependency wiring does not reach every low-level helper, especially
queue submission paths. Context variables keep request/user correlation
available without coupling those helpers back to ``main.py``.
"""

from __future__ import annotations

from contextvars import ContextVar


request_id_var: ContextVar[str | None] = ContextVar(
    "vocarium_request_id",
    default=None,
)
user_id_var: ContextVar[int | None] = ContextVar(
    "vocarium_user_id",
    default=None,
)


def current_request_id() -> str | None:
    return request_id_var.get()


def current_user_id() -> int | None:
    return user_id_var.get()
