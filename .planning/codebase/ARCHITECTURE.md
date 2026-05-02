<!-- refreshed: 2026-05-02 -->
# Architecture

**Analysis Date:** 2026-05-02

## System Overview

OntoKit API is a **layered, async-first FastAPI application** built around semantic web ontology management with Git-based version control. It separates concerns across five distinct layers: REST endpoints, business logic services, data models, domain models (RDF/OWL), and infrastructure (database, cache, storage).

```text
┌─────────────────────────────────────────────────────────────────┐
│                  HTTP Request / Response                         │
├──────────────────────────────────────────────────────────────────┤
│                    REST API Routes Layer                          │
│  `ontokit/api/routes/` — 19 endpoint modules (projects, PR, etc) │
│  Response handlers, content negotiation (Turtle/RDF+XML/JSON-LD)│
└────────────────────────────────┬────────────────────────────────┘
                                  │
┌─────────────────────────────────▼────────────────────────────────┐
│              Business Logic Services Layer                        │
│  `ontokit/services/` — Domain operations (ontology, linting,     │
│  pull_request, git, embeddings, quality analysis, etc.)          │
│  Dependency injection via FastAPI Depends()                      │
└────────────────────────────────┬────────────────────────────────┘
                                  │
         ┌────────────────────────┼────────────────────────────┐
         │                        │                            │
         ▼                        ▼                            ▼
┌──────────────────────┐  ┌────────────────────┐  ┌──────────────────┐
│ ORM Models Layer     │  │ RDF/OWL Graphs     │  │ Background Tasks │
│ `ontokit/models/`    │  │ RDFLib + OWLReady2 │  │ `ontokit/worker` │
│ SQLAlchemy (async)   │  │ Graph operations   │  │ ARQ Job Queue    │
│ PostgreSQL relations │  │ Diff, linting      │  │ (Redis-backed)   │
└──────────────────────┘  └────────────────────┘  └──────────────────┘
         │                        │                            │
         └────────────────────────┼────────────────────────────┘
                                  │
┌─────────────────────────────────▼────────────────────────────────┐
│                 Infrastructure Layer                             │
├──────────────────────────────────────────────────────────────────┤
│  Database:  PostgreSQL (async via asyncpg + SQLAlchemy 2.0)     │
│  Cache:     Redis (pub/sub, session storage)                    │
│  Storage:   MinIO (S3-compatible ontology file storage)          │
│  Git:       Bare repos via pygit2 (concurrent, no workdir)       │
│  Auth:      Zitadel (OIDC/OAuth2, JWT validation)              │
└──────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| REST Routes | HTTP endpoint handlers, content negotiation, request/response mapping | `ontokit/api/routes/*.py` |
| Project Service | Project CRUD, member management, ownership transfer, GitHub sync | `ontokit/services/project_service.py` |
| Ontology Service | RDF graph operations, class/property CRUD, serialization, content negotiation | `ontokit/services/ontology.py` |
| Linter Service | 20+ semantic validation rules, issue detection and reporting | `ontokit/services/linter.py` |
| Pull Request Service | PR workflow, branch management, git merges, semantic diffs | `ontokit/services/pull_request_service.py` |
| Git Service | Bare repository operations (branches, commits, diffs), pygit2 wrapper | `ontokit/git/bare_repository.py` |
| Storage Service | MinIO S3 integration for ontology file storage | `ontokit/services/storage.py` |
| Embedding Service | Vector embedding generation and similarity search | `ontokit/services/embedding_service.py` |
| Normalization Service | Format conversion, turtle canonicalization, background jobs | `ontokit/services/normalization_service.py` |
| Quality Service | Duplicate detection, consistency checking, cross-references | `ontokit/services/consistency_service.py`, `ontokit/services/duplicate_detection_service.py` |
| Database Models | SQLAlchemy ORM definitions for projects, users, PRs, lint, embeddings | `ontokit/models/*.py` |
| Schemas | Pydantic v2 request/response schemas with validation | `ontokit/schemas/*.py` |
| Auth | Zitadel OIDC token validation, current user resolution | `ontokit/core/auth.py` |
| Config | Environment-based settings with validation | `ontokit/core/config.py` |
| Worker | ARQ background job processor for lint, normalization, embeddings | `ontokit/worker.py` |

## Pattern Overview

**Overall:** Hexagonal/Layered architecture with clean separation between HTTP boundary (routes) and domain logic (services). Each service is a **self-contained business logic unit** that can be tested independently. Services accept dependency-injected collaborators (database sessions, git service, storage) rather than creating them internally.

**Key Characteristics:**
- **Async-first**: All I/O (database, Redis, S3, git) uses async/await, enabled by `asyncpg`, `aioredis`, etc.
- **Dependency injection via FastAPI Depends()**: Services receive collaborators at route level, enabling flexible composition and testing
- **Bare repositories**: Git operations use pygit2 with bare repos (no working directory), allowing concurrent access by multiple users on different branches
- **PostgreSQL-first ORM**: SQLAlchemy 2.0 async with proper type hints and relationships for referential integrity
- **Pydantic v2 validation**: Strict request validation with computed fields and custom validators
- **Semantic RDF/OWL operations**: RDFLib 7.1+ for graph manipulation; OWLReady2 for ontology-specific logic

## Layers

**HTTP / FastAPI Entry Point:**
- Purpose: Request/response handling, authentication, rate limiting, middleware
- Location: `ontokit/main.py`, `ontokit/api/routes/`
- Contains: Endpoint handlers, content negotiation, exception mapping
- Depends on: Service layer (via Depends()), auth (JWT validation), middleware
- Used by: HTTP clients (web UI, CLI, external integrations)

**Service / Business Logic Layer:**
- Purpose: Domain operations (CRUD, validation, orchestration, external API calls)
- Location: `ontokit/services/`
- Contains: Service classes with business methods, external API integration (GitHub, embedding providers), RDF/OWL logic
- Depends on: Database (SQLAlchemy session), Git (pygit2), Storage (MinIO), external APIs (Zitadel, GitHub, embedding providers)
- Used by: Routes, other services (nested composition), background workers

**ORM / Data Models Layer:**
- Purpose: Persistent entity definitions, relationships, constraints
- Location: `ontokit/models/`
- Contains: SQLAlchemy declarative models (Project, PullRequest, LintRun, etc.) with relationships and indexes
- Depends on: SQLAlchemy, database driver (asyncpg)
- Used by: Services (loading/persisting entities), migrations (Alembic)

**RDF / OWL / Domain Models:**
- Purpose: In-memory graph representation, semantic operations (class hierarchy, property reasoning)
- Location: Distributed across services (ontology.py, linter.py, quality services)
- Contains: RDFLib Graph operations, SPARQL queries, OWL reasoning
- Depends on: RDFLib, OWLReady2, external ontology files (stored in MinIO)
- Used by: Services for semantic analysis, endpoint content negotiation

**Infrastructure / External Systems:**
- Purpose: Provide persistent storage, caching, task queuing, authentication
- Location: `ontokit/core/` (config, database, auth), `ontokit/git/` (repository), `ontokit/services/storage.py`
- Contains: Database engine, Redis connection, MinIO client, JWT validation, Zitadel integration
- Depends on: External systems (PostgreSQL, Redis, MinIO, Zitadel)
- Used by: All services, routes, worker

## Data Flow

### Primary Request Path: Create/Update Ontology Class

1. **Client sends HTTP POST** (`/api/v1/projects/{project_id}/ontologies/{ontology_id}/classes`)
2. **Route handler** (`ontokit/api/routes/classes.py:create_class`) receives request, parses OWLClassCreate schema
3. **Route injects service** (`IndexedOntologyService` via `Depends()`)
4. **Service orchestrates** (`ontokit/services/indexed_ontology.py:create_class`):
   - Load current ontology from git (`BareOntologyRepository.read_file()`)
   - Parse RDF graph with RDFLib
   - Create new class with URIRef, label, annotation properties
   - Validate against linter rules
   - Serialize back to Turtle
5. **Service persists to git** (`BareOntologyRepository.write_file(branch, filepath, content, message)`)
   - Creates git blob from Turtle bytes
   - Updates git tree
   - Creates commit with author info
   - Returns CommitInfo
6. **Service updates database** (SQLAlchemy session):
   - Record change event to `change_events` table for analytics
   - Update ontology_index with new class metadata
7. **Route returns OWLClassResponse** with IRI, label, created timestamp
8. **Client receives 201 Created** with response body

### Background Job Path: Lint Run

1. **Route receives** `POST /api/v1/projects/{project_id}/lint/run`
2. **Route enqueues ARQ job** (`get_arq_pool().enqueue('run_lint_job', project_id)`)
3. **Worker process** picks up job from Redis queue
4. **Worker function** (`ontokit/worker.py:run_lint_job`) executes:
   - Load project from DB
   - Read ontology file from git
   - Instantiate linter with graph
   - Run 20+ semantic checks (missing labels, circular hierarchies, undefined parents, etc.)
   - Build LintResult list
5. **Worker persists results** to `lint_runs` and `lint_issues` tables
6. **Worker publishes Redis event** (`lint:updates` channel) for real-time UI updates
7. **Route polls or subscribes** to updates, returns LintRun summary

### Collaborative Editing Path: Pull Request Merge

1. **Route receives** `POST /api/v1/projects/{project_id}/pull-requests/{pr_id}/merge`
2. **Route calls** `PullRequestService.merge(pr_id, user)`
3. **Service loads** PullRequest and GitHubIntegration from DB
4. **Service invokes git** (`BareOntologyRepository.merge_branches()`):
   - Resolves feature branch commits
   - Computes semantic diff using RDFLib graph diff
   - Performs git merge with conflict detection
   - If conflicts, returns MergeResult with conflict list
5. **Service updates PR status** in DB (`pr.status = PRStatus.MERGED`)
6. **Service publishes event** to Redis (`pull_requests:updates`)
7. **If GitHub synced**, service calls `sync_to_github()` to push merge upstream
8. **Service records change event** for analytics

## Key Abstractions

**BareOntologyRepository:**
- Purpose: Git operations without working directory (enables concurrent access)
- Examples: `ontokit/git/bare_repository.py:BareOntologyRepository`
- Pattern: Direct git object manipulation via pygit2 (blobs, trees, commits)

**IndexedOntologyService:**
- Purpose: Dual-backed entity lookup (PostgreSQL index + RDFLib fallback)
- Examples: `ontokit/services/indexed_ontology.py`
- Pattern: Try SQL index first for performance, fall back to graph traversal if needed

**RDF Graph Diff:**
- Purpose: Semantic diff (not text diff) for PRs and change tracking
- Examples: `ontokit/services/pull_request_service.py`, uses `rdflib.compare.graph_diff()`
- Pattern: Compute isomorphic graphs, extract added/removed triples

**Service Singletons via Dependency Injection:**
- Purpose: Lazy initialization of expensive resources (e.g., OntologyService instantiates RDFLib Graph on first use)
- Examples: `get_storage_service()`, `get_ontology_service()`, `get_git_service()`
- Pattern: Functions that return service instance, called via FastAPI Depends()

## Entry Points

**HTTP Server:**
- Location: `ontokit/main.py:app` (FastAPI instance)
- Triggers: `uvicorn ontokit.main:app --reload` or `ontokit --reload` (CLI runner)
- Responsibilities: Lifespan (startup/shutdown), middleware, router registration, exception handling
- Startup: Verifies DB, Redis, MinIO; initializes storage bucket

**CLI Runner:**
- Location: `ontokit/runner.py:main()`
- Triggers: `ontokit --reload` (entry point in pyproject.toml)
- Responsibilities: Delegates to uvicorn, handles --reload flag
- Startup: Passes through to uvicorn, which imports `ontokit.main:app`

**Background Worker:**
- Location: `ontokit/worker.py` (ARQ worker definition)
- Triggers: `arq ontokit.worker.settings` (ARQ CLI)
- Responsibilities: Enqueues background jobs (lint, normalization, GitHub sync, embeddings)
- Startup: Connects to Redis, waits for job queue

## Architectural Constraints

- **Async everywhere**: All I/O must use async/await. Blocking calls (e.g., `requests.get()`) will block the event loop. Use `httpx.AsyncClient` instead.
- **Module-level state for services**: Service singletons (e.g., `OntologyService`, `StorageService`) are instantiated once per request via dependency injection. Avoid module-level state outside of config.
- **No global mutable state except redis_pool**: `ontokit/main.py` defines a module-level `redis_pool` for access by other modules. All other mutable state should be request-scoped or transaction-scoped.
- **Session-per-request database pattern**: Each route receives a fresh `AsyncSession` via `Depends(get_db)`. The session is closed after the request completes.
- **Bare repository requirement**: Git operations must use `BareOntologyRepository` (pygit2 with bare repos). Working-directory repos are not supported for concurrent access.
- **Pydantic v2 strict validation**: All request/response bodies are validated strictly with `Pydantic v2`. Invalid data raises `ValidationError`, which maps to HTTP 422.
- **Type hints required**: MyPy strict mode is enabled. All functions must have full type hints, including return types and nested generics.

## Anti-Patterns

### Database Session Leaks

**What happens:** Service methods accept `db: AsyncSession` parameter but fail to close it or let the caller manage it.

**Why it's wrong:** If an exception occurs, the session may not be returned to the pool, causing connection exhaustion.

**Do this instead:** Follow the **session-per-request** pattern. Routes create the session via `Depends(get_db)`, and FastAPI automatically closes it. Services receive the session and trust the caller to manage its lifecycle. Example:

```python
# Route: FastAPI manages session lifetime
async def create_project(
    db: Annotated[AsyncSession, Depends(get_db)],
    ...
) -> ProjectResponse:
    service = ProjectService(db)  # Pass session to service
    return await service.create(...)

# Service: Trusts caller to manage session
class ProjectService:
    def __init__(self, db: AsyncSession):
        self.db = db
    async def create(self, ...):
        self.db.add(...)
        await self.db.commit()  # Service commits, route closes session
```

### Synchronous RDF Operations in Async Context

**What happens:** Service calls `graph.serialize()` or `list(graph.subjects())` in an async method without wrapping in `asyncio.to_thread()`.

**Why it's wrong:** RDFLib operations block the event loop, preventing other requests from being serviced.

**Do this instead:** Use `asyncio.to_thread()` for CPU-bound RDF operations:

```python
# Good
graph_data = await asyncio.to_thread(graph.serialize, format='turtle')

# Bad
graph_data = graph.serialize(format='turtle')  # Blocks event loop
```

### Storing Passwords or Tokens in Logs

**What happens:** Debug logs include `github_token`, `api_key`, or other secrets from environment or database.

**Why it's wrong:** Logs may be retained in monitoring systems, exposing secrets.

**Do this instead:** Never log sensitive data. Mask tokens in error messages:

```python
# Good
logger.info(f"Using token ending in {token[-8:]}")

# Bad
logger.info(f"Using token: {token}")
```

### Circular Dependencies Between Services

**What happens:** ServiceA imports ServiceB, ServiceB imports ServiceA, at module level.

**Why it's wrong:** Python cannot resolve circular imports at load time, causing AttributeError.

**Do this instead:** Use late imports inside functions or pass dependencies as parameters:

```python
# Good: Dependency injection in constructor
class ServiceA:
    def __init__(self, service_b: ServiceB):
        self.service_b = service_b

# Good: Late import in method
def get_service():
    from ontokit.services.other import OtherService
    return OtherService()

# Bad: Module-level circular import
from ontokit.services.b import ServiceB  # If b.py imports from a.py, fails
```

### Forgetting to Await Async Calls

**What happens:** Service calls `await db.execute()` but forgets the `await`, returning a coroutine object instead of the result.

**Why it's wrong:** The query never runs, and the coroutine is abandoned, leaving the caller with None or a coroutine object.

**Do this instead:** Always `await` async function calls:

```python
# Good
result = await session.execute(select(Project).where(...))
project = result.scalar_one_or_none()

# Bad
result = session.execute(select(...))  # Returns coroutine, not result
```

## Error Handling

**Strategy:** Exceptions are raised as domain-specific custom exceptions (`NotFoundError`, `ValidationError`, `ConflictError`, `ForbiddenError`), caught by FastAPI exception handlers in `main.py`, and converted to JSON error responses.

**Patterns:**
- **Custom domain exceptions** (`ontokit/core/exceptions.py`) are raised by services for domain errors (project not found, ontology validation failed, insufficient permissions).
- **HTTP exceptions** (FastAPI's `HTTPException`) are avoided in services; instead, domain exceptions are mapped by handlers in `main.py`.
- **Validation errors** are raised by Pydantic during schema parsing; FastAPI's default handler converts them to 422 responses.
- **Database errors** (e.g., IntegrityError) are caught in services and mapped to `ConflictError` (409) or `ValidationError` (422) as appropriate.

## Cross-Cutting Concerns

**Logging:** Configured at `ontokit/core/logging.py` (if exists) or via `logging.getLogger(__name__)` in each module. Structured logging with `structlog` or plain Python logging; avoid logging secrets.

**Validation:** Pydantic v2 schemas validate all request bodies. Custom validators (Pydantic v2 @field_validator or @model_validator) enforce domain rules (e.g., project name length, IRI format).

**Authentication:** Zitadel OIDC JWT tokens are validated by `ontokit/core/auth.py:get_current_user()`. Public endpoints use `OptionalUser` (may be None); private endpoints use `RequiredUser` (raises 401 if missing).

**Authorization:** Services check `CurrentUser.id` against database ownership/membership records. No RBAC framework; simple ownership checks or role lookups in DB.

**Rate Limiting:** Applied globally via `slowapi` middleware in `main.py` (100 requests/minute per IP). Custom rate limit rules can be added via `@limiter.limit()` decorator on routes.

---

*Architecture analysis: 2026-05-02*
