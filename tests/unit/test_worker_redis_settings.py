"""Tests for get_redis_settings() — the worker's Redis DSN translation.

Regression coverage for the worker Redis-auth bug found on the FOLIO DEV
deploy (2026-07-06): ARQ's RedisSettings takes the URL apart into fields, and
the translation dropped the credentials, so the worker could not authenticate
against a password-protected Redis. The repo's own compose file uses a
password-less Redis, which is why the suite never noticed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ontokit import worker


def _settings_for(url: str) -> Any:
    with patch.object(worker.settings, "redis_url", url):
        return worker.get_redis_settings()


class TestGetRedisSettings:
    def test_plain_url_without_credentials(self) -> None:
        rs = _settings_for("redis://localhost:6379/0")
        assert rs.host == "localhost"
        assert rs.port == 6379
        assert rs.database == 0
        assert rs.username is None
        assert rs.password is None
        assert rs.ssl is False

    def test_password_is_carried_across(self) -> None:
        """The bug: this used to arrive at the wire as None."""
        rs = _settings_for("redis://:s3cret@redis.internal:6380/2")
        assert rs.host == "redis.internal"
        assert rs.port == 6380
        assert rs.database == 2
        assert rs.password == "s3cret"

    def test_username_and_password_are_both_carried(self) -> None:
        """Redis 6 ACLs authenticate with a username as well."""
        rs = _settings_for("redis://ontokit:s3cret@redis.internal:6379/0")
        assert rs.username == "ontokit"
        assert rs.password == "s3cret"

    def test_percent_encoded_password_is_decoded(self) -> None:
        """A password with reserved characters is encoded in the DSN."""
        rs = _settings_for("redis://:p%40ss%2Fword%3A1@redis.internal:6379/0")
        assert rs.password == "p@ss/word:1"

    def test_percent_encoded_username_is_decoded(self) -> None:
        rs = _settings_for("redis://user%40host:pw@redis.internal:6379/0")
        assert rs.username == "user@host"

    def test_rediss_scheme_enables_tls(self) -> None:
        rs = _settings_for("rediss://:pw@redis.internal:6380/1")
        assert rs.ssl is True

    def test_redis_scheme_does_not_enable_tls(self) -> None:
        assert _settings_for("redis://redis.internal:6379/1").ssl is False

    @pytest.mark.parametrize(
        ("url", "expected_db"),
        [
            ("redis://localhost:6379", 0),
            ("redis://localhost:6379/", 0),
            ("redis://localhost:6379/3", 3),
        ],
    )
    def test_database_defaults_to_zero(self, url: str, expected_db: int) -> None:
        assert _settings_for(url).database == expected_db

    def test_missing_host_falls_back_to_localhost(self) -> None:
        assert _settings_for("redis:///0").host == "localhost"
