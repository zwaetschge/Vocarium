"""Explicit user-owned relationships; linking never starts a production."""
from typing import Callable
import sqlite3
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

class EditionLink(BaseModel):
    book_id: str
    project_id: str

def create_library_router(*, get_current_user: Callable, db_getter: Callable[[], sqlite3.Connection]) -> APIRouter:
    router = APIRouter(prefix="/api/library", tags=["library"])

    def owned(db: sqlite3.Connection, link: EditionLink, uid: int) -> None:
        if not db.execute("SELECT id FROM ab_books WHERE id=? AND user_id=?", (link.book_id, uid)).fetchone():
            raise HTTPException(404, "Book not found")
        if not db.execute("SELECT id FROM hs_projects WHERE id=? AND user_id=?", (link.project_id, uid)).fetchone():
            raise HTTPException(404, "Audio drama not found")

    @router.get("/links")
    async def list_links(request: Request) -> dict:
        uid = get_current_user(request)["id"]
        rows = db_getter().execute(
            "SELECT l.book_id, l.project_id FROM library_links l "
            "JOIN ab_books b ON b.id=l.book_id AND b.user_id=l.user_id "
            "JOIN hs_projects p ON p.id=l.project_id AND p.user_id=l.user_id WHERE l.user_id=?", (uid,)
        ).fetchall()
        return {"links": [{"book_id": r[0], "project_id": r[1]} for r in rows]}

    @router.post("/links")
    async def link_editions(body: EditionLink, request: Request) -> dict:
        uid = get_current_user(request)["id"]
        db = db_getter()
        owned(db, body, uid)
        db.execute("INSERT OR IGNORE INTO library_links (user_id, book_id, project_id) VALUES (?,?,?)", (uid, body.book_id, body.project_id))
        db.commit()
        return body.model_dump()

    @router.delete("/links")
    async def unlink_editions(body: EditionLink, request: Request) -> dict:
        uid = get_current_user(request)["id"]
        db = db_getter()
        db.execute("DELETE FROM library_links WHERE user_id=? AND book_id=? AND project_id=?", (uid, body.book_id, body.project_id))
        db.commit()
        return {"ok": True}

    return router
