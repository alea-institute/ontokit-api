# oa-7 lint repair verification

Date: 2026-11-08. Branch: `drawdown/oa-7`. Kind: expansion.
Plan: `docs/plans/2026-11-08-1342-fix-oa7-lint-repair-plan.md`.

The inherited oa-5 lint repair passes the current lint contract. One additional
test-harness failure was reproduced and fixed without changing its assertions.
The full local suite passes with 154 existing prerequisite skips. Service-backed
acceptance, hosted checks, campaign reviews, and PR supersession remain with the
controller. The repository's explicit gate hold remains in force.

## Starting state and retained work

The build started from clean plan commit
`87f59ad8c20e1564174754d6f793f02e0ada15f3`, whose parent is
`fd52eff6f8e6b06ede05d140e7068bf016c92b2c`. Local `dev` remains
`ace5b9312e82530d902124308f6c57b7187e3e19`; it is not a freshly verified upstream.
No remote is configured, and the two requested oa-5 branch refs are absent.

Commit `1c74df145ba48e16eec4fba56008829e3af6230e`, labelled as PR #76 in local
history, is an ancestor of this branch. Its health client already uses an
explicitly typed Redis local. Its guided-refusal fixture already provides
`is_public=True` and `members=[]`. Both files are unchanged since that commit.
The October 20 dependency, image-contract, and readiness work is also retained
through `49ff0bea`, `38fc1307`, and `83af4be6`.

The chief campaign plan and common mandate source are not mounted. The requested
`docs/handoffs/2026-10-05-lock-drift-guard-pr63.md` is absent from the available
tree and path history. The build used the supplied campaign rules, repository
instructions, U9 reconciliation plan, October 5 lock-drift plan, and October 20
sweep handoff. These missing sources remain controller reconciliation inputs.

## Reproduced residual failure and repair

Initial focused verification passed all 90 cases. The initial full command,
`make test PYTEST_ARGS="-ra"`, exited 2: **4,130 passed, 154 skipped, 1 failed**.
The failure was `test_setup_installs_dev_group_and_hooks` in
`tests/unit/test_typecheck_contract.py`.

Running pytest through Make supplies a nonzero `MAKELEVEL` to subprocesses.
The test's nested `make --dry-run setup` then prints entering/leaving-directory
banners around its recipes. The exact stdout assertion correctly rejected those
extra lines. Direct pytest invocation did not expose the problem.

The test now exercises `MAKELEVEL=0` and `MAKELEVEL=1`. Before the fix, that
parameterization produced **1 passed, 1 failed**, with the nested case failing
on the directory banners. The probe now uses `--no-print-directory`, preserving
the exact expected commands and the existing exit-code assertion:

```text
uv sync --group dev
uv run pre-commit install
```

No assertion is filtered, relaxed, or removed. The Makefile, setup behavior,
application code, and every other test remain unchanged. The repair is committed
as `3407061267d32a4299f7d1588928080f93e9b1a1`; all final results below use those
code and test bytes. Later commits add documentation only.

## Checker environment recovery

Runtime: Python 3.13.11 in `.venv`. Authoritative checker: Python 3.11.14 in
`.venv-mypy`. Tools: uv 0.9.22, Ruff 0.16.4, mypy 2.3.1, pytest 9.1.1, and
Pyright 1.1.411.

The first locked Python 3.11 sync succeeded, but the following Ruff invocation
exited 2 because its executable was absent despite installed package metadata.
The first `make typecheck` also failed: missing installed typing markers caused
280 diagnostics in 93 files. An audit against distribution file records found
15 incomplete installations, including SQLAlchemy, NumPy, and cryptography.

The affected packages were restored using `uv sync --python 3.11 --locked
--group dev`, with `UV_PROJECT_ENVIRONMENT=.venv-mypy` and one
`--reinstall-package` argument for each affected distribution. The restore
exited 0. A second file-record audit found no missing non-bytecode files. No
package version, manifest, lockfile, Python target, suppression, or checker
configuration changed. The complete CI lint sequence then passed.

## Final verification

| Check | Result |
|---|---|
| `uv run --locked ruff check ontokit/ tests/` | Initial read-only diagnostic passed. |
| `make lint` | Exit 0 after the repair; no autofix changes. |
| `UV_PROJECT_ENVIRONMENT=.venv-mypy uv sync --python 3.11 --locked --group dev` | Exit 0; 158 installed packages audited. |
| `UV_PROJECT_ENVIRONMENT=.venv-mypy uv run --python 3.11 --locked ruff check ontokit/` | Exit 0. |
| `UV_PROJECT_ENVIRONMENT=.venv-mypy uv run --python 3.11 --locked ruff format --check ontokit/` | Exit 0; 199 files already formatted. |
| `make typecheck` | Exit 0; no issues in 198 source files through the unchanged shared runner. |
| `uv run --locked pyright ontokit/` | Exit 0; 0 errors, 0 warnings. |
| `uv run --locked ruff format --check tests/unit/test_typecheck_contract.py` | Exit 0; changed test file already formatted. |
| Focused command below | Exit 0; **91 passed**, 1 warning, 15.42 seconds. |
| `make test PYTEST_ARGS="-ra"` | Exit 0; **4,132 passed, 154 skipped**, 24 warnings, 110.21 seconds; **90%** rounded coverage. |
| `git diff --check` | Passed. |

Focused command:

```bash
bash scripts/memory-cap.sh uv run --locked pytest \
  tests/unit/test_health.py tests/unit/test_health_service.py \
  tests/unit/test_health_adapter.py tests/unit/test_guided_refusals.py \
  tests/unit/test_api_error_guidance.py tests/unit/test_search_routes.py \
  tests/unit/test_typecheck_contract.py -ra --no-cov
```

Both test commands used the existing 6,144 MiB `prlimit` fallback because systemd
user scopes are unavailable. The focused tests retain the Redis socket/retry,
health cleanup/cancellation, independent dependency status, closed-review,
request-correlation, search-validation, and checker failure-propagation coverage.
The installed-adapter timing and delayed-cleanup qualifications in
`docs/plans/evidence/oa-5/verification.md` still apply; these tests do not prove
live dependency readiness or an absolute driver-shutdown deadline.

The full suite's **154 skips** are unchanged between baseline and repaired runs:

| Existing prerequisite | Cases | Acceptance owner |
|---|---:|---|
| `DATABASE_URL` absent | 147 | Controller: disposable migrated PostgreSQL with pgvector. |
| Disposable B12 PostgreSQL and Redis services absent | 6 | Controller: existing B12 integration setup. |
| `REDIS_URL` absent | 1 | Controller: disposable Redis. |

These skips are unverified integration coverage. The 24 warnings also occurred
in the baseline: existing deprecations, unregistered integration marks, and
unawaited mock-coroutine warnings. No warning filter or skip condition changed.

## Local quality and publication boundary

The simplification pass used one combined reviewer for reuse, quality,
efficiency, and test value. It found no changes to make. The new proof test and
all existing assertions were preserved.

No frontend file or rendered route changed, so browser testing and a changed-page
screenshot do not apply. The branch changes no production behavior, dependency
manifest or lock, workflow, deployment configuration, migration, or release.

## Controller handoff

Suggested successor title: **Verify inherited oa-5 lint repair and fix nested
Make test invocation**.

Suggested description: The branch retains the Redis typing and guided-refusal
fixture repairs already present in the commit associated with #76. Current lint,
formatting, mypy, Pyright, and the local full-suite invocation pass. The remaining
test-harness repair covers direct and nested Make contexts while retaining exact
setup assertions. Service-dependent skips remain explicit in this receipt.

Before claiming global completion, the controller must:

1. Reconcile the unavailable campaign excerpts and October 5 handoff disposition.
2. Confirm current upstream `dev` and the live state of #76. If #76 is already
   merged, describe this as a verification follow-up and resolve the catalog's
   supersession wording; do not claim to close an open predecessor or re-land its
   changes. If still open, verify and record the successor relationship.
3. Scan and publish this branch against `dev`, obtain required hosted lint and
   full service-backed test results, and complete the campaign Astra/Fable reviews.
4. Preserve the repository gate hold. Local passing checks do not remove it.

No Damien-only decision was established by this build. There was no push, PR
creation, deployment, credential discovery, or remote configuration change.
