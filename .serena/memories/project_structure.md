# Codebase Structure — ontokit-api

## Top-level layout
```
ontokit-api/
├── ontokit/          # main package
├── tests/            # unit + integration tests
├── alembic/          # DB migrations
├── scripts/          # release, migration, setup scripts
├── data/             # data assets
├── docs/             # documentation
├── config/           # config files
├── compose.yaml             # docker compose (dev)
├── compose.prod.yaml        # docker compose (prod infra)
├── Dockerfile / Dockerfile.prod
├── pyproject.toml / uv.lock
├── alembic.ini
├── Makefile
├── .env.example
├── CLAUDE.md, AGENTS.md, GEMINI.md, README.md, SECURITY.md, RELEASING.md
```

## ontokit/ package layout (layered architecture)
```
ontokit/
├── api/routes/       # REST endpoints (FastAPI routers)
├── services/         # Business logic layer
├── models/           # SQLAlchemy ORM models
├── schemas/          # Pydantic v2 request/response schemas
├── core/             # Config, database, auth infrastructure
├── git/              # Git repository management (bare repos via pygit2)
├── collab/           # WebSocket real-time collaboration
├── version.py        # Version mgmt (Weblate-style, with -dev/-rc suffix support)
├── runner.py         # CLI entry point (`ontokit` script)
├── worker.py         # ARQ background job worker
└── main.py           # FastAPI app + router registration
```

URL prefix `/api/v1/` is registered in `main.py`, NOT in directory structure.

## Key services (ontokit/services/)
- **ontology.py** — RDF/OWL graph operations (RDFLib + OWLReady2)
- **linter.py** — 20+ ontology validation rules
- **pull_request_service.py** — git-based PR workflow with diff generation
- **github_service.py** — GitHub App integration for remote sync
- **project_service.py** — project CRUD + member management

## Git module (ontokit/git/)
- **bare_repository.py** — `BareOntologyRepository` + `BareGitRepositoryService`; pygit2-based, no working dir, supports concurrent multi-user branch work
- **repository.py** — Legacy GitPython implementation (DEPRECATED)

## Tests
- `tests/unit/` — unit tests
- `tests/integration/` — integration tests
- `tests/conftest.py` — shared fixtures
