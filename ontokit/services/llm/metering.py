"""Atomic provider-boundary metering for ordinary LLM calls."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ontokit.services.llm.audit import finalize_llm_call, reserve_llm_call
from ontokit.services.llm.base import LLMProvider
from ontokit.services.llm.budget import BudgetConfig

logger = logging.getLogger(__name__)

MAX_GENERATION_OUTPUT_TOKENS = 4096
_FINALIZE_FAILURE_EVENT = "llm_audit_finalize_failed"
_MESSAGE_FRAMING_TOKEN_ALLOWANCE = 16


def _input_token_upper_bound(messages: list[dict[str, str]]) -> int:
    """Return a tokenizer-independent conservative input allowance.

    UTF-8 byte length bounds ordinary byte-backed tokenization; the per-message
    allowance covers provider role/framing tokens that are absent from content.
    """
    return sum(
        len(message.get("content", "").encode("utf-8")) + _MESSAGE_FRAMING_TOKEN_ALLOWANCE
        for message in messages
    )


class LLMBudgetExceeded(RuntimeError):
    """A provider call was refused by the atomic project budget gate."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class MeteredLLMProvider(LLMProvider):
    """Reserve projected spend immediately before delegating to a provider.

    The committed reservation serializes concurrent project calls and remains
    budget-visible if the process crashes. Successful calls reconcile the
    conservative output allowance to provider-reported token usage.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        project_id: uuid.UUID,
        config: BudgetConfig,
        user_id: str,
        model: str,
        provider_name: str,
        endpoint: str,
        input_cost_per_token: float,
        output_cost_per_token: float,
        is_byo_key: bool,
    ) -> None:
        super().__init__(
            api_key=provider.api_key,
            base_url=provider.base_url,
            model=provider.model,
        )
        self._provider = provider
        self._project_id = project_id
        self._config = config
        self._user_id = user_id
        self._model_name = model
        self._provider_name = provider_name
        self._endpoint = endpoint
        self._input_cost_per_token = input_cost_per_token
        self._output_cost_per_token = output_cost_per_token
        self._is_byo_key = is_byo_key
        self.supports_true_batch_api = provider.supports_true_batch_api

    async def _finalize(
        self,
        reservation_id: uuid.UUID,
        *,
        succeeded: bool,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_estimate_usd: float | None = None,
    ) -> None:
        from ontokit.core.database import async_session_maker

        try:
            async with async_session_maker() as audit_db:
                if succeeded:
                    await finalize_llm_call(
                        audit_db,
                        reservation_id,
                        self._endpoint,
                        succeeded=True,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_estimate_usd=cost_estimate_usd,
                    )
                else:
                    await finalize_llm_call(
                        audit_db,
                        reservation_id,
                        self._endpoint,
                        succeeded=False,
                    )
        except Exception as exc:
            # The committed reservation stays budget-visible. Log only stable
            # identifiers and the exception type; provider errors may contain keys.
            logger.error(
                "ALERT %s: project=%s provider=%s error_type=%s",
                _FINALIZE_FAILURE_EVENT,
                self._project_id,
                self._provider_name,
                type(exc).__name__,
                extra={
                    "event": _FINALIZE_FAILURE_EVENT,
                    "project_id": str(self._project_id),
                    "provider": self._provider_name,
                    "error_type": type(exc).__name__,
                },
            )

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> tuple[str, int, int]:
        requested_max = kwargs.get("max_tokens", MAX_GENERATION_OUTPUT_TOKENS)
        if isinstance(requested_max, bool) or not isinstance(requested_max, int):
            raise ValueError("max_tokens must be a positive integer")
        if requested_max <= 0:
            raise ValueError("max_tokens must be a positive integer")
        max_tokens = min(requested_max, MAX_GENERATION_OUTPUT_TOKENS)
        kwargs["max_tokens"] = max_tokens

        projected_input_tokens = _input_token_upper_bound(messages)
        projected_cost = (
            projected_input_tokens * self._input_cost_per_token
            + max_tokens * self._output_cost_per_token
        )

        from ontokit.core.database import async_session_maker

        async with async_session_maker() as reservation_db:
            reservation_id, reason = await reserve_llm_call(
                reservation_db,
                project_id=self._project_id,
                config=self._config,
                user_id=self._user_id,
                model=self._model_name,
                provider=self._provider_name,
                endpoint=self._endpoint,
                input_tokens=projected_input_tokens,
                output_tokens=max_tokens,
                cost_estimate_usd=projected_cost,
                is_byo_key=self._is_byo_key,
            )
        if reservation_id is None:
            raise LLMBudgetExceeded(reason or "budget_exhausted")

        try:
            result = await self._provider.chat(messages, **kwargs)
        except BaseException:
            await self._finalize(reservation_id, succeeded=False)
            raise

        _, input_tokens, output_tokens = result
        actual_cost = (
            input_tokens * self._input_cost_per_token + output_tokens * self._output_cost_per_token
        )
        await self._finalize(
            reservation_id,
            succeeded=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate_usd=actual_cost,
        )
        return result

    async def test_connection(self) -> bool:
        return await self._provider.test_connection()

    async def list_models(self) -> list[str]:
        return await self._provider.list_models()
