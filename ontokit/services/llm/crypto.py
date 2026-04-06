"""Fernet symmetric encryption helpers for LLM API key storage.

Uses the same key-derivation pattern as embedding_service.py — both derive from
settings.secret_key via SHA-256 so keys are consistent across services.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    """Derive a Fernet key from the application secret."""
    from ontokit.core.config import settings

    key = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string (e.g. an API key) using Fernet symmetric encryption."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted secret string."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
