"""Human-verification challenge for an untrusted contributor's first suggestion (R10).

A provider protocol with a no-op default (KTD10), so a deployment that has not
configured a challenge is not broken by this feature — it simply has no
challenge, which is exactly the status quo before the trust ladder.

No new dependency: the Turnstile provider dials Cloudflare over the existing
SSRF-hardened httpx client.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ontokit.core.config import settings
from ontokit.services.llm.ssrf import secure_async_client

logger = logging.getLogger(__name__)

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class VerificationProvider(Protocol):
    """Verifies a human-challenge token supplied by the client."""

    @property
    def enabled(self) -> bool:
        """Whether this provider actually challenges anyone."""
        ...

    async def verify(self, token: str | None, client_ip: str | None = None) -> bool:
        """True when the challenge passed."""
        ...


class NullVerificationProvider:
    """Always passes. The default, so no environment is broken by R10."""

    @property
    def enabled(self) -> bool:
        return False

    async def verify(self, token: str | None, client_ip: str | None = None) -> bool:  # noqa: ARG002
        return True


class TurnstileVerificationProvider:
    """Cloudflare Turnstile, verified server-side.

    Any failure — missing token, non-200, malformed body, network error — is a
    DENIAL. A verification provider that passes when it cannot reach its backend
    is not a verification provider.
    """

    def __init__(self, secret_key: str) -> None:
        self._secret_key = secret_key

    @property
    def enabled(self) -> bool:
        return bool(self._secret_key)

    async def verify(self, token: str | None, client_ip: str | None = None) -> bool:
        if not token:
            return False
        payload = {"secret": self._secret_key, "response": token}
        if client_ip:
            payload["remoteip"] = client_ip

        try:
            async with secure_async_client(timeout=10) as client:
                response = await client.post(TURNSTILE_VERIFY_URL, data=payload)
        except Exception as e:
            logger.warning("Turnstile verification failed to reach the backend: %r", e)
            return False

        if response.status_code != 200:
            logger.warning("Turnstile verification returned HTTP %s", response.status_code)
            return False

        try:
            body = response.json()
        except Exception:
            logger.warning("Turnstile verification returned a non-JSON body")
            return False

        if not body.get("success"):
            logger.info("Turnstile challenge failed: %s", body.get("error-codes"))
            return False
        return True


def get_verification_provider() -> VerificationProvider:
    """Build the configured provider.

    Falls back to the null provider — and says so — when Turnstile is selected
    without a secret, rather than blocking every first-time contributor on a
    half-configured deployment.
    """
    if settings.verification_provider == "turnstile":
        if not settings.turnstile_secret_key:
            logger.warning(
                "VERIFICATION_PROVIDER=turnstile but TURNSTILE_SECRET_KEY is unset — "
                "human verification is DISABLED"
            )
            return NullVerificationProvider()
        return TurnstileVerificationProvider(settings.turnstile_secret_key)
    return NullVerificationProvider()
