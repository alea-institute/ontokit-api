# Phase 3: Seed-on-Startup Public Projects — Context

**Gathered:** 2026-05-02
**Status:** Ready for planning

<domain>
## Phase Boundary

A fresh deploy with an empty `projects` table boots into a working public ontology browser. On first startup, the API automatically seeds two reference projects as public, view-only:

- **FOLIO** — Free Open Legal Information Ontology, sourced from `https://raw.githubusercontent.com/alea-institute/FOLIO/main/FOLIO.owl`
- **Catholic Semantic Canon** — sourced from `https://raw.githubusercontent.com/CatholicOS/ontology-semantic-canon/main/sources/ontology-semantic-canon.ttl`

Both projects are owned by a configurable system administrator (env-overridable, default `ontokit-system`), have `is_public=true`, and their bare git repos plus Postgres ontology indexes are populated as part of the seed. Idempotent across restarts. Source-fetch failures degrade gracefully — they log warnings and never block API startup.

In scope: server-side seed routine, env config, idempotency check, retry logic, integration with existing project-creation + ontology-indexing pipelines.

Out of scope: web-side single-project UX, scheduled refresh of seed contents (a future "admin re-seed" affordance can call the same service), seeding arbitrary user-supplied lists.

</domain>

<decisions>
## Implementation Decisions

### Seed routine location
- **D-01:** New module `ontokit/services/seed_service.py` exposes `async def seed_reference_projects(...) -> list[Project]` (and a per-source helper). Lifespan in `ontokit/main.py` awaits this after DB+Redis+MinIO are ready and before yielding control.
- **D-02:** The same service is the canonical "import OWL/Turtle URL → Project + git repo + Postgres index" path. PR #27's `scripts/seed-project.py` CLI calls into this service to avoid duplicating logic.

### Project owner
- **D-03:** Owner identity is configurable via env var. New setting: `SEED_OWNER_ID` (default: `ontokit-system`). Type: `str` (Zitadel-style user id).
- **D-04:** Operator overrides the default per-deployment if they want a real Zitadel admin user as owner instead of the synthetic `ontokit-system` id.
- **D-05:** Seeded projects have `is_public=true` and the standard role-based access from existing project membership semantics — anonymous viewers get read-only access automatically (no special-case code path).

### Idempotency + re-seeding policy
- **D-06:** Idempotency key is the project name (case-sensitive). Before seeding each reference, the service checks `SELECT id FROM projects WHERE name = :name`. If a row exists, that source is skipped with an INFO log line.
- **D-07:** No `is_seeded` flag, no `seed_runs` audit table — name-match is sufficient for v0.4.0. (A future audit/refresh story can add this without breaking existing deploys.)
- **D-08:** If the operator deletes a seeded project, the next boot recreates it (intentional re-seed). To opt out permanently, set `SEED_REFERENCE_PROJECTS=false`.

### Network failure handling
- **D-09:** Each source fetch uses exponential backoff: 3 attempts at 1s / 2s / 4s. Total worst-case delay per failed source: ~7s. After 3 failures, the source is logged as a `WARNING` (with the URL and last error) and skipped. Seeding continues with any sources that succeeded.
- **D-10:** Source-fetch failure is never fatal to startup. The API serves whatever seeds succeeded (or an empty `/api/v1/projects` if both failed). This satisfies STARTUP-SEED-08.
- **D-11:** Retries apply to the HTTP fetch step only. Parse errors (malformed RDF, e.g. RDFLib raises `BadSyntax`) fail immediately without retry — re-fetching won't fix bad content.

### Configuration surface
- **D-12:** New env vars (added to `ontokit/core/config.py` `Settings`):
  - `SEED_REFERENCE_PROJECTS: bool` (default: `True`) — master toggle
  - `SEED_OWNER_ID: str` (default: `"ontokit-system"`) — owner of seeded projects
  - `SEED_FOLIO_URL: str` (default: the GitHub raw URL above)
  - `SEED_FOLIO_NAME: str` (default: `"FOLIO"`)
  - `SEED_CANON_URL: str` (default: the GitHub raw URL above)
  - `SEED_CANON_NAME: str` (default: `"Catholic Semantic Canon"`)
- **D-13:** Default values of name + URL match the FOLIO and CSC repos as of 2026-05-02. Pinning to a commit SHA is left to operators via env override (per STARTUP-SEED-09).

### Claude's Discretion
- HTTP client choice (likely `httpx.AsyncClient` since the codebase already uses async patterns, but verify what's already in `services/`).
- Exact log message wording (follow existing `[ontokit] ...` stderr style from PR #138).
- Whether `seed_service` exposes one bulk `seed_reference_projects()` or two specific helpers `seed_folio()` / `seed_canon()` plus a public coordinator. Either works; pick whatever the planner finds cleaner.
- Where to plug into the existing OWL-import pipeline (`ontokit/services/ontology.py` + `ontology_index.py` + project_service). The planner should reuse PR #27's `scripts/seed-project.py` logic if it's a clean extraction.

</decisions>

<specifics>
## Specific Ideas

- The CLI script `scripts/seed-project.py` (added in PR #27) is the closest existing analog. The startup-seed should be a thinner wrapper: same pipeline, zero CLI args, two hard-coded defaults, idempotency guard.
- Lifespan log style: match PR #138's stderr-prefixed `[ontokit] Seeding reference projects...` / `[ontokit] Reference project FOLIO already exists, skipping` / `[ontokit] Seeded 2 reference projects`.
- "View-only for unauthenticated users" is achieved by `is_public=true` plus the existing `AUTH_MODE=optional` semantics from PR #27 — no new authorization code.

</specifics>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase requirements & roadmap
- `.planning/REQUIREMENTS.md` — STARTUP-SEED-01..10 are the falsifiable requirements for this phase
- `.planning/ROADMAP.md` §"Phase 3: Seed-on-Startup Public Projects" — goal, dependencies, success criteria
- `.planning/STATE.md` §"Open Questions for Phase 3" — original questions that drove this discussion
- `.planning/PROJECT.md` §"Current Milestone" + §"Key Decisions" — milestone context

### Codebase context
- `.planning/codebase/ARCHITECTURE.md` — service layering, dependency injection patterns, lifespan structure
- `.planning/codebase/STRUCTURE.md` — where new modules go (`ontokit/services/`)
- `.planning/codebase/CONVENTIONS.md` — async patterns, error handling, settings
- `.planning/codebase/TESTING.md` — pytest-asyncio fixtures, mocking patterns

### Files this phase touches (existing or to be created)
- `ontokit/main.py` — lifespan integration point (post-DB, post-MinIO, pre-yield)
- `ontokit/core/config.py` — new `Settings` fields per D-12
- `ontokit/services/seed_service.py` — **new** module
- `ontokit/services/ontology.py` — existing OWL/Turtle import + git + index pipeline (read-only reference)
- `ontokit/services/ontology_index.py` — Postgres index population (read-only reference)
- `ontokit/services/project_service.py` — project creation entry point (read-only reference)
- `scripts/seed-project.py` — PR #27 CLI script; the startup-seed should share the same underlying service
- `ontokit/models/project.py` — `Project` ORM (already has `is_public`, `owner_id` — no schema change needed)

### Cross-PR dependencies
- **PR #27 (rebased) — feat/seed-project-script branch on CatholicOS/ontokit-api.** Provides:
  - `AUTH_MODE` config (D-05 depends on `optional` semantics)
  - `scripts/seed-project.py` (D-02 reuses this pipeline)
  - Anonymous user infra (NOT used by D-03 — we use a system admin instead)
- **PR #138 — fix/startup-async-safety branch on CatholicOS/ontokit-api.** Provides:
  - `asyncio.wait_for` lifespan timeouts (informs how long the seed step is allowed to take)
  - Stderr log mirroring (D-09 log lines should follow this pattern)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`ontokit/services/ontology.py`** + `ontology_index.py` — established async pipeline for parsing RDF/OWL → bare git repo → Postgres index population. Seed routine should call into this exactly the way `scripts/seed-project.py` does.
- **`ontokit/services/project_service.py`** — has `create_project()` async helper. Use it; don't reimplement.
- **`ontokit/core/config.py` `Settings` class** — Pydantic-settings already wires env vars. Add the six new fields per D-12 with defaults; no other plumbing needed.
- **`ontokit/main.py` lifespan (post-PR #138)** — has bounded async sections with stderr mirror. Add a `_startup_print("Seeding reference projects...")` block after MinIO, before `yield`.

### Established Patterns
- **Async-first I/O** — every existing service method is `async`. Seed service must match. HTTP fetch via `httpx.AsyncClient` (not `requests`).
- **Pydantic v2 strict** — config values use `Settings` with type hints; existing pattern for validating env-var inputs.
- **Service singleton via factory** — convention is `def get_seed_service() -> SeedService: return SeedService()`. Match that.
- **Stderr-mirrored logging in lifespan** — PR #138 sets the precedent: `print("[ontokit] ...", file=sys.stderr, flush=True)` alongside `logger.info(...)`.

### Integration Points
- **Lifespan call site** — after MinIO check succeeds (or warns) and before `yield`, await `seed_service.seed_reference_projects(db, settings)`.
- **DB session** — lifespan currently doesn't open a long-lived session. Seed service either:
  - takes a session explicitly via dependency injection (cleaner); or
  - opens its own short-lived `AsyncSession` from the global `engine` (matches the lifespan style for the DB ping). Planner picks.
- **Bare git repo creation** — already handled by `ontokit/git/bare_repository.py`; reuse via `project_service.create_project()`.

### Tests
- **Add unit tests** for `seed_service` mocking the HTTP fetch + RDF parsing.
- **Add integration test** at `tests/integration/test_startup_seed.py` covering the four explicit scenarios in ROADMAP.md success criteria 4-5: idempotency on second boot, `SEED_REFERENCE_PROJECTS=false` skip, source-fetch 404 graceful degradation.

</code_context>

<deferred>
## Deferred Ideas

- **Admin "refresh seed" route** — `POST /admin/seeds/refresh` calling the same service. Not v0.4.0; surface in v0.5.0 if there's demand.
- **Audit table tracking when each source was seeded with which commit SHA** — useful for compliance/reproducibility but not v0.4.0; add to backlog.
- **Web-side single-project auto-redirect UX** — if there's only one project visible to the user, skip the project list and land on its editor. Belongs in the ontokit-web roadmap.
- **Auto-update reference ontologies on a schedule** — out of scope; users can re-seed via Sync-from-Remote if needed.
- **Seeding arbitrary user-supplied lists of ontologies** — generalizing this from "two named refs" to "list of (name, url) pairs in env" is overkill for v0.4.0.

</deferred>

---

*Phase: 03-seed-on-startup-public-projects*
*Context gathered: 2026-05-02*
