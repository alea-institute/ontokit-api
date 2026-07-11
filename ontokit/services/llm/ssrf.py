"""SSRF protection for LLM provider base URLs.

Ported from folio-enrich/backend/app/services/llm/url_validator.py with adaptations
for ontokit-api's LLMProviderType location and env-var naming.

Guards against:
- Private/reserved IP ranges being used as cloud provider endpoints
- Non-HTTP(S) schemes (file://, ftp://, etc.)
- AWS/GCP/Azure metadata endpoint (169.254.169.254)

Local providers (ollama, lmstudio, custom, llamafile) are exempt from IP checks
because they are intentionally self-hosted; HTTP is allowed for them.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Allow overriding for development / testing environments
_ALLOW_PRIVATE = os.environ.get("ONTOKIT_ALLOW_PRIVATE_URLS", "").lower() in (
    "1",
    "true",
    "yes",
)

# Providers that run locally — HTTP allowed, private IPs allowed
_LOCAL_PROVIDER_VALUES = {"ollama", "lmstudio", "custom", "llamafile"}

# Cloud metadata endpoints — always blocked, even for local providers.
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure IMDS (IPv4)
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS (IPv6)
    }
)


def _normalize_ip(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an address, unwrapping IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254)."""
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_metadata_ip(addr: str) -> bool:
    try:
        return _normalize_ip(addr) in _METADATA_IPS
    except ValueError:
        return False


def _is_private_ip(addr: str) -> bool:
    try:
        ip = _normalize_ip(addr)
        return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local
    except ValueError:
        return False


def validate_base_url(url: str, allow_private: bool = False) -> str:
    """Validate a base URL for SSRF safety.

    Args:
        url: The URL to validate.
        allow_private: If True, skip private-IP checks (useful for local providers
            passed explicitly; also overridden by the ONTOKIT_ALLOW_PRIVATE_URLS env var).

    Returns:
        The validated URL string (unchanged).

    Raises:
        ValueError: If the URL fails any safety check.
    """
    parsed = urlparse(url)

    if not parsed.scheme:
        raise ValueError(f"URL must include a scheme (http:// or https://): {url!r}")

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Only http:// and https:// are allowed. Got scheme: {parsed.scheme!r}"
        )

    if not parsed.hostname:
        raise ValueError(f"URL must include a hostname: {url!r}")

    # Always block the cloud metadata endpoint regardless of provider type
    try:
        results = socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
        for _family, _type, _proto, _canonname, sockaddr in results:
            addr = str(sockaddr[0])
            if _is_metadata_ip(addr):
                raise ValueError(
                    f"URL resolves to a cloud metadata endpoint ({addr}), "
                    "which is blocked for security."
                )
    except socket.gaierror:
        # For local/custom providers with hostnames that may not resolve in CI, skip
        pass
    except ValueError:
        # Re-raise ValueError from the metadata check above
        raise

    # Skip further IP checks if explicitly allowed
    if allow_private or _ALLOW_PRIVATE:
        return url

    # Cloud providers require HTTPS
    if parsed.scheme != "https":
        raise ValueError(
            f"Cloud provider endpoints require HTTPS. Got: {parsed.scheme!r}. "
            "Use a local provider type for HTTP endpoints."
        )

    # Resolve and check for private IPs
    try:
        results = socket.getaddrinfo(
            parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP
        )
        for _family, _type, _proto, _canonname, sockaddr in results:
            addr = str(sockaddr[0])
            if _is_private_ip(addr):
                raise ValueError(
                    f"Cloud provider URL resolves to a private IP address ({addr}). "
                    "Set ONTOKIT_ALLOW_PRIVATE_URLS=true to allow (development only)."
                )
    except socket.gaierror:
        raise ValueError(
            f"Cannot resolve hostname: {parsed.hostname!r}"
        ) from None

    return url


def resolve_and_validate(url: str, *, allow_private: bool = False) -> list[str]:
    """Resolve ``url``'s host and validate **every** resolved address at call time.

    This is the strict *connect-time* gate (vs. :func:`validate_base_url`, which
    runs at config-save/test-connection time and tolerates unresolvable local
    hostnames). Call it immediately before opening a connection so a hostname
    that passed validation earlier cannot later resolve to a private or metadata
    address (DNS-rebinding TOCTOU).

    Args:
        url: The request URL to check.
        allow_private: Skip private-IP checks (local/self-hosted providers).
            Also implied by the ``ONTOKIT_ALLOW_PRIVATE_URLS`` env var.

    Returns:
        The list of resolved IP strings, all validated safe.

    Raises:
        ValueError: On an invalid scheme/host, a resolution failure, or any
            resolved address that is a cloud metadata endpoint or (unless
            allowed) a private/reserved IP.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http:// and https:// are allowed. Got: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError(f"URL must include a hostname: {url!r}")

    allow = allow_private or _ALLOW_PRIVATE

    # Cloud endpoints must be HTTPS (plaintext is only for explicit local providers).
    if not allow and parsed.scheme != "https":
        raise ValueError(
            f"Cloud provider endpoints require HTTPS. Got: {parsed.scheme!r}."
        )

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        results = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {parsed.hostname!r}") from None

    ips: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in results:
        addr = str(sockaddr[0])
        if _is_metadata_ip(addr):
            raise ValueError(
                f"URL resolves to a cloud metadata endpoint ({addr}), which is blocked."
            )
        if not allow and _is_private_ip(addr):
            raise ValueError(
                f"URL resolves to a private IP address ({addr}). "
                "Set ONTOKIT_ALLOW_PRIVATE_URLS=true to allow (development only)."
            )
        ips.append(addr)
    return ips


class SSRFProtectedTransport(httpx.AsyncBaseTransport):
    """httpx transport that re-validates the destination host at connect time.

    Kills the resolves-then-connects TOCTOU: a ``base_url`` validated when a
    provider was configured is re-resolved and re-checked on **every** request,
    immediately before the underlying transport opens the socket. If the host
    now resolves to a private or cloud-metadata address the request is refused
    (``httpx.ConnectError``) rather than dialed.
    """

    def __init__(
        self,
        *,
        allow_private: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allow_private = allow_private
        self._transport = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            resolve_and_validate(str(request.url), allow_private=self._allow_private)
        except ValueError as exc:
            # Surface as a connection error so callers handle it like any other
            # failed dial, and never leak that we probed DNS.
            logger.warning("SSRF guard blocked request to %s: %s", request.url.host, exc)
            raise httpx.ConnectError(str(exc), request=request) from exc
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def secure_async_client(*, allow_private: bool = False, **kwargs: Any) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` hardened against SSRF.

    - Re-validates the target host at connect time (:class:`SSRFProtectedTransport`).
    - Disables redirect following by default, so a 3xx to a private IP can't
      bypass the guard. Callers that genuinely need redirects must opt in.

    Use this for every outbound call to a project-controlled provider base URL.
    """
    kwargs.setdefault("follow_redirects", False)
    transport = SSRFProtectedTransport(allow_private=allow_private)
    return httpx.AsyncClient(transport=transport, **kwargs)
