"""Fernet symmetric encryption helpers for LLM API key storage.

Uses the same key-derivation pattern as embedding_service.py — both derive from
settings.secret_key via SHA-256 so keys are consistent across services.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# The shipped default for settings.secret_key. Encrypting user-supplied provider
# API keys under this publicly-known constant is equivalent to storing them in
# plaintext, so we refuse it in production.
_INSECURE_DEFAULT_SECRET = "change-me-in-production"  # noqa: S105 (not a real secret)


def _get_fernet() -> Fernet:
    """Derive a Fernet key from the application secret.

    Guards against the shipped default secret: because this key encrypts
    user/BYO provider API keys, a publicly-known secret would make them
    trivially decryptable. Hard-fails in production; warns in development.
    """
    from ontokit.core.config import settings

    if settings.secret_key == _INSECURE_DEFAULT_SECRET:
        # Any deployed environment (production OR staging) is shared and
        # network-reachable and may hold real tenant keys — a log warning is
        # not a control there. Only local development is allowed to proceed.
        if not settings.is_development:
            raise RuntimeError(
                f"SECRET_KEY is the insecure shipped default in a deployed "
                f"environment (app_env={settings.app_env!r}). LLM provider API "
                "keys would be encrypted under a publicly-known constant "
                "(equivalent to plaintext). Set a strong SECRET_KEY."
            )
        logger.warning(
            "SECRET_KEY is the insecure shipped default; LLM API keys are "
            "encrypted under a publicly-known constant. Acceptable for local "
            "development only — never in a shared or deployed environment."
        )

    key = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string (e.g. an API key) using Fernet symmetric encryption."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted secret string."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
