# External Integrations

**Analysis Date:** 2026-05-02

## APIs & External Services

**Embedding Providers (pluggable):**
- **Local (sentence-transformers)** - Default. Runs embedding models locally (e.g., `all-MiniLM-L6-v2`)
  - SDK/Client: `sentence_transformers` library loaded lazily
  - Auth: None (local models)
  - Config: `EmbeddingConfig.model` in project settings

- **OpenAI** - Remote embedding API
  - SDK/Client: `httpx` async HTTP calls to `api.openai.com`
  - Auth: `OPENAI_API_KEY` (passed at runtime, not in config)
  - Models: `text-embedding-3-small` (1536 dims), `text-embedding-3-large` (3072 dims), `text-embedding-ada-002`
  - Implementation: `ontokit/services/embedding_providers/openai_provider.py`

- **Voyage AI** - Remote embedding API
  - SDK/Client: `httpx` async HTTP calls to Voyage API
  - Auth: `VOYAGE_API_KEY` (passed at runtime)
  - Models: `voyage-3-lite`, `voyage-3`, `voyage-code-3` (all 1024 dims)
  - Implementation: `ontokit/services/embedding_providers/voyage_provider.py`

**GitHub Integration:**
- Service: GitHub REST API (`api.github.com`)
  - SDK/Client: `PyGithub` + `httpx` for custom requests
  - Auth: GitHub Personal Access Token (user-provided, encrypted in `UserGitHubToken` model)
  - Token encryption: AES-256 (Fernet) with app `SECRET_KEY` as entropy source
  - Use cases: Sync ontologies to/from GitHub, create PRs, fetch issues/comments
  - Implementation: `ontokit/services/github_service.py`, `ontokit/services/github_sync.py`
  - GitHub App support: Optional (config: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`)

## Data Storage

**Databases:**
- **PostgreSQL 17**
  - Connection: `DATABASE_URL` env var (e.g., `postgresql+asyncpg://ontokit:ontokit@localhost:5432/ontokit`)
  - Client: **SQLAlchemy 2.0 async** + **asyncpg** (non-blocking driver)
  - Features: pgvector extension for embedding vectors (`pgvector>=0.3.0`)
  - ORM models: `ontokit/models/` directory
  - Migrations: **Alembic** (see `alembic/` directory, `alembic.ini`)

**File Storage:**
- **MinIO (S3-compatible)**
  - Connection: `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`
  - Bucket: `MINIO_BUCKET` (default: `ontokit`)
  - Secure: `MINIO_SECURE` (default: false for dev, true for prod)
  - Purpose: Ontology file uploads, exports, backups
  - Client: **minio** Python SDK
  - Implementation: `ontokit/services/storage.py` (`StorageService` class)

**Caching:**
- **Redis 7**
  - Connection: `REDIS_URL` env var (e.g., `redis://localhost:6379/0`)
  - Purpose: Session cache, pub/sub for real-time updates, ARQ job queue
  - Client: **redis** async library (`redis.asyncio`)
  - ARQ integration: `ontokit/api/utils/redis.py` creates ARQ pool

## Authentication & Identity

**Auth Provider:**
- **Zitadel** - OIDC/OAuth2 identity provider
  - Service URL: `ZITADEL_ISSUER` (e.g., `http://localhost:8080`)
  - Internal URL: `ZITADEL_INTERNAL_URL` (for Docker inter-container DNS when API runs in container)
  - JWKS endpoint: `.well-known/openid-configuration` (cached for 1 hour)
  - Implementation: `ontokit/core/auth.py` — JWT validation, JWKS fetching
  
  **OIDC Configuration:**
  - Client credentials: `ZITADEL_CLIENT_ID`, `ZITADEL_CLIENT_SECRET`
  - Service token: `ZITADEL_SERVICE_TOKEN` (PAT for user lookups)
  - Device flow: OAuth2 device authorization grant (for CLI auth)
  - Roles claim: `urn:zitadel:iam:org:project:roles` (custom Zitadel claim)
  
  **Token Validation:**
  - Tokens verified against JWKS (public key set)
  - Cache-stampede prevention: Double-checked locking in `get_jwks()`
  - Refresh trigger: Manual force refresh or TTL expiration (1 hour)
  - Claims extracted: `sub`, `email`, `name`, `roles`
  
  **Superadmin Support:**
  - Hardcoded user IDs: `SUPERADMIN_USER_IDS` (comma-separated)
  - Bypass project-level permissions
  - Parsed in `CurrentUser.is_superadmin` property

**Local Auth (Optional):**
- Password hashing: **passlib** with bcrypt (for future local auth support)
- Not currently active; auth via Zitadel only

## Monitoring & Observability

**Error Tracking:**
- Not detected — no Sentry, Datadog, or similar integration

**Logs:**
- Python built-in `logging` module
- Console-based (stdout) in Docker containers
- Request logging: `AccessLogMiddleware` in `ontokit/core/middleware.py`
- Structured fields: Request ID (generated per request), user ID, method, path, status

## CI/CD & Deployment

**Hosting:**
- Docker Compose (full-stack development) — see `compose.yaml`
- Docker (production) — see `Dockerfile.prod`, `compose.prod.yaml`
- Target platforms: Any Docker-capable infrastructure (Kubernetes, VPS, cloud)

**CI Pipeline:**
- GitHub Actions (inferred from repo structure)
- Key workflows (assumed from CLAUDE.md references):
  - **semgrep** (security scanning) - Diff-aware, with Pro rules or community OSS
  - **pytest** (test suite) — coverage reporting required
  - **ruff** (lint/format check)
  - **mypy** (type checking)

## Environment Configuration

**Required env vars:**
- `DATABASE_URL` - PostgreSQL async connection string
- `REDIS_URL` - Redis connection string
- `ZITADEL_ISSUER` - OIDC endpoint URL
- `ZITADEL_CLIENT_ID`, `ZITADEL_CLIENT_SECRET` - OAuth2 client credentials
- `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` - Object storage

**Optional env vars:**
- `ZITADEL_INTERNAL_URL` - Internal Docker DNS for JWKS fetch
- `ZITADEL_SERVICE_TOKEN` - PAT for user lookups (Zitadel API)
- `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` - GitHub App auth
- `OPENAI_API_KEY` - If using OpenAI embeddings
- `VOYAGE_API_KEY` - If using Voyage embeddings
- `FRONTEND_URL`, `REVALIDATION_SECRET` - For Next.js sitemap revalidation webhooks
- `SUPERADMIN_USER_IDS` - Comma-separated user IDs with full system access

**Secrets location:**
- `.env` file (local development) — NOT committed to git (ignored by `.gitignore`)
- Environment variables (Docker, Kubernetes, managed services)
- GitHub Secrets (CI/CD) for semgrep, publish workflows
- Zitadel secrets: PATs stored in mounted volume in Docker (`/zitadel-data/`)

## Webhooks & Callbacks

**Incoming:**
- **GitHub Webhooks** — Received from GitHub when events occur (push, PR, issue)
  - Endpoint: (inferred from routes) likely `/api/v1/github/webhook` or similar
  - Auth: HMAC signature verification (via `github_service.py`)
  - Trigger: Auto-sync of GitHub-linked projects

- **Zitadel Notifications** — Optional event notifications (not detected in current code)

**Outgoing:**
- **Sitemap Revalidation** — On-demand HTTP POST to frontend
  - Endpoint: `FRONTEND_URL + /api/revalidate` (Next.js ISR)
  - Auth: `REVALIDATION_SECRET` (shared secret in query or header)
  - Implementation: `ontokit/services/sitemap_notifier.py`
  - Trigger: After ontology changes

- **Mailpit** (local email testing) — Catches all outgoing SMTP
  - Server: `mailpit:1025` in Docker
  - Purpose: Development email capture (no production integration expected)
  - Access: Web UI at `http://localhost:8025`

---

*Integration audit: 2026-05-02*
