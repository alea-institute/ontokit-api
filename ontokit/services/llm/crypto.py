"""Fernet symmetric encryption helpers for LLM API key storage.

Uses the same key-derivation pattern as embedding_service.py — both derive from
settings.secret_key via SHA-256 so keys are consistent across services.

Key rotation is supported via ``MultiFernet``: the *current* ``SECRET_KEY``
always encrypts, while any retired keys listed in ``SECRET_KEY_PREVIOUS`` remain
valid for *decryption* only. This gives zero-downtime rotation — set a new
``SECRET_KEY``, move the old one into ``SECRET_KEY_PREVIOUS``, and existing
ciphertext keeps decrypting. Use :func:`rotate_secret` to migrate stored
ciphertext onto the current key, then drop the retired key from the list.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, MultiFernet

logger = logging.getLogger(__name__)

# The shipped default for settings.secret_key. Encrypting user-supplied provider
# API keys under this publicly-known constant is equivalent to storing them in
# plaintext, so we refuse it in production.
_INSECURE_DEFAULT_SECRET = "change-me-in-production"  # noqa: S105 (not a real secret)


def _derive_fernet(secret: str) -> Fernet:
    """Derive a Fernet key from an application secret via SHA-256."""
    key = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _previous_secrets() -> list[str]:
    """Parse the comma-separated retired-secret list (decryption-only keys)."""
    from ontokit.core.config import settings

    raw = settings.secret_key_previous or ""
    # Ignore the shipped default even if it leaks into the rotation list —
    # a publicly-known key must never be trusted to authenticate ciphertext.
    return [
        s.strip() for s in raw.split(",") if s.strip() and s.strip() != _INSECURE_DEFAULT_SECRET
    ]


def _get_fernet() -> MultiFernet:
    """Build a MultiFernet: current SECRET_KEY encrypts; retired keys decrypt.

    Guards against the shipped default secret: because this key encrypts
    user/BYO provider API keys, a publicly-known secret would make them
    trivially decryptable. Hard-fails in production; warns in development.

    The current key is always first, so ``encrypt``/``rotate`` use it while
    ``decrypt`` transparently falls back to any retired key.
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

    # Current key first (used for encryption), retired keys after (decrypt-only).
    keys = [_derive_fernet(settings.secret_key), *(_derive_fernet(s) for s in _previous_secrets())]
    return MultiFernet(keys)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string (e.g. an API key) using Fernet symmetric encryption.

    Encryption always uses the current SECRET_KEY (the first MultiFernet key).
    """
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted secret string.

    Tries the current SECRET_KEY first, then any retired keys in
    ``SECRET_KEY_PREVIOUS`` — so ciphertext written before a rotation still
    decrypts.
    """
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def rotate_secret(ciphertext: str) -> str:
    """Re-encrypt ciphertext under the current SECRET_KEY.

    ``MultiFernet.rotate`` decrypts with whichever key still validates (current
    or retired) and re-encrypts under the current key, refreshing the timestamp.
    Run stored provider-key ciphertext through this after a rotation to migrate
    it off a retired key, then drop that key from ``SECRET_KEY_PREVIOUS``.
    """
    return _get_fernet().rotate(ciphertext.encode()).decode()
