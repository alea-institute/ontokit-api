"""Tests for the human-verification providers (U6, R10, KTD10).

The default must be a no-op — a deployment that never configured a challenge is
not broken by the trust ladder — and the real provider must treat every failure
as a denial. A verification provider that passes when it cannot reach its
backend is not a verification provider.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ontokit.services.verification import (
    TURNSTILE_VERIFY_URL,
    NullVerificationProvider,
    TurnstileVerificationProvider,
    get_verification_provider,
)


def _client_returning(response: MagicMock) -> MagicMock:
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


def _response(status_code: int = 200, body: object = None, json_raises: bool = False):
    response = MagicMock()
    response.status_code = status_code
    if json_raises:
        response.json = MagicMock(side_effect=ValueError("not json"))
    else:
        response.json = MagicMock(return_value=body if body is not None else {"success": True})
    return response


class TestNullProvider:
    def test_is_not_enabled(self) -> None:
        assert NullVerificationProvider().enabled is False

    @pytest.mark.parametrize("token", [None, "", "anything"])
    async def test_always_passes(self, token: str | None) -> None:
        assert await NullVerificationProvider().verify(token) is True


class TestTurnstileProvider:
    async def test_valid_token_passes(self) -> None:
        ctx, client = _client_returning(_response(200, {"success": True}))
        with patch("ontokit.services.verification.secure_async_client", return_value=ctx):
            assert await TurnstileVerificationProvider("secret").verify("tok") is True
        assert client.post.await_args.args[0] == TURNSTILE_VERIFY_URL

    async def test_client_ip_is_forwarded_when_known(self) -> None:
        ctx, client = _client_returning(_response())
        with patch("ontokit.services.verification.secure_async_client", return_value=ctx):
            await TurnstileVerificationProvider("secret").verify("tok", "203.0.113.5")
        assert client.post.await_args.kwargs["data"]["remoteip"] == "203.0.113.5"

    async def test_secret_is_sent_not_the_token_alone(self) -> None:
        ctx, client = _client_returning(_response())
        with patch("ontokit.services.verification.secure_async_client", return_value=ctx):
            await TurnstileVerificationProvider("secret").verify("tok")
        data = client.post.await_args.kwargs["data"]
        assert data["secret"] == "secret"
        assert data["response"] == "tok"

    @pytest.mark.parametrize("token", [None, ""])
    async def test_missing_token_is_denied_without_a_network_call(self, token: str | None) -> None:
        with patch("ontokit.services.verification.secure_async_client") as factory:
            assert await TurnstileVerificationProvider("secret").verify(token) is False
        factory.assert_not_called()

    async def test_unsuccessful_body_is_denied(self) -> None:
        ctx, _ = _client_returning(
            _response(200, {"success": False, "error-codes": ["invalid-input-response"]})
        )
        with patch("ontokit.services.verification.secure_async_client", return_value=ctx):
            assert await TurnstileVerificationProvider("secret").verify("tok") is False

    async def test_non_200_is_denied(self) -> None:
        ctx, _ = _client_returning(_response(503, {"success": True}))
        with patch("ontokit.services.verification.secure_async_client", return_value=ctx):
            assert await TurnstileVerificationProvider("secret").verify("tok") is False

    async def test_non_json_body_is_denied(self) -> None:
        ctx, _ = _client_returning(_response(200, json_raises=True))
        with patch("ontokit.services.verification.secure_async_client", return_value=ctx):
            assert await TurnstileVerificationProvider("secret").verify("tok") is False

    async def test_network_failure_is_denied_not_passed(self) -> None:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=OSError("connection refused"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("ontokit.services.verification.secure_async_client", return_value=ctx):
            assert await TurnstileVerificationProvider("secret").verify("tok") is False

    def test_enabled_requires_a_secret(self) -> None:
        assert TurnstileVerificationProvider("secret").enabled is True
        assert TurnstileVerificationProvider("").enabled is False


class TestProviderSelection:
    def test_default_is_the_null_provider(self) -> None:
        with patch("ontokit.services.verification.settings") as mock_settings:
            mock_settings.verification_provider = "none"
            assert isinstance(get_verification_provider(), NullVerificationProvider)

    def test_turnstile_is_selected_when_configured(self) -> None:
        with patch("ontokit.services.verification.settings") as mock_settings:
            mock_settings.verification_provider = "turnstile"
            mock_settings.turnstile_secret_key = "secret"
            assert isinstance(get_verification_provider(), TurnstileVerificationProvider)

    def test_turnstile_without_a_secret_degrades_loudly_to_null(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Better than blocking every first-time contributor on a half-config."""
        with (
            patch("ontokit.services.verification.settings") as mock_settings,
            caplog.at_level("WARNING", logger="ontokit.services.verification"),
        ):
            mock_settings.verification_provider = "turnstile"
            mock_settings.turnstile_secret_key = ""
            provider = get_verification_provider()
        assert isinstance(provider, NullVerificationProvider)
        assert any("TURNSTILE_SECRET_KEY is unset" in r.message for r in caplog.records)
