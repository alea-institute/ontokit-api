"""Resolve the real visitor's IP address for rate limiting and verification.

Behind a reverse proxy such as Cloudflare Tunnel, every request's TCP peer is
the proxy, so keying limits on ``request.client.host`` would put every visitor
in one bucket. Forwarded headers are only believed when the immediate peer is
listed in ``TRUSTED_PROXY_IPS``; otherwise a direct caller could choose its own
bucket by sending the header.
"""

from __future__ import annotations

from ipaddress import ip_address

from starlette.requests import Request

from ontokit.core.config import settings


def _parse_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ip_address(value.strip()))
    except ValueError:
        return None


def get_client_ip(request: Request) -> str | None:
    """Return the client IP, honoring forwarded headers only from trusted proxies.

    Order when the peer is trusted: ``CF-Connecting-IP``, then the first
    ``X-Forwarded-For`` hop, then the peer itself. Malformed header values are
    ignored. Returns ``None`` when the request has no peer address.
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return None

    networks = settings.trusted_proxy_networks
    peer_ip = _parse_ip(peer)
    if not networks or peer_ip is None:
        return peer
    if not any(ip_address(peer_ip) in network for network in networks):
        return peer

    forwarded = _parse_ip(request.headers.get("cf-connecting-ip"))
    if forwarded is None:
        xff = request.headers.get("x-forwarded-for")
        forwarded = _parse_ip(xff.split(",", 1)[0]) if xff else None
    return forwarded or peer
