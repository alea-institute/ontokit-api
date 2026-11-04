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
from ontokit.core.client_ip import (
    anonymous_ip_storage_form,
    get_client_ip,
    ipv6_bucket_prefix,
)
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


def test_trusted_peer_falls_back_to_forwarded_for(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    req = _request("127.0.0.1", {"X-Forwarded-For": " 198.51.100.2 "})
    assert get_client_ip(req) == "198.51.100.2"


def test_forwarded_for_walks_from_the_right_skipping_trusted_hops(trust) -> None:  # type: ignore[no-untyped-def]
    # The leftmost entry is client-supplied and spoofable; the first hop not
    # added by one of our own proxies is the real visitor.
    trust("127.0.0.1, 172.16.0.0/12")
    req = _request(
        "127.0.0.1",
        {"X-Forwarded-For": "6.6.6.6, 198.51.100.2, 172.18.0.9, 127.0.0.1"},
    )
    assert get_client_ip(req) == "198.51.100.2"


def test_forwarded_for_of_only_trusted_hops_uses_peer(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1, 172.16.0.0/12")
    req = _request("127.0.0.1", {"X-Forwarded-For": "172.18.0.9, 127.0.0.1"})
    assert get_client_ip(req) == "127.0.0.1"


def test_forwarded_for_stops_at_malformed_hop(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    req = _request("127.0.0.1", {"X-Forwarded-For": "198.51.100.2, garbage"})
    assert get_client_ip(req) == "127.0.0.1"


def test_cf_connecting_ip_outranks_forwarded_for(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    req = _request(
        "127.0.0.1",
        {"CF-Connecting-IP": "198.51.100.1", "X-Forwarded-For": "198.51.100.2"},
    )
    assert get_client_ip(req) == "198.51.100.1"


def test_ipv4_mapped_peer_matches_ipv4_trusted_proxy(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    req = _request("::ffff:127.0.0.1", {"CF-Connecting-IP": "198.51.100.1"})
    assert get_client_ip(req) == "198.51.100.1"


def test_ipv4_mapped_peer_is_normalized_when_untrusted(trust) -> None:  # type: ignore[no-untyped-def]
    trust("")
    assert get_client_ip(_request("::ffff:203.0.113.9")) == "203.0.113.9"


def test_ipv4_mapped_header_value_is_normalized(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    req = _request("127.0.0.1", {"CF-Connecting-IP": "::ffff:198.51.100.1"})
    assert get_client_ip(req) == "198.51.100.1"


def test_ipv4_mapped_forwarded_hop_is_recognized_as_trusted(trust) -> None:  # type: ignore[no-untyped-def]
    trust("127.0.0.1")
    req = _request("127.0.0.1", {"X-Forwarded-For": "198.51.100.2, ::ffff:127.0.0.1"})
    assert get_client_ip(req) == "198.51.100.2"


# ── Rate-limit bucketing ──────────────────────────────────────────────────────


def test_ipv4_is_its_own_bucket() -> None:
    assert anonymous_ip_storage_form("198.51.100.7") == "198.51.100.7"
    assert ipv6_bucket_prefix("198.51.100.7") is None


def test_ipv6_is_stored_exploded() -> None:
    assert anonymous_ip_storage_form("2001:db8::1") == "2001:0db8:0000:0000:0000:0000:0000:0001"


def test_ipv6_addresses_in_one_slash64_share_a_bucket() -> None:
    a = ipv6_bucket_prefix("2001:db8:0:0:aaaa::1")
    b = ipv6_bucket_prefix("2001:db8::ffff:ffff:ffff:ffff")
    assert a == b == "2001:0db8:0000:0000:"


def test_ipv6_addresses_in_different_slash64s_do_not_share() -> None:
    assert ipv6_bucket_prefix("2001:db8:0:1::1") != ipv6_bucket_prefix("2001:db8:0:2::1")


def test_non_ip_key_is_kept_verbatim() -> None:
    assert anonymous_ip_storage_form("unknown") == "unknown"
    assert ipv6_bucket_prefix("unknown") is None


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
