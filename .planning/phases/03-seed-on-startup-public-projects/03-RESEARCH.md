# Phase 3: Seed-on-Startup Public Projects — Research

**Researched:** 2026-05-02
**Domain:** FastAPI lifespan integration, idempotent service-layer seeding, async HTTP fetch with retry, RDF parsing, pygit2 bare-repo init, Postgres ontology indexing
**Confidence:** HIGH

## Summary

Every building block needed for Phase 3 already exists in the codebase. The seed routine is a thin orchestrator that:

1. Reads two `(name, url)` pairs from `Settings`.
2. For each pair, runs an idempotency check (`SELECT id FROM projects WHERE name = :name`).
3. Fetches the source bytes with `httpx.AsyncClient` and an explicit 1s/2s/4s exponential backoff over 3 attempts (matching D-09 verbatim).
4. Parses with `rdflib.Graph().parse(...)` to validate; parse errors are non-retryable (D-11).
5. Persists by reusing the existing pipeline established in `scripts/seed-project.py` from `upstream/feat/seed-project-script` (PR #27): create `Project` + `ProjectMember` rows, init bare git repo via `BareGitRepositoryService.initialize_repository(...)`, and `OntologyIndexService.full_reindex(...)` against the parsed graph.
6. Logs successes/skips/failures with the `[ontokit] ...` stderr-mirrored pattern that PR #138 (`upstream/fix/startup-async-safety`) established in lifespan.

The only net-new module is `ontokit/services/seed_service.py`. Six new fields are added to `ontokit/core/config.py:Settings`. Lifespan in `ontokit/main.py` gets one additional `_startup_print` block between MinIO and `yield`. **Total new code surface is small (≈250–400 LOC) and the integration risk is low because every collaborator (project_service, ontology_index, bare_repository, OntologyMetadataExtractor) is already exercised by tests.**

**Primary recommendation:** Build `seed_service.py` as a stateless service that opens its own short-lived `AsyncSession` per source via `async_session_maker()`. The seed CLI in PR #27 is the de-facto reference implementation — port its logic into the service, then have the CLI shell out to the service (D-02). Use `httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))` per attempt; wrap the whole seed routine in `asyncio.wait_for` with a generous bound (~60s per source) to keep the lifespan recoverable if a network stall outlasts httpx's own timeouts.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Seed routine location**
- **D-01:** New module `ontokit/services/seed_service.py` exposes `async def seed_reference_projects(...) -> list[Project]` (and a per-source helper). Lifespan in `ontokit/main.py` awaits this after DB+Redis+MinIO are ready and before yielding control.
- **D-02:** The same service is the canonical "import OWL/Turtle URL → Project + git repo + Postgres index" path. PR #27's `scripts/seed-project.py` CLI calls into this service to avoid duplicating logic.

**Project owner**
- **D-03:** Owner identity is configurable via env var. New setting: `SEED_OWNER_ID` (default: `ontokit-system`). Type: `str` (Zitadel-style user id).
- **D-04:** Operator overrides the default per-deployment if they want a real Zitadel admin user as owner instead of the synthetic `ontokit-system` id.
- **D-05:** Seeded projects have `is_public=true` and the standard role-based access from existing project membership semantics — anonymous viewers get read-only access automatically (no special-case code path).

**Idempotency + re-seeding policy**
- **D-06:** Idempotency key is the project name (case-sensitive). Before seeding each reference, the service checks `SELECT id FROM projects WHERE name = :name`. If a row exists, that source is skipped with an INFO log line.
- **D-07:** No `is_seeded` flag, no `seed_runs` audit table — name-match is sufficient for v0.4.0. (A future audit/refresh story can add this without breaking existing deploys.)
- **D-08:** If the operator deletes a seeded project, the next boot recreates it (intentional re-seed). To opt out permanently, set `SEED_REFERENCE_PROJECTS=false`.

**Network failure handling**
- **D-09:** Each source fetch uses exponential backoff: 3 attempts at 1s / 2s / 4s. Total worst-case delay per failed source: ~7s. After 3 failures, the source is logged as a `WARNING` (with the URL and last error) and skipped. Seeding continues with any sources that succeeded.
- **D-10:** Source-fetch failure is never fatal to startup. The API serves whatever seeds succeeded (or an empty `/api/v1/projects` if both failed). This satisfies STARTUP-SEED-08.
- **D-11:** Retries apply to the HTTP fetch step only. Parse errors (malformed RDF, e.g. RDFLib raises `BadSyntax`) fail immediately without retry — re-fetching won't fix bad content.

**Configuration surface**
- **D-12:** New env vars in `ontokit/core/config.py:Settings`:
  - `SEED_REFERENCE_PROJECTS: bool` (default: `True`)
  - `SEED_OWNER_ID: str` (default: `"ontokit-system"`)
  - `SEED_FOLIO_URL: str` (default: GitHub raw URL above)
  - `SEED_FOLIO_NAME: str` (default: `"FOLIO"`)
  - `SEED_CANON_URL: str` (default: GitHub raw URL above)
  - `SEED_CANON_NAME: str` (default: `"Catholic Semantic Canon"`)
- **D-13:** Default name + URL match FOLIO and CSC repos as of 2026-05-02. Pinning to commit SHA is left to operators via env override.

### Claude's Discretion

- HTTP client choice (likely `httpx.AsyncClient` since codebase already uses async patterns; verify what's already in `services/`).
- Exact log message wording (follow existing `[ontokit] ...` stderr style from PR #138).
- Whether `seed_service` exposes one bulk `seed_reference_projects()` or two specific helpers `seed_folio()` / `seed_canon()` plus a public coordinator.
- Where to plug into the existing OWL-import pipeline. Reuse PR #27's `scripts/seed-project.py` logic if it's a clean extraction.

### Deferred Ideas (OUT OF SCOPE)

- Admin "refresh seed" route (`POST /admin/seeds/refresh`).
- Audit table tracking when each source was seeded with which commit SHA.
- Web-side single-project auto-redirect UX.
- Auto-update reference ontologies on a schedule.
- Seeding arbitrary user-supplied lists of ontologies (generalizing beyond two named refs).

</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| STARTUP-SEED-01 | On startup, when DB has zero projects, seed two reference projects as public, view-only | Lifespan integration (§Architecture Patterns); idempotency-by-name (D-06) preserves zero-projects behavior implicitly — name-match returns "skip" if anything exists |
| STARTUP-SEED-02 | One seeded project is FOLIO from `https://raw.githubusercontent.com/alea-institute/FOLIO/main/FOLIO.owl` | Default for `SEED_FOLIO_URL` per D-12; format auto-detected as `xml` from `.owl` extension (matches PR #27 CLI logic) |
| STARTUP-SEED-03 | One seeded project is Catholic Semantic Canon from `https://raw.githubusercontent.com/CatholicOS/ontology-semantic-canon/main/sources/ontology-semantic-canon.ttl` | Default for `SEED_CANON_URL` per D-12; format auto-detected as `turtle` from `.ttl` extension |
| STARTUP-SEED-04 | Seeded projects owned by anonymous user OR configurable system user, `is_public=true`, read-only for anonymous viewers | `SEED_OWNER_ID` (D-03/D-04). `Project.owner_id: str` accepts arbitrary string. Read-only access via existing `is_public=True` branch in `ProjectService.list_accessible` (`access_clause = Project.is_public == True` for `user is None`) |
| STARTUP-SEED-05 | Each seeded project gets bare git repo, initial commit, Postgres ontology index populated | `BareGitRepositoryService.initialize_repository()` creates bare repo + initial commit; `OntologyIndexService.full_reindex(project_id, branch, graph, commit_hash)` populates index — both tested and used by PR #27 CLI |
| STARTUP-SEED-06 | Idempotent: skip by name (D-06) | `SELECT id FROM projects WHERE name = :name` → if row, log skip and return existing |
| STARTUP-SEED-07 | Disable via `SEED_REFERENCE_PROJECTS=false` | Top-level early-return guard in seed entry point (D-12) |
| STARTUP-SEED-08 | Source-fetch failures logged but never block startup | Per-source try/except with WARNING log; lifespan never re-raises (D-10). `asyncio.wait_for` ceiling adds belt-and-suspenders timeout |
| STARTUP-SEED-09 | URLs and names configurable via env vars | Six new `Settings` fields (D-12); operators pin commit SHAs by overriding the URL env var |
| STARTUP-SEED-10 | Seeded projects display in `/api/v1/projects` for both authenticated and anonymous users | No new code path — existing `ProjectService.list_accessible` already returns `is_public=True` projects to anonymous callers; verified in `tests/integration/test_projects_crud.py` |

</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|--------------|----------------|-----------|
| Read seed config from env | core/config | — | Pydantic-settings is the single source of truth for env vars |
| Toggle seeding on/off | core/config + main lifespan | — | `settings.seed_reference_projects` checked at lifespan entry point |
| Coordinate idempotency + retry + parse + persist | services/seed_service | — | Service orchestrates; no HTTP boundary involved |
| HTTP fetch with retry | services/seed_service (helper) | — | Outbound HTTP via `httpx.AsyncClient` consistent with other services |
| RDF parsing | services/seed_service (delegates to existing pipeline) | services/ontology_extractor | `OntologyMetadataExtractor.extract_metadata()` is the existing canonical parse entrypoint and surfaces `OntologyParseError` cleanly |
| Project row insert + member row insert | services/seed_service | models/project | Uses ORM directly (mirrors PR #27 CLI which uses raw SQL); ORM keeps SQLAlchemy session semantics |
| Bare git repo init + first commit | git/bare_repository | — | `BareGitRepositoryService.initialize_repository()` is the established API |
| Postgres ontology index population | services/ontology_index | — | `OntologyIndexService.full_reindex(...)` is already the canonical path |
| Lifespan progress logging | main.py + Python logging | — | Stderr `_startup_print` + `logger.info` mirror (PR #138 pattern) |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `httpx` | `>=0.28.0` (declared in pyproject.toml) | Async HTTP fetch with timeout + structured exceptions | [VERIFIED: pyproject.toml + grep] Already used in `core/auth.py`, `services/github_service.py`, `services/user_service.py`, `services/sitemap_notifier.py`, `services/embedding_providers/openai_provider.py` — the codebase has zero `requests` or `aiohttp` calls. Adding a different client would violate convention. |
| `rdflib` | `>=7.1.0` (declared in pyproject.toml) | Parse OWL/RDF/Turtle/N3 source bytes into a `Graph` for indexing | [VERIFIED: pyproject.toml] Used everywhere in `services/ontology.py`, `ontology_index.py`, `linter.py` |
| `pydantic-settings` | `>=2.6.0,<2.11` (declared in pyproject.toml) | Env-var driven `Settings` class | [VERIFIED: pyproject.toml] Existing `Settings` class is the only config mechanism |
| `SQLAlchemy` async | `>=2.0` (project standard) | Insert `Project` + `ProjectMember` rows in same transaction | [VERIFIED: codebase grep] Mirrors `ProjectService.create_from_import` |
| `pygit2` | `>=1.13.0` (declared in pyproject.toml) | Bare git repo init + initial commit (via `BareGitRepositoryService`) | [VERIFIED: pyproject.toml + STRUCTURE.md] |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `asyncio` (stdlib) | — | `wait_for` ceiling + `sleep` for backoff | Wrap whole seed call site to keep lifespan bounded; `asyncio.sleep(delay)` between retry attempts |
| `logging` (stdlib) | — | Structured INFO/WARNING logs | Module-level `logger = logging.getLogger(__name__)` per CONVENTIONS.md |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `httpx.AsyncClient` raw retry loop | `tenacity` or `httpx-retries` | [ASSUMED tradeoff text] Adds new dep for ~10 lines of retry logic; explicit `for attempt in range(3): ... await asyncio.sleep(2**attempt)` is more legible and matches D-09's specific cadence (1s/2s/4s) without configuration gymnastics. **Recommendation: do NOT add a new dep.** |
| ARQ background job for seeding | Seed via `arq` on first request | [ASSUMED tradeoff text] Defers seeding past startup, breaks STARTUP-SEED-01's "on startup" semantics, and means an empty `/api/v1/projects` response could happen even on a healthy boot. Lifespan-bounded synchronous-ish seeding (with `asyncio.wait_for`) is what the success criteria require. |
| Raw SQL `text(...)` inserts (PR #27 CLI style) | `Project()` + `ProjectMember()` ORM | The CLI in PR #27 uses raw SQL because it predates the full project_service refactor. The service-layer path (`ProjectService.create_from_import`) uses ORM models. Use ORM for consistency. |

**Installation:** No new packages required. Every dep is already declared in `pyproject.toml`.

**Version verification:** Skipping `npm view` — these are Python deps, all declared with version pins in pyproject.toml. The pins were last reviewed at PR-merge time and are within current major-version stable releases.

## Architecture Patterns

### System Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                  uvicorn boot                            │
│  (or `ontokit --reload` via runner.py)                   │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│            FastAPI lifespan (main.py)                    │
│                                                          │
│  1. _startup_print("Connecting to database...")          │
│  2. await asyncio.wait_for(DB ping, timeout=20)         │
│  3. await Redis ping (optional)                          │
│  4. await asyncio.wait_for(MinIO bucket, timeout=15)    │
│  5. ┌──────────────────────────────────────┐            │
│     │ NEW: if settings.seed_reference_projects: │       │
│     │   await asyncio.wait_for(                 │       │
│     │     seed_reference_projects(),            │       │
│     │     timeout=60                            │       │
│     │   )  # WARN+continue on TimeoutError      │       │
│     └──────────────────────────────────────┘            │
│  6. yield                                                │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│       services/seed_service.py (NEW)                     │
│                                                          │
│  seed_reference_projects(settings, session_maker):       │
│   ├─ for each (name, url) in [(folio), (canon)]:        │
│   │    ├─ _seed_one_source(name, url, owner_id, ...)    │
│   │    │    ├─ idempotency check (SELECT ... WHERE name)│
│   │    │    │     └─ skip → INFO log                    │
│   │    │    ├─ _fetch_with_retry(url)                   │
│   │    │    │     ├─ try 3x (1s, 2s, 4s backoff)        │
│   │    │    │     └─ all fail → WARNING + return None   │
│   │    │    ├─ rdflib.Graph().parse(content, format)    │
│   │    │    │     └─ BadSyntax → WARNING + return None  │
│   │    │    │       (no retry per D-11)                 │
│   │    │    ├─ Insert Project + ProjectMember (owner)   │
│   │    │    ├─ git_service.initialize_repository(...)   │
│   │    │    ├─ index_service.full_reindex(...)          │
│   │    │    └─ commit                                   │
│   │    └─ catch-all: WARNING + continue (per D-10)      │
│   └─ return list[Project]                               │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│   Existing collaborators (no changes)                    │
│                                                          │
│  • git/bare_repository.py                                │
│      BareGitRepositoryService.initialize_repository()    │
│  • services/ontology_index.py                            │
│      OntologyIndexService.full_reindex()                 │
│  • models/project.py: Project, ProjectMember             │
│  • core/database.py: async_session_maker                 │
└─────────────────────────────────────────────────────────┘
```

### Recommended Project Structure

```
ontokit/
├── main.py                       # MODIFY: add seed call to lifespan
├── core/
│   └── config.py                 # MODIFY: add 6 Settings fields
├── services/
│   └── seed_service.py           # NEW: SeedService class + helpers
└── git/, models/, ...            # UNCHANGED: reuse as-is

tests/
├── unit/
│   └── test_seed_service.py      # NEW: mocked HTTP/DB unit tests
└── integration/
    └── test_startup_seed.py      # NEW: lifespan-driven integration tests
```

### Pattern 1: Service factory (matching CONVENTIONS.md)

**What:** Module-level `get_*` factory function returns a service instance for use either via DI or direct call from lifespan.
**When to use:** Always for new services. Matches `get_project_service`, `get_ontology_service`, `get_storage_service` pattern.
**Example:**
```python
# Source: ontokit/services/project_service.py:1226-1228 [VERIFIED]
def get_project_service(db: AsyncSession) -> ProjectService:
    """Factory function for dependency injection."""
    return ProjectService(db)
```

For seed service, since it manages its own session lifecycle (no DI from a route), the factory takes no args:
```python
def get_seed_service() -> SeedService:
    return SeedService()
```

### Pattern 2: Stderr-mirrored lifespan logging

**What:** Print to stderr alongside structured logger to ensure visibility in container/Railway logs before logging pipeline configures.
**When to use:** All lifespan progress messages.
**Example:**
```python
# Source: ontokit/main.py (post-PR #138, on upstream/fix/startup-async-safety) [VERIFIED]
def _startup_print(message: str) -> None:
    print(f"[ontokit] {message}", file=sys.stderr, flush=True)

# Usage:
_startup_print("Seeding reference projects...")
logger.info("Seeding reference projects")
```

### Pattern 3: `asyncio.wait_for` lifespan ceiling

**What:** Wrap each lifespan I/O block in `asyncio.wait_for(coroutine, timeout=N)` so a stuck dependency can't hang boot.
**When to use:** Any lifespan I/O. Phase 3 should add a `timeout=60` ceiling around the seed call.
**Example:**
```python
# Source: ontokit/main.py post-PR #138 lines 53-61 [VERIFIED]
try:
    async with engine.connect() as conn:
        await asyncio.wait_for(conn.execute(text("SELECT 1")), timeout=20.0)
except TimeoutError:
    _startup_print("Database connection timed out after 20s")
    raise
```

### Pattern 4: Self-managed AsyncSession (lifespan-style)

**What:** Open a short-lived `AsyncSession` from `async_session_maker()` inside the service when no DI session is available.
**When to use:** Lifespan-callable services (no Request scope). Each `_seed_one_source` opens its own session so failures in one source don't roll back another's commit.
**Example:**
```python
# Source: ontokit/core/database.py:28-30 [VERIFIED]
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Seed service usage (NEW, follows PR #27 CLI pattern):
async with async_session_maker() as session:
    # ... do all work for one source ...
    await session.commit()
```

### Pattern 5: HTTP retry with explicit backoff (D-09)

**What:** Plain Python `for attempt in range(3)` loop with `asyncio.sleep` between attempts.
**When to use:** D-09 specifies 3 attempts at 1s/2s/4s — encode this exactly.
**Example:**
```python
# Source: D-09 + httpx async docs [CITED: context7.com/encode/httpx]
async def _fetch_with_retry(url: str) -> bytes | None:
    delays = [1.0, 2.0, 4.0]
    last_err: Exception | None = None
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt, delay in enumerate(delays, start=1):
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.content
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_err = exc
                if attempt < len(delays):
                    await asyncio.sleep(delay)
    logger.warning("Failed to fetch %s after 3 attempts: %s", url, last_err)
    return None
```

Note: D-09 reads "1s/2s/4s × 3" which most naturally maps to "delay BEFORE attempt N" (so total worst-case ~7s). The implementation above uses delays AFTER each failed attempt; if read as "delay BEFORE", the math is identical: sleep 1, try, sleep 2, try, sleep 4, try → total ~7s wall clock. The planner should pick a precise interpretation and write it in the task description.

### Anti-Patterns to Avoid

- **Calling `requests.get()` or any sync HTTP client:** Blocks the event loop. ARCHITECTURE.md §"Async-first" forbids this. Always `httpx.AsyncClient`.
- **Re-using one DB session across both sources:** If FOLIO succeeds and CSC fails, you don't want CSC's rollback to evict FOLIO. Open one session per source.
- **Catching parse errors and retrying:** Parse errors are deterministic. Per D-11, retries are HTTP-only.
- **Letting seeding raise into lifespan:** Per D-10, seed-service exceptions must be caught at the lifespan call site and logged, not propagated. The API must boot regardless.
- **Hard-coding seed identity in tests:** Use `monkeypatch.setenv("SEED_OWNER_ID", "test-owner")` or override `Settings` in tests; never assume `ontokit-system` is the test owner.
- **Reading `settings.seed_reference_projects` inside the inner loop:** Read once at lifespan entry; the toggle controls the whole routine, not per-source.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Bare git repo init + first commit | `subprocess.run(["git", "init", "--bare", ...])` (the PR #27 CLI does this) | `BareGitRepositoryService.initialize_repository(project_id, content, filename, ...)` | The service is the established API used by `ProjectService.create_from_import` and is already covered by tests in `tests/unit/test_bare_repository_service.py`. The CLI's subprocess approach predates the bare-repo refactor and shouldn't be ported into a long-running async process — it forks shells per call and leaks tempdirs on error paths. |
| RDF/OWL parsing of arbitrary URL bodies | Custom format-detection or `urllib.request.urlopen` + manual `Graph()` | `OntologyMetadataExtractor.extract_metadata(content, filename)` then `Graph().parse(data=content, format=...)` directly when only the graph is needed | `OntologyMetadataExtractor` already handles `.owl` (xml), `.ttl`, `.rdf`, `.n3`, `.jsonld` and surfaces `UnsupportedFormatError` / `OntologyParseError`. Using its detection logic keeps seed behavior identical to user-facing imports. |
| Postgres ontology index population | Walking the rdflib Graph and inserting `IndexedEntity` rows by hand | `OntologyIndexService.full_reindex(project_id, branch, graph, commit_hash)` | One canonical entry point, batched inserts (BATCH_SIZE=1000), status row management, and concurrency guard against re-indexing race. |
| HTTP retry/backoff | Custom retry library (`tenacity`, `backoff`) | Plain `for attempt in range(3): try: ... except: await asyncio.sleep(...)` | D-09 specifies an exact cadence (1s/2s/4s, 3 attempts). Adding a new dep would obscure intent. ~10 LOC of explicit code is clearer. |
| Pydantic env var loading | `os.getenv()` | Add fields to `Settings` class | `Settings` already centralises env handling, type coercion (`bool`, `str`), and `.env` loading. Anything outside `Settings` won't be picked up by `.env` and breaks the CONFIGURATION convention. |

**Key insight:** Phase 3 is plumbing, not algorithms. Every hard part — bare repo init, RDF parsing, index population, project ORM — is already a well-tested function. The seed service should be the world's smallest orchestrator on top of those. **If the seed_service.py grows past ~250 lines, that's a smell that you've duplicated logic that already lives elsewhere.**

## Runtime State Inventory

> Phase 3 is greenfield (new module, additive Settings, additive lifespan block). Most categories are not relevant. Documenting explicitly per discipline.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — verified via grep for "FOLIO", "Catholic Semantic Canon", "ontokit-system" in codebase. No prior data exists keyed on these names. The first deploy will be the first to populate them. | None |
| Live service config | None — fresh deploys have no n8n / Datadog / Tailscale / Cloudflare wiring tied to seeded project names. | None |
| OS-registered state | None — no Windows Task Scheduler, launchd, or systemd touchpoints. The seed runs inside the FastAPI process. | None |
| Secrets/env vars | Six new env vars added (D-12). Operators may set these in `.env` or container envs. **Action:** ensure `.env.example` documents all six. | Update `.env.example` |
| Build artifacts | None — no compiled artifacts depend on seed names. | None |

## Common Pitfalls

### Pitfall 1: Missing `auth_mode` field on current branch

**What goes wrong:** The current `feat/seed-folio-public-project` branch is off `upstream/main`. `Settings.auth_mode` does NOT yet exist on this branch — it lives on PR #27 (`upstream/feat/seed-project-script`). If the planner assumes `auth_mode=optional` is already wired, anonymous browsing will not work.
**Why it happens:** Phase dependencies in ROADMAP.md state Phase 3 depends on Phase 1 (PR #27). The user is building Phase 3 against `feat/seed-folio-public-project` while PR #27 is still in flight upstream.
**How to avoid:** The plan should state explicitly: Phase 3 makes the seed routine work, but anonymous read access to seeded projects requires PR #27 to land. Don't add an `auth_mode` field in this phase. Test STARTUP-SEED-10 with an authenticated read; defer the anonymous-read test to a follow-up integration once PR #27 merges.
**Warning signs:** A test asserting `GET /api/v1/projects` (no auth) returns the seeded project will fail with 401 in absence of PR #27's `optional`/`disabled` mode wiring.

### Pitfall 2: rdflib `parse()` is blocking and CPU-bound

**What goes wrong:** Calling `Graph().parse(data=content, format="xml")` synchronously inside an async function blocks the event loop. FOLIO is a large OWL file (~MB), parse can take seconds.
**Why it happens:** rdflib has no async API.
**How to avoid:** Wrap parse in `asyncio.to_thread(graph.parse, data=content, format=fmt)` per ARCHITECTURE.md §"Synchronous RDF Operations in Async Context".
**Warning signs:** Other async tasks (Redis ping during startup, health checks if seed runs after `yield` somehow) stall during seed parse.

### Pitfall 3: Bare repo path collision on re-seed after delete

**What goes wrong:** Operator deletes a seeded project via UI/API. Per `ProjectService.delete`, `git_service.delete_repository(project_id)` removes the bare repo. On next boot, seed creates a new project with a NEW UUID and a NEW bare repo path. **But:** if `delete_repository` previously failed (e.g., disk error), the next seed will collide if the new project's UUID happens to match. Probabilistically near-zero, but worth noting.
**Why it happens:** Bare repo path is `${GIT_REPOS_BASE_PATH}/{project_id}.git`. New `uuid4()` per re-seed.
**How to avoid:** `BareOntologyRepository.write_file` to a fresh project_id should always succeed. If `git_service.repository_exists(project_id)` returns True for a brand-new UUID, treat as a hard error — something is wrong with on-disk state.
**Warning signs:** "Repository already exists" error from pygit2 during seed.

### Pitfall 4: `FOLIO.owl` content type and format auto-detection

**What goes wrong:** GitHub raw URLs return `text/plain` Content-Type even for `.owl` files. If the parser is fed `format="text/plain"` it errors.
**Why it happens:** Default `httpx` doesn't know about `.owl`. Auto-detection by URL suffix is more reliable than HTTP header.
**How to avoid:** Map URL suffix → rdflib format explicitly:
- `.owl` / `.rdf` → `xml`
- `.ttl` → `turtle`
- `.n3` → `n3`
- `.jsonld` → `json-ld`

This matches the PR #27 CLI's `args.owl_format` plus extension-fallback logic.
**Warning signs:** `rdflib.plugin.PluginException: No plugin registered for ('text/plain', <class 'rdflib.parser.Parser'>)`.

### Pitfall 5: `expire_on_commit=False` requires explicit refresh for `Project.id`

**What goes wrong:** After `session.add(db_project)` and `await session.commit()`, you might assume `db_project.id` is populated. With `expire_on_commit=False`, the attribute is populated only after the implicit flush during `add` if `Project.id` has a Python-side default — which it does (`default=uuid.uuid4`). So `db_project.id` IS populated immediately after `add()`. **But:** `created_at` (server default `now()`) is NOT populated until refresh.
**Why it happens:** `core/database.py:30` sets `expire_on_commit=False`.
**How to avoid:** Explicitly `await session.refresh(db_project)` after commit if you need server-side fields. The bare-repo init only needs `db_project.id`, which is fine — but the index task call also only needs `id` + branch + graph, so no refresh required.
**Warning signs:** `created_at` is `None` in returned `Project` object.

### Pitfall 6: `httpx.AsyncClient()` per-request reconstruction

**What goes wrong:** Wrapping each retry attempt in its own `async with httpx.AsyncClient()` block tears down + rebuilds connection pools. Wasteful but not wrong.
**Why it happens:** Idiomatic-looking code.
**How to avoid:** Construct one `AsyncClient` per `_fetch_with_retry` call (i.e., one per source URL), with timeout config. The 3 attempts share that client.
**Warning signs:** Extra DNS lookups, slow startup with sources behind slow DNS.

## Code Examples

### Adding `Settings` fields (D-12)

```python
# Source: ontokit/core/config.py — append before `class Config:` [VERIFIED pattern]
class Settings(BaseSettings):
    # ... existing fields ...

    # Seed-on-Startup Public Projects
    seed_reference_projects: bool = True
    seed_owner_id: str = "ontokit-system"
    seed_folio_url: str = "https://raw.githubusercontent.com/alea-institute/FOLIO/main/FOLIO.owl"
    seed_folio_name: str = "FOLIO"
    seed_canon_url: str = (
        "https://raw.githubusercontent.com/CatholicOS/"
        "ontology-semantic-canon/main/sources/ontology-semantic-canon.ttl"
    )
    seed_canon_name: str = "Catholic Semantic Canon"
```

`pydantic-settings` automatically reads `SEED_REFERENCE_PROJECTS`, `SEED_OWNER_ID`, etc. from env (case-insensitive per `case_sensitive=False`).

### Lifespan integration

```python
# Source: pattern from ontokit/main.py post-PR #138 [VERIFIED]
# Add AFTER the MinIO block, BEFORE `_startup_print("Startup complete")`

if settings.seed_reference_projects:
    _startup_print("Seeding reference projects...")
    try:
        from ontokit.services.seed_service import seed_reference_projects

        seeded = await asyncio.wait_for(seed_reference_projects(), timeout=60.0)
        names = ", ".join(p.name for p in seeded) if seeded else "(none)"
        _startup_print(f"Seeded {len(seeded)} reference projects: {names}")
        logger.info("Seeded %d reference projects: %s", len(seeded), names)
    except TimeoutError:
        _startup_print("Reference seeding timed out after 60s — continuing startup")
        logger.warning("Reference seeding timed out after 60s — continuing startup")
    except Exception:
        _startup_print("Reference seeding failed — continuing startup")
        logger.exception("Reference seeding failed — continuing startup")
else:
    _startup_print("Reference seeding disabled (SEED_REFERENCE_PROJECTS=false)")
```

### Seed service skeleton

```python
# Source: NEW module ontokit/services/seed_service.py
"""Seed reference public projects (FOLIO, Catholic Semantic Canon) on first boot."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from rdflib import Graph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import Settings, settings as default_settings
from ontokit.core.database import async_session_maker
from ontokit.git import get_git_service
from ontokit.models.project import Project, ProjectMember
from ontokit.services.ontology_index import OntologyIndexService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedSource:
    name: str
    url: str
    rdflib_format: str  # "xml" | "turtle" | "n3" | "json-ld"
    filename: str       # name to use inside the bare repo, e.g. "FOLIO.owl"


def _format_for(url: str) -> tuple[str, str]:
    """Return (rdflib_format, filename) from a URL."""
    filename = url.rsplit("/", 1)[-1] or "ontology.owl"
    lower = filename.lower()
    if lower.endswith((".ttl",)):
        return "turtle", filename
    if lower.endswith((".n3",)):
        return "n3", filename
    if lower.endswith((".jsonld",)):
        return "json-ld", filename
    # default: xml (covers .owl, .rdf, .xml)
    return "xml", filename


async def _fetch_with_retry(url: str) -> bytes | None:
    """Fetch URL with 3 attempts at 1s/2s/4s backoff. Returns None if all attempts fail."""
    delays = [1.0, 2.0, 4.0]
    last_err: Exception | None = None
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for attempt, delay in enumerate(delays, start=1):
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.content
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_err = exc
                logger.info("Seed fetch attempt %d/%d for %s failed: %s", attempt, len(delays), url, exc)
                if attempt < len(delays):
                    await asyncio.sleep(delay)
    logger.warning("Failed to fetch seed source %s after 3 attempts: %s", url, last_err)
    return None


async def _seed_one(source: SeedSource, owner_id: str) -> Project | None:
    """Seed a single source. Returns the Project if newly created, None if skipped/failed."""
    async with async_session_maker() as session:
        # 1. Idempotency check (D-06)
        existing = await session.execute(select(Project).where(Project.name == source.name))
        if existing.scalar_one_or_none() is not None:
            logger.info("Reference project %r already exists, skipping", source.name)
            return None

        # 2. Fetch (with retry; D-09)
        content = await _fetch_with_retry(source.url)
        if content is None:
            return None  # already logged as WARNING

        # 3. Parse (no retry; D-11)
        try:
            graph = await asyncio.to_thread(_parse_graph, content, source.rdflib_format)
        except Exception as exc:
            logger.warning("Failed to parse seed source %s: %s", source.url, exc)
            return None

        # 4. Insert Project + owner ProjectMember
        project = Project(
            name=source.name,
            description=f"Reference ontology seeded from {source.url}",
            is_public=True,
            owner_id=owner_id,
            source_file_path=source.filename,
        )
        session.add(project)
        await session.flush()
        session.add(ProjectMember(project_id=project.id, user_id=owner_id, role="owner"))
        await session.commit()
        await session.refresh(project)

        # 5. Bare git repo + initial commit
        git_service = get_git_service()
        commit_info = await asyncio.to_thread(
            git_service.initialize_repository,
            project.id,
            content,
            source.filename,
            "OntoKit Seed",
            "seed@ontokit.local",
            source.name,
        )

        # 6. Postgres ontology index
        index_service = OntologyIndexService(session)
        await index_service.full_reindex(project.id, "main", graph, commit_info.hash)

        logger.info("Seeded reference project %s (id=%s, %d entities)",
                    source.name, project.id, len(graph))
        return project


def _parse_graph(content: bytes, rdflib_format: str) -> Graph:
    g = Graph()
    g.parse(data=content, format=rdflib_format)
    return g


async def seed_reference_projects(cfg: Settings | None = None) -> list[Project]:
    """Top-level seed entry point. Idempotent. Never raises (logs and continues)."""
    cfg = cfg or default_settings
    sources = []
    for name, url in [(cfg.seed_folio_name, cfg.seed_folio_url),
                      (cfg.seed_canon_name, cfg.seed_canon_url)]:
        fmt, filename = _format_for(url)
        sources.append(SeedSource(name=name, url=url, rdflib_format=fmt, filename=filename))

    seeded: list[Project] = []
    for source in sources:
        try:
            project = await _seed_one(source, cfg.seed_owner_id)
            if project is not None:
                seeded.append(project)
        except Exception:
            logger.exception("Unexpected error seeding %s — continuing", source.name)
    return seeded
```

This is illustrative; the planner may decompose differently (e.g., a `SeedService` class). The key invariants are: (1) per-source isolation via separate sessions, (2) HTTP retry with exact 1s/2s/4s cadence, (3) parse error = no retry, (4) all exceptions logged, none re-raised.

### Test fixture for graph parsing

```python
# Source: tests/conftest.py:163-172 [VERIFIED]
@pytest.fixture
def sample_graph(sample_ontology_turtle: str) -> Graph:
    graph = Graph()
    graph.parse(data=sample_ontology_turtle, format="turtle")
    return graph
```

### Mocking httpx in async unit tests

```python
# Source: standard pytest-httpx pattern + codebase precedent in
# tests/unit/test_github_service.py [CITED: codebase pattern]
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_fetch_with_retry_succeeds_on_third_attempt() -> None:
    """Returns content after 2 failures and 1 success."""
    mock_responses = [
        httpx.ConnectError("first"),
        httpx.ReadTimeout("second"),
        AsyncMock(content=b"<rdf>...</rdf>", raise_for_status=lambda: None),
    ]
    # Patch httpx.AsyncClient.get to yield the sequence
    ...
```

The codebase doesn't currently use `pytest-httpx`. Recommend adding it as a `[dev]` dep OR mocking via `unittest.mock.patch("httpx.AsyncClient.get", ...)`. **Don't make a real HTTP call in tests.**

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Working-directory git via `subprocess` (used by PR #27 CLI) | Bare git via `pygit2.BareGitRepositoryService` | 2025-Q4 (per migrate_to_bare_repos.py) | Required for concurrent multi-user editing; mandatory for new code per ARCHITECTURE.md |
| Synchronous `urllib.request.urlopen` (PR #27 CLI line 234) | `httpx.AsyncClient` | Codebase convention | Async-first; non-blocking |
| `requests` library | `httpx` | Project standard | Zero `requests` calls in `ontokit/` (verified by grep) |
| Raw SQL `text("INSERT ...")` for project rows (PR #27 CLI) | ORM `Project()` + `session.add()` | Project_service refactor | Type-safe, relationship-aware, consistent with rest of codebase |

**Deprecated/outdated:**
- `ontokit/git/repository.py` (legacy GitPython) is deprecated per CLAUDE.md. Don't import from it.
- The PR #27 CLI's `subprocess` git approach should NOT be ported into the long-running async process. Refactor PR #27's CLI later to call into the new service per D-02; that's a follow-up, not a Phase 3 task.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The seed service should run inline in lifespan (not enqueued to ARQ) | Architecture Patterns | Low — D-01 explicitly states lifespan integration. ARQ alternative was rejected by the user. |
| A2 | "1s/2s/4s × 3" in D-09 means 3 attempts with backoff between attempts; total ~7s wall clock | Pattern 5 | Low — interpretation is consistent with "3 attempts" wording; planner should pin in the task description |
| A3 | rdflib parse errors should be caught broadly (not just `BadSyntax`) — D-11 mentions BadSyntax as an example | _seed_one error handling | Low — overly-narrow except clauses miss `xml.sax.SAXParseException`, `rdflib.plugin.PluginException`, etc. Catching `Exception` then logging is safer. |
| A4 | The seed service can use the ORM (not raw SQL like the PR #27 CLI) | Don't Hand-Roll table | Low — ORM is the established service-layer pattern; raw SQL was a CLI concession |
| A5 | `seed@ontokit.local` is an acceptable git author email for seed commits | Code example | Trivial — operator can override later via env var if desired; not in the v0.4.0 surface |
| A6 | Anonymous browsing of seeded projects is gated on PR #27 landing | Pitfall 1 | High — STARTUP-SEED-10 will fail if tested with no auth before PR #27 lands. Plan should explicitly note this dependency. |

## Open Questions

1. **Should `tests/integration/test_startup_seed.py` start the actual lifespan, or unit-test `seed_reference_projects()` directly with a real DB and HTTP mocks?**
   - What we know: `tests/conftest.py` uses `TestClient(app)` which DOES trigger lifespan. `tests/unit/conftest.py` has `authed_client` for DI overrides.
   - What's unclear: There's no existing precedent for asserting on lifespan side effects. Existing lifespan tests don't exist.
   - Recommendation: Test `seed_reference_projects()` directly as a unit/integration test (call the function, assert DB state). For the lifespan path, add a thin "calls seed if enabled" integration test using `TestClient(app)` with `monkeypatch.setenv("SEED_REFERENCE_PROJECTS", "false")` to verify the toggle works without real network.

2. **Does FOLIO have an `rdfs:label` or `dc:title` that the existing `OntologyMetadataExtractor` would extract as the project name, possibly conflicting with `SEED_FOLIO_NAME`?**
   - What we know: `ProjectService.create_from_import` uses extracted metadata as project name unless `name_override` is set.
   - What's unclear: The seed service skips `OntologyMetadataExtractor` entirely and uses `SEED_FOLIO_NAME` directly — this is correct behavior per D-12.
   - Recommendation: Don't call `OntologyMetadataExtractor`. The env-supplied name IS the canonical name (it's the idempotency key). Comment this in the seed service to prevent future "helpful" refactors.

3. **What should happen if `OntologyIndexService.full_reindex` fails but the Project + bare repo are already created?**
   - What we know: `full_reindex` updates a status row to `failed` on exception.
   - What's unclear: Should the seed service then delete the half-created project to keep boot idempotent? Or leave it (next boot will skip by name, leaving the broken state)?
   - Recommendation: **Leave it** — the index status row records the failure, and the project still exists with a valid bare repo. An operator can re-trigger indexing via the existing ARQ task or admin route. Aggressive cleanup risks data loss if the failure was transient.

## Environment Availability

> Phase 3 needs network egress to GitHub raw-content URLs at boot. List the dependencies that must be available in the runtime environment.

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Postgres | Project insert + index | ✓ (project mandatory infra) | 17 | None — DB is required for ANY startup, not just seed |
| Outbound HTTPS to `raw.githubusercontent.com` | `_fetch_with_retry` | Assumed yes for typical Railway/AWS deploys | — | Per D-10, fetch failure is logged and seed-skipped; API still boots |
| `git_repos_base_path` writable | Bare repo init | ✓ (Project requirement) | — | None — required for any project creation |
| Python 3.11+ | All | ✓ | 3.11 | None |
| `httpx` 0.28+ | HTTP fetch | ✓ (declared) | 0.28+ | None |
| `rdflib` 7.1+ | Parse | ✓ (declared) | 7.1+ | None |
| `pygit2` 1.13+ | Bare repo | ✓ (declared) | 1.13+ | None |

**Missing dependencies with no fallback:** None for the build itself. **The default GitHub URLs assume internet egress** — operators in air-gapped deploys must either (a) override URLs to internal mirrors via env vars, or (b) set `SEED_REFERENCE_PROJECTS=false`.

**Missing dependencies with fallback:** The two source URLs themselves. D-09 + D-10 specify: 3 retries → WARNING → continue. Per-source failure is the only "missing" dep with a fallback (skip).

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 8.3.0+ with pytest-asyncio (`asyncio_mode=auto`) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `pytest tests/unit/test_seed_service.py -v` |
| Full suite command | `pytest tests/ -v --cov=ontokit --cov-report=term-missing` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| STARTUP-SEED-01 | On first boot, both reference projects are created | integration | `pytest tests/integration/test_startup_seed.py::test_seeds_two_projects_on_empty_db -x` | ❌ Wave 0 |
| STARTUP-SEED-02 | FOLIO project name + URL match defaults | unit | `pytest tests/unit/test_seed_service.py::test_default_seed_sources_include_folio -x` | ❌ Wave 0 |
| STARTUP-SEED-03 | CSC project name + URL match defaults | unit | `pytest tests/unit/test_seed_service.py::test_default_seed_sources_include_canon -x` | ❌ Wave 0 |
| STARTUP-SEED-04 | Seeded projects: `is_public=True`, `owner_id == settings.seed_owner_id`, owner ProjectMember row exists | integration | `pytest tests/integration/test_startup_seed.py::test_seeded_projects_are_public_with_configured_owner -x` | ❌ Wave 0 |
| STARTUP-SEED-05 | Bare git repo exists at `${GIT_REPOS_BASE_PATH}/{project_id}.git` with one commit; ontology_index row count matches graph entity count | integration | `pytest tests/integration/test_startup_seed.py::test_seed_creates_bare_repo_and_index -x` | ❌ Wave 0 |
| STARTUP-SEED-06 | Second call to `seed_reference_projects()` returns empty list and adds no new rows | integration | `pytest tests/integration/test_startup_seed.py::test_seed_is_idempotent -x` | ❌ Wave 0 |
| STARTUP-SEED-07 | With `SEED_REFERENCE_PROJECTS=false`, seed function is a no-op (or lifespan never calls it) | unit + integration | `pytest tests/unit/test_seed_service.py::test_disabled_via_settings -x` AND `pytest tests/integration/test_startup_seed.py::test_lifespan_skips_seed_when_disabled -x` | ❌ Wave 0 |
| STARTUP-SEED-08 | HTTP 404 → no project created, WARNING logged, seed function returns successfully | unit | `pytest tests/unit/test_seed_service.py::test_fetch_404_logs_warning_and_skips -x` | ❌ Wave 0 |
| STARTUP-SEED-09 | Overriding `SEED_FOLIO_URL` causes that URL to be fetched | unit | `pytest tests/unit/test_seed_service.py::test_url_env_override_used -x` | ❌ Wave 0 |
| STARTUP-SEED-10 | After seeding, `GET /api/v1/projects` (authenticated) returns both projects | integration | `pytest tests/integration/test_startup_seed.py::test_seeded_projects_appear_in_list -x` | ❌ Wave 0 |
| (anonymous variant of -10) | Anonymous `GET /api/v1/projects` returns both projects | integration | Same file, different test | ❌ Wave 0 — **deferred until PR #27 lands and `auth_mode=optional` is available** |

### Sampling Rate

- **Per task commit:** `pytest tests/unit/test_seed_service.py -v` (~5s)
- **Per wave merge:** `pytest tests/unit/test_seed_service.py tests/integration/test_startup_seed.py -v` (~30s)
- **Phase gate:** Full suite green before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `tests/unit/test_seed_service.py` — covers STARTUP-SEED-02, -03, -07 (unit), -08, -09
- [ ] `tests/integration/test_startup_seed.py` — covers STARTUP-SEED-01, -04, -05, -06, -07 (integration), -10
- [ ] Possibly add `pytest-httpx` to dev deps — OR use `unittest.mock.patch` to stub `httpx.AsyncClient.get`. Existing tests use the latter pattern (see `test_github_service.py`).
- [ ] `tests/integration/conftest.py` already exists; check if a "fresh DB per test" fixture exists or needs adding for idempotency tests.

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | Seed runs in-process during lifespan; no auth boundary |
| V3 Session Management | no | No sessions involved |
| V4 Access Control | yes (indirect) | Seeded projects use `is_public=True` and rely on existing access-control in `ProjectService.list_accessible` and `_can_view`. No new auth code added in this phase. |
| V5 Input Validation | yes | `Settings` field types coerce env input; URL validity is checked implicitly by `httpx.get` raising on malformed; rdflib parse validates RDF structure |
| V6 Cryptography | no | No secrets handled; no encryption needed |
| V10 Malicious Code | yes (low) | Source URLs are operator-configurable. An operator pointing `SEED_FOLIO_URL` at a malicious endpoint could feed arbitrary RDF into the system. Mitigation: rdflib parse rejects non-RDF; validate `Content-Length` reasonable bounds (e.g., reject > 50 MB). |
| V12 Files & Resources | yes | Bare repo creation is filesystem I/O. Path is `${GIT_REPOS_BASE_PATH}/{uuid}.git` — UUID is server-generated, no user-controlled path component. Safe. |
| V13 API & Web Service | no | No new API endpoints in this phase |

### Known Threat Patterns for FastAPI + async services

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| URL pointing at SSRF target (`SEED_*_URL` overridden to internal IP) | I (Information disclosure) | Documented operator-configurable risk. v0.4.0 doesn't add an SSRF allowlist — the env var is by design operator-controlled, not user-controlled at runtime. Note in `.env.example`. |
| RDF parser DoS via deeply-nested or huge file | D (Denial of service) | Add a Content-Length / response-size cap on `_fetch_with_retry` (e.g., 50 MB). rdflib has known parse-time blowups on pathological inputs. The `asyncio.wait_for(60s)` ceiling on the seed call site bounds total damage. |
| HTTP redirect to file:// or javascript: | I/T | `httpx.AsyncClient(follow_redirects=True)` follows only http/https by default. Verify with Context7 if uncertain. [ASSUMED based on httpx docs] |
| Compromised seed source corrupts ontology_index | T (Tampering) | Seeded projects are flagged `is_public=True`. Consumers should not treat them as canonical sources of truth without their own validation. Recommend documenting this in a future README section. |

**Recommendation for the planner:** Add an explicit max-bytes cap to `_fetch_with_retry` (e.g., abort if `Content-Length` > 50 MB OR if streamed bytes exceed 50 MB). This is a small additional task that meaningfully reduces parse-DoS risk.

## Sources

### Primary (HIGH confidence)
- Codebase grep — `/home/damienriehl/Coding Projects/ontokit-api/ontokit/main.py` (current state of lifespan)
- Codebase grep — `upstream/fix/startup-async-safety:ontokit/main.py` (PR #138 lifespan with stderr mirror + `wait_for`)
- Codebase grep — `upstream/feat/seed-project-script:scripts/seed-project.py` (reference seed pipeline)
- `ontokit/services/project_service.py:55-230` — `create()` and `create_from_import()` are the canonical project-creation paths
- `ontokit/services/ontology_index.py:80-189` — `OntologyIndexService.full_reindex` API
- `ontokit/git/bare_repository.py:890-923` — `BareGitRepositoryService.initialize_repository` API
- `ontokit/core/database.py:28-30` — `async_session_maker` factory
- `ontokit/core/config.py` — current `Settings` class (no `auth_mode` yet on this branch)
- `pyproject.toml:27,28,35,40` — declared versions for pydantic-settings, rdflib, httpx, pygit2
- `.planning/codebase/{ARCHITECTURE,STRUCTURE,CONVENTIONS,TESTING}.md` — repo conventions
- Context7 `/encode/httpx` — httpx async timeout + exception handling patterns

### Secondary (MEDIUM confidence)
- Inferred from CONTEXT.md `<code_context>` — PR #138 stderr-log style and PR #27 anonymous-user assumptions

### Tertiary (LOW confidence)
- None — no claims rest on unverified WebSearch results in this research.

## Project Constraints (from CLAUDE.md)

Extracted directives that the planner must honor:

- **Testing after edits:** Run `pytest tests/ -v --cov=ontokit` after code changes (per CLAUDE.md "Commands § Testing")
- **Linting:** `ruff check ontokit/ --fix` and `ruff format ontokit/` after changes
- **Type checking:** `mypy ontokit/` (strict mode)
- **Line length:** 100 chars (Ruff config in pyproject.toml)
- **Async-first:** All I/O must be async (CLAUDE.md "Key Patterns")
- **UTC datetime:** Use `datetime.now(UTC)` not naive datetime
- **Service layering:** New service goes in `ontokit/services/seed_service.py` (CLAUDE.md "Layer Structure")
- **Pydantic v2 strict:** `Settings` updates use Pydantic v2 conventions
- **Bare repos only:** Never use `ontokit/git/repository.py` (legacy GitPython, deprecated)
- **No new dependencies without approval:** All required deps already exist in `pyproject.toml`. If the planner finds a need for `pytest-httpx` (test dep), this needs a CLAUDE.md "What Still Requires My Approval" check — flag it.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every dep is verified in pyproject.toml; codebase already uses httpx, rdflib, pygit2 extensively
- Architecture: HIGH — lifespan, config, service patterns are all directly observed in the current and PR #138 source
- Pitfalls: HIGH for #1, #2, #4, #5; MEDIUM for #3 (rare path), #6 (cosmetic)
- Test mapping: HIGH — requirements decompose cleanly to known test patterns
- Security: MEDIUM — V10 (malicious URL) is operator-configurable risk; recommended Content-Length cap is best-practice but not strictly required by phase requirements

**Research date:** 2026-05-02
**Valid until:** 2026-06-01 (30 days; codebase is stable, but PR #27 / PR #138 land could shift the integration surface)

---

*Research: 2026-05-02 by gsd-researcher*
