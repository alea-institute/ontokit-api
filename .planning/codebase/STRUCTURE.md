# Codebase Structure

**Analysis Date:** 2026-05-02

## Directory Layout

```
ontokit-api/                       # Project root
├── ontokit/                        # Main package
│   ├── __init__.py                 # Package metadata (__version__)
│   ├── main.py                     # FastAPI app, lifespan, middleware, exception handlers
│   ├── runner.py                   # CLI entry point (uvicorn wrapper)
│   ├── worker.py                   # ARQ background job processor
│   │
│   ├── api/                        # HTTP API layer (routes)
│   │   ├── __init__.py
│   │   ├── routes/                 # REST endpoint modules
│   │   │   ├── __init__.py         # Router aggregation, prefix registration
│   │   │   ├── auth.py             # OAuth2, device flow, token refresh
│   │   │   ├── projects.py         # Project CRUD, members, GitHub sync
│   │   │   ├── pull_requests.py    # PR workflow, branches, merges, reviews
│   │   │   ├── ontologies.py       # Standalone ontology CRUD
│   │   │   ├── classes.py          # OWL class operations within ontologies
│   │   │   ├── properties.py       # OWL property operations
│   │   │   ├── lint.py             # Ontology validation rules, issue reporting
│   │   │   ├── normalization.py    # Format conversion, turtle canonicalization
│   │   │   ├── quality.py          # Duplicate detection, consistency checking
│   │   │   ├── semantic_search.py  # Vector similarity search via embeddings
│   │   │   ├── embeddings.py       # Embedding provider config, job triggering
│   │   │   ├── suggestions.py      # Non-editor suggestion sessions
│   │   │   ├── join_requests.py    # Project membership requests
│   │   │   ├── analytics.py        # Activity timelines, contributor stats
│   │   │   ├── search.py           # Full-text search, SPARQL read-only
│   │   │   ├── user_settings.py    # User profile, GitHub token management
│   │   │   ├── notifications.py    # Notification delivery, preferences
│   │   │   ├── remote_sync.py      # Sync from GitHub/external sources
│   │   │   └── [others].py
│   │   └── utils/                  # Route utilities
│   │       ├── __init__.py
│   │       └── redis.py            # ARQ pool connection, channel publishing
│   │
│   ├── services/                   # Business logic layer
│   │   ├── __init__.py
│   │   ├── project_service.py      # Project CRUD, member management
│   │   ├── ontology.py             # RDF graph operations, class/property CRUD
│   │   ├── indexed_ontology.py     # SQL index + RDFLib graph dual lookup
│   │   ├── ontology_index.py       # PostgreSQL entity index (name, IRI, type)
│   │   ├── pull_request_service.py # PR creation, merge, semantic diff
│   │   ├── linter.py               # 20+ semantic validation rules
│   │   ├── normalization_service.py# Format conversion, turtle output
│   │   ├── consistency_service.py  # Cycle detection, hierarchy validation
│   │   ├── duplicate_detection_service.py # Label similarity clustering
│   │   ├── cross_reference_service.py # External ontology link validation
│   │   ├── embedding_service.py    # Vector embedding generation, storage
│   │   ├── embedding_text_builder.py # Extract text for embedding from classes
│   │   ├── embedding_providers/    # Embedding provider implementations
│   │   │   ├── __init__.py
│   │   │   ├── base.py             # Abstract EmbeddingProvider
│   │   │   ├── sentence_transformers.py # Local huggingface embeddings
│   │   │   ├── openai.py           # OpenAI embedding API
│   │   │   └── voyage.py           # Voyage.ai embeddings
│   │   ├── llm/                    # LLM provider wrappers
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   └── prompts/            # Prompt templates
│   │   ├── storage.py              # MinIO S3 integration
│   │   ├── ontology_extractor.py   # Parse uploaded files, extract metadata
│   │   ├── rdf_utils.py            # RDF/OWL utility functions
│   │   ├── user_service.py         # User CRUD, profile management
│   │   ├── github_service.py       # GitHub App integration
│   │   ├── github_sync.py          # Two-way sync with GitHub repos
│   │   ├── change_event_service.py # Record entity change for analytics
│   │   ├── notification_service.py # Notification dispatch, preferences
│   │   ├── remote_sync_service.py  # Import from external sources
│   │   ├── sitemap_notifier.py     # Revalidation webhook to frontend
│   │   └── [others].py
│   │
│   ├── models/                     # SQLAlchemy ORM layer
│   │   ├── __init__.py
│   │   ├── project.py              # Project, ProjectMember, ownership
│   │   ├── pull_request.py         # PullRequest, Comment, Review, GitHubIntegration
│   │   ├── lint.py                 # LintRun, LintIssue, dismissals
│   │   ├── normalization.py        # NormalizationRun (background job tracking)
│   │   ├── embedding.py            # EmbeddingRun, EntityEmbedding (vectors)
│   │   ├── suggestion_session.py   # SuggestionSession (non-editor edits)
│   │   ├── branch_metadata.py      # BranchMetadata (PR associations)
│   │   ├── ontology_index.py       # OntologyIndexEntry (class/property metadata)
│   │   ├── change_event.py         # ChangeEvent (audit trail)
│   │   ├── join_request.py         # JoinRequest (membership requests)
│   │   ├── notification.py         # Notification, preferences
│   │   ├── remote_sync.py          # RemoteSyncRun (GitHub/external sync tracking)
│   │   ├── user_github_token.py    # UserGitHubToken (encrypted tokens)
│   │   └── [others].py
│   │
│   ├── schemas/                    # Pydantic v2 request/response validation
│   │   ├── __init__.py
│   │   ├── auth.py                 # TokenResponse, DeviceFlowResponse
│   │   ├── project.py              # ProjectCreate, ProjectResponse, MemberResponse
│   │   ├── pull_request.py         # PRCreate, PRResponse, BranchInfo, DiffResponse
│   │   ├── ontology.py             # OntologyCreate, OntologyResponse
│   │   ├── owl_class.py            # OWLClassCreate, OWLClassResponse, tree nodes
│   │   ├── owl_property.py         # OWLPropertyCreate, OWLPropertyResponse
│   │   ├── lint.py                 # LintRunResponse, LintIssueResponse
│   │   ├── embedding.py            # EmbeddingRunResponse, search results
│   │   ├── suggestion.py           # SuggestionSessionResponse
│   │   ├── user_settings.py        # UserSettingsResponse, GitHub token
│   │   ├── analytics.py            # ActivityTimeline, ContributorStats
│   │   ├── notification.py         # NotificationResponse, preferences
│   │   └── [others].py
│   │
│   ├── core/                       # Infrastructure & configuration
│   │   ├── __init__.py
│   │   ├── config.py               # Settings (pydantic env-based)
│   │   ├── database.py             # SQLAlchemy async engine, session factory
│   │   ├── auth.py                 # JWT validation, CurrentUser, Zitadel integration
│   │   ├── exceptions.py           # Domain exceptions (NotFoundError, etc.)
│   │   ├── middleware.py           # RequestID, AccessLog, SecurityHeaders
│   │   ├── encryption.py           # Token encryption/decryption
│   │   └── beacon_token.py         # Transient tokens for sendBeacon
│   │
│   ├── git/                        # Version control layer
│   │   ├── __init__.py             # Public API (exports, backward-compat aliases)
│   │   └── bare_repository.py      # BareOntologyRepository, pygit2 operations
│   │
│   └── collab/                     # Real-time collaboration (WebSocket)
│       ├── __init__.py
│       ├── protocol.py             # WebSocket message protocol
│       ├── presence.py             # User presence tracking
│       └── transform.py            # Operational transform / conflict resolution
│
├── tests/                          # Test suite
│   ├── __init__.py
│   ├── conftest.py                 # Pytest fixtures (db, services, mocks)
│   ├── unit/                       # Unit tests (isolated)
│   │   ├── test_auth_core.py
│   │   ├── test_health.py
│   │   ├── test_main.py
│   │   ├── test_project_service.py
│   │   ├── test_ontology.py
│   │   ├── test_linter.py
│   │   └── [others].py
│   └── integration/                # Integration tests (with fixtures)
│       ├── test_projects_crud.py
│       ├── test_pull_requests.py
│       ├── test_ontology_operations.py
│       └── [others].py
│
├── alembic/                        # Database migrations
│   ├── env.py                      # Migration runner config
│   ├── script.py.mako              # Migration template
│   └── versions/                   # Individual migration files
│       ├── 001_initial_schema.py
│       ├── 002_add_ontology_index.py
│       └── [others].py
│
├── scripts/                        # Utility scripts
│   ├── migrate_to_bare_repos.py    # Migration script (GitPython → pygit2)
│   ├── prepare-release.py          # Strip -dev version suffix
│   └── set-version.py              # Set version for development builds
│
├── config/                         # External configuration files
│   └── zitadel/                    # Zitadel OIDC config examples
│
├── docs/                           # Documentation
│   └── plans/                      # GSD planning docs
│
├── pyproject.toml                  # Project metadata, dependencies, tool config
├── uv.lock                         # Dependency lock file
├── alembic.ini                     # Alembic configuration
├── compose.yaml                    # Docker compose for development
├── compose.prod.yaml               # Docker compose for production
├── Dockerfile                      # Dev image (with reload)
├── Dockerfile.prod                 # Prod image (optimized)
├── .env.example                    # Environment variable template
├── .pre-commit-config.yaml         # Pre-commit hooks (ruff, mypy, etc.)
├── Makefile                        # Build shortcuts
└── README.md                       # Project overview
```

## Directory Purposes

**ontokit/api/routes/**
- Purpose: HTTP endpoint handlers grouped by domain (projects, ontologies, PRs, etc.)
- Contains: FastAPI route functions with request/response mapping, authentication/authorization checks, dependency injection
- Key files: `__init__.py` (aggregates all routers), `projects.py` (most complex, handles CRUD + GitHub sync), `pull_requests.py` (PR workflow)

**ontokit/services/**
- Purpose: Business logic, domain operations, external API integration
- Contains: Service classes with async methods, orchestration logic, RDF/OWL operations, persistence
- Key files: `project_service.py` (project CRUD), `ontology.py` (RDF operations), `pull_request_service.py` (PR workflow), `linter.py` (validation rules)

**ontokit/models/**
- Purpose: SQLAlchemy ORM entity definitions
- Contains: Declarative models with relationships, constraints, indexes
- Key files: `project.py` (Project, ProjectMember), `pull_request.py` (PullRequest, Comment, Review), `lint.py` (LintRun, LintIssue)

**ontokit/schemas/**
- Purpose: Pydantic v2 request/response validation and serialization
- Contains: Strict validation schemas, computed fields, custom validators, response models
- Key files: `project.py`, `pull_request.py`, `ontology.py`, `owl_class.py`

**ontokit/core/**
- Purpose: Infrastructure, config, authentication, exception handling
- Contains: Settings loading, SQLAlchemy engine, JWT validation, custom exceptions, middleware
- Key files: `config.py` (environment-based settings), `database.py` (async session factory), `auth.py` (Zitadel JWT validation)

**ontokit/git/**
- Purpose: Git version control abstraction
- Contains: `BareOntologyRepository` class wrapping pygit2 for bare repository operations (no working directory)
- Key files: `bare_repository.py` (only meaningful file; pygit2 wrappers for branch, commit, merge, diff)

**ontokit/collab/**
- Purpose: Real-time collaboration (WebSocket support for concurrent editing)
- Contains: WebSocket message protocol, presence tracking, operational transform
- Key files: `protocol.py` (message format), `presence.py` (user activity), `transform.py` (conflict resolution)

**tests/**
- Purpose: Automated test suite
- Contains: Unit tests (isolated service logic), integration tests (with fixtures), mocks, factories
- Key files: `conftest.py` (pytest fixtures for db, services, mocks)

**alembic/**
- Purpose: Database schema versioning and migrations
- Contains: Migration scripts with up/down steps, declarative models tracking
- Key files: `versions/` directory with numbered migration files

## Key File Locations

**Entry Points:**
- `ontokit/main.py`: FastAPI app initialization, startup/shutdown, middleware, exception handlers
- `ontokit/runner.py`: CLI entry point, delegates to uvicorn
- `ontokit/worker.py`: ARQ background job processor

**Configuration:**
- `ontokit/core/config.py`: Settings via Pydantic (env vars, validation)
- `pyproject.toml`: Project metadata, dependencies, tool config (ruff, mypy, pytest)
- `.env.example`: Environment variable template

**Core Logic:**
- `ontokit/services/ontology.py`: RDF graph operations, class/property CRUD
- `ontokit/services/project_service.py`: Project CRUD, member management
- `ontokit/services/pull_request_service.py`: PR workflow, semantic diff
- `ontokit/services/linter.py`: 20+ semantic validation rules
- `ontokit/git/bare_repository.py`: pygit2 bare repository wrapper

**Testing:**
- `tests/conftest.py`: Pytest fixtures for database, services, authentication mocks
- `tests/unit/`: Isolated service/function tests
- `tests/integration/`: Tests with live fixtures (db, redis)

## Naming Conventions

**Files:**
- Service files: `{domain}_service.py` (e.g., `project_service.py`, `pull_request_service.py`)
- Model files: `{entity}.py` (e.g., `project.py`, `pull_request.py`)
- Route files: `{domain}.py` matching service domain (e.g., `projects.py`, `pull_requests.py`)
- Utility files: `{utility}_utils.py` or specific purpose (e.g., `rdf_utils.py`, `sitemap_notifier.py`)
- Schema files: `{entity}.py` or domain (e.g., `project.py`, `owl_class.py`)

**Directories:**
- Plural for collections: `services/`, `routes/`, `models/`, `schemas/`, `tests/`
- Singular for singleton services: `core/`, `git/`, `collab/`
- Provider modules: `{provider_type}_providers/` (e.g., `embedding_providers/`)

**Classes:**
- Service classes: `{Domain}Service` (e.g., `ProjectService`, `OntologyService`)
- Model classes: `{Entity}` matching table (e.g., `Project`, `PullRequest`)
- Schema classes: `{Entity}{Action}` for requests, `{Entity}Response` for responses (e.g., `ProjectCreate`, `ProjectResponse`)
- Repository/Data layer: `{Entity}Repository` (e.g., `BareOntologyRepository`)
- Exceptions: `{Error}Error` (e.g., `NotFoundError`, `ValidationError`)

**Functions:**
- Dependency getters: `get_{service_name}()` (e.g., `get_project_service()`, `get_db()`)
- Async functions: use `async def`
- Private/internal functions: prefix with `_` (e.g., `_resolve_ref()`, `_commit_to_info()`)

## Where to Add New Code

**New REST Endpoint:**
- Primary code: `ontokit/api/routes/{domain}.py` (create if needed)
- Service: `ontokit/services/{domain}_service.py` (create if needed)
- Models: `ontokit/models/{entity}.py` (add relationships to Project/PullRequest if applicable)
- Schema: `ontokit/schemas/{domain}.py` (define Request, Response schemas)
- Tests: `tests/integration/test_{domain}.py`

**New Service/Business Logic:**
- Implementation: `ontokit/services/{service_name}.py`
- Tests: `tests/unit/test_{service_name}.py` for isolated logic, `tests/integration/` for integration
- Expose via getter: Add `get_{service_name}()` function at bottom of file, import in routes via `Depends()`

**New Database Model/Entity:**
- Model definition: `ontokit/models/{entity}.py`
- Migration: `alembic revision --autogenerate -m "add {entity} table"`, then review generated migration
- Relationship updates: Add `relationship()` entries to related models
- Schema: `ontokit/schemas/{entity}.py` with Create, Response variants

**New Validation Rule (Linter):**
- Rule definition: Add `LintRuleInfo` entry to `LINT_RULES` list in `ontokit/services/linter.py`
- Check function: Add `def _check_{rule_id}(graph: Graph) -> list[LintResult]:` to linter class
- Register check: Call it in main lint loop

**New Embedding Provider:**
- Implementation: `ontokit/services/embedding_providers/{provider_name}.py`, inherit from `EmbeddingProvider` base class
- Register: Add factory branch in `get_embedding_provider()` function
- Config: Add env vars to `ontokit/core/config.py` (API key, model name, etc.)

**New Background Job:**
- Job function: Add async function to `ontokit/worker.py` with `async def {job_name}(ctx: dict[str, Any], **kwargs):`
- Enqueue: Call `get_arq_pool().enqueue('{job_name}', **kwargs)` from route
- Monitoring: Publish status updates to Redis channel (e.g., `lint:updates`)

## Special Directories

**alembic/versions/:**
- Purpose: Database schema versioning
- Generated: Yes (via `alembic revision --autogenerate`)
- Committed: Yes (tracked in git)
- Usage: Applied on startup by `alembic upgrade head` or triggered manually

**.env:**
- Purpose: Environment variable secrets and configuration
- Generated: No (created manually; template: `.env.example`)
- Committed: No (in .gitignore)
- Usage: Loaded by Pydantic Settings in `ontokit/core/config.py`

**.planning/codebase/:**
- Purpose: GSD planning documents (architecture, structure, conventions, concerns, etc.)
- Generated: Yes (by GSD agents)
- Committed: Yes (tracked in git)
- Usage: Consulted during planning and execution phases

**docs/plans/:**
- Purpose: GSD phase planning documents (one per feature/phase)
- Generated: Yes (by `/gsd:plan-phase`)
- Committed: Yes (tracked in git)
- Usage: Referenced during implementation

---

*Structure analysis: 2026-05-02*
