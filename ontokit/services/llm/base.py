"""Abstract LLM provider base class.

Ported from folio-enrich/backend/app/services/llm/base.py with token-count additions
needed for cost accounting (audit log records input_tokens + output_tokens per call).
"""

from __future__ import annotations

import abc
from typing import Any


class LLMProvider(abc.ABC):
    """Abstract base class for all LLM provider implementations.

    Every provider implementation must:
    - Accept api_key, base_url, and model in its constructor.
    - Return (text, input_tokens, output_tokens) from chat() for cost accounting.
    - Implement test_connection() for key validation.
    - Implement list_models() for the model picker UI.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    @abc.abstractmethod
    async def chat(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> tuple[str, int, int]:
        """Multi-turn chat completion.

        Returns:
            (text, input_tokens, output_tokens) — text is the response content,
            token counts are used for cost estimation and audit logging.
        """

    @abc.abstractmethod
    async def test_connection(self) -> bool:
        """Test connectivity to the provider.

        Returns True on success, raises on error.
        """

    @abc.abstractmethod
    async def list_models(self) -> list[str]:
        """Return a list of available model IDs from this provider."""
