"""Resolve the real visitor's IP address for rate limiting and verification.

Behind a reverse proxy such as Cloudflare Tunnel, every request's TCP peer is
the proxy, so keying limits on ``request.client.host`` would put every visitor
in one bucket. Forwarded headers are only believed when the immediate peer is
listed in ``TRUSTED_PROXY_IPS``; otherwise a direct caller could choose its own
bucket by sending the header. Uvicorn runs with ``--no-proxy-headers`` so this
module is the only interpreter of those headers.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address

from starlette.requests import Request

from ontokit.core.config import settings

IPAddress = IPv4Address | IPv6Address

# IPv6 end sites are routinely delegated a whole /64, so one visitor can
# rotate through 2**64 addresses; the anonymous limiter counts per /64.
IPV6_BUCKET_PREFIX_LENGTH = 64


def _parse_ip(value: str | None) -> IPAddress | None:
    """Parse an address, unwrapping IPv4-mapped IPv6 (``::ffff:a.b.c.d``)."""
    if not value:
        return None
    try:
        ip = ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_trusted(ip: IPAddress, networks: tuple[IPv4Network | IPv6Network, ...]) -> bool:
    return any(ip.version == network.version and ip in network for network in networks)


def get_client_ip(request: Request) -> str | None:
    """Return the client IP, honoring forwarded headers only from trusted proxies.

    Order when the peer is trusted: ``CF-Connecting-IP``; then
    ``X-Forwarded-For`` walked from the right, skipping hops that are
    themselves trusted proxies, taking the first untrusted hop (a malformed
    hop stops the walk); then the peer itself. Returns ``None`` when the
    request has no peer address.
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return None

    peer_ip = _parse_ip(peer)
    if peer_ip is None:
        return peer
    networks = settings.trusted_proxy_networks
    if not _is_trusted(peer_ip, networks):
        return str(peer_ip)

    cf_ip = _parse_ip(request.headers.get("cf-connecting-ip"))
    if cf_ip is not None:
        return str(cf_ip)

    xff = request.headers.get("x-forwarded-for")
    if xff:
        for hop in reversed(xff.split(",")):
            hop_ip = _parse_ip(hop)
            if hop_ip is None:
                break
            if not _is_trusted(hop_ip, networks):
                return str(hop_ip)
    return str(peer_ip)


def anonymous_ip_storage_form(client_ip: str) -> str:
    """Canonical stored form: IPv4 dotted, IPv6 fully exploded, else verbatim.

    The exploded IPv6 form keeps the full address for audit while giving
    every address in a /64 the same fixed-width textual prefix, so the
    per-bucket count is a plain ``LIKE 'prefix%'`` with no schema change.
    """
    ip = _parse_ip(client_ip)
    if ip is None:
        return client_ip
    if isinstance(ip, IPv6Address):
        return ip.exploded
    return str(ip)


def ipv6_bucket_prefix(client_ip: str) -> str | None:
    """Return the exploded /64 prefix (``"xxxx:xxxx:xxxx:xxxx:"``) for IPv6, else None."""
    ip = _parse_ip(client_ip)
    if not isinstance(ip, IPv6Address):
        return None
    # Each exploded group is 4 hex digits plus a colon.
    return ip.exploded[: (IPV6_BUCKET_PREFIX_LENGTH // 16) * 5]
