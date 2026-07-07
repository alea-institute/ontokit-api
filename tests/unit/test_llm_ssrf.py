"""Tests for LLM base-url SSRF metadata/private-IP detection."""

import pytest

from ontokit.services.llm.ssrf import _is_metadata_ip, _is_private_ip


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
