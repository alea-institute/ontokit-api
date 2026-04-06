"""Pydantic schemas for LLM configuration, status, and usage reporting."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class LLMProviderType(str, Enum):
    """Supported LLM provider types. Mirrors folio-enrich/folio-mapper exactly."""

    openai = "openai"
    anthropic = "anthropic"
    google = "google"
    mistral = "mistral"
    cohere = "cohere"
    meta_llama = "meta_llama"
    ollama = "ollama"
    lmstudio = "lmstudio"
    custom = "custom"
    groq = "groq"
    xai = "xai"
    github_models = "github_models"
    llamafile = "llamafile"


class LLMConfigResponse(BaseModel):
    """Public representation of a project's LLM config — never exposes the raw key."""

    provider: LLMProviderType
    model: str | None = None
    model_tier: str  # "quality" | "cheap"
    # True if a project-level API key is stored (does NOT return the key itself)
    api_key_set: bool
    base_url: str | None = None
    monthly_budget_usd: float | None = None
    daily_cap_usd: float | None = None


class LLMConfigUpdate(BaseModel):
    """Request body for updating a project's LLM configuration."""

    provider: LLMProviderType | None = None
    model: str | None = None
    model_tier: str | None = None  # "quality" | "cheap"
    # Write-only — if provided, will be encrypted and stored
    api_key: str | None = None
    base_url: str | None = None
    monthly_budget_usd: float | None = None
    daily_cap_usd: float | None = None


class LLMStatusResponse(BaseModel):
    """LLM feature availability status for the current project."""

    configured: bool
    provider: LLMProviderType | None = None
    budget_exhausted: bool
    daily_remaining: int | None = None  # None means uncapped
    monthly_budget_usd: float | None = None
    monthly_spent_usd: float
    burn_rate_daily_usd: float


class LLMUserUsage(BaseModel):
    """Per-user aggregated LLM usage for the usage dashboard."""

    user_id: str
    user_name: str | None = None
    calls_today: int
    calls_this_month: int
    cost_this_month_usd: float
    is_byo_key: bool


class LLMUsageResponse(BaseModel):
    """Project-level LLM usage summary for the usage dashboard."""

    total_calls: int
    total_cost_usd: float
    budget_consumed_pct: float  # 0–100; None if no budget set returns 0
    burn_rate_daily_usd: float
    users: list[LLMUserUsage]


class LLMAuditEntry(BaseModel):
    """Single audit log entry for the usage history table."""

    timestamp: datetime
    user_id: str
    model: str
    provider: str
    endpoint: str
    input_tokens: int
    output_tokens: int
    cost_estimate_usd: float
    is_byo_key: bool


class LLMProviderInfo(BaseModel):
    """Static metadata about a provider for the dropdown UI."""

    provider: LLMProviderType
    display_name: str
    requires_api_key: bool
    # Lucide icon name for the provider logo in the UI
    icon_name: str


class LLMKnownModel(BaseModel):
    """Well-known model entry for a given provider."""

    provider: LLMProviderType
    model_id: str
    display_name: str
    tier: str  # "quality" | "cheap"
