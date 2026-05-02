---
gsd_state_version: 1.0
milestone: v0.4.0
milestone_name: Public Onboarding & Operational Readiness
status: "Phase 3 planned and verified; awaiting PR #27 + PR #138 to land before /gsd-execute-phase 3"
last_updated: "2026-05-02T22:30:00.000Z"
progress:
  total_phases: 3
  completed_phases: 0
  total_plans: 3
  completed_plans: 0
  percent: 0
---

# State: OntoKit API

## Project Reference

**Core Value:** Provide a reliable, async-first, git-versioned API surface that enables grassroots collaborative ontology editing — where every change is auditable, every suggestion is reviewable, and integrity guarantees (validation, duplicate detection, role-based access) are enforced server-side.

**Current Milestone:** v0.4.0 Public Onboarding & Operational Readiness

**Current Focus:** Land PR #27 (auth modes + anonymous stack + seed-project CLI + translation index), then PR #138 (startup robustness), then build STARTUP-SEED (auto-seed FOLIO + Catholic Semantic Canon on first boot).

## Current Position

**Phase:** 3 — Seed-on-Startup Public Projects (planned, awaiting prerequisite PRs)
**Plans:** 03-01-PLAN.md, 03-02-PLAN.md, 03-03-PLAN.md (verified by gsd-plan-checker — PLAN VERIFIED)
**Status:** Ready to execute once PR #27 + PR #138 merge to upstream/main. Prerequisite chain: #138 first (no dependencies), then #27 (independent of #138 but needed for AUTH_MODE/anonymous user infra used by Phase 3 tests).
**Progress:** [██░░░░░░░░] 17% (planning done for 3/3 phases; execution pending for all)

## Phase Roster

| # | Phase | Status | Branch / PR | Plan files |
|---|-------|--------|-------------|------------|
| 1 | Auth Modes, Anonymous Stack & Seed CLI | In flight | PR #27 (rebased; CI green; awaiting review) | (delivered by PR #27) |
| 2 | Startup Robustness | In flight | PR #138 (CI green; awaiting review) | (delivered by PR #138) |
| 3 | Seed-on-Startup Public Projects | Planned, ready to execute | `feat/seed-folio-public-project` (local; not yet pushed) | 03-01, 03-02, 03-03 |

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

### Phase 3 Decisions Locked (in 03-CONTEXT.md)

All 13 decisions resolved during /gsd-discuss-phase 3:
- D-01/02: Seed routine in `ontokit/services/seed_service.py`, called from lifespan; PR #27's `scripts/seed-project.py` calls into the same service.
- D-03/04/05: Owner via `SEED_OWNER_ID` env (default `ontokit-system`); `is_public=true` + existing role-based access.
- D-06/07/08: Idempotency by name match; deletion → recreate next boot; no audit table.
- D-09/10/11: Exponential backoff sleep-BEFORE 1s/2s/4s × 3 (~7s worst case); HTTP-fetch failure logs WARNING and continues; parse errors fail without retry.
- D-12/13: 6 new env vars (master toggle, owner, FOLIO+CSC URLs, FOLIO+CSC names) defaulting to live GitHub raw URLs.

## Execution Readiness

**Phase 3 is ready to execute. Prerequisites:**

1. **PR #138** lands on `CatholicOS/ontokit-api:main` (startup async-safety + stderr lifespan logs). CI green; awaiting review.
2. **PR #27** lands on `CatholicOS/ontokit-api:main` (AUTH_MODE + anonymous user + seed-project CLI + translation index). CI green; awaiting review.
3. After both merge: rebase `feat/seed-folio-public-project` onto the new `upstream/main` to pick up #138 + #27 changes.
4. Then run `/gsd-execute-phase 3` from this directory.

**Why both PRs first:** Phase 3 plans assume:
- PR #138's stderr-mirrored lifespan logs exist (Plan 03-03 mirrors that pattern).
- PR #27's AUTH_MODE / anonymous user infra exist (Plan 03-03's STARTUP-SEED-10 anonymous-read variant references it; deferred until landed).
- PR #27's `scripts/seed-project.py` exists for the planner-noted "share the same service" link (Plan 03-02 reuses the OWL/Turtle → project + bare repo + index pipeline that PR #27 establishes).

**Resume command (once both PRs are merged):**
```bash
cd /home/damienriehl/Coding\ Projects/ontokit-api
git fetch upstream --prune
git checkout feat/seed-folio-public-project
git rebase upstream/main      # absorb #27 + #138
# resolve any conflicts (likely minor — Phase 3 only adds new files + small main.py block)
# Then:
/gsd-execute-phase 3
```

The executor will run waves sequentially:
- Wave 1: 03-01-PLAN — Settings fields + .env.example + test scaffolds
- Wave 2: 03-02-PLAN — `seed_service.py` + unit tests (depends on Wave 1)
- Wave 3: 03-03-PLAN — lifespan integration + integration tests (depends on Wave 2)

Each wave commits atomically. After all 3 waves succeed, push and open a PR against `CatholicOS/ontokit-api:main`.

## Session Continuity

**Last action:** Phase 3 plans verified (gsd-plan-checker → PLAN VERIFIED) on 2026-05-02. All blockers and warnings from the first review pass were resolved in revision commit `2d15d98`.

**Next action:** Wait for PR #27 + PR #138 to merge. Then rebase + run `/gsd-execute-phase 3`.

**Working branch:** `feat/seed-folio-public-project` (off `upstream/main`; 11 commits ahead consisting of GSD planning artifacts only — no source code changes yet).

---
*State updated: 2026-05-02 after Phase 3 plan-checker pass*
