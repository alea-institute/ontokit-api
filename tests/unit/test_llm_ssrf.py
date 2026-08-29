"""Tests for LLM base-url SSRF metadata/private-IP detection."""

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpcore
import httpx
import pytest

from ontokit.services.llm.ssrf import (
    _PINNED_ADDRESSES,
    PinnedAsyncHTTPTransport,
    PinnedDNSBackend,
    SSRFProtectedTransport,
    _is_metadata_ip,
    _is_private_ip,
    resolve_and_validate,
    secure_async_client,
)


@pytest.mark.parametrize(
    "addr",
    [
        "169.254.169.254",  # AWS/GCP/Azure IMDS (IPv4)
        "::ffff:169.254.169.254",  # IPv4-mapped IPv6 form of the same
        "fd00:ec2::254",  # AWS IMDS (IPv6)
    ],
)
def test_metadata_ips_detected(addr):
    """All representations of the cloud metadata endpoint are blocked."""
    assert _is_metadata_ip(addr) is True


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_ips_not_flagged_as_metadata(addr):
    assert _is_metadata_ip(addr) is False


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.1",  # private
        "169.254.1.1",  # link-local
        "::ffff:10.0.0.5",  # IPv4-mapped private
        "fd00::1",  # unique-local (private)
    ],
)
def test_private_ips_detected(addr):
    assert _is_private_ip(addr) is True


@pytest.mark.parametrize("addr", ["8.8.8.8", "2606:4700:4700::1111", "garbage"])
def test_public_and_invalid_not_private(addr):
    assert _is_private_ip(addr) is False


# --- Connect-time resolution guard (DNS-rebinding TOCTOU) ---


def _gai(*addrs):
    """Build a socket.getaddrinfo return value for the given IP strings."""
    out = []
    for a in addrs:
        fam = socket.AF_INET6 if ":" in a else socket.AF_INET
        out.append((fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (a, 443)))
    return out


class TestResolveAndValidate:
    def test_public_host_returns_resolved_ips(self):
        with patch("ontokit.services.llm.ssrf.socket.getaddrinfo", return_value=_gai("8.8.8.8")):
            assert resolve_and_validate("https://api.example.com/v1") == ["8.8.8.8"]

    def test_private_ip_rejected(self):
        with (
            patch("ontokit.services.llm.ssrf.socket.getaddrinfo", return_value=_gai("10.0.0.5")),
            pytest.raises(ValueError, match="private IP"),
        ):
            resolve_and_validate("https://sneaky.example.com/v1")

    def test_metadata_ip_rejected_even_when_private_allowed(self):
        with (
            patch(
                "ontokit.services.llm.ssrf.socket.getaddrinfo",
                return_value=_gai("169.254.169.254"),
            ),
            pytest.raises(ValueError, match="metadata"),
        ):
            resolve_and_validate("http://metadata.local/latest", allow_private=True)

    def test_local_host_allowed_when_private_allowed(self):
        with patch("ontokit.services.llm.ssrf.socket.getaddrinfo", return_value=_gai("127.0.0.1")):
            assert resolve_and_validate("http://localhost:11434/v1", allow_private=True) == [
                "127.0.0.1"
            ]

    def test_plaintext_cloud_rejected(self):
        with pytest.raises(ValueError, match="require HTTPS"):
            resolve_and_validate("http://api.example.com/v1")

    def test_unresolvable_host_rejected(self):
        with (
            patch(
                "ontokit.services.llm.ssrf.socket.getaddrinfo",
                side_effect=socket.gaierror,
            ),
            pytest.raises(ValueError, match="Cannot resolve"),
        ):
            resolve_and_validate("https://nope.invalid/v1")


class TestSSRFProtectedTransport:
    @pytest.mark.asyncio
    async def test_rebinding_to_private_ip_blocked_at_connect(self):
        """A host that resolved safely earlier but now points at a private IP is refused."""
        transport = SSRFProtectedTransport(allow_private=False)
        request = httpx.Request("GET", "https://rebind.example.com/v1/models")
        with (
            patch(
                "ontokit.services.llm.ssrf.socket.getaddrinfo",
                return_value=_gai("192.168.0.9"),
            ),
            pytest.raises(httpx.ConnectError),
        ):
            await transport.handle_async_request(request)
        await transport.aclose()

    @pytest.mark.asyncio
    async def test_safe_answer_is_passed_to_dial_boundary_without_reresolving(self):
        inner = AsyncMock(spec=httpx.AsyncBaseTransport)

        async def handle(request: httpx.Request) -> httpx.Response:
            assert _PINNED_ADDRESSES.get() == {("api.example.com", 443): ("8.8.8.8",)}
            return httpx.Response(200, request=request)

        inner.handle_async_request.side_effect = handle
        transport = SSRFProtectedTransport()
        transport._transport = inner
        request = httpx.Request("GET", "https://api.example.com/v1/models")

        with patch(
            "ontokit.services.llm.ssrf.socket.getaddrinfo",
            return_value=_gai("8.8.8.8"),
        ) as resolve:
            response = await transport.handle_async_request(request)

        assert response.status_code == 200
        resolve.assert_called_once()


class TestPinnedDNSBackend:
    @pytest.mark.asyncio
    async def test_dials_the_validated_numeric_ip(self):
        inner = AsyncMock(spec=httpcore.AsyncNetworkBackend)
        stream = MagicMock(spec=httpcore.AsyncNetworkStream)
        inner.connect_tcp.return_value = stream
        backend = PinnedDNSBackend(backend=inner)

        with patch(
            "ontokit.services.llm.ssrf.socket.getaddrinfo",
            return_value=_gai("8.8.8.8"),
        ):
            result = await backend.connect_tcp("api.example.com", 443, timeout=2.0)

        assert result is stream
        inner.connect_tcp.assert_awaited_once_with(
            "8.8.8.8",
            443,
            timeout=2.0,
            local_address=None,
            socket_options=None,
        )

    @pytest.mark.asyncio
    async def test_private_answer_is_never_dialed(self):
        inner = AsyncMock(spec=httpcore.AsyncNetworkBackend)
        backend = PinnedDNSBackend(backend=inner)

        with (
            patch(
                "ontokit.services.llm.ssrf.socket.getaddrinfo",
                return_value=_gai("10.0.0.5"),
            ),
            pytest.raises(httpcore.ConnectError, match="private IP"),
        ):
            await backend.connect_tcp("rebind.example.com", 443)

        inner.connect_tcp.assert_not_awaited()


@pytest.mark.asyncio
async def test_secure_async_client_disables_redirects_by_default():
    async with secure_async_client() as client:
        assert client.follow_redirects is False
        assert isinstance(client._transport, SSRFProtectedTransport)
        assert isinstance(client._transport._transport, PinnedAsyncHTTPTransport)
