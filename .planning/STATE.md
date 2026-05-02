---
gsd_state_version: 1.0
milestone: v0.4.0
milestone_name: Public Onboarding & Operational Readiness
status: "Not started (PR #27 in flight upstream — discuss-phase will reconcile)"
last_updated: "2026-05-02T21:14:37.355Z"
progress:
  total_phases: 3
  completed_phases: 0
  total_plans: 0
  completed_plans: 0
  percent: 0
---

# State: OntoKit API

## Project Reference

**Core Value:** Provide a reliable, async-first, git-versioned API surface that enables grassroots collaborative ontology editing — where every change is auditable, every suggestion is reviewable, and integrity guarantees (validation, duplicate detection, role-based access) are enforced server-side.

**Current Milestone:** v0.4.0 Public Onboarding & Operational Readiness

**Current Focus:** Land PR #27 (auth modes + anonymous stack + seed-project CLI + translation index), then PR #138 (startup robustness), then build STARTUP-SEED (auto-seed FOLIO + Catholic Semantic Canon on first boot).

## Current Position

**Phase:** 1 — Auth Modes, Anonymous Stack & Seed CLI
**Plan:** TBD (next: `/gsd-discuss-phase 1`)
**Status:** Not started (PR #27 in flight upstream — discuss-phase will reconcile)
**Progress:** [░░░░░░░░░░] 0% (0/3 phases complete)

## Phase Roster

| # | Phase | Status | Branch / PR |
|---|-------|--------|-------------|
| 1 | Auth Modes, Anonymous Stack & Seed CLI | Not started | PR #27 (rebased) |
| 2 | Startup Robustness | Not started | PR #138 |
| 3 | Seed-on-Startup Public Projects | Not started | `feat/seed-folio-public-project` |

## Performance Metrics

- Phases planned: 3
- Phases complete: 0
- Plans complete: 0
- Requirements covered: 29/29 (100%)
- Tests added: 0 (baseline; brownfield)

## Accumulated Context

### Key Decisions

- **AUTH_MODE three-mode toggle (req/opt/disabled)** — one codebase serves CatholicOS, FOLIO, and local-dev shapes. (PR #27)
- **Anonymous suggestions stack** — token module + `is_anonymous`/`submitter_name`/`submitter_email` columns + dedicated routes; lets FOLIO viewers contribute without sign-up. (PR #27)
- **MinIO sync calls wrapped in `asyncio.to_thread`** — fixes async-on-sync bug that blocked the event loop. (PR #138)
- **Reject Bommarito's `feature/folio-adapter`** — wholesale backend delete loses every CatholicOS capability; instead seed FOLIO via the existing pipeline.
- **Seed two reference ontologies on first boot, not CLI-only** — zero-config public reference browser; complements `scripts/seed-project.py` for explicit imports.
- **Seeding is idempotent + env-toggleable + non-fatal on source-fetch failure** — operational safety: a network blip should never block boot.

### Active Todos

(none — discuss-phase 1 will populate)

### Active Blockers

(none)

### Open Questions for Phase 1

- Are there gaps in PR #27 between what's on the branch and the AUTH/ANON/SEED-CLI/INDEX requirements as written? (To be resolved in `/gsd-discuss-phase 1`.)
- Does PR #27 already include the Alembic migration for `is_anonymous`/`submitter_name`/`submitter_email` (ANON-05)? Verify before planning.
- Is the seed CLI (`scripts/seed-project.py`) wired to call into the indexing pipeline (SEED-CLI-01) or does it only persist files? Verify before planning.

### Open Questions for Phase 3

- Should reference projects be owned by the anonymous user directly, or by a dedicated `system` user (configurable)? Decision deferred to `/gsd-discuss-phase 3`.
- Where does the seed routine live — `lifespan` in `ontokit/main.py`, a separate `ontokit/services/seed_service.py`, or an ARQ background task fired from lifespan? Decision deferred.
- How does idempotency interact with deleted-then-recreated projects (slug collision vs. soft-delete)? Decision deferred.

## Session Continuity

**Last action:** Roadmap initialization (2026-05-02). Wrote `.planning/ROADMAP.md`, `.planning/STATE.md`, and updated `.planning/REQUIREMENTS.md` traceability section.

**Next action:** `/gsd-discuss-phase 1` to capture implementation decisions for landing PR #27 (gap analysis between rebased PR and the AUTH/ANON/SEED-CLI/INDEX requirements as written).

**Working branch:** `entity-graph-endpoint` (current). Will switch to PR #27's branch for Phase 1 work, PR #138's branch for Phase 2, and `feat/seed-folio-public-project` for Phase 3.

---
*State initialized: 2026-05-02*
