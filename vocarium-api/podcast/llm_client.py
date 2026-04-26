"""LLM client — configurable per-user OpenAI-compatible endpoint.

Supports completion, streaming, and JSON extraction with the same 4-strategy
fallback (whole response, markdown code block, first ``{...}``, first ``[...]``).

User-specific configuration is resolved via ``database.get_active_llm_provider``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int


@dataclass
class LLMMessage:
    role: str  # 'system' | 'user' | 'assistant'
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


def _default_config() -> LLMConfig:
    return LLMConfig(
        base_url=os.environ.get("LLM_API_URL", ""),
        api_key=os.environ.get("LLM_API_KEY", ""),
        model=os.environ.get("LLM_MODEL", ""),
        temperature=float(os.environ.get("LLM_TEMPERATURE", "0.8")),
        max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "16384")),
    )


def _config_from_provider(provider: dict | None) -> LLMConfig:
    """Build LLMConfig from a provider row dict (from DB) or fall back to env."""
    if not provider:
        return _default_config()
    return LLMConfig(
        base_url=provider.get("base_url", ""),
        api_key=provider.get("api_key", ""),
        model=provider.get("model", "default"),
        temperature=provider.get("temperature", 0.8),
        max_tokens=provider.get("max_tokens", 16384),
    )


_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```")
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


def extract_json(response: str) -> Any:
    """Extract JSON from an LLM response using 4 fallback strategies."""
    # 1. Whole response
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # 2. Markdown code block
    m = _CODE_BLOCK_RE.search(response)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. First JSON object
    m = _OBJECT_RE.search(response)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # 4. First JSON array
    m = _ARRAY_RE.search(response)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError("Could not extract valid JSON from LLM response")


class LLMClient:
    def __init__(self, config: LLMConfig | None = None):
        self.config = config or _default_config()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def health_check(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.config.base_url}/models", headers=self._headers()
                )
                if resp.status_code == 200:
                    return {"healthy": True}
                return {"healthy": False, "error": f"HTTP {resp.status_code}"}
            except Exception as exc:
                return {"healthy": False, "error": str(exc)}

    async def complete(
        self,
        messages: list[LLMMessage] | list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload_messages = [
            m.to_dict() if isinstance(m, LLMMessage) else m for m in messages
        ]
        payload = {
            "model": model or self.config.model,
            "messages": payload_messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
            "stream": False,
        }

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                    resp = await client.post(
                        f"{self.config.base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"LLM request failed: {resp.status_code} {resp.text}"
                        )
                    data = resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise RuntimeError("No choices returned from LLM")

                    message = choices[0].get("message", {})
                    content = message.get("content") or ""
                    if not content and message.get("reasoning_content"):
                        logger.warning(
                            "LLM returned empty content with reasoning_content — model in thinking mode"
                        )
                        return cast(str, message["reasoning_content"])
                    return cast(str, content)
            except Exception as exc:
                last_err = exc
                if attempt < 2:
                    await asyncio.sleep(1.0 * (2**attempt))
        assert last_err is not None
        raise last_err

    async def stream(
        self,
        messages: list[LLMMessage] | list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        payload_messages = [
            m.to_dict() if isinstance(m, LLMMessage) else m for m in messages
        ]
        payload = {
            "model": model or self.config.model,
            "messages": payload_messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            async with client.stream(
                "POST",
                f"{self.config.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"LLM stream request failed: {resp.status_code} {body.decode('utf-8', 'replace')}"
                    )

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    trimmed = line.strip()
                    if trimmed == "data: [DONE]":
                        break
                    if not trimmed.startswith("data: "):
                        continue
                    try:
                        data = json.loads(trimmed[6:])
                    except json.JSONDecodeError:
                        logger.debug("Failed to parse SSE chunk: %s", trimmed)
                        continue

                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield content

    async def complete_json(
        self,
        messages: list[LLMMessage] | list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 3,
    ) -> Any:
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = await self.complete(
                    messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return extract_json(response)
            except Exception as exc:
                last_err = exc
                logger.debug(
                    "Failed to extract JSON, attempt %d/%d: %s", attempt, retries, exc
                )
        assert last_err is not None
        raise last_err

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return (len(text) + 3) // 4


_instance: LLMClient | None = None


def get_llm_client(user_id: int | None = None) -> LLMClient:
    """Return an LLMClient.
    
    If ``user_id`` is given, looks up the user's active LLM provider from the
    database and configures the client accordingly. Otherwise falls back to
    environment defaults (global singleton).
    """
    if user_id is None:
        global _instance
        if _instance is None:
            _instance = LLMClient()
        return _instance
    # Per-user client: resolve provider from DB
    try:
        from database import get_active_llm_provider
        provider = get_active_llm_provider(user_id)
    except Exception:
        provider = None
    return LLMClient(config=_config_from_provider(provider))
