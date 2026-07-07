"""LLM service package for ontokit-api.

Provides:
- get_provider(): factory for all 13 supported LLM providers
- LLMProvider: abstract base class
- encrypt_secret() / decrypt_secret(): Fernet helpers for API key storage
- get_model_pricing(): LiteLLM-backed token cost lookup
- validate_base_url(): SSRF protection for custom/local provider URLs
- log_llm_call() / get_usage_summary(): audit log writer and usage aggregation
"""

from ontokit.services.llm.audit import get_usage_summary, log_llm_call
from ontokit.services.llm.base import LLMProvider
from ontokit.services.llm.crypto import decrypt_secret, encrypt_secret
from ontokit.services.llm.pricing import get_model_pricing
from ontokit.services.llm.registry import get_provider
from ontokit.services.llm.ssrf import validate_base_url

__all__ = [
    "LLMProvider",
    "decrypt_secret",
    "encrypt_secret",
    "get_model_pricing",
    "get_provider",
    "get_usage_summary",
    "log_llm_call",
    "validate_base_url",
]
