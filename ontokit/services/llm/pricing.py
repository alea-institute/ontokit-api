"""Dynamic model pricing via LiteLLM's public pricing database.

Ported from folio-enrich/backend/app/services/llm/pricing.py with adaptations
for ontokit-api's async style and audit-log use case (input + output cost per token).

The LiteLLM pricing JSON is fetched on first use, cached in memory for 7 days,
and falls back to stale cache if the fetch fails.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# Module-level cache — maps model_id → (input_cost_per_token, output_cost_per_token)
_pricing_cache: dict[str, tuple[float, float]] | None = None
_pricing_fetched_at: float = 0.0


def _is_cache_valid() -> bool:
    return _pricing_cache is not None and (
        time.time() - _pricing_fetched_at
    ) < CACHE_TTL_SECONDS


async def _fetch_and_cache() -> None:
    """Fetch the LiteLLM pricing JSON and populate the module-level cache."""
    global _pricing_cache, _pricing_fetched_at

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(LITELLM_PRICING_URL)
            resp.raise_for_status()
            raw: dict = resp.json()
    except Exception:
        logger.warning("Failed to fetch LiteLLM pricing data; using stale cache", exc_info=True)
        return  # keep existing stale cache (or None on first attempt)

    prices: dict[str, tuple[float, float]] = {}
    for model_id, info in raw.items():
        if not isinstance(info, dict):
            continue
        input_cost = info.get("input_cost_per_token")
        output_cost = info.get("output_cost_per_token")
        if input_cost is None or output_cost is None:
            continue
        try:
            entry = (float(input_cost), float(output_cost))
        except (ValueError, TypeError):
            continue
        prices[model_id] = entry
        # Also store without provider prefix (e.g. "openai/gpt-4o" → "gpt-4o")
        if "/" in model_id:
            short = model_id.split("/", 1)[1]
            if short not in prices:
                prices[short] = entry

    _pricing_cache = prices
    _pricing_fetched_at = time.time()
    logger.info("Loaded LiteLLM pricing for %d models", len(prices))


async def get_model_pricing(model: str) -> tuple[float, float]:
    """Return (input_cost_per_token, output_cost_per_token) for the given model ID.

    If pricing is not found, returns (0.0, 0.0) — cost will be recorded as zero
    rather than failing the LLM call.
    """
    if not _is_cache_valid():
        await _fetch_and_cache()

    if _pricing_cache:
        # Exact match first
        if model in _pricing_cache:
            return _pricing_cache[model]
        # Try with provider prefix stripped
        if "/" in model:
            short = model.split("/", 1)[1]
            if short in _pricing_cache:
                return _pricing_cache[short]

    logger.debug("No pricing data found for model %r; using 0.0", model)
    return (0.0, 0.0)


def get_pricing_cache_age() -> datetime | None:
    """Return the UTC datetime when pricing was last fetched, or None."""
    if _pricing_fetched_at == 0.0:
        return None
    return datetime.fromtimestamp(_pricing_fetched_at, tz=timezone.utc)
