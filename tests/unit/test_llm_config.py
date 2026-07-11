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
    rotate_secret,
)


@pytest.fixture
def strong_secret(monkeypatch):
    """Point the crypto helpers at a strong, non-default secret."""
    from ontokit.core.config import settings

    monkeypatch.setattr(settings, "secret_key", "a" * 48)
    monkeypatch.setattr(settings, "app_env", "development")
    return settings


def test_crypto_round_trip(strong_secret):  # noqa: ARG001 (pytest fixture applied via side effects)
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


# --- MultiFernet key rotation (SECRET_KEY_PREVIOUS) ---


def test_ciphertext_from_previous_key_still_decrypts_after_rotation(monkeypatch):
    """Rotating SECRET_KEY keeps old ciphertext readable via SECRET_KEY_PREVIOUS."""
    from ontokit.core.config import settings

    old_secret = "old-" + "a" * 44
    new_secret = "new-" + "b" * 44

    # Encrypt under the OLD key (no rotation list yet).
    monkeypatch.setattr(settings, "secret_key", old_secret)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "secret_key_previous", "")
    ciphertext = encrypt_secret("sk-rotate-me")

    # Rotate: new current key, old key retained for decryption only.
    monkeypatch.setattr(settings, "secret_key", new_secret)
    monkeypatch.setattr(settings, "secret_key_previous", old_secret)

    # Old ciphertext still decrypts through the retired key.
    assert decrypt_secret(ciphertext) == "sk-rotate-me"


def test_decrypt_fails_when_previous_key_dropped(monkeypatch):
    """Once the retired key is removed, its ciphertext no longer decrypts."""
    from cryptography.fernet import InvalidToken

    from ontokit.core.config import settings

    old_secret = "old-" + "a" * 44
    new_secret = "new-" + "b" * 44

    monkeypatch.setattr(settings, "secret_key", old_secret)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "secret_key_previous", "")
    ciphertext = encrypt_secret("sk-rotate-me")

    # New key only, retired key dropped from the list.
    monkeypatch.setattr(settings, "secret_key", new_secret)
    monkeypatch.setattr(settings, "secret_key_previous", "")

    with pytest.raises(InvalidToken):
        decrypt_secret(ciphertext)


def test_rotate_secret_migrates_ciphertext_to_current_key(monkeypatch):
    """rotate_secret re-encrypts under the current key so the retired key can be retired."""
    from cryptography.fernet import InvalidToken

    from ontokit.core.config import settings

    old_secret = "old-" + "a" * 44
    new_secret = "new-" + "b" * 44

    monkeypatch.setattr(settings, "secret_key", old_secret)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "secret_key_previous", "")
    old_ciphertext = encrypt_secret("sk-migrate")

    # Rotate, then migrate the stored ciphertext onto the current key.
    monkeypatch.setattr(settings, "secret_key", new_secret)
    monkeypatch.setattr(settings, "secret_key_previous", old_secret)
    migrated = rotate_secret(old_ciphertext)

    # The migrated ciphertext decrypts with NO retired keys present.
    monkeypatch.setattr(settings, "secret_key_previous", "")
    assert decrypt_secret(migrated) == "sk-migrate"
    # And the original pre-rotation ciphertext no longer decrypts once retired.
    with pytest.raises(InvalidToken):
        decrypt_secret(old_ciphertext)


def test_shipped_default_never_trusted_as_rotation_key(monkeypatch):
    """The insecure default is stripped from SECRET_KEY_PREVIOUS, not used to decrypt."""
    from cryptography.fernet import InvalidToken

    from ontokit.core.config import settings

    # Ciphertext written under the shipped default (dev-tolerated).
    monkeypatch.setattr(settings, "secret_key", _INSECURE_DEFAULT_SECRET)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "secret_key_previous", "")
    legacy_ciphertext = encrypt_secret("sk-legacy")

    # Move to a strong key but (mistakenly) list the default as a previous key.
    monkeypatch.setattr(settings, "secret_key", "strong-" + "c" * 44)
    monkeypatch.setattr(settings, "secret_key_previous", _INSECURE_DEFAULT_SECRET)

    # The default is ignored, so the legacy ciphertext does NOT silently decrypt.
    with pytest.raises(InvalidToken):
        decrypt_secret(legacy_ciphertext)
