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

The default `Dockerfile` installs runtime dependencies from `uv.lock` without the
dev group and fails the build if the lockfile is out of sync with `pyproject.toml`.

The image bakes in dependencies, while the local compose stack mounts live source.
After `uv.lock` changes, run `docker compose up -d --build api worker` to rebuild
the image and recreate both containers.
The API and worker containers refuse to start when the mounted lock differs from
the image's recorded lock hash and print the rebuild command.
Set `ONTOKIT_SKIP_LOCK_CHECK=1` in the container environment to skip this check.

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

`AUTH_MODE=disabled` means **read and suggest only**: safe reads retain the shared
anonymous identity, and anonymous suggestion sessions remain available. Routes
using `RequiredUser` or `RequiredUserWithToken` return 403 for methods other than
GET, HEAD, and OPTIONS; normalization refresh is also refused. Documented exceptions
are anonymous suggestions, token-authenticated beacons, signed webhooks, auth
endpoints, and read-only SPARQL.
Use `required` or `optional` with sign-in for editing.

## Testing and memory limits

Run `make test` for the full suite, or
`make test PYTEST_ARGS='-x --durations=20'` to investigate a failure.
For a selected test, use `bash scripts/memory-cap.sh uv run pytest tests/ -k test_name`.
The wrapper uses a Linux systemd user scope with `MemoryMax=6G` and
`MemorySwapMax=0`, falling back to `prlimit --as` when the user scope is unavailable.
The fallback limits virtual address space per process; it does not provide an
aggregate process-tree or zero-swap limit. It fails closed if `prlimit` is unavailable.
Root `conftest.py` also applies a hard Linux address-space limit before application
fixtures load, protecting bare `pytest` runs. Set `ONTOKIT_TEST_MEMORY_MIB` to a
positive MiB value to override the default 6144; inherited stricter hard limits
remain in effect. Prefer serial tests on the shared development box: independent
test runs and worker processes can each consume their own allowance.

Both root Compose stacks cap every container with equal `mem_limit` and
`memswap_limit` (no container swap): API 2 GiB, ARQ worker 3 GiB, PostgreSQL 2 GiB,
Redis 512 MiB, MinIO 1 GiB, Zitadel 1 GiB, login 512 MiB, and Mailpit 256 MiB.
The infrastructure-only production stack uses the same conservative service limits.
The deployed development stack (`deploy/compose.dev.yaml`) also caps every service,
with 1 GiB for its additional frontend container.
Recreate containers with `docker compose up -d` to apply the limits.
These limits cover these Compose stacks and test commands; host-run application
processes and multiple concurrent stacks need their own aggregate budget.

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
