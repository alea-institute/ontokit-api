# oa-3 mypy environment parity evidence

Date: 2026-10-27. Item: `oa-3` (planned). Branch: `drawdown/oa-3`.
Plan: `docs/plans/2026-10-26-1109-fix-oa3-mypy-environment-parity-plan.md`.

## Starting state and scope

The build started at plan commit `cc631d451b1187158e30935bee323f4afaed3d04`.
The checkout already contained the plan's runner, caller, exclusion, documentation
and contract-test changes, plus both installed environments. Those changes were
reviewed and retained. This run added a real offline stale-lock regression test
and collected the acceptance evidence below. Implementation commit: `0d68c6ed`.

The chief campaign checkout and the named October 5 lock-drift handoff remain
unavailable in this sandbox. The supplied campaign rules govern this lane.
The U9 reconciliation plan, the related October 5 lock-drift plan, and the
October 20 sweep handoff were read. Local history contains the October 20
dependency, image-contract and readiness commits; none was reimplemented.

No Git remote is configured. Publication, the required CI checks, and campaign
reviews belong to the controller. No credentials, external account actions,
deployments, migrations, releases or dependency upgrades were needed.

## Reproduced failure and repaired entry points

| Component | Runtime environment | Checker environment |
|---|---|---|
| Python | 3.13.11 | 3.11.14 |
| NumPy | 2.5.2 | 2.4.6 |
| mypy | 2.3.1 | 2.3.1 |
| Environment | `.venv` | `.venv-mypy` |

Tooling: uv 0.9.22, pytest 9.1.1, pre-commit 4.6.2, Ruff 0.16.4,
pyright 1.1.411. The NumPy versions match the existing lock's interpreter branches.

`uv run --python 3.13 --locked --group dev mypy --no-incremental ontokit/`
exited 2 with the original failure:

```text
.venv/lib/python3.13/site-packages/numpy/__init__.pyi:737: error: Type statement is only supported in Python 3.12 and greater [syntax]
Found 1 error in 1 file (errors prevented further checking)
```

`make typecheck` passed through `scripts/typecheck.sh` under Python 3.11:
`Success: no issues found in 192 source files`.

`uv run --locked pre-commit run mypy --files ontokit/main.py -v` actually ran
the configured hook and passed over the same 192 source files. It did not skip.
The hook retains its existing file filter, serialized execution and
`pass_filenames: false`.

The runner also passed from another working directory with ambient
`UV_PROJECT_ENVIRONMENT`, `UV_PYTHON`, `VIRTUAL_ENV`, `UV_NO_SYNC`, `UV_FROZEN`,
`UV_PROJECT` and `UV_WORKING_DIR` set to conflicting values. It still checked
all 192 source files in the repository's Python 3.11 environment.

## Preservation and failure behavior

Before/after snapshots of interpreter versions and every installed distribution's
name and version were identical for both `.venv` and `.venv-mypy`. The runtime
environment remained runnable: importing `ontokit.main` and NumPy succeeded,
with NumPy still at 2.5.2. Repeated checking reused `.venv-mypy`.

In a fresh disposable checkout containing the unchanged project manifest, lock,
runtime pin and source, `make setup` installed the runtime dependencies and
pre-commit hook. Two consecutive `make typecheck` calls then passed over all
192 source files, first creating `.venv-mypy` and then reusing it. The lock
remained byte-for-byte identical. The first checker run installed 158 packages;
this environment copied cached files because hardlinking was unavailable.

SHA-256 comparisons confirmed no changes to `uv.lock`, `pyproject.toml`,
`.python-version`, `Dockerfile` or `Dockerfile.prod`. Strict mypy configuration,
its Python 3.11 target and the Pydantic plugin remain unchanged. Other CI jobs
retain their existing interpreter selection and gates.

A disposable dependency-free project was locked with Python 3.11.
Changing its Python compatibility range made that lock stale. The real runner
exited 2, reported that `--locked` prevented an update, and left the lock bytes
unchanged. The stale-lock check runs offline; fixture setup permits uv to obtain
Python 3.11 when it is absent. This is now a regression test, alongside stubbed
failure propagation tests for uv, Make, the configured hook entry, and the CI
shell step.

## Checks

| Check | Result |
|---|---|
| Final unit suite, using `scripts/memory-cap.sh` | 3,792 passed, 1 existing skip; 90% coverage |
| Focused runner, release-workflow and Docker contract tests | 53 passed; also included in the final unit suite |
| Ruff check: `ontokit/` and the new test file | Passed |
| Ruff format check: same scope | 194 files already formatted |
| `uv run --locked pyright ontokit/` | 0 errors, 0 warnings |
| `git diff --check` | Passed |

The final unit suite includes the stale-lock case and strengthened ambient
settings assertions. No existing test was changed, removed or weakened. The
memory wrapper used its existing `prlimit` fallback because systemd user scopes
are unavailable in this sandbox.

## Review and fixes

The simplification pass removed one redundant runner comment. The correctness
review and an independent same-family adversarial reviewer identified two
issues; both were fixed and the reviewer confirmed their resolution:

- The real stale-lock fixture originally required Python 3.11 while disabling
  interpreter downloads. With an empty managed-interpreter directory, it failed
  before exercising the lock check. Allowing acquisition during fixture setup
  made that same cold-interpreter scenario pass; the actual stale-lock check
  remains offline and retains every assertion.
- `UV_NO_DEV=1` and `UV_NO_GROUP=dev` each overrode `--group dev`. Real `uv export`
  runs confirmed that either setting omitted `mypy==2.3.1`. The runner now clears
  both settings, and the strengthened ambient regression failed before that fix
  and passes after it. A real uv resolver probe through the runner retained
  `mypy==2.3.1` with both conflicting settings present, then the checker passed
  over all 192 source files.

No actionable review findings remain. Cross-model review was disabled by this
checkout's configuration. The controller's campaign reviews remain required;
the local same-family review does not substitute for Astra and Fable review.

Full service-backed integration tests and hosted CI were not run here. The
controller must obtain the required lint and full test checks before merge;
local results do not stand in for those checks. No browser route changed, so
browser screenshots do not apply.
