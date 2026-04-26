"""Embedding client — Jina v5-small (or any OpenAI-compatible embedding API).

Ported from PodForge's embeddingService.ts. The base URL already includes
``/v1/embeddings`` by convention.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from typing import overload

import httpx

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    dimensions: int


def _default_config() -> EmbeddingConfig:
    return EmbeddingConfig(
        base_url=os.environ.get("EMBEDDING_API_URL", ""),
        api_key=os.environ.get("EMBEDDING_API_KEY", ""),
        model=os.environ.get("EMBEDDING_MODEL", ""),
        dimensions=int(os.environ.get("EMBEDDING_DIMENSIONS", "1024")),
    )


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig | None = None):
        self.config = config or _default_config()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def health_check(self) -> dict:
        start = asyncio.get_event_loop().time()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.config.base_url,
                    headers=self._headers(),
                    json={"input": ["test"], "model": self.config.model},
                )
                latency = int((asyncio.get_event_loop().time() - start) * 1000)
                if resp.status_code == 200:
                    return {"healthy": True, "latency": latency}
                return {"healthy": False, "latency": latency, "error": f"HTTP {resp.status_code}"}
        except Exception as exc:
            latency = int((asyncio.get_event_loop().time() - start) * 1000)
            return {"healthy": False, "latency": latency, "error": str(exc)}

    @overload
    async def embed(self, text: str) -> list[float]: ...
    @overload
    async def embed(self, text: list[str]) -> list[list[float]]: ...
    async def embed(self, text):  # type: ignore[no-untyped-def]
        single = isinstance(text, str)
        texts = [text] if single else list(text)

        payload = {"input": texts, "model": self.config.model}

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                    resp = await client.post(
                        self.config.base_url,
                        headers=self._headers(),
                        json=payload,
                    )
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"Embedding request failed: {resp.status_code} {resp.text}"
                        )
                    data = resp.json()
                    items = data.get("data") or []
                    if not items:
                        raise RuntimeError("No embeddings returned from API")

                    items_sorted = sorted(items, key=lambda x: x.get("index", 0))
                    embeddings = [item["embedding"] for item in items_sorted]

                    for emb in embeddings:
                        if len(emb) != self.config.dimensions:
                            logger.warning(
                                "Embedding dimension mismatch: expected %d, got %d",
                                self.config.dimensions,
                                len(emb),
                            )

                    return embeddings[0] if single else embeddings
            except Exception as exc:
                last_err = exc
                if attempt < 2:
                    await asyncio.sleep(1.0 * (2**attempt))
        assert last_err is not None
        raise last_err

    async def embed_single(self, text: str) -> list[float]:
        return await self.embed(text)

    async def embed_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[list[float]]:
        results: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch_embeddings = await self.embed(batch)
            results.extend(batch_embeddings)
        return results

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        if len(a) != len(b):
            raise ValueError("Embeddings must have the same length")

        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for x, y in zip(a, b, strict=True):
            dot += x * y
            norm_a += x * x
            norm_b += y * y

        denom = math.sqrt(norm_a) * math.sqrt(norm_b)
        if denom == 0:
            return 0.0
        return dot / denom

    def find_most_similar(
        self,
        query_embedding: list[float],
        chunk_embeddings: list[dict],
        top_k: int = 10,
    ) -> list[dict]:
        """Rank chunks by cosine similarity to the query.

        ``chunk_embeddings`` is a list of dicts with ``id`` and ``embedding`` keys.
        Returns the top-k as dicts with ``id`` and ``similarity`` keys.
        """
        scored = [
            {"id": item["id"], "similarity": self.cosine_similarity(query_embedding, item["embedding"])}
            for item in chunk_embeddings
        ]
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:top_k]


_instance: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    global _instance
    if _instance is None:
        _instance = EmbeddingClient()
    return _instance
