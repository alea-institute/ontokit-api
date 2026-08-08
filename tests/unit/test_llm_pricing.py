"""Fail-closed model pricing tests."""

from unittest.mock import AsyncMock

import pytest

from ontokit.services.llm import pricing


@pytest.fixture(autouse=True)
def reset_pricing_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, "_pricing_cache", None)
    monkeypatch.setattr(pricing, "_pricing_fetched_at", 0.0)
    monkeypatch.setattr(pricing, "_pricing_fetch_failed_at", 0.0)


@pytest.mark.asyncio
async def test_unknown_model_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, "_pricing_cache", {"known": (0.1, 0.2)})
    monkeypatch.setattr(pricing, "_pricing_fetched_at", pricing.time.time())

    with pytest.raises(pricing.PricingUnavailableError):
        await pricing.get_model_pricing("unknown-paid-model")


@pytest.mark.asyncio
async def test_fetch_failure_is_negative_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = AsyncMock(side_effect=lambda: setattr(pricing, "_pricing_fetch_failed_at", pricing.time.time()))
    monkeypatch.setattr(pricing, "_fetch_and_cache", fetch)

    with pytest.raises(pricing.PricingUnavailableError):
        await pricing.get_model_pricing("unknown")
    with pytest.raises(pricing.PricingUnavailableError):
        await pricing.get_model_pricing("unknown")

    fetch.assert_awaited_once()
