"""Google Gemini provider using direct REST API via httpx.

Ported from folio-enrich/backend/app/services/llm/google_provider.py with
token-count return values added for audit logging.

Uses httpx for REST calls rather than the google-generativeai SDK — the SDK is
deprecated (as of 2025-07) and the REST API is more stable across model generations.
Token counts are extracted from response.usageMetadata when available.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ontokit.services.llm.base import LLMProvider

logger = logging.getLogger(__name__)


class GoogleProvider(LLMProvider):
    """Google Gemini provider using the Generative Language REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, model=model)
        self._base = (
            base_url or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key or "",
        }

    async def _post_with_retry(
        self, url: str, body: dict[str, Any], max_retries: int = 5
    ) -> dict[str, Any]:
        """POST with exponential back-off on 429 / 503."""
        timeout = httpx.Timeout(60.0, connect=10.0)
        for attempt in range(max_retries + 1):
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
                if resp.status_code in (429, 503) and attempt < max_retries:
                    delay = 2**attempt
                    logger.warning(
                        "Google API %d, retrying in %ds (attempt %d/%d)",
                        resp.status_code,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
        return {}

    async def chat(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> tuple[str, int, int]:
        model = kwargs.pop("model", self.model or "gemini-2.0-flash")

        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            text = msg.get("content", "")
            if role == "system":
                system_parts.append(text)
            else:
                gemini_role = "model" if role == "assistant" else "user"
                contents.append({"role": gemini_role, "parts": [{"text": text}]})

        body: dict[str, Any] = {"contents": contents}
        if system_parts:
            body["system_instruction"] = {
                "parts": [{"text": "\n".join(system_parts)}]
            }

        url = f"{self._base}/models/{model}:generateContent"
        data = await self._post_with_retry(url, body)

        # Extract response text
        candidates = data.get("candidates", [])
        response_text = ""
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            response_text = parts[0].get("text", "") if parts else ""

        # Extract token counts from usageMetadata
        usage = data.get("usageMetadata", {})
        input_tokens = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
        # Fallback estimation if API doesn't return counts
        if input_tokens == 0:
            input_tokens = int(
                sum(len(m.get("content", "").split()) for m in messages) * 1.3
            )
        if output_tokens == 0:
            output_tokens = int(len(response_text.split()) * 1.3)

        return response_text, input_tokens, output_tokens

    async def test_connection(self) -> bool:
        model = self.model or "gemini-2.0-flash"
        url = f"{self._base}/models/{model}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
            "generationConfig": {"maxOutputTokens": 1},
        }
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
            models = []
            for m in data.get("models", []):
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                model_id = m.get("name", "").replace("models/", "")
                if model_id:
                    models.append(model_id)
            return sorted(models)
        except Exception:
            logger.debug("Failed to list Google models", exc_info=True)
            return []
