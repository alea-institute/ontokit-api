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
import os
import socket
from urllib.parse import urlparse

# Allow overriding for development / testing environments
_ALLOW_PRIVATE = os.environ.get("ONTOKIT_ALLOW_PRIVATE_URLS", "").lower() in (
    "1",
    "true",
    "yes",
)

# Providers that run locally — HTTP allowed, private IPs allowed
_LOCAL_PROVIDER_VALUES = {"ollama", "lmstudio", "custom", "llamafile"}

# The cloud metadata endpoint — always blocked, even for local providers
_METADATA_IP = "169.254.169.254"


def _is_private_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
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
            addr = sockaddr[0]
            if addr == _METADATA_IP or ipaddress.ip_address(addr) == ipaddress.ip_address(_METADATA_IP):
                raise ValueError(
                    f"URL resolves to the cloud metadata endpoint ({_METADATA_IP}), "
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
            addr = sockaddr[0]
            if _is_private_ip(addr):
                raise ValueError(
                    f"Cloud provider URL resolves to a private IP address ({addr}). "
                    "Set ONTOKIT_ALLOW_PRIVATE_URLS=true to allow (development only)."
                )
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {parsed.hostname!r}")

    return url
