# Requirements: OntoKit API — v0.4.0 Public Onboarding & Operational Readiness

**Defined:** 2026-05-02
**Core Value:** Provide a reliable, async-first, git-versioned API surface that enables grassroots collaborative ontology editing, deployable as a standalone reference browser (FOLIO + Catholic Semantic Canon) or full multi-tenant CatholicOS install.

## v0.4.0 Requirements

### Auth Modes & Anonymous Users (PR #27 in flight)

- [ ] **AUTH-01**: System reads `AUTH_MODE` env var with three values: `required` (current behavior), `optional` (auth tokens accepted but not required), `disabled` (all requests run as anonymous)
- [ ] **AUTH-02**: An `ANONYMOUS_USER` constant (id=anonymous, roles=[viewer]) is defined and used when no auth token is provided in `optional`/`disabled` modes
- [ ] **AUTH-03**: `get_current_user_optional` returns `None` when no auth is provided in `optional` mode (browse works without sign-in)
- [ ] **AUTH-04**: `get_current_user` (RequiredUser) still returns 401 in `optional` mode for write endpoints (read works anonymously, write requires auth)
- [ ] **AUTH-05**: All three auth modes are covered by tests (`test_auth_disabled.py`, etc.)

### Anonymous Suggestions (PR #27 in flight)

- [ ] **ANON-01**: Anonymous tokens module mints short-lived submission tokens for unauthenticated suggesters
- [ ] **ANON-02**: `SuggestionSession` model has new fields: `is_anonymous`, `submitter_name`, `submitter_email`
- [ ] **ANON-03**: `/projects/{id}/suggestions/anonymous/*` endpoints accept anonymous-token-authorized submissions with rate limiting
- [ ] **ANON-04**: Existing review summaries display anonymous submitter info correctly (name + email captured at submit time, distinct from generic user_name/email at session creation)
- [ ] **ANON-05**: Alembic migration adds `is_anonymous`, `submitter_name`, `submitter_email` columns to `suggestion_sessions` table

### Seed-Project CLI (PR #27 in flight)

- [ ] **SEED-CLI-01**: `scripts/seed-project.py` imports an OWL/Turtle file as a new project with full Postgres index population
- [ ] **SEED-CLI-02**: Seed script accepts a project name, owner, and source file path
- [ ] **SEED-CLI-03**: Seed script handles re-runs idempotently (skip if project name already exists)

### Index Path Class Detail (PR #27 in flight)

- [ ] **INDEX-01**: `get_class_detail` includes non-rdfs:label `IndexedLabel` entries (translations/synonyms via skos:altLabel, skos:prefLabel, dcterms:title) as annotations grouped by property IRI
- [ ] **INDEX-02**: Translations/synonyms appear in the same response shape as the RDFLib fallback path (consistency across browse modes)

### Startup Robustness (PR #138 in flight)

- [ ] **OPS-01**: `StorageService` async methods wrap synchronous MinIO client calls in `asyncio.to_thread` so async callers never block the event loop
- [ ] **OPS-02**: Lifespan database connect is bounded by `asyncio.wait_for(20s)` — fail fast on stuck DB instead of hanging boot indefinitely
- [ ] **OPS-03**: Lifespan MinIO bucket check is bounded by `asyncio.wait_for(15s)` — MinIO is optional; timeout warns and continues startup
- [ ] **OPS-04**: Lifespan progress is mirrored to stderr (`print(..., file=sys.stderr, flush=True)`) so Railway/container deploy logs show startup steps even before logging pipeline configuration

### Seed-on-Startup Public Projects (this phase)

- [ ] **STARTUP-SEED-01**: On startup, when the database has zero projects, the API automatically seeds **two reference projects** as public, view-only
- [ ] **STARTUP-SEED-02**: One seeded project is **FOLIO** sourced from `https://raw.githubusercontent.com/alea-institute/FOLIO/main/FOLIO.owl`
- [ ] **STARTUP-SEED-03**: One seeded project is **Catholic Semantic Canon** sourced from `https://raw.githubusercontent.com/CatholicOS/ontology-semantic-canon/main/sources/ontology-semantic-canon.ttl`
- [ ] **STARTUP-SEED-04**: Seeded projects are owned by the anonymous user (or a configurable system user) with `is_public=true` and read-only access for unauthenticated viewers
- [ ] **STARTUP-SEED-05**: Each seeded project gets its own bare git repository, initial commit, and Postgres ontology index populated
- [ ] **STARTUP-SEED-06**: Seeding is idempotent: if either project already exists by name/slug, that one is skipped (re-runs don't duplicate)
- [ ] **STARTUP-SEED-07**: Seeding can be disabled via env var (`SEED_REFERENCE_PROJECTS=false`) for test/dev environments
- [ ] **STARTUP-SEED-08**: Source-fetch failures (network unreachable, 404, malformed RDF) are logged but do not block API startup
- [ ] **STARTUP-SEED-09**: Source URLs and project names are configurable via env vars (so deployments can pin a commit SHA, swap mirrors, or omit a reference)
- [ ] **STARTUP-SEED-10**: Seeded projects display correctly in `/api/v1/projects` listing for both authenticated and anonymous users

### Out of Scope

- **FOLIO adapter wholesale rewrite** — Bommarito's `feature/folio-adapter` deletes the entire backend; rejected
- **Auto-update reference ontologies** — seed runs once on first boot only; later refreshes happen via Sync-from-Remote or manual re-import
- **User-specific seeding** — only the two named reference ontologies; arbitrary seed lists are out of scope for v0.4.0
- **Web-side single-project UX** — handled in the ontokit-web roadmap, not here

---

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| AUTH-01 | Phase 1 | Pending (PR #27) |
| AUTH-02 | Phase 1 | Pending (PR #27) |
| AUTH-03 | Phase 1 | Pending (PR #27) |
| AUTH-04 | Phase 1 | Pending (PR #27) |
| AUTH-05 | Phase 1 | Pending (PR #27) |
| ANON-01 | Phase 1 | Pending (PR #27) |
| ANON-02 | Phase 1 | Pending (PR #27) |
| ANON-03 | Phase 1 | Pending (PR #27) |
| ANON-04 | Phase 1 | Pending (PR #27) |
| ANON-05 | Phase 1 | Pending (PR #27) |
| SEED-CLI-01 | Phase 1 | Pending (PR #27) |
| SEED-CLI-02 | Phase 1 | Pending (PR #27) |
| SEED-CLI-03 | Phase 1 | Pending (PR #27) |
| INDEX-01 | Phase 1 | Pending (PR #27) |
| INDEX-02 | Phase 1 | Pending (PR #27) |
| OPS-01 | Phase 2 | Pending (PR #138) |
| OPS-02 | Phase 2 | Pending (PR #138) |
| OPS-03 | Phase 2 | Pending (PR #138) |
| OPS-04 | Phase 2 | Pending (PR #138) |
| STARTUP-SEED-01 | Phase 3 | Pending |
| STARTUP-SEED-02 | Phase 3 | Pending |
| STARTUP-SEED-03 | Phase 3 | Pending |
| STARTUP-SEED-04 | Phase 3 | Pending |
| STARTUP-SEED-05 | Phase 3 | Pending |
| STARTUP-SEED-06 | Phase 3 | Pending |
| STARTUP-SEED-07 | Phase 3 | Pending |
| STARTUP-SEED-08 | Phase 3 | Pending |
| STARTUP-SEED-09 | Phase 3 | Pending |
| STARTUP-SEED-10 | Phase 3 | Pending |

**Coverage:** 29/29 requirements mapped (100%)
