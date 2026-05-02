# Roadmap: OntoKit API

## Milestones

- ✅ **v0.1.0–v0.3.0** — Core API, auth, git, lint, index, embeddings, entity graph (shipped)
- 🚧 **v0.4.0 Public Onboarding & Operational Readiness** — Phases 1-3 (in progress)

## Phases

### 🚧 v0.4.0 Public Onboarding & Operational Readiness (In Progress)

**Milestone Goal:** Make the API trivially deployable as a standalone ontology browser (zero-config, view-only, anonymous-friendly) seeded with FOLIO and the Catholic Semantic Canon, while keeping all existing CatholicOS multi-tenant capabilities intact. Close the gap with ontokit-web v0.3.0 (which already shipped against features still landing on `main`) and add first-boot seeding of both reference ontologies.

- [ ] **Phase 1: Auth Modes, Anonymous Stack & Seed CLI** — Land PR #27: AUTH_MODE config, anonymous user infra, anonymous suggestion endpoints, seed-project CLI, and translation/synonym surfacing in the index path
- [ ] **Phase 2: Startup Robustness** — Land PR #138: async-safe MinIO, bounded lifespan timeouts, stderr log mirroring for Railway/container deploys
- [ ] **Phase 3: Seed-on-Startup Public Projects** — Auto-seed FOLIO and Catholic Semantic Canon as public, view-only projects on first boot when DB has zero projects

## Phase Details

### Phase 1: Auth Modes, Anonymous Stack & Seed CLI
**Goal**: One codebase serves three deployment shapes (CatholicOS full-auth, public reference browser, single-developer local) with anonymous users able to browse and submit suggestions, and operators able to import OWL/Turtle files into a project from the CLI
**Depends on**: Nothing (PR #27 is rebased and ready to land)
**Status**: PR #27 in flight against CatholicOS/ontokit-api `main` (rebased 2026-05-02)
**Requirements**: AUTH-01, AUTH-02, AUTH-03, AUTH-04, AUTH-05, ANON-01, ANON-02, ANON-03, ANON-04, ANON-05, SEED-CLI-01, SEED-CLI-02, SEED-CLI-03, INDEX-01, INDEX-02
**Success Criteria** (what must be TRUE):
  1. With `AUTH_MODE=disabled`, the API starts without Zitadel configuration and `GET /api/v1/projects` returns 200 with the anonymous user resolved as the caller (no 401)
  2. With `AUTH_MODE=optional`, an unauthenticated `GET` to a public project returns data while an unauthenticated `POST` to a write endpoint returns 401
  3. An anonymous-token-authorized `POST /projects/{id}/suggestions/anonymous/...` creates a `SuggestionSession` row with `is_anonymous=true` and the submitter's name/email captured at submit time, and rate limiting kicks in on burst submissions
  4. `python scripts/seed-project.py --name FOLIO --owner anonymous --source FOLIO.owl` creates a project, populates the bare git repo, builds the Postgres ontology index, and is idempotent on re-run (skip-if-exists by name)
  5. `GET /api/v1/projects/{id}/classes/{iri}` returns translation and synonym annotations (skos:altLabel, skos:prefLabel, dcterms:title) grouped by property IRI in both the index path and the RDFLib fallback path, with consistent response shape
  6. `tests/unit/test_auth_disabled.py` and the matching required/optional test files pass green covering all three auth modes
**Plans**: TBD (PR #27 already structured upstream — discuss-phase will capture remaining gaps)

### Phase 2: Startup Robustness
**Goal**: API boot is observable and bounded — async callers never block on synchronous MinIO, lifespan checks fail fast on stuck infra rather than hanging indefinitely, and Railway/container logs show startup progress before the logging pipeline is configured
**Depends on**: Phase 1 (PR #27 should land first to avoid merge conflicts in `main.py` lifespan)
**Status**: PR #138 in flight against CatholicOS/ontokit-api `main` (opened 2026-05-02)
**Requirements**: OPS-01, OPS-02, OPS-03, OPS-04
**Success Criteria** (what must be TRUE):
  1. Every `StorageService` async method that touches the synchronous MinIO client wraps the call in `asyncio.to_thread(...)` — verified by code review and a regression test that proves the event loop is not blocked during a slow MinIO operation
  2. With Postgres unreachable, `uvicorn ontokit.main:app` exits with a clear timeout error within ~20 seconds (not hanging indefinitely) — verified by integration test that points `DATABASE_URL` at a black-hole port
  3. With MinIO unreachable, the lifespan logs a warning at ~15 seconds and the API continues startup successfully (MinIO is treated as optional infrastructure)
  4. `docker compose up` (or Railway deploy logs) shows lifespan progress lines on stderr — "DB connecting...", "DB ready", "MinIO check...", "API ready" — even when the structured logging pipeline has not yet been configured
**Plans**: TBD (PR #138 already structured upstream — discuss-phase will capture remaining gaps)

### Phase 3: Seed-on-Startup Public Projects
**Goal**: A fresh deploy with an empty database boots into a working public ontology browser — FOLIO and the Catholic Semantic Canon are seeded as public, view-only projects on first boot, owned by the anonymous user (or a configurable system user), with full Postgres index population and bare git repos initialized. Idempotent across restarts. Source-fetch failures degrade gracefully and never block API startup.
**Depends on**: Phase 1 (anonymous user infra, seed-project plumbing) and Phase 2 (bounded async-safe lifespan to host the seed routine without blocking)
**Status**: Active build — working branch `feat/seed-folio-public-project`
**Requirements**: STARTUP-SEED-01, STARTUP-SEED-02, STARTUP-SEED-03, STARTUP-SEED-04, STARTUP-SEED-05, STARTUP-SEED-06, STARTUP-SEED-07, STARTUP-SEED-08, STARTUP-SEED-09, STARTUP-SEED-10
**Success Criteria** (what must be TRUE):
  1. On a fresh database with zero `projects` rows and `SEED_REFERENCE_PROJECTS=true` (default), startup logs `"Seeded 2 reference projects: FOLIO, Catholic Semantic Canon"` and `GET /api/v1/projects` (unauthenticated) returns both projects with `is_public=true` and the anonymous user as owner
  2. Each seeded project has a populated bare git repo at `${GIT_REPOS_BASE_PATH}/{project_id}.git` containing the source ontology in an initial commit, and the Postgres `ontology_index` table has rows for every class/property in the source file
  3. A second startup against the same database is a no-op for seeding: idempotency check finds both projects by name/slug, logs `"Reference project FOLIO already exists, skipping"` for each, and does not duplicate rows or re-fetch sources
  4. With `SEED_REFERENCE_PROJECTS=false`, startup completes without attempting to seed, and `GET /api/v1/projects` returns an empty list on a fresh database — confirmed by `tests/integration/test_startup_seed.py`
  5. With the source URLs pointed at unreachable hosts (or returning 404), startup logs a `WARNING` per failed source but the API still starts successfully and serves `GET /api/v1/projects` returning whichever sources succeeded (or an empty list if both failed)
  6. Source URLs and project display names are overridable via env vars (`SEED_FOLIO_URL`, `SEED_FOLIO_NAME`, `SEED_CANON_URL`, `SEED_CANON_NAME`) — verified by deploying with a pinned commit-SHA URL and seeing that exact content land in the bare repo
**Plans:** 3 plans
- [ ] 03-01-PLAN.md — Settings fields + .env.example documentation + test scaffolds (Wave 1)
- [ ] 03-02-PLAN.md — `seed_service.py` implementation + unit tests for retry/idempotency/parse-failure (Wave 2)
- [ ] 03-03-PLAN.md — Lifespan integration in `main.py` + Postgres-backed integration tests (Wave 3)

## Progress

**Execution Order:** 1 → 2 → 3

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Auth Modes, Anonymous Stack & Seed CLI | v0.4.0 | 0/0 | In flight (PR #27) | - |
| 2. Startup Robustness | v0.4.0 | 0/0 | In flight (PR #138) | - |
| 3. Seed-on-Startup Public Projects | v0.4.0 | 0/3 | Planned     | - |

## Notes

- **PR boundaries**: Each phase produces a clean PR against `CatholicOS/ontokit-api` main. Phase 1 = PR #27 (rebased), Phase 2 = PR #138, Phase 3 = new PR off `feat/seed-folio-public-project`.
- **Brownfield bootstrap**: Phases 1-2 are partially implemented in flight; their `discuss-phase` step should focus on identifying remaining gaps in the open PRs rather than greenfield design.
- **Granularity**: Standard (3 phases for 29 requirements aligns with delivery boundaries — PR #27 is one cohesive auth+anon+seed-CLI+index bundle, PR #138 is operational hardening, STARTUP-SEED is the new build).
- **Source URLs (Phase 3)**:
  - FOLIO: `https://raw.githubusercontent.com/alea-institute/FOLIO/main/FOLIO.owl`
  - Catholic Semantic Canon: `https://raw.githubusercontent.com/CatholicOS/ontology-semantic-canon/main/sources/ontology-semantic-canon.ttl`

---
*Last updated: 2026-05-02 (roadmap initialization)*
