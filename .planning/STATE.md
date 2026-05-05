---
gsd_state_version: 1.0
milestone: v0.4.0
milestone_name: Public Onboarding & Operational Readiness
status: "Phase 2 (PR #138) merged to catholicos/dev; Phase 3 planned and verified; awaiting PR #27 to land before /gsd-execute-phase 3"
last_updated: "2026-05-05T00:00:00.000Z"
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
| 2 | Startup Robustness | **Merged** | PR #138 ✅ on `catholicos/dev` (commit `bac18a5`) | (delivered by PR #138) |
| 3 | Seed-on-Startup Public Projects | Planned, ready to execute | `feat/seed-folio-public-project` (rebased onto `catholicos/dev`; local; not yet pushed) | 03-01, 03-02, 03-03 |

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

**Phase 3 is ready to execute. Status of prerequisites:**

1. ✅ **PR #138** merged to `catholicos/dev` as commit `bac18a5` (startup async-safety + stderr lifespan logs).
2. ⏳ **PR #27** still open on `catholicos/feat/seed-project-script` (CI green; awaiting human review). Once merged to `catholicos/dev`, Phase 3 is fully unblocked.
3. After PR #27 merges: pull `catholicos/dev` and re-rebase `feat/seed-folio-public-project` to absorb the new commits.
4. Then run `/gsd-execute-phase 3` from this directory.

**Resume command (once PR #27 merges):**
```bash
cd /home/damienriehl/Coding\ Projects/ontokit-api
git fetch catholicos dev
git checkout feat/seed-folio-public-project
git rebase catholicos/dev      # absorb PR #27 once merged
# resolve any conflicts (likely minor — Phase 3 only adds new files + small main.py block)
# Then:
/gsd-execute-phase 3
```

The executor will run waves sequentially:
- Wave 1: 03-01-PLAN — Settings fields + .env.example + test scaffolds
- Wave 2: 03-02-PLAN — `seed_service.py` + unit tests (depends on Wave 1)
- Wave 3: 03-03-PLAN — lifespan integration + integration tests (depends on Wave 2)

Each wave commits atomically. After all 3 waves succeed, push and open a PR against `catholicos/dev` (per CatholicOS's dev/main branch model — features land on dev, releases promote to main).

## Branching Model (alea fork mirrors CatholicOS)

- **`alea/main` = FOLIO prod** — mirrors `catholicos/main`. Released code only. Force-pushed during sync (alea/main has no unique history).
- **`alea/dev` = FOLIO staging (Hetzner — pipeline TBD)** — mirrors `catholicos/dev`. Integration branch.
- **Feature branches** fork from `catholicos/dev` (or `alea/dev`). Local Phase 11/12/13 work saved to `feat/phase-11-llm-abstraction`, `feat/phase-12-duplicate-detection`, `feat/phase-13-validation-suggestion-gen`.
- **Sync cadence**: manual. `git fetch catholicos && git push origin main` and `git push origin dev` when ready to refresh alea.
- **Hotfix discipline**: if alea/main breaks in prod, fix forward via a CatholicOS PR; if a same-day fix is required on alea, use a `hotfix/*` branch and port the fix upstream within 48h. Never commit directly to `alea/main` long-term — auto-sync would overwrite it.

## Session Continuity

**Last action:** Branch reorg on 2026-05-05. PR #138 confirmed merged to `catholicos/dev`. Local main reset to `catholicos/main`. Local dev fast-forwarded to `catholicos/dev`. `feat/seed-folio-public-project` rebased onto `catholicos/dev`. Phase 11/12/13 LLM milestone work preserved on per-phase feature branches. `alea/dev` created as a fresh shadow of `catholicos/dev`.

**Next action:** (1) Force-push alea/main to mirror catholicos/main once user approves. (2) Wait for PR #27 to merge to catholicos/dev. (3) `git fetch catholicos dev && git rebase catholicos/dev` on this branch. (4) Run `/gsd-execute-phase 3`.

**Working branch:** `feat/seed-folio-public-project` (off `catholicos/dev`; carries GSD planning artifacts).

**Local feature branches with unique work (saved during reorg):**
- `feat/phase-11-llm-abstraction` (tip `9d1d7ca`) — 5 commits, LLM provider/registry/routes
- `feat/phase-12-duplicate-detection` (tip `2a07937`) — 9 commits, ANN index + duplicate detection
- `feat/phase-13-validation-suggestion-gen` (tip `481284a`) — 8 commits, validation + suggestion generation
- `pr27-rebase` — already pushed to `catholicos/feat/seed-project-script` (PR #27)
- `fix/startup-async-safety` — already merged to `catholicos/dev` (PR #138 ✅)

---
*State updated: 2026-05-05 after branch reorg + alea/dev sync*
