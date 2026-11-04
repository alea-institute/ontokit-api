"""Real-visitor client IP resolution behind a trusted proxy (BAIF demo, U5).

The anonymous suggestion limiter keys on the client IP. Behind Cloudflare
Tunnel every request arrives from the tunnel connector, so the limiter must
read ``CF-Connecting-IP`` -- but only when the immediate peer is a configured
trusted proxy; otherwise any caller could spoof its bucket with a header.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from ontokit.api.routes.anonymous_suggestions import get_service as get_suggestion_service
from ontokit.core.client_ip import get_client_ip
from ontokit.core.config import Settings, settings
from ontokit.main import app

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
SESSIONS_URL = f"/api/v1/projects/{PROJECT_ID}/suggestions/anonymous/sessions"


def _request(peer: str | None, headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (peer, 12345) if peer is not None else None,
    }
    return Request(scope)


@pytest.fixture
def trust(monkeypatch: pytest.MonkeyPatch) -> object:
    def _set(value: str) -> None:
        monkeypatch.setattr(settings, "trusted_proxy_ips", value)

    _set("")
    return _set


# ── Helper semantics ──────────────────────────────────────────────────────────


def test_untrusted_peer_header_is_ignored(trust) -> None:  # type: ignore[no-untyped-def]
    trust("")
    req = _request("203.0.113.9", {"CF-Connecting-IP": "198.51.100.1"})
    assert get_client_ip(req) == "203.0.113.9"


def test_peer_outside_trusted_set_header_is_ignored(trust) -> None:  # type: ignore[no-untyped-def]
    trust("10.0.0.0/8")
    req = _request(
        "203.0.113.9",
        {"CF-Connecting-IP": "198.51.100.1", "X-Forwarded-For": "198.51.100.2"},
    )
    assert get_client_ip(req) == "203.0.113.9"


def test_trusted_peer_cf_connecting_ip_is_honored(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1, 172.16.0.0/12")
    req = _request(
        "172.18.0.5",
        {"CF-Connecting-IP": "198.51.100.1", "X-Forwarded-For": "198.51.100.2"},
    )
    assert get_client_ip(req) == "198.51.100.1"


def test_trusted_peer_falls_back_to_first_forwarded_hop(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    req = _request("127.0.0.1", {"X-Forwarded-For": " 198.51.100.2 , 172.18.0.1"})
    assert get_client_ip(req) == "198.51.100.2"


def test_trusted_peer_without_headers_uses_peer(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    assert get_client_ip(_request("127.0.0.1")) == "127.0.0.1"


def test_trusted_peer_malformed_header_falls_back(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    req = _request("127.0.0.1", {"CF-Connecting-IP": "not-an-ip", "X-Forwarded-For": "also bad"})
    assert get_client_ip(req) == "127.0.0.1"


def test_ipv6_header_is_normalized(trust) -> None:  # type: ignore[no-untyped-def]
    trust("::1")
    req = _request("::1", {"CF-Connecting-IP": "2001:DB8::0001"})
    assert get_client_ip(req) == "2001:db8::1"


def test_missing_peer_returns_none(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    assert get_client_ip(_request(None, {"CF-Connecting-IP": "198.51.100.1"})) is None


def test_invalid_trusted_proxy_setting_is_rejected_at_load() -> None:
    with pytest.raises(ValueError, match="TRUSTED_PROXY_IPS"):
        Settings(trusted_proxy_ips="127.0.0.1, not-a-network")


def test_trusted_proxy_setting_defaults_empty() -> None:
    assert Settings().trusted_proxy_ips == ""


# ── Route keying: the anonymous limiter sees the real visitor ────────────────


@pytest.fixture
async def anon_route(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, MagicMock]]:
    monkeypatch.setattr("ontokit.api.routes.anonymous_suggestions.settings.auth_mode", "optional")
    service = MagicMock()
    service.create_anonymous_session = AsyncMock(
        return_value={
            "session_id": "s_test",
            "branch": "suggest/anonymous/s_test",
            "created_at": datetime.now(UTC),
            "anonymous_token": "test-token",
        }
    )
    monkeypatch.setattr(app, "dependency_overrides", {get_suggestion_service: lambda: service})
    # ASGITransport reports the peer as 127.0.0.1, standing in for cloudflared.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, service


async def test_distinct_cf_connecting_ips_get_separate_buckets(
    anon_route: tuple[AsyncClient, MagicMock],
    trust,  # type: ignore[no-untyped-def]
) -> None:
    trust("127.0.0.1")
    client, service = anon_route
    for visitor in ("198.51.100.1", "198.51.100.2"):
        resp = await client.post(SESSIONS_URL, headers={"CF-Connecting-IP": visitor})
        assert resp.status_code == 201
    keys = [call.args[1] for call in service.create_anonymous_session.await_args_list]
    assert keys == ["198.51.100.1", "198.51.100.2"]
    assert all(
        call.args[0] == UUID(PROJECT_ID)
        for call in service.create_anonymous_session.await_args_list
    )


async def test_spoofed_header_from_untrusted_peer_keeps_one_bucket(
    anon_route: tuple[AsyncClient, MagicMock],
    trust,  # type: ignore[no-untyped-def]
) -> None:
    trust("")
    client, service = anon_route
    for visitor in ("198.51.100.1", "198.51.100.2"):
        await client.post(SESSIONS_URL, headers={"CF-Connecting-IP": visitor})
    keys = [call.args[1] for call in service.create_anonymous_session.await_args_list]
    assert keys == ["127.0.0.1", "127.0.0.1"]
