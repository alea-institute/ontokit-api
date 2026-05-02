# OntoKit API

## What This Is

A FastAPI backend for collaborative OWL ontology curation. Powers the ontokit-web frontend with REST endpoints, git-based version control of ontologies, suggestion workflows, lint/quality checks, and LLM-assisted improvement features. Used by both CatholicOS (Catholic Semantic Canon) and ALEA (FOLIO — Free Open Legal Information Ontology). Distributed as the `ontokit` package on PyPI.

## Core Value

Provide a reliable, async-first, git-versioned API surface that enables grassroots collaborative ontology editing — where every change is auditable, every suggestion is reviewable, and integrity guarantees (validation, duplicate detection, role-based access) are enforced server-side.

## Current Milestone: v0.4.0 Public Onboarding & Operational Readiness

**Goal:** Make the API trivially deployable as a standalone ontology browser (zero-config, view-only, anonymous-friendly) seeded with FOLIO and the Catholic Semantic Canon, while keeping all existing CatholicOS multi-tenant capabilities intact. Sister repo ontokit-web's v0.4.0 (LLM-Assisted Ontology Improvements) shipped against an API that doesn't yet have AUTH_MODE/anonymous-suggestions on `main` — this milestone closes that gap and adds first-boot seeding of both reference ontologies.

**Target features:**
- AUTH_MODE (required/optional/disabled) + anonymous user infra (PR #27 in flight)
- Anonymous suggestions endpoint stack (PR #27 in flight)
- Seed two public, view-only projects on first boot (this phase):
  - **FOLIO** — Free Open Legal Information Ontology (ALEA), source: `https://raw.githubusercontent.com/alea-institute/FOLIO/main/FOLIO.owl`
  - **Catholic Semantic Canon** — CatholicOS reference ontology, source: `https://raw.githubusercontent.com/CatholicOS/ontology-semantic-canon/main/sources/ontology-semantic-canon.ttl`
- Startup robustness: async-safe MinIO/DB checks, fail-fast timeouts, Railway log visibility (PR #138 in flight)
- Sync ALEA fork with CatholicOS upstream

## Requirements

### Validated

<!-- Shipped on upstream/main. -->

- ✓ FastAPI application with /api/v1 prefix and OpenAPI docs — v0.1.0
- ✓ PostgreSQL + asyncpg + SQLAlchemy 2.0 async ORM — v0.1.0
- ✓ Redis 7 + ARQ background job queue — v0.1.0
- ✓ MinIO object storage for ontology files — v0.1.0
- ✓ Zitadel OIDC/JWT authentication — v0.2.0
- ✓ Project CRUD with team membership and role-based access — v0.2.0
- ✓ Pygit2 bare-repository git layer for ontology versioning — v0.2.0
- ✓ Ontology CRUD + class/property operations (RDFLib) — v0.2.0
- ✓ Pull-request workflow with semantic diffs — v0.2.0
- ✓ Suggestion sessions with auto-save, draft branches, beacon endpoint — v0.2.0
- ✓ Lint engine with 20+ ontology validation rules — v0.2.0
- ✓ Normalization service (canonical Turtle conversion) — v0.2.0
- ✓ GitHub App integration for two-way sync — v0.2.0
- ✓ Quality service: cycle detection, duplicate detection, cross-references — v0.3.0
- ✓ Postgres ontology index (entities/labels/annotations/parents) — v0.3.0
- ✓ Embedding service (local + OpenAI + Voyage providers) — v0.3.0
- ✓ Server-side BFS entity graph (PR #37) — v0.3.0
- ✓ Sync from Remote (formerly Upstream Sync) tables and routes — v0.3.0

### Active

<!-- v0.4.0 scope. -->

- [ ] **AUTH_MODE config + anonymous user** — PR #27 (rebased)
- [ ] **Anonymous suggestion endpoints + token module** — PR #27 (rebased)
- [ ] **Seed-project CLI script** (importing OWL files with indexing) — PR #27 (rebased)
- [ ] **Startup async-safety + Railway log visibility** — PR #138
- [ ] **Seed FOLIO + Catholic Semantic Canon as public, view-only projects on first boot** — this phase (the immediate next build)
- [ ] **Translations/synonyms in index path class detail** — PR #27 (rebased)

### Out of Scope

- **FOLIO adapter rewrite** — Mike Bommarito's `feature/folio-adapter` branch deletes the entire backend. Rejected: we're keeping the full CatholicOS codebase and adding FOLIO-friendly seeding.
- **`ontokit/git/repository.py` (legacy GitPython)** — superseded by `bare_repository.py` (pygit2). Should be deleted once all callers migrate.
- **Cross-repo monorepo** — ontokit-api and ontokit-web stay as separate repos; sister repo coordination via shared PROJECT.md style.
- **Real-time collaborative editing** — not the bottleneck for ontology quality; suggestion + PR workflow is sufficient.
- **Custom auth provider** — Zitadel with optional/disabled modes covers all deployment scenarios.

## Context

- **Production**: AWS Ubuntu 24.04 ARM64 at 54.224.195.12 (ontokit.openlegalstandard.org), running CatholicOS main via systemd (no Docker). Postgres + Redis local, Caddy proxy.
- **Sister repo coordination**: ontokit-web at `/home/damienriehl/Coding Projects/ontokit-web/`. Web uses `/api/v1/*` endpoints. Web's v0.3.0 milestone (Optional Auth, Production Deployment, Anonymous Suggestions) shipped against an API that's still landing the matching backend — PR #27 closes that gap.
- **Three deployment shapes**: CatholicOS (full auth + private projects), public reference browser (anonymous browse + AUTH_MODE=optional, seeded with FOLIO + Catholic Semantic Canon), single-developer local (AUTH_MODE=disabled).
- **Codebase map**: `.planning/codebase/` (STACK, INTEGRATIONS, ARCHITECTURE, STRUCTURE, CONVENTIONS, TESTING, CONCERNS).
- **Two open PRs**: #138 (startup fix) and #27 (AUTH_MODE bundle, just rebased).

## Constraints

- **Async-first throughout** — every I/O path uses `async`/`await`; sync clients (MinIO urllib3) must be wrapped in `asyncio.to_thread`.
- **Strict typing** — mypy strict mode enabled, Python 3.11 target. New code must pass mypy without `# type: ignore`.
- **Pydantic v2 strict** — all schemas use Pydantic v2; computed fields preferred over manual conversion.
- **CatholicOS upstream is source of truth** — alea-institute fork stays in sync via PRs back to CatholicOS.
- **Backwards compatibility** — existing v0.1–v0.3 endpoints must keep working; new functionality is additive.
- **Lean dependencies** — no new top-level packages without justification; reuse existing services.
- **PyPI distributable** — package must `uv build && twine check --strict` cleanly.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Bare git repos via pygit2 (not GitPython) | Concurrent multi-user access without working-dir checkouts | ✓ v0.2.0 |
| Postgres ontology index alongside RDFLib graph | Fast list/search/lineage without re-parsing turtle | ✓ v0.3.0 |
| Zitadel OIDC + JWT validation | Self-hostable identity without rolling own auth | ✓ v0.2.0 |
| AUTH_MODE three-mode toggle (req/opt/disabled) | One codebase serves CatholicOS + FOLIO + local dev | — Pending (PR #27) |
| Anonymous suggestions stack (token module + routes) | Lets FOLIO viewers contribute without sign-up | — Pending (PR #27) |
| MinIO sync calls wrapped in asyncio.to_thread | StorageService had async-on-sync bug, blocked event loop | — Pending (PR #138) |
| Reject Bommarito's folio-adapter | Wholesale backend delete loses every CatholicOS capability | ✓ Decided |
| Seed FOLIO + Catholic Semantic Canon on first boot (not CLI-only) | Zero-config public reference browser; users pick which to view; complements seed-project.py | — Pending (this phase) |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-05-02 after initialization (brownfield bootstrap)*
