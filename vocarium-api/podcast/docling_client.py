"""Docling client — document parsing via the Docling API.

Uses ``/v1/convert/file`` (multipart) for file uploads and
``/v1/convert/source`` (JSON) for URLs. Falls back to reading plain text
directly when Docling is unavailable.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from metrics import inc, observe

logger = logging.getLogger(__name__)
_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            timeout=None,
        )
    return _http_client


async def close_docling_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


_FORMAT_MAP = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "doc",
    ".pptx": "pptx",
    ".ppt": "ppt",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".odt": "odt",
    ".rtf": "rtf",
    ".txt": "md",
    ".md": "md",
    ".html": "html",
    ".htm": "html",
    ".epub": "epub",
    ".xml": "xml_jats",
}


_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".epub": "application/epub+zip",
    ".xml": "application/xml",
}

SUPPORTED_EXTENSIONS = tuple(_FORMAT_MAP.keys())


@dataclass
class DoclingConfig:
    base_url: str


def _default_config() -> DoclingConfig:
    return DoclingConfig(
        base_url=os.environ.get("DOCLING_API_URL", ""),
    )


def _extract_text(result: dict) -> str | None:
    text = result.get("text") or result.get("content")
    if not text:
        doc = result.get("document") or {}
        text = (
            doc.get("md_content")
            or doc.get("text_content")
            or doc.get("content")
        )
    return text or None


def _extension(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx >= 0 else ""


_PLAIN_TEXT_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".xml", ".rtf"}

# Ohne laufenden Docling-Dienst muss ein Upload trotzdem funktionieren. Der
# Hoerbuch-Bereich parst PDF/EPUB/DOCX bereits lokal (pypdf + zipfile), also
# wird derselbe Parser hier wiederverwendet, statt einen Dienst zu verlangen,
# den dieser Stack gar nicht mitbringt.
_LOCAL_FORMATS = {".pdf": "pdf", ".epub": "epub", ".docx": "docx"}


def _parse_locally(path: Path) -> str | None:
    """Text aus einem Binaerdokument ohne Docling. None, wenn nicht moeglich."""
    fmt = _LOCAL_FORMATS.get(_extension(path.name))
    if not fmt:
        return None
    try:
        from audiobooks.text_pipeline import parse_document

        document = parse_document(path.read_bytes(), fmt)
    except Exception as exc:
        logger.warning("Local parse of %s failed: %s", path.name, exc)
        return None
    parts: list[str] = []
    for chapter in document.chapters:
        body = (chapter.content or "").strip()
        if not body:
            continue
        title = (chapter.title or "").strip()
        parts.append(f"{title}\n\n{body}" if title else body)
    text = "\n\n".join(parts).strip()
    return text or None


async def _fetch_url_text(url: str) -> str:
    """Sichtbaren Text einer URL holen. Wirft, wenn nichts Brauchbares kommt."""
    from audiobooks.text_pipeline import _strip_html

    resp = await _client().get(
        url, timeout=httpx.Timeout(60.0), follow_redirects=True
    )
    resp.raise_for_status()
    body = resp.text
    content_type = resp.headers.get("content-type", "")
    text = _strip_html(body) if "html" in content_type or "<" in body[:512] else body
    text = text.strip()
    if not text:
        raise RuntimeError(f"Keine lesbaren Inhalte unter {url}")
    return text


def _read_text_fallback(path: Path) -> str | None:
    """Text ohne Docling: erst als Klartext lesen, sonst lokal parsen."""
    if _extension(path.name) not in _PLAIN_TEXT_EXTENSIONS:
        return _parse_locally(path)
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            content = path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        if content.strip():
            return content
    return None


class DoclingClient:
    def __init__(self, config: DoclingConfig | None = None):
        self.config = config or _default_config()

    async def health_check(self) -> dict:
        try:
            resp = await _client().get(f"{self.config.base_url}/health", timeout=10.0)
            # 404 means endpoint doesn't exist but service might still be up
            if resp.status_code in (200, 404):
                return {"healthy": True}
            return {"healthy": False, "error": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}

    async def parse_file(self, file_path: str | Path) -> dict:
        path = Path(file_path)
        start = time.perf_counter()
        status = "error"
        try:
            # Try common extensions if the file is missing
            if not path.exists():
                for ext in (".md", ".txt", ".pdf"):
                    candidate = Path(str(path) + ext)
                    if candidate.exists():
                        path = candidate
                        break
                else:
                    raise FileNotFoundError(f"File not found: {file_path}")

            ext = _extension(path.name)
            if not ext:
                ext = ".md"

            from_format = _FORMAT_MAP.get(ext)
            mime = _MIME_TYPES.get(ext, "application/octet-stream")

            if not self.config.base_url:
                content = _read_text_fallback(path)
                if content is not None:
                    status = "success"
                    return {"status": "success", "text": content}
                raise RuntimeError(
                    f"Kein Text aus {path.name} lesbar ({ext or 'ohne Endung'}); "
                    "DOCLING_API_URL ist nicht gesetzt, lokal werden nur "
                    "PDF, EPUB, DOCX und Textdateien unterstuetzt"
                )

            data: dict[str, str] = {"to_formats": "md"}
            if from_format:
                data["from_formats"] = from_format

            with path.open("rb") as f:
                files = {"files": (path.name, f.read(), mime)}

            try:
                resp = await _client().post(
                    f"{self.config.base_url}/v1/convert/file",
                    data=data,
                    files=files,
                    timeout=httpx.Timeout(300.0),
                )
            except Exception as exc:
                content = _read_text_fallback(path)
                if content is not None:
                    logger.warning("Docling unavailable, using text fallback: %s", exc)
                    status = "success"
                    return {"status": "success", "text": content}
                raise

            if resp.status_code != 200:
                logger.warning(
                    "Docling parse failed (status=%s), falling back to raw read",
                    resp.status_code,
                )
                content = _read_text_fallback(path)
                if content is not None:
                    status = "success"
                    return {"status": "success", "text": content}
                raise RuntimeError(f"Docling parse failed: {resp.status_code} {resp.text}")

            result = resp.json()
            text = _extract_text(result)

            if result.get("status") == "error":
                raise RuntimeError(result.get("error") or "Docling parse failed")

            if text:
                status = "success"
                return {"status": "success", "text": text}

            # Fallback: read file directly
            content = _read_text_fallback(path)
            if content is not None:
                status = "success"
                return {"status": "success", "text": content}
            raise RuntimeError("No text content returned from Docling")

            status = "success"
            return {"status": "success", "text": ""}
        except Exception as exc:
            logger.error("Docling file parse error: %s (path=%s)", exc, file_path)
            return {"status": "error", "error": str(exc)}
        finally:
            labels = {"kind": "file", "status": status}
            inc("vocarium_docling_requests_total", labels=labels)
            observe("vocarium_docling_seconds", time.perf_counter() - start, labels)

    async def parse_url(self, url: str, format_: str = "text") -> dict:
        start = time.perf_counter()
        status = "error"
        try:
            if not self.config.base_url:
                # Ohne Docling die Seite selbst holen und das Markup strippen —
                # eine URL-Quelle soll nicht daran scheitern, dass ein
                # optionaler Dienst fehlt.
                text = await _fetch_url_text(url)
                status = "success"
                return {"status": "success", "text": text}
            resp = await _client().post(
                f"{self.config.base_url}/v1/convert/source",
                json={"url": url, "format": format_},
                headers={"Content-Type": "application/json"},
                timeout=httpx.Timeout(300.0),
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Docling URL parse failed: {resp.status_code} {resp.text}")

            result = resp.json()
            text = _extract_text(result)

            if result.get("status") == "error":
                raise RuntimeError(result.get("error") or "Docling parse failed")
            if not text:
                raise RuntimeError("No text content returned from Docling")

            status = "success"
            return {"status": "success", "text": text}
        except Exception as exc:
            logger.error("Docling URL parse error: %s (url=%s)", exc, url)
            return {"status": "error", "error": str(exc)}
        finally:
            labels = {"kind": "url", "status": status}
            inc("vocarium_docling_requests_total", labels=labels)
            observe("vocarium_docling_seconds", time.perf_counter() - start, labels)

    async def parse_text(self, content: str, filename: str | None = None) -> dict:
        """Plain text passthrough. Docling's text endpoint is optional; fall back
        to returning the content as-is, which is the only behaviour we actually
        rely on in production."""
        return {
            "status": "success",
            "text": content,
            "title": filename or "Text Input",
        }

    async def parse_document(
        self,
        source: str,
        kind: Literal["file", "url", "text"],
        content: str | None = None,
    ) -> dict:
        if kind == "file":
            return await self.parse_file(source)
        if kind == "url":
            return await self.parse_url(source)
        if kind == "text" and content is not None:
            return await self.parse_text(content, source)
        return {"status": "error", "error": "Invalid source type"}

    @staticmethod
    def supported_extensions() -> tuple[str, ...]:
        return SUPPORTED_EXTENSIONS

    @staticmethod
    def is_supported(filename: str) -> bool:
        return _extension(filename) in _FORMAT_MAP

    @staticmethod
    def mime_type(filename: str) -> str:
        return _MIME_TYPES.get(_extension(filename), "application/octet-stream")


_instance: DoclingClient | None = None


def get_docling_client() -> DoclingClient:
    global _instance
    if _instance is None:
        _instance = DoclingClient()
    return _instance
