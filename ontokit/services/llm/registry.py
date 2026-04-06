"""LLM provider registry — factory and metadata for all 13 supported providers.

Ported from folio-enrich/backend/app/services/llm/registry.py with adaptations for
ontokit-api (imports from ontokit.schemas.llm, no app.config dependency for local
provider default URLs — those come from the project's LLMConfig.base_url instead).

Usage:
    from ontokit.services.llm.registry import get_provider, LLMProviderType

    provider = get_provider("openai", api_key="sk-...", model="gpt-4o")
    text, input_tok, output_tok = await provider.chat([{"role": "user", "content": "Hi"}])
"""

from __future__ import annotations

from ontokit.schemas.llm import LLMProviderType
from ontokit.services.llm.base import LLMProvider

# Re-export so callers can do: from ontokit.services.llm.registry import LLMProviderType
__all__ = [
    "DEFAULT_BASE_URLS",
    "DEFAULT_MODELS",
    "KNOWN_MODELS",
    "LLMProviderType",
    "PROVIDER_DISPLAY_NAMES",
    "PROVIDER_ICON_NAMES",
    "PROVIDER_REQUIRES_KEY",
    "get_provider",
]

DEFAULT_BASE_URLS: dict[LLMProviderType, str] = {
    LLMProviderType.openai: "https://api.openai.com/v1",
    LLMProviderType.anthropic: "https://api.anthropic.com",
    LLMProviderType.google: "https://generativelanguage.googleapis.com/v1beta",
    LLMProviderType.mistral: "https://api.mistral.ai/v1",
    LLMProviderType.cohere: "https://api.cohere.com/v2",
    LLMProviderType.meta_llama: "https://api.llama.com/v1",
    LLMProviderType.ollama: "http://localhost:11434/v1",
    LLMProviderType.lmstudio: "http://localhost:1234/v1",
    LLMProviderType.custom: "http://localhost:8080/v1",
    LLMProviderType.groq: "https://api.groq.com/openai/v1",
    LLMProviderType.xai: "https://api.x.ai/v1",
    LLMProviderType.github_models: "https://models.github.ai/inference",
    LLMProviderType.llamafile: "http://localhost:8080/v1",
}

DEFAULT_MODELS: dict[LLMProviderType, str] = {
    LLMProviderType.openai: "gpt-4o",
    LLMProviderType.anthropic: "claude-sonnet-4-6",
    LLMProviderType.google: "gemini-2.5-flash",
    LLMProviderType.mistral: "mistral-medium-latest",
    LLMProviderType.cohere: "command-a-03-2025",
    LLMProviderType.meta_llama: "llama-4-scout",
    LLMProviderType.ollama: "",
    LLMProviderType.lmstudio: "",
    LLMProviderType.custom: "",
    LLMProviderType.groq: "llama-3.3-70b-versatile",
    LLMProviderType.xai: "grok-3",
    LLMProviderType.github_models: "openai/gpt-4o",
    LLMProviderType.llamafile: "",
}

PROVIDER_DISPLAY_NAMES: dict[LLMProviderType, str] = {
    LLMProviderType.openai: "OpenAI",
    LLMProviderType.anthropic: "Anthropic",
    LLMProviderType.google: "Google Gemini",
    LLMProviderType.mistral: "Mistral AI",
    LLMProviderType.cohere: "Cohere",
    LLMProviderType.meta_llama: "Meta Llama",
    LLMProviderType.ollama: "Ollama (Local)",
    LLMProviderType.lmstudio: "LM Studio (Local)",
    LLMProviderType.custom: "Custom OpenAI-Compatible",
    LLMProviderType.groq: "Groq",
    LLMProviderType.xai: "xAI (Grok)",
    LLMProviderType.github_models: "GitHub Models",
    LLMProviderType.llamafile: "Llamafile (Local)",
}

PROVIDER_REQUIRES_KEY: dict[LLMProviderType, bool] = {
    LLMProviderType.openai: True,
    LLMProviderType.anthropic: True,
    LLMProviderType.google: True,
    LLMProviderType.mistral: True,
    LLMProviderType.cohere: True,
    LLMProviderType.meta_llama: True,
    LLMProviderType.ollama: False,
    LLMProviderType.lmstudio: False,
    LLMProviderType.custom: False,
    LLMProviderType.groq: True,
    LLMProviderType.xai: True,
    LLMProviderType.github_models: True,
    LLMProviderType.llamafile: False,
}

# Lucide icon names for provider logos in the UI
PROVIDER_ICON_NAMES: dict[LLMProviderType, str] = {
    LLMProviderType.openai: "Sparkles",
    LLMProviderType.anthropic: "Bot",
    LLMProviderType.google: "Star",
    LLMProviderType.mistral: "Wind",
    LLMProviderType.cohere: "Zap",
    LLMProviderType.meta_llama: "Flame",
    LLMProviderType.ollama: "Cpu",
    LLMProviderType.lmstudio: "Cpu",
    LLMProviderType.custom: "Globe",
    LLMProviderType.groq: "Bolt",
    LLMProviderType.xai: "X",
    LLMProviderType.github_models: "Github",
    LLMProviderType.llamafile: "Cpu",
}

# Well-known models per provider, used for the model picker without an API key
# Format: list of {"id": str, "name": str, "tier": "quality" | "cheap"}
KNOWN_MODELS: dict[LLMProviderType, list[dict[str, str]]] = {
    LLMProviderType.openai: [
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "tier": "cheap"},
        {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano", "tier": "cheap"},
        {"id": "gpt-4o", "name": "GPT-4o", "tier": "quality"},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "tier": "cheap"},
        {"id": "gpt-4.1", "name": "GPT-4.1", "tier": "quality"},
        {"id": "o4-mini", "name": "o4 Mini", "tier": "cheap"},
        {"id": "o3", "name": "o3", "tier": "quality"},
    ],
    LLMProviderType.anthropic: [
        {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5", "tier": "cheap"},
        {"id": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5", "tier": "quality"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "tier": "quality"},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "tier": "quality"},
    ],
    LLMProviderType.google: [
        {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash-Lite", "tier": "cheap"},
        {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "tier": "cheap"},
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "tier": "quality"},
    ],
    LLMProviderType.mistral: [
        {"id": "mistral-small-latest", "name": "Mistral Small", "tier": "cheap"},
        {"id": "mistral-medium-latest", "name": "Mistral Medium", "tier": "quality"},
        {"id": "mistral-large-latest", "name": "Mistral Large", "tier": "quality"},
    ],
    LLMProviderType.cohere: [
        {"id": "command-r-08-2024", "name": "Command R", "tier": "cheap"},
        {"id": "command-r-plus-08-2024", "name": "Command R+", "tier": "quality"},
        {"id": "command-a-03-2025", "name": "Command A", "tier": "quality"},
    ],
    LLMProviderType.meta_llama: [
        {"id": "llama-3.3-70b-instruct", "name": "Llama 3.3 70B", "tier": "cheap"},
        {"id": "llama-4-scout", "name": "Llama 4 Scout", "tier": "cheap"},
        {"id": "llama-4-maverick", "name": "Llama 4 Maverick", "tier": "quality"},
    ],
    LLMProviderType.groq: [
        {"id": "llama-3.1-8b-instant", "name": "Llama 3.1 8B Instant", "tier": "cheap"},
        {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B Versatile", "tier": "quality"},
        {"id": "qwen/qwen3-32b", "name": "Qwen3 32B", "tier": "quality"},
    ],
    LLMProviderType.xai: [
        {"id": "grok-3-mini", "name": "Grok 3 Mini", "tier": "cheap"},
        {"id": "grok-3", "name": "Grok 3", "tier": "quality"},
        {"id": "grok-4-0709", "name": "Grok 4", "tier": "quality"},
    ],
    LLMProviderType.github_models: [
        {"id": "openai/gpt-4o-mini", "name": "OpenAI GPT-4o Mini", "tier": "cheap"},
        {"id": "openai/gpt-4o", "name": "OpenAI GPT-4o", "tier": "quality"},
        {"id": "meta/llama-3.3-70b-instruct", "name": "Meta Llama 3.3 70B", "tier": "cheap"},
        {"id": "mistral-ai/mistral-large-2411", "name": "Mistral Large", "tier": "quality"},
    ],
    LLMProviderType.ollama: [
        {"id": "qwen3:4b", "name": "Qwen3 4B", "tier": "cheap"},
        {"id": "qwen3:8b", "name": "Qwen3 8B", "tier": "cheap"},
        {"id": "qwen3:14b", "name": "Qwen3 14B", "tier": "quality"},
        {"id": "llama3.3:8b", "name": "Llama 3.3 8B", "tier": "cheap"},
        {"id": "mistral:7b", "name": "Mistral 7B", "tier": "cheap"},
    ],
    LLMProviderType.lmstudio: [],
    LLMProviderType.custom: [],
    LLMProviderType.llamafile: [],
}

# Providers handled by the single OpenAI-compatible client
_OPENAI_COMPAT_PROVIDERS: frozenset[LLMProviderType] = frozenset({
    LLMProviderType.openai,
    LLMProviderType.mistral,
    LLMProviderType.meta_llama,
    LLMProviderType.ollama,
    LLMProviderType.lmstudio,
    LLMProviderType.custom,
    LLMProviderType.groq,
    LLMProviderType.xai,
    LLMProviderType.llamafile,
})


def get_provider(
    provider_type: LLMProviderType | str,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Factory: create an LLM provider instance.

    Accepts both LLMProviderType enum values and plain strings for convenience.
    Resolves default base_url and model from the registry if not provided.

    Args:
        provider_type: Provider identifier (e.g. "openai", LLMProviderType.anthropic).
        api_key: API key for the provider; None for local providers.
        base_url: Override the default base URL (used for custom/local endpoints).
        model: Override the default model for this provider.

    Returns:
        An LLMProvider instance ready for use.

    Raises:
        ValueError: If the provider_type string is not recognized.
    """
    # Normalize string → enum
    if isinstance(provider_type, str):
        name = provider_type.replace("-", "_")
        if name == "lm_studio":
            name = "lmstudio"
        try:
            provider_type = LLMProviderType(name)
        except ValueError:
            available = [p.value for p in LLMProviderType]
            raise ValueError(
                f"Unknown LLM provider: {provider_type!r}. Available: {available}"
            )

    # Resolve defaults
    resolved_base_url = base_url or DEFAULT_BASE_URLS.get(provider_type)
    resolved_model = model or DEFAULT_MODELS.get(provider_type) or None

    # Local providers don't require a real key — supply a placeholder so the SDK
    # doesn't reject the client construction
    if not PROVIDER_REQUIRES_KEY.get(provider_type, True) and not api_key:
        api_key = provider_type.value

    if provider_type == LLMProviderType.anthropic:
        from ontokit.services.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=api_key, base_url=resolved_base_url, model=resolved_model)

    if provider_type == LLMProviderType.google:
        from ontokit.services.llm.google_provider import GoogleProvider

        return GoogleProvider(api_key=api_key, base_url=resolved_base_url, model=resolved_model)

    if provider_type == LLMProviderType.cohere:
        from ontokit.services.llm.cohere_provider import CohereProvider

        return CohereProvider(api_key=api_key, base_url=resolved_base_url, model=resolved_model)

    if provider_type == LLMProviderType.github_models:
        from ontokit.services.llm.github_models_provider import GitHubModelsProvider

        return GitHubModelsProvider(
            api_key=api_key, base_url=resolved_base_url, model=resolved_model
        )

    if provider_type in _OPENAI_COMPAT_PROVIDERS:
        from ontokit.services.llm.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            api_key=api_key, base_url=resolved_base_url, model=resolved_model
        )

    raise ValueError(f"No provider implementation for: {provider_type.value!r}")
