# OntoKit API

[![CI](https://github.com/CatholicOS/ontokit-api/actions/workflows/release.yml/badge.svg)](https://github.com/CatholicOS/ontokit-api/actions/workflows/release.yml)
[![PyPI](https://img.shields.io/pypi/v/ontokit)](https://pypi.org/project/ontokit/)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2FCatholicOS%2Fontokit-api%2Fmain%2Fpyproject.toml)](https://github.com/CatholicOS/ontokit-api)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Collaborative OWL ontology curation API built with FastAPI.

## Purpose

OntoKit API is the backend service for **OntoKit**, a platform for building and maintaining
OWL 2 ontologies the way software teams maintain code: on branches, through pull requests,
with review, history, and automated quality gates. It turns ontology curation from a
single-editor, file-passing exercise into a collaborative, version-controlled workflow.

Every project is backed by a **bare Git repository** (via pygit2), so multiple contributors
can work on different branches of the same ontology simultaneously without stepping on each
other. Changes flow through pull requests with semantic diffs; a linter with 20+ rules and a
consistency checker guard quality; and a suggestion workflow lets people without write access
propose edits for editor review. On top of that sits **semantic intelligence** — vector
embeddings power natural-language search, similar-entity discovery, and duplicate detection.

OntoKit is part of the **CatholicOS / Catholic Semantic Canon** ecosystem, where it serves as
the ontology-engineering backbone for building large, curated knowledge graphs. The engine
itself is domain-agnostic — it manages any OWL/RDF ontology — and is published to PyPI as the
`ontokit` package under an open-source license.

## Who it's for / Personas

- **Ontology engineers & knowledge modelers** — the primary editors. They create classes,
  properties, and individuals; open pull requests; run the linter and consistency checks; and
  merge reviewed changes into the canonical branch.
- **Domain experts & reviewers** — subject-matter contributors who may not hold write access.
  They open **suggestion sessions** (each backed by a dedicated branch with auto-save) to
  propose changes, and review pull requests with inline comments before merge.
- **Project owners & admins** — manage project visibility (public/private), team membership
  and roles, join requests, and GitHub sync configuration.
- **Application & tooling developers** — consume the ontologies programmatically through the
  REST API, semantic search, and the read-only SPARQL endpoint; build desktop/CLI clients that
  authenticate via the OAuth2 Device Authorization Grant.
- **Data & research teams** — use analytics (activity timelines, contributor stats, hot
  entities) and quality reports to monitor the health of large, evolving ontologies.

## Use cases

- **Version-controlled ontology editing** — Branch-based editing of OWL classes, properties,
  and individuals, with pull requests, semantic diffs, review comments, and merge.
- **Two-way GitHub sync** — Import an ontology from a GitHub repository or file upload, and
  keep a project synchronized with an upstream GitHub repo via the GitHub App integration.
- **Guarded quality** — Run the linter (20+ semantic rules), check consistency (cycle
  detection, hierarchy validation, deprecated-entity tracking), and detect duplicates via
  label-similarity clustering, before changes land.
- **Open contribution without open write access** — Let non-editors propose edits through
  suggestion sessions that editors review and accept or reject.
- **Semantic discovery** — Find entities by natural-language query, surface entities similar to
  a given IRI, and rank candidates by contextual relevance using pluggable embedding providers.
- **Canonical formatting & clean diffs** — Normalize ontology files to canonical Turtle so
  that diffs stay meaningful and review stays readable.
- **Querying & integration** — Full-text search over ontologies plus a read-only SPARQL
  endpoint (SELECT, ASK, CONSTRUCT) for downstream applications.
- **Real-time collaboration** — WebSocket-based presence and live update streams (e.g. lint
  results) so teammates see activity as it happens.

## API surface

All endpoints live under the `/api/v1` prefix. Interactive docs are served at `/docs`
(Swagger UI) and `/redoc`, with the raw schema at `/openapi.json`. Major areas:

| Area | What it does |
| --- | --- |
| **Authentication** | OAuth2 Device Authorization Grant for desktop/CLI clients and token refresh; web clients use Zitadel OIDC directly. |
| **Projects** | Create and configure ontology projects; each wraps a bare Git repo with team membership, branches, and role-based access. Import from file upload or GitHub. |
| **Ontologies / Classes / Properties** | CRUD on ontologies and their OWL classes and properties (object, datatype, annotation), including hierarchy queries, import/export, diff, and history. |
| **Pull Requests** | Git-based PR workflow with semantic diffs, review comments, merge, and GitHub two-way sync. |
| **Suggestions** | Suggestion sessions for non-editor contributors — dedicated branch, auto-save, editor review, and a `sendBeacon` save-on-close endpoint. |
| **Join Requests** | Request project membership; admins/owners approve or decline; pending summaries for notification badges. |
| **Lint / Quality / Normalization** | 20+ lint rules, consistency and cross-reference checks, duplicate detection, and canonical-Turtle normalization jobs. |
| **Embeddings / Semantic Search / Search** | Vector embedding generation (local sentence-transformers, OpenAI, or Voyage), similarity search, full-text search, and a read-only SPARQL endpoint. |
| **Analytics / Notifications / User Settings** | Activity and contributor analytics, notifications, and per-user integration settings (e.g. GitHub tokens). |

## Tech stack

- **Framework**: FastAPI on Python 3.11+ (async-first; Uvicorn ASGI server)
- **Database**: PostgreSQL 17 via SQLAlchemy 2.0 (async, asyncpg) with Alembic migrations
- **Vector search**: pgvector + sentence-transformers (pluggable OpenAI / Voyage providers)
- **Cache & queue**: Redis 7 (ARQ background job worker, pub/sub)
- **Object storage**: MinIO (S3-compatible) for ontology files
- **Version control**: pygit2 with bare repositories for concurrent, checkout-free access
- **Authentication**: Zitadel (OIDC/OAuth2, JWT validation), with OAuth2 Device Grant for CLI
- **Ontology processing**: RDFLib and Owlready2 for RDF/OWL graph operations and SPARQL
- **Real-time**: WebSockets for presence and live update streams
- **Quality tooling**: Ruff (lint + format), MyPy (strict), pytest + coverage, pre-commit
- **Packaging**: hatchling build backend, `uv` for dependency management; published to PyPI as `ontokit`

The `ontokit` package also installs an `ontokit` CLI entry point that launches the API server.

## Getting started

### Full Docker mode

Runs the API, worker, and all infrastructure (PostgreSQL, Redis, MinIO, Zitadel) in containers.

```bash
# Start all services
docker compose up -d

# Run database migrations
docker compose exec api alembic upgrade head

# Set up Zitadel authentication (creates OIDC apps, updates .env files)
./scripts/setup-zitadel.sh --update-env

# Recreate API/worker containers to pick up the new credentials
docker compose up -d --force-recreate api worker
```

### Hybrid mode (API on host)

Runs infrastructure in Docker while the API runs directly on your machine for fast reloads.

```bash
# Start infrastructure
docker compose -f compose.prod.yaml up -d

# Install dependencies and pre-commit hooks (one command)
make setup

# Configure
cp .env.example .env

# Set up Zitadel authentication (creates OIDC apps, updates .env files)
./scripts/setup-zitadel.sh --update-env

# Run database migrations
alembic upgrade head

# Start the server
uvicorn ontokit.main:app --reload
# ...or, using the installed CLI:
ontokit --reload
```

> **Note:** `make setup` requires [uv](https://docs.astral.sh/uv/). It installs all dev
> dependencies and sets up pre-commit hooks (Ruff + MyPy) so that code-quality checks run
> automatically on every commit.

Once running, the API is available at `http://localhost:8000`, with interactive docs at
`http://localhost:8000/docs` and a health check at `http://localhost:8000/health`.

### Configuration

Environment variables are set in `.env` (copy from `.env.example`). Key groups:

- **Database** — `DATABASE_URL` (PostgreSQL with the asyncpg driver)
- **Redis** — `REDIS_URL` (cache, pub/sub, and the ARQ job queue)
- **Storage** — `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET`
- **Auth** — `ZITADEL_ISSUER`, `ZITADEL_CLIENT_ID`, `ZITADEL_CLIENT_SECRET`, `ZITADEL_SERVICE_TOKEN`
- **Git** — `GIT_REPOS_BASE_PATH` for on-disk bare repositories (one per project)
- **GitHub (optional)** — `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` for GitHub sync
- **CORS** — `CORS_ORIGINS` (JSON array of allowed origins)

### Development

```bash
ruff check ontokit/ --fix      # Lint with auto-fix
ruff format ontokit/           # Format
mypy ontokit/                  # Type-check (strict)
pytest tests/ -v --cov=ontokit # Run tests with coverage
```

The API enforces a default rate limit of **100 requests per minute per IP**; rate-limited
responses include `Retry-After` and `X-RateLimit-Limit` headers.

## Documentation

See the [wiki](https://github.com/CatholicOS/ontokit-api/wiki) for full documentation, and
[`RELEASING.md`](RELEASING.md) for the release and publishing process.

## License

MIT
