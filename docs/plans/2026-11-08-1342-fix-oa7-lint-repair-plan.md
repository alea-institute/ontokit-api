---
title: Repair oa-5 lint and verify inherited fixes - Plan
type: fix
date: 2026-11-08
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
item: oa-7
kind: expansion
---

# Repair oa-5 lint and verify inherited fixes - Plan

## Goal Capsule

- **Objective:** Maintainers have a verified successor to PR #76 that passes repository lint and the full test suite while retaining oa-5's API improvements.
- **Means:** Verify the inherited repair against the current lint contract, fix only reproduced residual defects, and supply controller publication evidence (KTD1–KTD4).
- **Authority:** The controller's oa-7 mandate and supplied campaign rules govern scope; repository `AGENTS.md` governs implementation. The chief campaign Goal Capsule and Campaign R11, R15–R20 remain controller reconciliation inputs.
- **Execution boundary:** This stage commits only this plan on `drawdown/oa-7`. Later build work produces one local delivery; the controller scans, pushes, opens the successor PR against `dev`, and obtains campaign reviews and CI.
- **Stop conditions:** R4–R6 govern scope and publication. Missing external evidence blocks the corresponding completion claim, not independent local verification.

## Product Contract

### Summary

Preserve the oa-5 work already present in the branch and establish whether its lint repair passes today.
Make a further source change only if current verification demonstrates a remaining defect.
Record local results separately from controller CI and PR supersession.

### Problem Frame

The mandate identifies a failing `lint` job on PR #76.
Local history is newer than that failure description: commit `1c74df14` includes both the Redis return-type repair and a guided-refusal fixture correction.
The relevant files remain unchanged since that commit.

The source repair therefore appears inherited.
Neither historical passing results nor its presence in the tree establishes that the current branch passes or that a successor PR exists.

### Requirements

**Correctness and preservation**

- R1. The repository lint command and all commands in the existing CI `lint` job pass on the delivered source revision.
- R2. The full repository test suite passes with its required services. Local prerequisite skips remain unverified integration coverage until controller evidence resolves them.
- R3. Preserve all inherited work and every existing test and assertion. Any new behavior required by a residual repair receives a regression test, while existing runtime behavior and API contracts remain intact.

**Scope and delivery**

- R4. Work only oa-7; leave release workflows, Python targets, dependency manifests and locks, deployment configuration, and the explicit repository gate hold unchanged. No migration, data rewrite, release, outside service, AWS provisioning, or frontend work belongs to this repair.
- R5. Deliver on `drawdown/oa-7`; the controller owns the successor PR against `dev`, its verified relationship to #76, required checks, and Astra/Fable reviews. Campaign R15–R19 apply, and expiry of the November 3–6 expansion freeze does not remove the repository hold.
- R6. Follow Campaign R11 and the supplied hard stops: no remote creation, credential discovery, spend, security-setting changes, sends in Damien's voice, force-push, or deletion without verified backup. Never inspect `.env*`, `*.pem`, `*.key`, `twin-secrets/`, or credential stores; artifacts carry only public-safe evidence and repo-relative paths.

### Key Decisions

- **Retain campaign review guardrails.** Governs R3–R5 (session-settled: user-directed — chosen over holding every expansion for personal review: campaign checks govern ordinary expansions; oa-7 separately preserves this repository's explicit hold).
- **Keep the assigned scope.** Governs R4 (session-settled: user-directed — chosen over speculative features when worthwhile work runs out: campaign capacity goes to durable hygiene).

### Acceptance Examples

- AE1. Covers R1–R3: if the inherited files pass all checks, deliver fresh verification evidence without manufacturing a production change.
- AE2. Covers R2: a successful local full-suite invocation with database-dependent skips leaves service-backed acceptance pending.
- AE3. Covers R5: a local commit bearing “#76” establishes history only; PR supersession requires a controller receipt grounded in the live PR state.

## Planning Contract

### Reverified Starting State

Static inspection on 2026-11-08 found a clean `drawdown/oa-7` at `fd52eff6f8e6b06ede05d140e7068bf016c92b2c`.
Local `dev` is `ace5b9312e82530d902124308f6c57b7187e3e19`, nine commits behind this branch, and is not a freshly verified upstream baseline.
No remote is configured, and neither `drawdown/oa-5` nor `origin/drawdown/oa-5` exists.
Ancestry inspection confirms that `1c74df14`, labelled as PR #76, is already an ancestor of HEAD.
Preserve this descendant tree rather than discarding its later work to reconstruct an older starting point.

In `ontokit/services/health.py`, `_redis_client()` already binds `Redis.from_url(...)` to an explicitly typed Redis local before returning it.
Commit `1c74df14` describes the original Python 3.11 `no-any-return` failure and this repair.
In `tests/unit/test_guided_refusals.py`, the project fixture already supplies `is_public=True` and `members=[]`, allowing the closed-review test to reach its intended refusal after access checking.

`docs/plans/evidence/oa-5/verification.md` predates these final fixes.
Its runtime-environment mypy failure is historical; `docs/plans/evidence/oa-3-mypy-parity.md` documents the separate Python 3.11 checker repair.
Neither receipt proves current oa-7 results.
No tests, lint commands, dependency installation, or runtime probes were run during planning.

The October 20 branch refs are absent, but their merged changes are present as `49ff0bea` (OSV/mako/virtualenv), `38fc1307` (image-deploy contract), and `83af4be6` (AWS readiness).
Context comes from `docs/plans/2026-09-30-fix-u9-evidence-reconciliation-plan.md`, `docs/plans/2026-10-05-fix-dev-image-lock-drift-guard-plan.md`, `docs/plans/2026-10-20-sweep-api-readiness-plan.md`, and `docs/handoffs/2026-10-20-sweep-cloud-ontokit-api.md`; none authorizes expanding this item's work.

### Key Technical Decisions

- KTD1. **Retain the inherited repairs.** Under R3, begin with the existing typed Redis local and complete project fixture. Passing checks warrant an evidence-only delivery; a reproduced residual failure warrants only the smallest source or fixture correction within oa-7.
- KTD2. **Use the authoritative checker environment.** Under R1 and R4, `make typecheck` invokes `scripts/typecheck.sh`, which uses locked Python 3.11 dependencies in `.venv-mypy`. Match CI's Ruff environment, retain strict mypy and its Pydantic plugin, and add no suppressions to evade a diagnostic.
- KTD3. **Use existing behavioral and checker tests as proof.** Under R2–R3, exercise health lifecycle coverage, guided refusals, and the shared-checker contract. The actual mypy run proves the typing repair; a source-text test enforcing the annotation's spelling adds no useful protection. Add a regression only for a newly demonstrated gap.
- KTD4. **Separate local evidence from remote acceptance.** Under R5, record revisions, commands, exits, counts, and skip reasons in `docs/plans/evidence/oa-7/verification.md`. The controller supplies hosted checks and the successor PR receipt; the lane does not infer them from local history.
- KTD5. **Retain the campaign's model choices.** Local work uses Astra; cloud build stages use default Sol and still require Astra/Fable review under R5 (session-settled: user-directed — chosen over applying the Sol-first ladder locally or the local model route to cloud builds: the campaign's dated direction distinguishes these environments).

### Controller Inputs and Execution Unknowns

The chief source `docs/plans/2026-10-26-0705-feat-codex-drawdown-campaign-plan.md` and `ops/drawdown/mandates/_common.md` are not mounted.
The requested `docs/handoffs/2026-10-05-lock-drift-guard-pr63.md` is absent from the tree and available path history.
Campaign R-ID references above identify the supplied standing rules; they do not claim that the unavailable source text was inspected.
The controller reconciles the Goal Capsule and cited R-IDs, plus the handoff or its unavailable-source disposition, before publication.

The live state and failing run of PR #76 remain unverified.
The controller must confirm current upstream `dev`, #76's state, and the successor relationship.
If #76 has already merged, the successor must describe the inherited repair and verification follow-up truthfully; it must not claim to close an open predecessor or re-land its changes.
The controller resolves any resulting mismatch with the catalog's supersession criterion before claiming global completion.
No Damien-only decision is currently established.

The sandbox denies Unix sockets; full integration evidence may require the controller environment.
Dependency availability and actual test results remain build-stage questions.
Neither a missing environment nor an unrelated failing baseline authorizes changing workflows, locks, or test skips.

## Implementation Units

### U1. Verify and preserve the inherited lint repair

**Goal:** Satisfy R1 and R3 using KTD1–KTD3.
**Dependencies:** None; controller metadata is needed only for final external disposition.
**Files:** Inspect `ontokit/services/health.py`, `tests/unit/test_guided_refusals.py`, `tests/unit/test_typecheck_contract.py`, `Makefile`, and `scripts/typecheck.sh`; create `docs/plans/evidence/oa-7/verification.md` for the receipt.

**Approach:** Confirm the inherited source before running read-only diagnostics, then verify the repository lint command and the CI lint contract.
Retain only oa-7 corrections demonstrated necessary by those diagnostics.
Inspect any `make lint` autofix before retaining it; do not absorb unrelated cleanup.
Follow KTD5 for later execution and review.

**Patterns and tests:** `tests/unit/test_health_service.py` already exercises the default Redis factory; `tests/unit/test_typecheck_contract.py` checks runner selection and failure propagation, while the real typecheck checks application typing.

**Verification scenarios:**

1. The current health source passes strict Python 3.11 mypy without a `no-any-return` suppression.
2. The default Redis client retains its one-second socket limits and attempts a failing operation once, as the existing factory test requires.
3. A failing checker or stale lock still propagates a nonzero result without rewriting the lock, using the existing checker contract tests.

**Test expectation:** No new test is needed for an already-present annotation if verification passes; any additional behavior repair must receive a failing-before, passing-after regression in its affected test file.
**Done:** The receipt ties passing lint results to the delivered code, or names the exact unresolved diagnostic without weakening a check.

### U2. Verify regression coverage and the full suite

**Goal:** Establish R2–R3 after U1.
**Dependencies:** U1; service prerequisites for integration acceptance.
**Files:** `tests/unit/test_health.py`, `tests/unit/test_health_service.py`, `tests/unit/test_health_adapter.py`, `tests/unit/test_guided_refusals.py`, `tests/unit/test_api_error_guidance.py`, `tests/unit/test_search_routes.py`, `tests/unit/test_typecheck_contract.py`, and the full `tests/` suite; append results to the U1 receipt.

**Approach:** Exercise the existing focused coverage, then the full suite through the memory wrapper.
These unit and ASGI tests protect response and probe behavior but cannot establish live dependency readiness.
Keep the adapter cancellation qualifications in `docs/plans/evidence/oa-5/verification.md` intact.

**Verification scenarios:**

1. Reviewing a closed or merged PR through the viewable project fixture reaches the expected 400 guidance, with unchanged legacy detail and request correlation.
2. Database/Redis failures remain independent; existing timeout, cleanup, cancellation, and recovery assertions pass without relaxing deadlines or removing cases.
3. Plain errors and liveness retain their existing responses; invalid search input returns 422 before the search service executes.
4. Every collected suite failure is accounted for, and each existing prerequisite skip has a reason and an owner for the missing service-backed evidence.

**Test expectation:** Preserve existing cases and assertions; add only a regression justified by an actual additional repair under R3.
**Done:** The receipt distinguishes focused passes, complete suite invocation, and any unresolved integration acceptance.

### U3. Prepare controller supersession evidence

**Goal:** Complete the local portion of R5–R6 under KTD4.
**Dependencies:** U1–U2 results, including any explicit unresolved checks.
**Files:** Finalize `docs/plans/evidence/oa-7/verification.md`; the controller owns the PR description and hosted receipts.

**Approach:** Record ancestry, retained repairs, verification, simplification/review outcomes, and outstanding controller inputs.
Draft the successor's relationship to #76 subject to its verified state.
Preserve historical receipts and the repository hold.

**Test expectation:** None for this evidence-only unit; inspect links, revisions, public-safe contents, and separation of local results from external claims.
**Done:** Local commits are reviewable and the controller has the evidence needed to publish and resolve supersession, without a false local claim of global completion.

## Verification Contract

These checks belong to the later build stage only.

| Check | Command or evidence | Passing condition |
|---|---|---|
| Initial read-only lint | `uv run --locked ruff check ontokit/ tests/` | Diagnose before autofixing; record any baseline failure |
| Repository lint | `make lint` | Exit 0; inspect resulting diff |
| CI lint environment | `UV_PROJECT_ENVIRONMENT=.venv-mypy uv sync --python 3.11 --locked --group dev` | Locked sync succeeds without manifest or lock changes |
| CI Ruff lint | `UV_PROJECT_ENVIRONMENT=.venv-mypy uv run --python 3.11 --locked ruff check ontokit/` | Exit 0 |
| CI formatting | `UV_PROJECT_ENVIRONMENT=.venv-mypy uv run --python 3.11 --locked ruff format --check ontokit/` | Exit 0 |
| Authoritative typing | `make typecheck` | Exit 0 through the shared runner |
| Advisory typing | `uv run --locked pyright ontokit/` | Zero errors |
| Focused regressions | Locked pytest on U2's named unit files through `scripts/memory-cap.sh` | Pass; no new skips or weakened assertions |
| Full local suite | `make test PYTEST_ARGS="-ra"` | Complete invocation; record passes, failures, skips, and reasons |
| Final diff | `git diff --check` and review of changed files | No whitespace errors, prohibited changes, or lost assertions |
| Controller acceptance | Existing hosted lint and full test jobs, campaign reviews, successor PR receipt | Required checks pass on the delivered revision; #76 disposition verified |

The existing CI test job uses PostgreSQL with pgvector and Redis, applies existing migrations to its disposable test database, and runs the complete suite with coverage through `scripts/memory-cap.sh`.
This existing setup is distinct from authoring or shipping a new migration.
Do not substitute a unit-only result for R2 or change the workflow to obtain a pass.

Check formatting of any changed Python test file.
CI's formatting step covers `ontokit/`; the older oa-5 receipt's unrelated test-formatting failures do not justify a repository-wide reformat.
No browser screenshot applies because R4 excludes frontend changes.

## Definition of Done

- U1 proves R1 on the delivered source and records whether any additional repair was needed.
- U2 supplies current regression and full-suite evidence; service-dependent gaps remain explicit until resolved.
- U3 delivers public-safe local commits and a controller-ready supersession account.
- R3–R6 preservation and scope constraints hold, and no abandoned experimental code remains in the diff.
- Global oa-7 completion requires the controller's verified #76 disposition, successor PR, and passing required checks/reviews. Local success alone establishes neither publication nor permission to remove the repository hold.
