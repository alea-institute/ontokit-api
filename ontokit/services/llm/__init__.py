"""LLM service package for ontokit-api.

Provides:
- get_provider(): factory for all 13 supported LLM providers
- LLMProvider: abstract base class
- encrypt_secret() / decrypt_secret(): Fernet helpers for API key storage
- get_model_pricing(): LiteLLM-backed token cost lookup
- validate_base_url(): SSRF protection for custom/local provider URLs
- check_rate_limit() / get_remaining_calls(): Redis-based daily rate limiting
- check_budget() / get_budget_status() / get_monthly_spend(): monthly budget enforcement
- log_llm_call() / get_usage_summary(): audit log writer and aggregation
- check_llm_access(): per-role LLM access gate
"""

from ontokit.services.llm.audit import get_usage_summary, log_llm_call
from ontokit.services.llm.base import LLMProvider
from ontokit.services.llm.budget import check_budget, get_budget_status, get_monthly_spend
from ontokit.services.llm.crypto import decrypt_secret, encrypt_secret
from ontokit.services.llm.pricing import get_model_pricing
from ontokit.services.llm.rate_limiter import check_rate_limit, get_remaining_calls
from ontokit.services.llm.registry import get_provider
from ontokit.services.llm.role_gates import check_llm_access
from ontokit.services.llm.ssrf import validate_base_url

__all__ = [
    "LLMProvider",
    "check_budget",
    "check_llm_access",
    "check_rate_limit",
    "decrypt_secret",
    "encrypt_secret",
    "get_budget_status",
    "get_model_pricing",
    "get_monthly_spend",
    "get_provider",
    "get_remaining_calls",
    "get_usage_summary",
    "log_llm_call",
    "validate_base_url",
]
