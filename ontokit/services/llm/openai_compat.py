"""OpenAI-compatible provider — handles 9 of 13 supported providers.

Ported from folio-enrich/backend/app/services/llm/openai_compat.py with token-count
additions required for ontokit-api's audit log (input_tokens + output_tokens per call).

Covers: openai, mistral, meta_llama, ollama, lmstudio, custom, groq, xai, llamafile.
All use the OpenAI Python SDK with a parameterized base_url.
"""

from __future__ import annotations

import logging
from typing import Any

from ontokit.services.llm.base import LLMProvider

logger = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    """Unified provider for all OpenAI-compatible APIs.

    Uses the OpenAI Python SDK's AsyncOpenAI client with a configurable base_url,
    which allows a single implementation to cover nine different providers.
    """

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
            import openai

            kwargs: dict[str, Any] = {"api_key": self.api_key or "no-key"}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = openai.AsyncOpenAI(**kwargs)
        return self._client

    async def chat(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> tuple[str, int, int]:
        client = self._get_client()
        response = await client.chat.completions.create(
            model=kwargs.pop("model", self.model or "gpt-4o-mini"),
            messages=messages,
            **kwargs,
        )
        text = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        return text, input_tokens, output_tokens

    async def test_connection(self) -> bool:
        client = self._get_client()
        if self.model:
            response = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=1,
            )
            return bool(response.choices)
        else:
            await client.models.list()
            return True

    async def list_models(self) -> list[str]:
        try:
            client = self._get_client()
            response = await client.models.list()
            return sorted(m.id for m in response)
        except Exception:
            logger.debug("Failed to list models dynamically", exc_info=True)
            return []
