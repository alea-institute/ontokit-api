# Technology Stack

**Analysis Date:** 2026-05-02

## Languages

**Primary:**
- Python 3.11+ - All backend code, CLI, workers, migrations

**Secondary:**
- YAML - Docker Compose configurations, Alembic migrations, GitHub Actions CI/CD

## Runtime

**Environment:**
- Python 3.11 (minimum) / 3.12 (in Docker)

**Package Manager:**
- `pip` / `uv` (via hatchling build backend)
- Lockfile: `pyproject.toml` (PEP 621 format)

## Frameworks

**Core:**
- **FastAPI** 0.115.0+ - REST API framework with async/await, dependency injection, automatic OpenAPI docs
- **Uvicorn** 0.32.0+ - ASGI server (async HTTP server)

**Database:**
- **SQLAlchemy** 2.0+ - Async ORM (`sqlalchemy.ext.asyncio` with `AsyncSession`)
- **asyncpg** 0.30.0+ - PostgreSQL async driver

**Async Infrastructure:**
- **redis** 5.2.0+ - Redis async client (cache, pub/sub)
- **arq** 0.26.0+ - Background job queue on top of Redis with pub/sub

**RDF/OWL:**
- **rdflib** 7.1+ - RDF graph manipulation, SPARQL
- **owlready2** 0.47+ - OWL ontology loading and reasoning

**Git:**
- **pygit2** 1.13.0+ - libgit2 Python bindings (bare repository operations)
- **GitPython** 3.1.0 - High-level Git interface (legacy, see concerns)
- **PyGithub** 2.5+ - GitHub API client library

**Storage:**
- **minio** 7.2.0+ - S3-compatible object storage client

**Authentication:**
- **python-jose** 3.3.0+ with cryptography - JWT encoding/decoding
- **passlib** 1.7.4+ with bcrypt - Password hashing (local auth support)

**Request/Network:**
- **httpx** 0.28.0+ - Async HTTP client (external API calls, GitHub, OpenAI, Voyage)

**Validation:**
- **pydantic** 2.10.0–2.11.x - Data validation and settings management
- **pydantic-settings** 2.6.0–2.10.x - Environment variable loading

**Embeddings:**
- **sentence-transformers** 3.0.0+ - Local embedding models (MiniLM, etc.)

**Utilities:**
- **slowapi** 0.1.9+ - Rate limiting
- **websockets** 14.0+ - WebSocket support for real-time collaboration (in `/collab`)
- **pgvector** 0.3.0+ - PostgreSQL vector type client
- **numpy** 1.26.0+ - Vector/array operations for embeddings

**Testing:**
- **pytest** 8.3.0+ - Test runner
- **pytest-asyncio** 0.24.0+ - Async test support
- **pytest-cov** 6.0.0+ - Coverage reporting

**Code Quality:**
- **ruff** 0.8.0+ - Linter + formatter (replaces Black, isort, flake8)
- **mypy** 1.13.0+ - Static type checker (strict mode)
- **pre-commit** 4.0.0+ - Git hooks for linting before commit

**Database Migrations:**
- **alembic** 1.14.0+ - Database schema versioning and migrations

## Key Dependencies

**Critical:**
- **FastAPI** - Core REST API framework; drives all route handling and dependency injection
- **SQLAlchemy 2.0 async** - Async ORM enables high-concurrency request handling
- **asyncpg** - Direct PostgreSQL driver; replaces psycopg2 for non-blocking I/O
- **pygit2** - Bare repository support allows concurrent access without file locks
- **arq + Redis** - Background job queue for long-running tasks (linting, embedding, indexing)

**Infrastructure:**
- **rdflib** - RDF/OWL graph manipulation and SPARQL execution
- **owlready2** - OWL ontology reasoning (rdfs, owl reasoners)
- **minio** - S3-compatible object storage for ontology exports and file uploads
- **python-jose** - JWT token validation for Zitadel integration

## Configuration

**Environment:**
- Loaded from `.env` file via **pydantic-settings** (`BaseSettings`)
- Location: `ontokit/core/config.py` — `Settings` class with defaults
- Env var prefix: None (case-insensitive loading)

**Key configs required:**
- `DATABASE_URL` - PostgreSQL async DSN (e.g., `postgresql+asyncpg://...`)
- `REDIS_URL` - Redis connection string
- `ZITADEL_ISSUER` - OIDC provider URL (defaults to `http://localhost:8080`)
- `ZITADEL_CLIENT_ID`, `ZITADEL_CLIENT_SECRET` - OIDC client credentials
- `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` - S3 storage
- `GIT_REPOS_BASE_PATH` - Where git repositories are stored (default: `/data/repos`)
- `CORS_ORIGINS` - Allowed frontend origins (JSON array format)

**Build:**
- Build system: **hatchling** (via `pyproject.toml` `[build-system]`)
- Version management: `ontokit/version.py` (Weblate-style: `VERSION = "X.Y.Z[-dev]"`)
- Package entry point: `ontokit/runner.py:main` (CLI command `ontokit`)
- Docker build: `Dockerfile` (multi-stage, non-root user `ontokit`)

## Platform Requirements

**Development:**
- Python 3.11+ with pip/uv
- PostgreSQL 17 (with pgvector extension for embeddings)
- Redis 7+ for cache/queue
- Git + libgit2 dev libraries (e.g., `apt-get install libgit2-dev` on Debian)
- MinIO (S3-compatible) for object storage
- Zitadel (OIDC provider) for authentication
- Docker + Docker Compose for full stack (see `compose.yaml`)

**Production:**
- Deployment target: Docker containers (see `Dockerfile`, `Dockerfile.prod`, `compose.prod.yaml`)
- Database: PostgreSQL 17+ with asyncpg driver
- Cache/Queue: Redis 7+
- Storage: MinIO or AWS S3
- Auth: Zitadel (self-hosted or cloud)
- Python 3.12-slim base image in production Dockerfile

---

*Stack analysis: 2026-05-02*
