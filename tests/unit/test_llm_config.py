"""Tests for LLM config crypto + schema secret-handling — LLM-01, LLM-02, LLM-03.

Focus: user/BYO provider API keys must never be exposed in responses and must
never be encrypted under the shipped default secret in production.
"""

import pytest

from ontokit.schemas.llm import LLMConfigResponse, LLMConfigUpdate
from ontokit.services.llm.crypto import (
    _INSECURE_DEFAULT_SECRET,
    decrypt_secret,
    encrypt_secret,
)


@pytest.fixture
def strong_secret(monkeypatch):
    """Point the crypto helpers at a strong, non-default secret."""
    from ontokit.core.config import settings

    monkeypatch.setattr(settings, "secret_key", "a" * 48)
    monkeypatch.setattr(settings, "app_env", "development")
    return settings


def test_crypto_round_trip(strong_secret):
    """encrypt_secret → decrypt_secret returns the original API key."""
    api_key = "sk-test-1234567890abcdef"
    ciphertext = encrypt_secret(api_key)

    assert ciphertext != api_key  # actually encrypted
    assert api_key not in ciphertext  # plaintext not embedded
    assert decrypt_secret(ciphertext) == api_key


def test_config_response_never_exposes_key():
    """LLMConfigResponse carries only api_key_set (bool) — never the raw key."""
    fields = set(LLMConfigResponse.model_fields)

    assert "api_key_set" in fields
    assert "api_key" not in fields  # the raw key field must not exist on the response
    assert LLMConfigResponse.model_fields["api_key_set"].annotation is bool


def test_config_update_key_is_write_only():
    """The raw api_key field lives only on the write model (LLMConfigUpdate)."""
    assert "api_key" in LLMConfigUpdate.model_fields
    assert "api_key" not in LLMConfigResponse.model_fields


@pytest.mark.parametrize("app_env", ["production", "staging"])
def test_insecure_default_secret_blocked_in_deployed_envs(monkeypatch, app_env):
    """Encrypting under the shipped default secret in ANY deployed env hard-fails.

    Otherwise user provider API keys would be encrypted under a publicly-known
    constant — equivalent to plaintext storage. Staging is deployed and shared,
    so it must be blocked too (not merely warned).
    """
    from ontokit.core.config import settings

    monkeypatch.setattr(settings, "secret_key", _INSECURE_DEFAULT_SECRET)
    monkeypatch.setattr(settings, "app_env", app_env)

    with pytest.raises(RuntimeError, match="insecure shipped default"):
        encrypt_secret("sk-should-not-encrypt")


def test_insecure_default_secret_allowed_in_development(monkeypatch):
    """The default secret is tolerated (with a warning) in local development."""
    from ontokit.core.config import settings

    monkeypatch.setattr(settings, "secret_key", _INSECURE_DEFAULT_SECRET)
    monkeypatch.setattr(settings, "app_env", "development")

    # Should not raise; round-trip still works under the (weak) dev secret.
    ciphertext = encrypt_secret("sk-dev-key")
    assert decrypt_secret(ciphertext) == "sk-dev-key"
