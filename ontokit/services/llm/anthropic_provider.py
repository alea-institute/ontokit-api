"""Anthropic Claude provider.

Ported from folio-enrich/backend/app/services/llm/anthropic_provider.py with
token-count return values added for audit logging.

Anthropic requires the system message to be passed as a separate `system` parameter
(not in the messages array). This provider handles that separation automatically.
"""

from __future__ import annotations

import logging
from typing import Any

from ontokit.services.llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Fallback model list used when the API is unreachable
_FALLBACK_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
]


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider using the official anthropic SDK."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, model=model)
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def chat(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> tuple[str, int, int]:
        client = self._get_client()
        max_tokens = kwargs.pop("max_tokens", 4096)

        # Anthropic requires system message as a separate param, not in messages array
        system_msg: str | None = None
        chat_messages: list[dict[str, str]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_msg = msg.get("content", "")
            else:
                chat_messages.append(msg)

        create_kwargs: dict[str, Any] = {
            "model": kwargs.pop("model", self.model or "claude-sonnet-4-6"),
            "max_tokens": max_tokens,
            "messages": chat_messages or [{"role": "user", "content": ""}],
        }
        if system_msg:
            create_kwargs["system"] = system_msg
        create_kwargs.update(kwargs)

        response = await client.messages.create(**create_kwargs)
        text = response.content[0].text if response.content else ""
        input_tokens = response.usage.input_tokens if response.usage else 0
        output_tokens = response.usage.output_tokens if response.usage else 0
        return text, input_tokens, output_tokens

    async def test_connection(self) -> bool:
        client = self._get_client()
        response = await client.messages.create(
            model=self.model or "claude-sonnet-4-6",
            max_tokens=1,
            messages=[{"role": "user", "content": "Hi"}],
        )
        return bool(response.content)

    async def list_models(self) -> list[str]:
        try:
            client = self._get_client()
            response = await client.models.list(limit=100)
            models = [m.id for m in response.data]
            return sorted(models) if models else _FALLBACK_MODELS
        except Exception:
            logger.debug("Failed to list Anthropic models; using fallback", exc_info=True)
            return _FALLBACK_MODELS
