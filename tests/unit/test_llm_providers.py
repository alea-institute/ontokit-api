"""Provider boundary regressions for usage accounting and secure transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ontokit.services.llm.anthropic_provider import AnthropicProvider
from ontokit.services.llm.cohere_provider import CohereProvider
from ontokit.services.llm.google_provider import GoogleProvider
from ontokit.services.llm.openai_compat import OpenAICompatProvider
from ontokit.services.llm.registry import get_provider


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


@pytest.mark.asyncio
async def test_cohere_forwards_output_token_cap() -> None:
    provider = CohereProvider(api_key="secret", model="command-test")
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "message": {"content": [{"text": "ok"}]},
        "meta": {"tokens": {"input_tokens": 1, "output_tokens": 1}},
    }
    client = AsyncMock()
    client.post.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client
    context.__aexit__.return_value = None

    with patch("ontokit.services.llm.cohere_provider.secure_async_client", return_value=context):
        await provider.chat([{"role": "user", "content": "test"}], max_tokens=4096)

    assert client.post.await_args.kwargs["json"]["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_google_forwards_output_token_cap() -> None:
    provider = GoogleProvider(api_key="secret", model="gemini-test")
    provider._post_with_retry = AsyncMock(
        return_value={
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
    )

    await provider.chat([{"role": "user", "content": "test"}], max_tokens=4096)

    body = provider._post_with_retry.await_args.args[1]
    assert body["generationConfig"]["maxOutputTokens"] == 4096


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


def test_custom_provider_does_not_bypass_private_network_guard() -> None:
    """Project-selected custom gateways are not implicitly trusted as local."""
    provider = get_provider(
        "custom",
        base_url="https://gateway.example.test/v1",
        model="gateway-model",
    )

    assert isinstance(provider, OpenAICompatProvider)
    assert provider._allow_private is False


def test_local_provider_label_does_not_authorize_arbitrary_private_origin() -> None:
    provider = get_provider(
        "ollama",
        base_url="http://internal-admin.example.test:8080/v1",
        model="local-model",
    )

    assert isinstance(provider, OpenAICompatProvider)
    assert provider._allow_private is False


def test_operator_can_authorize_one_exact_custom_origin(monkeypatch) -> None:
    monkeypatch.setenv("ONTOKIT_PRIVATE_LLM_ORIGINS", "http://gateway.private.example:8081")

    allowed = get_provider(
        "custom",
        base_url="http://gateway.private.example:8081/v1",
        model="gateway-model",
    )
    different_port = get_provider(
        "custom",
        base_url="http://gateway.private.example:8082/v1",
        model="gateway-model",
    )

    assert isinstance(allowed, OpenAICompatProvider)
    assert allowed._allow_private is True
    assert different_port._allow_private is False


@pytest.mark.parametrize("provider_name", ["ollama", "lmstudio", "llamafile"])
def test_explicit_local_providers_keep_private_network_support(provider_name: str) -> None:
    provider = get_provider(provider_name, model="local-model")

    assert isinstance(provider, OpenAICompatProvider)
    assert provider._allow_private is True
