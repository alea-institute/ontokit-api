"""Provider boundary regressions for usage accounting and secure transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ontokit.services.llm.anthropic_provider import AnthropicProvider
from ontokit.services.llm.openai_compat import OpenAICompatProvider


@pytest.mark.asyncio
async def test_openai_compat_estimates_missing_usage() -> None:
    provider = OpenAICompatProvider(model="gateway-model")
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="paid output"))],
            usage=None,
        )
    )
    provider._client = client

    _, input_tokens, output_tokens = await provider.chat(
        [{"role": "user", "content": "paid input"}]
    )

    assert input_tokens > 0
    assert output_tokens > 0


@pytest.mark.asyncio
async def test_anthropic_estimates_missing_usage() -> None:
    provider = AnthropicProvider(model="claude-test")
    client = MagicMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text="paid output")],
            usage=None,
        )
    )
    provider._client = client

    _, input_tokens, output_tokens = await provider.chat(
        [{"role": "user", "content": "paid input"}]
    )

    assert input_tokens > 0
    assert output_tokens > 0


def test_anthropic_preserves_base_url_and_secure_client() -> None:
    provider = AnthropicProvider(
        api_key="secret",
        base_url="https://anthropic-proxy.example.test",
        model="claude-test",
    )
    sdk_client = object()
    with (
        patch("anthropic.AsyncAnthropic", return_value=sdk_client) as constructor,
        patch(
            "ontokit.services.llm.ssrf.secure_async_client",
            return_value=MagicMock(name="secure-client"),
        ) as secure_client,
    ):
        assert provider._get_client() is sdk_client

    secure_client.assert_called_once_with()
    kwargs = constructor.call_args.kwargs
    assert kwargs["api_key"] == "secret"
    assert kwargs["base_url"] == "https://anthropic-proxy.example.test"
    assert kwargs["http_client"] is secure_client.return_value
