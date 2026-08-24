"""LLM service package for ontokit-api.

Provides:
- get_provider(): factory for all 13 supported LLM providers
- LLMProvider: abstract base class
- encrypt_secret() / decrypt_secret() / rotate_secret(): Fernet helpers for API
  key storage, with MultiFernet key rotation
- get_model_pricing(): LiteLLM-backed token cost lookup
- validate_base_url() / resolve_and_validate() / secure_async_client(): SSRF
  protection for provider URLs (config-time + connect-time)
- check_rate_limit() / get_remaining_calls(): Redis-based daily rate limiting
- check_budget() / get_budget_status() / get_monthly_spend(): monthly budget enforcement
- check_llm_access(): per-role LLM access gate
- log_llm_call() / get_usage_summary(): audit log writer and usage aggregation
"""

from ontokit.services.llm.audit import get_usage_summary, log_llm_call
from ontokit.services.llm.base import LLMProvider
from ontokit.services.llm.budget import (
    BudgetLimits,
    check_budget,
    get_budget_status,
    get_monthly_spend,
)
from ontokit.services.llm.crypto import decrypt_secret, encrypt_secret, rotate_secret
from ontokit.services.llm.metering import LLMBudgetExceeded, MeteredLLMProvider
from ontokit.services.llm.pricing import PricingUnavailableError, get_model_pricing
from ontokit.services.llm.rate_limiter import (
    RateLimitReservation,
    check_rate_limit,
    consume_rate_limit_units,
    get_remaining_calls,
    release_rate_limit_units,
    reserve_rate_limit_units,
)
from ontokit.services.llm.registry import get_provider
from ontokit.services.llm.role_gates import check_llm_access
from ontokit.services.llm.ssrf import (
    resolve_and_validate,
    secure_async_client,
    validate_base_url,
)

__all__ = [
    "BudgetLimits",
    "LLMProvider",
    "LLMBudgetExceeded",
    "MeteredLLMProvider",
    "PricingUnavailableError",
    "RateLimitReservation",
    "check_budget",
    "check_llm_access",
    "check_rate_limit",
    "consume_rate_limit_units",
    "decrypt_secret",
    "encrypt_secret",
    "get_budget_status",
    "get_model_pricing",
    "get_monthly_spend",
    "get_provider",
    "get_remaining_calls",
    "get_usage_summary",
    "log_llm_call",
    "resolve_and_validate",
    "release_rate_limit_units",
    "reserve_rate_limit_units",
    "rotate_secret",
    "secure_async_client",
    "validate_base_url",
]
