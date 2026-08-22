"""SSRF protection for LLM provider base URLs.

Ported from folio-enrich/backend/app/services/llm/url_validator.py with adaptations
for ontokit-api's LLMProviderType location and env-var naming.

Guards against:
- Private/reserved IP ranges being used as cloud provider endpoints
- Non-HTTP(S) schemes (file://, ftp://, etc.)
- AWS/GCP/Azure metadata endpoint (169.254.169.254)

Private-network access is granted to exact operator-approved origins, never to
a project-selected provider label. Built-in local runtimes have one exact
default origin each; additional origins require ``ONTOKIT_PRIVATE_LLM_ORIGINS``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache, partial
from typing import Any
from urllib.parse import urlparse, urlunparse

import anyio
import httpcore
import httpx

logger = logging.getLogger(__name__)

_PINNED_ADDRESSES: ContextVar[dict[tuple[str, int], tuple[str, ...]] | None] = ContextVar(
    "llm_pinned_addresses", default=None
)

# Cloud metadata endpoints — always blocked, even for local providers.
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure IMDS (IPv4)
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS (IPv6)
    }
)


def canonical_origin(url: str) -> str | None:
    """Return a normalized scheme/host/effective-port origin."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}"


def sanitize_base_url(url: str | None) -> str | None:
    """Strip legacy URL userinfo before a stored base URL is returned.

    New writes reject userinfo. This compatibility path prevents credentials
    embedded by older versions from reaching member-visible responses while an
    operator migrates the stored value.
    """
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username is None and parsed.password is None:
        return url
    try:
        explicit_port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{explicit_port}" if explicit_port is not None else host
    return urlunparse(parsed._replace(netloc=netloc))


@lru_cache(maxsize=32)
def _parse_operator_private_origins(origins: str) -> frozenset[str]:
    return frozenset(
        origin
        for value in origins.split(",")
        if (origin := canonical_origin(value.strip())) is not None
    )


def _operator_private_origins() -> frozenset[str]:
    return _parse_operator_private_origins(os.environ.get("ONTOKIT_PRIVATE_LLM_ORIGINS", ""))


def provider_allows_private_network(provider: object, base_url: str | None) -> bool:
    """Return whether this exact provider origin may reach private addresses."""
    if not base_url:
        return False
    value = getattr(provider, "value", provider)
    origin = canonical_origin(base_url)
    if origin is None:
        return False
    from ontokit.services.llm.registry import LOCAL_PRIVATE_BASE_URLS

    built_in = next(
        (url for key, url in LOCAL_PRIVATE_BASE_URLS.items() if key.value == value),
        None,
    )
    return (built_in is not None and origin == canonical_origin(built_in)) or (
        origin in _operator_private_origins()
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
        allow_private: If True, skip private-IP checks for an exact trusted origin.

    Returns:
        The validated URL string (unchanged).

    Raises:
        ValueError: If the URL fails any safety check.
    """
    parsed = urlparse(url)

    if not parsed.scheme:
        raise ValueError(f"URL must include a scheme (http:// or https://): {url!r}")

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http:// and https:// are allowed. Got scheme: {parsed.scheme!r}")

    if not parsed.hostname:
        raise ValueError(f"URL must include a hostname: {url!r}")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not include embedded credentials (userinfo)")

    # Always block the cloud metadata endpoint regardless of provider type
    try:
        results = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
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
    if allow_private:
        return url

    # Cloud providers require HTTPS
    if parsed.scheme != "https":
        raise ValueError(
            f"Cloud provider endpoints require HTTPS. Got: {parsed.scheme!r}. "
            "Use a local provider type for HTTP endpoints."
        )

    # Resolve and check for private IPs
    try:
        results = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
        for _family, _type, _proto, _canonname, sockaddr in results:
            addr = str(sockaddr[0])
            if _is_private_ip(addr):
                raise ValueError(f"Cloud provider URL resolves to a private IP address ({addr}).")
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {parsed.hostname!r}") from None

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
        allow_private: Skip private-IP checks for an exact trusted origin.

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
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not include embedded credentials (userinfo)")

    allow = allow_private

    # Cloud endpoints must be HTTPS (plaintext is only for explicit local providers).
    if not allow and parsed.scheme != "https":
        raise ValueError(f"Cloud provider endpoints require HTTPS. Got: {parsed.scheme!r}.")

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
            raise ValueError(f"URL resolves to a private IP address ({addr}).")
        ips.append(addr)
    return ips


async def resolve_and_validate_async(url: str, *, allow_private: bool = False) -> list[str]:
    """Resolve and validate without blocking the application's event loop."""
    return await anyio.to_thread.run_sync(
        partial(resolve_and_validate, url, allow_private=allow_private)
    )


class PinnedDNSBackend(httpcore.AsyncNetworkBackend):
    """Resolve, validate, and dial the same numeric IP address."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._allow_private = allow_private
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        # Validate every answer before dialing any of them. This refuses mixed
        # public/private answer sets and closes the DNS-rebinding TOCTOU.
        url_host = f"[{host}]" if ":" in host else host
        scheme = "http" if self._allow_private else "https"
        pinned = _PINNED_ADDRESSES.get()
        ips = pinned.get((host, port)) if pinned is not None else None
        if ips is None:
            try:
                ips = tuple(
                    await resolve_and_validate_async(
                        f"{scheme}://{url_host}:{port}",
                        allow_private=self._allow_private,
                    )
                )
            except ValueError as exc:
                raise httpcore.ConnectError(str(exc)) from exc

        last_error: Exception | None = None
        for ip in ips:
            try:
                return await self._backend.connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError(f"No usable address found for {host!r}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


@contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    mappings = (
        (httpcore.ConnectTimeout, httpx.ConnectTimeout),
        (httpcore.ReadTimeout, httpx.ReadTimeout),
        (httpcore.WriteTimeout, httpx.WriteTimeout),
        (httpcore.PoolTimeout, httpx.PoolTimeout),
        (httpcore.ConnectError, httpx.ConnectError),
        (httpcore.ReadError, httpx.ReadError),
        (httpcore.WriteError, httpx.WriteError),
        (httpcore.ProxyError, httpx.ProxyError),
        (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
        (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
        (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    )
    try:
        yield
    except Exception as exc:
        for source, target in mappings:
            if isinstance(exc, source):
                raise target(str(exc)) from exc
        raise


class _PinnedResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_httpcore_exceptions():
            async for chunk in self._stream:
                yield chunk

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if close is not None:
            await close()


class PinnedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    """HTTPX transport whose connection pool uses :class:`PinnedDNSBackend`."""

    def __init__(self, *, allow_private: bool = False) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=PinnedDNSBackend(allow_private=allow_private),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            response = await self._pool.handle_async_request(core_request)
        if not isinstance(response.stream, AsyncIterable):
            raise TypeError("Expected an asynchronous HTTP response stream")
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_PinnedResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


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
    ) -> None:
        self._allow_private = allow_private
        self._transport = PinnedAsyncHTTPTransport(allow_private=allow_private)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            ips = await resolve_and_validate_async(
                str(request.url), allow_private=self._allow_private
            )
        except ValueError as exc:
            logger.warning("SSRF guard blocked request: host=%s", request.url.host)
            raise httpx.ConnectError(str(exc), request=request) from exc

        host = request.url.host
        if host is None:
            raise httpx.ConnectError("URL must include a hostname", request=request)
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        token = _PINNED_ADDRESSES.set({(host, port): tuple(ips)})
        try:
            return await self._transport.handle_async_request(request)
        except httpx.ConnectError:
            logger.warning("Protected provider connection failed: host=%s", request.url.host)
            raise
        finally:
            _PINNED_ADDRESSES.reset(token)

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
