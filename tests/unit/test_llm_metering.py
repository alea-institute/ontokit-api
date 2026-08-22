"""Provider-boundary budget reservation for ordinary LLM generation."""

import asyncio
import logging
import uuid
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ontokit.services.llm.base import LLMProvider
from ontokit.services.llm.metering import LLMBudgetExceeded, MeteredLLMProvider


class _Provider(LLMProvider):
    def __init__(self, events: list[str], result: tuple[str, int, int] | BaseException) -> None:
        super().__init__(model="paid-model")
        self.events = events
        self.result = result
        self.chat_kwargs: dict[str, Any] = {}

    async def chat(self, _messages: list[dict[str, str]], **kwargs: Any) -> tuple[str, int, int]:
        self.events.append("provider")
        self.chat_kwargs = kwargs
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def test_connection(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["paid-model"]


class _SessionContext(AbstractAsyncContextManager[AsyncMock]):
    def __init__(self) -> None:
        self.session = AsyncMock()

    async def __aenter__(self) -> AsyncMock:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        return None


def _metered(provider: LLMProvider) -> MeteredLLMProvider:
    return MeteredLLMProvider(
        provider,
        project_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        config=SimpleNamespace(monthly_budget_usd=10.0, daily_cap_usd=5.0),
        user_id="user-1",
        model="paid-model",
        provider_name="openai",
        endpoint="llm/generate-suggestions",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
        is_byo_key=False,
    )


@pytest.mark.asyncio
async def test_reserves_before_provider_and_reconciles_actual_usage() -> None:
    events: list[str] = []
    provider = _Provider(events, ("ok", 12, 7))
    reservation_id = uuid.uuid4()

    async def reserve(*_args: Any, **kwargs: Any) -> tuple[uuid.UUID, None]:
        events.append("reserve")
        assert kwargs["input_tokens"] == 20
        assert kwargs["output_tokens"] == 4096
        assert kwargs["cost_estimate_usd"] == pytest.approx(82.12)
        return reservation_id, None

    with (
        patch("ontokit.core.database.async_session_maker", side_effect=_SessionContext),
        patch("ontokit.services.llm.metering.reserve_llm_call", side_effect=reserve),
        patch("ontokit.services.llm.metering.finalize_llm_call", new=AsyncMock()) as finalize,
    ):
        result = await _metered(provider).chat([{"role": "user", "content": "test"}])

    assert result == ("ok", 12, 7)
    assert events == ["reserve", "provider"]
    assert provider.chat_kwargs["max_tokens"] == 4096
    finalize.assert_awaited_once()
    assert finalize.await_args.kwargs == {
        "succeeded": True,
        "input_tokens": 12,
        "output_tokens": 7,
        "cost_estimate_usd": pytest.approx(0.26),
    }


@pytest.mark.asyncio
async def test_budget_refusal_never_calls_provider() -> None:
    events: list[str] = []
    provider = _Provider(events, ("should not run", 1, 1))

    with (
        patch("ontokit.core.database.async_session_maker", side_effect=_SessionContext),
        patch(
            "ontokit.services.llm.metering.reserve_llm_call",
            new=AsyncMock(return_value=(None, "daily_cap_reached")),
        ),
        pytest.raises(LLMBudgetExceeded, match="daily_cap_reached") as raised,
    ):
        await _metered(provider).chat([{"role": "user", "content": "test"}])

    assert raised.value.reason == "daily_cap_reached"
    assert events == []


@pytest.mark.asyncio
async def test_provider_failure_keeps_conservative_failed_receipt() -> None:
    events: list[str] = []
    provider = _Provider(events, TimeoutError("provider unavailable"))

    with (
        patch("ontokit.core.database.async_session_maker", side_effect=_SessionContext),
        patch(
            "ontokit.services.llm.metering.reserve_llm_call",
            new=AsyncMock(return_value=(uuid.uuid4(), None)),
        ),
        patch("ontokit.services.llm.metering.finalize_llm_call", new=AsyncMock()) as finalize,
        pytest.raises(TimeoutError, match="provider unavailable"),
    ):
        await _metered(provider).chat([{"role": "user", "content": "test"}])

    finalize.assert_awaited_once_with(
        finalize.await_args.args[0],
        finalize.await_args.args[1],
        "llm/generate-suggestions",
        succeeded=False,
    )


@pytest.mark.asyncio
async def test_cancellation_finalizes_failed_receipt() -> None:
    provider = _Provider([], asyncio.CancelledError())

    with (
        patch("ontokit.core.database.async_session_maker", side_effect=_SessionContext),
        patch(
            "ontokit.services.llm.metering.reserve_llm_call",
            new=AsyncMock(return_value=(uuid.uuid4(), None)),
        ),
        patch("ontokit.services.llm.metering.finalize_llm_call", new=AsyncMock()) as finalize,
        pytest.raises(asyncio.CancelledError),
    ):
        await _metered(provider).chat([{"role": "user", "content": "test"}])

    assert finalize.await_args.kwargs == {"succeeded": False}


@pytest.mark.asyncio
async def test_finalize_outage_does_not_fail_success_or_log_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "provider-secret-value"
    provider = _Provider([], ("ok", 1, 1))

    with (
        patch("ontokit.core.database.async_session_maker", side_effect=_SessionContext),
        patch(
            "ontokit.services.llm.metering.reserve_llm_call",
            new=AsyncMock(return_value=(uuid.uuid4(), None)),
        ),
        patch(
            "ontokit.services.llm.metering.finalize_llm_call",
            new=AsyncMock(side_effect=RuntimeError(secret)),
        ),
        caplog.at_level(logging.ERROR, logger="ontokit.services.llm.metering"),
    ):
        result = await _metered(provider).chat([{"role": "user", "content": "test"}])

    assert result == ("ok", 1, 1)
    assert secret not in caplog.text
    assert any(
        getattr(record, "event", None) == "llm_audit_finalize_failed" for record in caplog.records
    )
