"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "OntoKit API"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    secret_key: str = Field(default="change-me-in-production")
    # Retired SECRET_KEY values kept for zero-downtime rotation. Comma-separated;
    # used for DECRYPTION only (never encryption) via MultiFernet, so ciphertext
    # written under a previous key still decrypts after SECRET_KEY is rotated.
    # Migrate stored ciphertext off a retired key with crypto.rotate_secret(),
    # then drop it from this list. See ontokit/services/llm/crypto.py.
    secret_key_previous: str = Field(default="")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://ontokit:ontokit@localhost:5432/ontokit"  # type: ignore[assignment]
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def convert_postgres_scheme(cls, v: object) -> object:
        """Railway provides postgresql:// but SQLAlchemy async needs postgresql+asyncpg://."""
        if isinstance(v, str) and v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # Redis
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")  # type: ignore[assignment]

    # MinIO / S3
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minio"
    minio_secret_key: str = "minio123"
    minio_bucket: str = "ontokit"
    minio_secure: bool = False

    # Git Repository Storage
    git_repos_base_path: str = "/data/repos"

    # Zitadel Authentication
    zitadel_issuer: str = "http://localhost:8080"
    zitadel_internal_url: str | None = None  # Internal URL for JWKS fetch (defaults to issuer)
    zitadel_client_id: str = ""
    zitadel_client_secret: str = ""
    zitadel_service_token: str = ""  # PAT for service account (user lookups)

    @property
    def zitadel_jwks_base_url(self) -> str:
        """URL to use for fetching JWKS (internal URL in Docker, issuer otherwise)."""
        return self.zitadel_internal_url or self.zitadel_issuer

    # CORS
    cors_origins: list[str] = Field(default=["http://localhost:3000"])

    # GitHub Integration
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_token_encryption_key: str = ""

    # External API URL (for webhook callback URLs)
    api_base_url: str = "http://localhost:8000"

    # Frontend / Sitemap Revalidation
    frontend_url: str = ""  # e.g. http://localhost:3000
    revalidation_secret: str = ""  # shared secret for sitemap revalidation

    # Auth mode: "required" (default), "optional" (browse without login, sign in for editing), "disabled" (no auth)
    # Literal (not bare str) so pydantic-settings rejects typos at startup instead of
    # silently falling through to required behavior (/ce:review MEDIUM, PR-2).
    auth_mode: Literal["required", "optional", "disabled"] = "required"

    # --- Contribution trust ladder ---
    # Domain for the synthetic noreply aliases that author suggestion commits
    # (R14). Git history is permanent and, once mirrored, public — a real email
    # address must never enter it. Set this per environment BEFORE the first
    # suggestion commit lands: the alias is baked into history.
    commit_noreply_domain: str = "users.noreply.ontokit.local"

    # Human-verification challenge on an untrusted contributor's first
    # suggestion (R10). "none" is a no-op provider, so a deployment that has
    # not configured Turnstile is not broken by this feature.
    verification_provider: Literal["none", "turnstile"] = "none"
    turnstile_secret_key: str = ""

    # Per-account daily submission cap for the untrusted rung (R10). Anonymous
    # submissions keep their separate, DB-backed per-IP session limit.
    untrusted_daily_suggestion_limit: int = 10

    # --- GitHub mirror (R3, R16) ---
    # System-owned machine identity that pushes the mirror. When empty, sync
    # falls back to the connecting user's stored PAT with a deprecation
    # warning, so an in-flight deployment keeps working.
    github_mirror_token: str = ""
    github_mirror_username: str = ""
    # Outbound-only: the system identity pushes canonical history out, and
    # GitHub-side changes never enter the canonical repository. Set False to
    # restore the legacy bidirectional behavior.
    github_mirror_outbound_only: bool = True

    # --- PR Party (KTD12, KTD13) ---
    # The reviewer registry is provisioned from configuration, not from schema:
    # comma-separated "zitadel_user_id:github_login" pairs, reconciled into
    # pr_party_reviewer rows at startup. There is deliberately no seed migration
    # and no admin mutation endpoint — reviewer identity is environment data, and
    # an operator with shell access to the env is exactly who should set it.
    # Leaving this EMPTY does not de-register anyone: reconcile treats an empty
    # value as "unconfigured" and leaves the registry alone, so a dropped env var
    # cannot silently delete every reviewer's stored credential.
    pr_party_reviewers: str = ""
    # KTD13's privilege split, read side: ONE shared read-only token powering
    # intake, briefs, and status. It can never actuate — writes go through a
    # per-reviewer PAT encrypted at rest (see services/pr_party_credentials.py).
    pr_party_readonly_token: str = ""
    # Base URL of the ntfy instance reviewers' notification topics live on. The
    # topic itself is per reviewer and is a secret.
    pr_party_ntfy_base_url: str = "https://ntfy.sh"
    # The GitHub organization the reconciliation sweep enumerates (R1). One
    # org-scoped search per cycle replaces per-repo iteration (KTD14).
    pr_party_org: str = "CatholicOS"
    # Shared secret for the ORG-level webhook (KTD14). One secret, not the
    # per-project ``GitHubIntegration.webhook_secret`` — this receiver is not
    # scoped to a project and deliberately does not reuse that path. Leaving it
    # EMPTY means the receiver is not configured: it answers 503 rather than
    # accepting unauthenticated deliveries, because the HMAC over the raw body
    # is the route's only credential.
    pr_party_webhook_secret: str = ""
    # Sweep cadence in minutes. The sweep is complete intake on its own (KTD14),
    # so this is the freshness floor until the org hook exists; once it does,
    # this can drop to a low-frequency backstop. Also the unit `missing_since`
    # arithmetic is measured in (see ``pr_party_intake.MISSING_MISS_THRESHOLD``).
    pr_party_sweep_minutes: int = 5

    # --- PR Party brief generation (KTD17, R21) ---
    # The brief worker runs a TOOL-DENIED LLM call over adversarial input, so
    # its provider is configured separately from every project's BYO LLM config
    # and is checked against an allowlist at task start
    # (``pr_party_brief.APPROVED_PR_PARTY_PROVIDERS``): PR text is untrusted, and
    # the set of hosts it may be sent to is an instance-level decision, not a
    # per-project one. Leaving the API key EMPTY simply disables brief
    # generation — cards still render from PR facts, and the 90-minute brewing
    # timeout releases them.
    pr_party_llm_provider: str = "anthropic"
    pr_party_llm_model: str = ""
    pr_party_llm_api_key: str = ""
    # Optional endpoint override. Must survive the same SSRF guard every other
    # project-controlled provider URL does (services/llm/ssrf.py).
    pr_party_llm_base_url: str = ""
    # Instance-level daily spend cap in USD, enforced fail-closed against a
    # Redis counter: no counter, no call.
    pr_party_llm_daily_budget_usd: float = 5.0
    # Bytes of unified diff sent verbatim before the remainder degrades to
    # per-file ``path (+adds/-dels)`` summary lines and ``brief_truncated`` is set.
    pr_party_brief_max_diff_bytes: int = 300_000
    # arq job timeout for one brief. Generous: the job makes up to three GitHub
    # calls plus one (retryable) LLM call.
    pr_party_brief_timeout_seconds: int = 240

    # --- PR Party actuation (KTD16, U6) ---
    # How long a ``pending`` action row is treated as "in flight" before a new
    # request may reclaim it. KTD16 commits the pending row BEFORE the GitHub
    # call, so a process that dies mid-call leaves one behind; without a window
    # that row would wedge the fingerprint forever. Long enough that a slow
    # GitHub call is never stolen from, short enough that a reviewer is not
    # locked out for a working day.
    pr_party_action_reclaim_minutes: int = 30
    # Per-reviewer daily actuation cap. Deliberately generous: two humans
    # reviewing PRs will never approach it, and the control exists to stop a
    # runaway client retry loop from spraying GitHub, not to throttle people.
    pr_party_daily_action_limit: int = 200

    @property
    def pr_party_reviewer_map(self) -> dict[str, str]:
        """Parsed ``PR_PARTY_REVIEWERS``: zitadel user id -> github login.

        Malformed entries (no ``:``, empty side) are dropped rather than
        crashing boot; ``reconcile_reviewers`` logs the count it discarded so a
        typo is visible in the startup log instead of silently costing someone
        their queue.
        """
        pairs: dict[str, str] = {}
        for entry in self.pr_party_reviewers.split(","):
            zitadel_id, sep, github_login = entry.partition(":")
            if not sep:
                continue
            zitadel_id, github_login = zitadel_id.strip(), github_login.strip()
            if zitadel_id and github_login:
                pairs[zitadel_id] = github_login
        return pairs

    # Superadmin - comma-separated list of user IDs with full system access
    superadmin_user_ids: str = ""

    @property
    def superadmin_ids(self) -> set[str]:
        """Get set of superadmin user IDs."""
        if not self.superadmin_user_ids:
            return set()
        return {uid.strip() for uid in self.superadmin_user_ids.split(",") if uid.strip()}

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
