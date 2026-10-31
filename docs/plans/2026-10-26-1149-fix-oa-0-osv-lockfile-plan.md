---
title: Restore the oa-0 OSV dependency scan - Plan
type: fix
date: 2026-10-26
artifact_contract: ce-unified-plan/v1
product_contract_source: drawdown-oa-0-mandate
execution: code
item: oa-0
kind: planned
---

# Restore the oa-0 OSV dependency scan - Plan

## Goal Capsule

- **Objective:** OntoKit maintainers can deliver the BAIF Rome demo API with a passing dependency security check and preserved application behavior.
- **Means:** Refresh the vulnerable locked packages, verify the resulting environment, and provide current scan evidence (KTD1–KTD3).
- **Authority:** The controller's `oa-0` mandate and campaign rules govern this item; repository `AGENTS.md` governs local work. The chief campaign source is `docs/plans/2026-10-26-0705-feat-codex-drawdown-campaign-plan.md`, Goal Capsule and Campaign R11/R15–R20; its unavailable-source limitation is recorded below.
- **Delivery:** Keep the item on `drawdown/oa-0`. This stage delivers only this plan; the later build stage implements and verifies it locally. The controller scans, pushes, and opens the PR targeting `dev`; the campaign gate owns merging (Campaign R15/R17/R19).
- **Stop conditions:** Missing prerequisites keep the affected verification open. A resolver conflict does not authorize changing Python support, suppressing advisories, or expanding into another mandate item.

## Product Contract

### Summary

Update `uv.lock` to remove the current findings for certifi, filelock, GitPython, setuptools, and virtualenv.
Prove the updated environment with the existing tests and type checks, then capture a fresh full-lock OSV result and account for every remaining finding.

### Problem Frame

The October 20 dependency repair is already in this branch, but new advisories now affect five locked packages.
A fresh local scan reproduces the failure.
The API needs current security evidence before its November 5–6 demo; an older clean scan cannot establish that the current dependency set is clean.

### Requirements

**Dependency repair**

- R1. Re-scan the complete `uv.lock` before changes and select available fixed releases from current advisory evidence.
- R2. Resolve every currently flagged package to a fixed version while preserving the declared Python support and unrelated locked dependencies.
- R3. The final full-lock OSV scan has no findings, or the controller's PR documents each remaining advisory, affected version, reason, and next action. A documented residual is not a passing scan.

**Compatibility and evidence**

- R4. The existing test suite, authoritative mypy check, and advisory pyright check pass in their intended environments; preserve all existing tests and assertions.
- R5. Keep the lock consistent with the manifest and consumable by the existing Docker dependency layers. Record commands, environments, outcomes, skips, and scan identity without presenting unavailable checks as passes.

**Scope and campaign boundaries**

- R6. Do not modify `.github/workflows/**`, add ignore entries, weaken scanning, change the `pyproject.toml` Python target, or upgrade unrelated dependencies. No new migration, data rewrite, package release, outside service, deployment, or AWS provisioning belongs to this item.
- R7. Use only this branch and public-safe repository artifacts. Do not add a remote, seek credentials or a GitHub login, read forbidden files, spend, send messages in the user's voice, change security settings, force-push, or delete without a verified backup (Campaign R11/R15–R17/R19).
- R8. Complete independent work without waiting for Damien through 2026-10-30; distinguish controller/environment prerequisites from demonstrated person-only decisions (Campaign R20).

### Key Decisions

- **Proceed within the assigned item while prerequisites are collected.** Governs R8 (session-settled: user-directed — chosen over waiting for Damien: he has zero time through 2026-10-30).
- **Keep campaign delivery policy intact.** Governs R6–R8. The settled Astra choice, guarded expansion merges, and durable hygiene after worthwhile work remain constraints; none adds an expansion or another item to `oa-0`.

### Scope Boundaries

The expected implementation diff is `uv.lock` plus sanitized verification evidence under `docs/plans/evidence/oa-0/`.
No application or frontend behavior is being added, so a changed-page screenshot is not applicable.
Keep `pyproject.toml`, `.python-version`, Dockerfiles, deploy files, and application source unchanged unless a concrete in-scope compatibility defect requires the controller to reconcile the scope.

A new dependency-version unit test or scanner wrapper is considered and not planned: it would duplicate the lockfile or the existing scanner without proving compatibility.
Add a focused regression test only if the upgrade exposes an actual application defect; route a fix outside this item's boundary to the controller.

#### Deferred to Follow-Up Work

`oa-1` owns October 20 landing reconciliation; `oa-2` owns U9 acceptance; `oa-3` owns NumPy/mypy environment drift.
The rehearsal defects, demo-path improvements, and broader coverage/audit belong to `oa-4`–`oa-6`.

## Planning Contract

### Reverified Starting State

Static inspection on 2026-10-26 found a clean `drawdown/oa-0` at `ace5b9312e82530d902124308f6c57b7187e3e19`.
The local `dev` ref is `62469e0c4cb2d0d322b6fcaec9c1acbdd5e6ae15`, six commits behind this branch.
There are no remotes or `origin/*` refs; current upstream `dev` cannot be established from this clone.

The requested October 20 refs are unavailable, but their merged commits are in the branch ancestry: dependency fixes `49ff0bea` (#69), image-deploy contract `38fc1307` (#70), and AWS readiness `83af4be6` (#71).
The current lock retains Mako 1.4.3 and virtualenv 21.14.11 from #69.
`docs/plans/2026-09-30-fix-u9-evidence-reconciliation-plan.md` and `docs/handoffs/2026-10-20-sweep-cloud-ontokit-api.md` were read as context, not as current verification receipts.

The requested `docs/handoffs/2026-10-05-lock-drift-guard-pr63.md` is absent, including available path history.
The existing `docs/plans/2026-10-05-fix-dev-image-lock-drift-guard-plan.md`, Dockerfiles, and contract tests establish the local lock-drift behavior instead.
The chief campaign plan and `ops/drawdown/mandates/_common.md` are also absent.
Campaign R-ID citations here refer to the controller-supplied rules, not independently inspected source text; the controller must supply or reconcile those excerpts before final acceptance.

### Fresh OSV Baseline

On 2026-10-26, before any dependency mutation, official OSV-Scanner 2.5.0 scanned the explicit `uv.lock` using `scan source --lockfile=uv.lock --format=json`.
It extracted 164 packages, filtered one local/unscannable project package, and returned exit 1 with **seven vulnerability groups, eight advisory records, and five affected packages**.
The eight records include two aliases describing the same certifi issue.
An earlier 2.3.5 scan produced the same affected packages and counts.

- Lock SHA-256: `430fec1e7523f4a4a034a290c45eb29c0e3a86978cc7154bf027951d3eb84d5c`.
- Scanner version: 2.5.0; commit `a258868211a57052da6bd323f758b8388dee02bb`.
- Official Linux amd64 binary SHA-256, verified against release metadata: `edcfc41d257db36148f065055655fe3fcfc434b0b423ea67468a84c207524e0c`.
- No dependency resolution, install, application tests, type checks, or recursive repository scan was run in this planning stage. The mandated explicit-lock security scan is the baseline evidence.

| Package | Locked version | Initial fixed target | Current advisory records |
|---|---|---|---|
| certifi | 2026.7.22 | 2026.10.19 | PYSEC-2026-4320; alias GHSA-6q48-6c7w-9gw2 |
| filelock | 3.32.3 | 4.2.1 | GHSA-567q-5qvw-pf92; GHSA-9vhv-jxww-xfm5; GHSA-v4x8-3wr5-3wq8 |
| gitpython | 3.2.0 | 3.2.1 | GHSA-83vg-56qc-22m7 |
| setuptools | 84.0.0 | 84.1.0 | GHSA-xmj3-9gf7-h52w |
| virtualenv | 21.14.11 | 21.14.13 | GHSA-cx6f-vjhg-g3cc |

All five target releases exist on PyPI and their declared Python requirements admit Python 3.11.
The certifi GHSA lists 2026.10.18, while its PYSEC alias lists 2026.10.19; use the latter and verify both records clear.
PyPI's virtualenv 21.14.11 metadata omits its vulnerability, while OSV reports it, so package-index vulnerability metadata is insufficient acceptance evidence.

### Key Technical Decisions

- KTD1. **Use targeted resolution from the existing lock.** With uv 0.9.22, select the table's exact versions through repeated `--upgrade-package` constraints, revised only when a fresh advisory requires it (R1–R2). Do not globally upgrade, recreate the lock, or hand-edit artifact hashes. Review package/version/marker changes semantically; retain only target changes and dependency changes demonstrably required to resolve them.
- KTD2. **Prove both scanner coverage and result.** Capture fresh before/after explicit-lock reports, diagnostics, original exit codes, UTC times, tool version, source revision, and lock hash (R3/R5). Require actual extraction and successful vulnerability lookup; exit 0 with no findings is the clean outcome, exit 1 is findings, and scanner/network/parse errors remain unverified. Alias groups must be reconciled without double-counting.
- KTD3. **Validate the intended Python environments.** Preserve `requires-python >=3.11` and all existing marker splits. Use a separate Python 3.11 locked dev environment for mypy, matching `.github/workflows/release.yml`; use the repository's Python 3.13 environment for the normal suite and pyright. The local system Python 3.14.4 is not the acceptance runtime. Lock validation and subsequent sync/run operations must not silently re-resolve dependencies.
- KTD4. **Match the effective OSV implementation without editing CI.** The local workflow comments say v2.3.5, but the pinned reusable workflow delegates to action `06b2ab4348248b456ee06c9e953637f55e03504f`, whose image tag is v2.5.0. Use the verified 2.5.0 release as the local comparison and record its actual version; the tag alone does not prove the CI container's binary identity. The controller must also obtain the unchanged recursive `scan-full` result on the candidate revision.

### Compatibility and Operational Risks

Filelock crosses a major version.
Its locked consumers are Hugging Face Hub, Torch, python-discovery, and virtualenv.
Public metadata for the first three allows 4.2.1; virtualenv 21.14.13 allows `filelock<5,>=3.24.2` on supported OntoKit Python versions.
These constraints make the targeted upgrade plausible; only resolution and execution can establish compatibility.

Certifi serves HTTPX/httpcore, requests, and MinIO; setuptools is required by Torch; virtualenv is pulled by pre-commit; GitPython is a direct project dependency.
No direct GitPython, filelock, or virtualenv import was found in current application/test source.
Use install/import and tooling smoke checks alongside the existing API suite rather than assuming the pygit2 repository tests alone exercise GitPython.

Both Dockerfiles install the exported lock with uv 0.9.22 and Python 3.13.
Their `uv lock --check --offline` step precedes frozen export; preserve that contract.
The local compose stack rejects a live lock that differs from the image hash, so a later operational rollout needs rebuilt API/worker images; it is not performed by this lane.

### Assumptions and Dependencies

- The available branch is the controller's intended baseline. The controller owns comparison with current upstream `dev`; do not reset to the older local ref or re-land the October 20 changes.
- A fresh scan may change the target set. Additional vulnerable lockfile packages fall under R1–R3 only when current findings justify them; unrelated refreshes remain excluded.
- Public package and vulnerability endpoints remain reachable during build. Capture outages or resolver conflicts as evidence and continue independent work.
- The build environment must provide Python 3.11/3.13 and disposable PostgreSQL 17 with pgvector plus Redis 7, or the controller must supply the missing checks. Unix-socket denial is an environment limitation, not a reason to weaken tests.
- Missing chief-repo excerpts and the October 5 handoff need controller reconciliation. No demonstrated person-only decision is required for this plan.

## Implementation Units

### U1. Refresh the vulnerable locked dependencies

**Goal:** Remove the current vulnerable versions with the smallest compatible lock change.
**Requirements:** R1–R2, R6–R8. **Dependencies:** Reverify the branch and current advisory inventory.
**Files:** Modify `uv.lock`; create sanitized baseline/update evidence under `docs/plans/evidence/oa-0/`.

**Approach:** Apply KTD1 to the current full-lock findings and record old/new versions with their advisory IDs.
Inspect every resolver-induced package, marker, artifact URL, and hash change.
If an indirect dependency must change, document the exact parent constraint that makes it necessary; an unexplained change is not eligible for the diff.

**Patterns:** The #69 lock-only security update and both Dockerfiles' existing locked export.
**Execution note:** Begin with the failing full-lock scan; dependency installation and security scanning are the useful proof for this configuration change.
**Test expectation:** No new unit test solely to restate package versions; preserve the existing tests and verify the actual resolved environment.

**Verification scenarios:**

1. The unchanged starting lock reproduces current findings with package extraction and a recorded exit code.
2. Resolution selects fixed versions for every affected variant while Python markers, manifest constraints, and unrelated package versions remain valid.
3. A fresh vulnerability or incompatibility has an explicit disposition under R3; it never produces an ignore rule or an unsupported Python target.

**Done:** A reviewed minimal lock diff passes lock consistency checks, with each changed dependency explained.

### U2. Verify application and tooling compatibility

**Goal:** Prove the updated locked environment preserves existing behavior.
**Requirements:** R4–R6. **Dependencies:** U1.
**Files:** Run existing `tests/`, including `tests/unit/test_dockerfile_contract.py`, `tests/unit/test_entrypoint_lock_check.py`, `tests/unit/test_health.py`, `tests/unit/test_storage.py`, `tests/unit/test_llm_providers.py`, and `tests/integration/test_git_operations.py`; record results under `docs/plans/evidence/oa-0/`.

**Approach:** Apply KTD3, run the full suite with its real service fixtures, and verify the five installed distributions plus a disposable virtualenv creation/file-lock smoke.
Retain the repository memory cap.
For a failure, compare the same check with the unchanged baseline in isolation; separate an upgrade regression from pre-existing `oa-3` drift without changing tests, checker settings, or the Python target.

**Patterns:** `.github/workflows/release.yml`, `tests/integration/conftest.py`, and `scripts/memory-cap.sh`.
**Test expectation:** Existing behavior coverage plus runtime/tooling smoke checks; add a focused regression test only for a concrete authorized compatibility fix.

**Verification scenarios:**

1. Python 3.13 installs the locked dev group, the named distributions import, and disposable virtualenv creation/file locking succeeds with the new tooling versions.
2. The full test suite passes with PostgreSQL/pgvector and Redis available; service-dependent skips are listed and do not establish integration success.
3. Strict mypy passes with the Python 3.11 dependency selection, and pyright reports zero errors for `ontokit/` in the intended environment.
4. Existing Docker/entrypoint tests still reject stale image locks and accept matching locks; the current lock passes the Docker-equivalent freshness/export checks.

**Done:** R4–R5 have actual results or precise outstanding environment/check failures. Outstanding failures prevent an unconditional compatibility-pass claim.

### U3. Capture final security and delivery evidence

**Goal:** Give the controller an auditable remediation result.
**Requirements:** R3, R5–R8. **Dependencies:** U1 and U2 outcomes.
**Files:** Finalize sanitized receipts under `docs/plans/evidence/oa-0/`; do not edit the workflow or this plan to track execution progress.

**Approach:** Apply KTD2/KTD4 to the final candidate lock and reconcile each baseline advisory with its final state.
The controller runs the unchanged recursive scan in its normal clean CI environment; a constrained local recursive substitute must not traverse campaign-forbidden files or invent exclusions.
Include each remaining advisory's reason and next action in the handoff for the PR.

**Test expectation:** No new test code; the full-lock scanner, unchanged CI scan, and U2 results supply acceptance evidence.
**Verification scenarios:**

1. A successful current scan covers the full lock and clears every baseline group, including both certifi aliases.
2. A lookup/extraction error, remaining finding, or missing full-suite/type-check result remains visible and cannot become a green receipt.
3. The final diff contains only the assigned item's lock/evidence changes and preserves every restriction in R6–R7.

**Done:** The controller can distinguish a clean local lock scan, compatibility results, remaining limitations, and the still-required upstream workflow result.

## Verification Contract

These checks belong to the build stage; only the explicit-lock baseline scan was executed during planning.
Use locked sync/run operations so validation cannot change the dependency set.

| Check | Required evidence |
|---|---|
| Lock consistency | `uv lock --check`, plus the existing Docker-equivalent offline freshness check and frozen runtime export; no mutation of the reviewed lock |
| Runtime installation | Locked dev-group sync for Python 3.13 and a separate Python 3.11 checker environment; verified installed target versions |
| Existing suite | The `scripts/memory-cap.sh`-wrapped `pytest tests/ -v --cov=ontokit --cov-branch` invocation with PostgreSQL 17/pgvector and Redis 7; counts, failures, and skip reasons |
| Lint and format | `ruff check ontokit/` and `ruff format --check ontokit/` pass |
| Authoritative types | `mypy ontokit/` passes in the locked Python 3.11 environment with existing strict/Pydantic settings |
| Advisory types | `pyright ontokit/` reports zero errors; a soft CI gate is not permission to ignore the mandate |
| Security | `osv-scanner scan source --lockfile=uv.lock --format=json` with KTD2 receipts, followed by the controller's unchanged recursive full workflow result |
| Diff boundary | No forbidden files changed, existing tests preserved, each dependency change justified, evidence free of machine-local paths and private data |

Existing database migrations may be applied only to fresh disposable test databases as fixture setup.
This does not authorize adding a migration or changing deployed data.
Unavailable services, socket restrictions, missing interpreters, and historical passing results do not satisfy R4.

## Definition of Done

The plan-stage deliverable is this reviewed plan committed locally on `drawdown/oa-0`, with no dependency or implementation change.

Implementation satisfies the mandate when R1–R8 are evidenced: the flagged packages resolve to fixed versions, the existing suite and type checks pass, and the local full-lock scan is clean or every residual is individually documented for the PR.
Residual findings keep the security objective open.
The controller reconciles missing authority sources and the current `dev` baseline, publishes the candidate, and verifies the unchanged full workflow; lane completion is not itself proof that CI is green on `dev`.
The final diff must contain no abandoned experiments or unrelated changes.

Rollback is a focused revert of the dependency change, which restores the prior vulnerable lock and must be treated as such.
No deployment, release, migration delivery, or data operation is required by this plan.

## Sources

- `AGENTS.md`; `.github/workflows/osv-scanner.yml`; `.github/workflows/release.yml`; `pyproject.toml`; `uv.lock`; `Dockerfile`; `Dockerfile.prod`.
- `docs/plans/2026-10-05-fix-dev-image-lock-drift-guard-plan.md`; `docs/handoffs/2026-10-20-sweep-cloud-ontokit-api.md`.
- [Pinned OSV reusable workflow](https://raw.githubusercontent.com/google/osv-scanner-action/8deb546fdb875b9996d27d4950be7312dac076a1/.github/workflows/osv-scanner-reusable.yml), [nested action](https://raw.githubusercontent.com/google/osv-scanner-action/06b2ab4348248b456ee06c9e953637f55e03504f/osv-scanner-action/action.yml), [OSV 2.5.0 release](https://github.com/google/osv-scanner/releases/tag/v2.5.0), and [OSV exit/report semantics](https://google.github.io/osv-scanner/output/).
- [certifi PYSEC record](https://osv.dev/vulnerability/PYSEC-2026-4320), [certifi GHSA](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2), [virtualenv GHSA](https://github.com/advisories/GHSA-cx6f-vjhg-g3cc), and [setuptools GHSA](https://github.com/advisories/GHSA-xmj3-9gf7-h52w).
- [uv 0.9.22 targeted locking](https://raw.githubusercontent.com/astral-sh/uv/0.9.22/docs/concepts/projects/sync.md) and [universal Python resolution](https://raw.githubusercontent.com/astral-sh/uv/0.9.22/docs/concepts/resolution.md).
