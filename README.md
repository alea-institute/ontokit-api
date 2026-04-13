# OntoKit API

[![CI](https://github.com/CatholicOS/ontokit-api/actions/workflows/release.yml/badge.svg)](https://github.com/CatholicOS/ontokit-api/actions/workflows/release.yml)
[![PyPI](https://img.shields.io/pypi/v/ontokit)](https://pypi.org/project/ontokit/)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2FCatholicOS%2Fontokit-api%2Fmain%2Fpyproject.toml)](https://github.com/CatholicOS/ontokit-api)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![codecov](https://codecov.io/gh/CatholicOS/ontokit-api/branch/dev/graph/badge.svg?token=MUF88DIN0X)](https://codecov.io/gh/CatholicOS/ontokit-api)

Collaborative OWL ontology curation API built with FastAPI.

## Features

- **RESTful API** for managing ontologies, classes, properties, and individuals
- **Project management** with public/private visibility and team collaboration
- **Git-based version control** with branching, pull requests, and sync from remote (pygit2 bare repos)
- **Ontology linting** with 20+ semantic validation rules
- **Semantic search** powered by sentence-transformers
- **Authentication** via Zitadel (OpenID Connect)
- **Real-time collaboration** via WebSockets
- **Background job queue** with ARQ + Redis
- **Object storage** integration with MinIO for ontology files

## Quick Start

### Full Docker Mode

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

### Hybrid Mode (API on host)

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

# Start server
uvicorn ontokit.main:app --reload
```

> **Note:** `make setup` requires [uv](https://docs.astral.sh/uv/). It installs
> all dev dependencies and sets up pre-commit hooks (ruff + mypy) so that code
> quality checks run automatically on every commit.

## Documentation

See the [wiki](https://github.com/CatholicOS/ontokit-api/wiki) for full documentation.

## Tech Stack

- **Framework**: FastAPI (Python 3.13)
- **Database**: PostgreSQL 17 + SQLAlchemy 2.0 (async)
- **Cache/Queue**: Redis 7 + ARQ
- **Object Storage**: MinIO (S3-compatible)
- **Authentication**: Zitadel (OIDC)
- **Git**: pygit2 (bare repositories for concurrent access)
- **Ontology Processing**: RDFLib, OWLReady2
- **Semantic Search**: sentence-transformers

## License

MIT
