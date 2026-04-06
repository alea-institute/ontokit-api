"""Cohere provider using direct REST API via httpx.

Ported from folio-enrich/backend/app/services/llm/cohere_provider.py with
token-count return values added for audit logging.

Uses httpx for REST calls to the Cohere v2 API. Token counts are extracted from
the response meta field when available, with word-count estimation as fallback.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ontokit.services.llm.base import LLMProvider

logger = logging.getLogger(__name__)


class CohereProvider(LLMProvider):
    """Cohere provider using the Cohere v2 REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, model=model)
        self._base = (base_url or "https://api.cohere.com/v2").rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key or ''}",
        }

    async def chat(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> tuple[str, int, int]:
        model = kwargs.pop("model", self.model or "command-a-03-2025")

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }

        url = f"{self._base}/chat"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
            resp.raise_for_status()
            data = resp.json()

        # Cohere v2 chat response structure
        message = data.get("message", {})
        content = message.get("content", [])
        text = content[0].get("text", "") if content else ""

        # Extract token counts from usage meta
        usage = data.get("meta", {}).get("tokens", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        # Fallback estimation
        if input_tokens == 0:
            input_tokens = int(
                sum(len(m.get("content", "").split()) for m in messages) * 1.3
            )
        if output_tokens == 0:
            output_tokens = int(len(text.split()) * 1.3)

        return text, input_tokens, output_tokens

    async def test_connection(self) -> bool:
        model = self.model or "command-a-03-2025"
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 1,
        }
        url = f"{self._base}/chat"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
            resp.raise_for_status()
        return True

    async def list_models(self) -> list[str]:
        try:
            url = f"{self._base}/models"
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
            models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            return sorted(models)
        except Exception:
            logger.debug("Failed to list Cohere models", exc_info=True)
            return []
