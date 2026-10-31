# oa-6 baseline — 2026-10-28 UTC

Starting revision: `241a430b` on `drawdown/oa-6`; clean index/worktree, no remotes.
Local `dev`: `62469e0c`; controller must reconcile fresh upstream. Plan checkpoint:
`docs/plans/2026-10-27-2050-test-suggestion-review-audit-plan.md`.

## Environment and verification

- CPython 3.13.11; pytest 9.1.1; FastAPI 0.141.1; NumPy 2.5.2; mypy 2.3.1; pyright 1.1.411.
- `uv sync --locked --dry-run --offline`: 164 lock packages resolved; 158 installed packages audited; no changes needed.
- Lock SHA256: `430fec1e7523f4a4a034a290c45eb29c0e3a86978cc7154bf027951d3eb84d5c`.
- `XDG_CACHE_HOME` was unset. Tool caches and scratch evidence were explicitly assigned to the controller-provided cache directory outside the checkout; no lock/config change.
- `bash scripts/memory-cap.sh .venv/bin/pytest tests/ --ignore=tests/unit/test_migration_inventory.py -q --cov=ontokit --cov-branch --cov-report=json:<audit-cache>/baseline-coverage.json`: **PASS for this subset**, 3,789 passed, 148 skipped, 20 warnings, 27.00 seconds. Exit 0. `baseline-pytest.log` preserves output.
- The complete suite is **NOT RUN**: the excluded inventory file reads `deploy/.env.example`, forbidden by Campaign R11. No existing test was edited, disabled or weakened. Integration skips are missing service configuration, not persistence passes.
- The memory wrapper used its existing 6 GiB `prlimit` fallback because the systemd user scope is unavailable.
- `bash scripts/memory-cap.sh .venv/bin/mypy ontokit/`: **FAIL baseline**, exit 2: NumPy stub type statement requires Python >=3.12 against the project's unchanged Python 3.11 target. oa-3 owns remediation. A separate locked Python 3.11 environment will also be attempted.
- `bash scripts/memory-cap.sh .venv/bin/pyright ontokit/`: **PASS**, exit 0, zero errors/warnings.
- Neither OSV-Scanner nor Semgrep nor PostgreSQL/Redis server binaries is available on PATH. U5 will attempt public tool acquisition; U4 requires a disposable migrated PostgreSQL service, not mocks or SQLite.

## Coverage baseline and missing boundaries

`baseline-coverage.json` contains measured line/branch summaries and missed branches for scoped modules, including `PullRequestService.create_review`. Route coverage: suggestions 50/70 lines; anonymous 51/54 lines and 5/6 branches; PR routes 164/168 lines and 32/34 branches.

| Boundary | Existing evidence | Planned addition |
|---|---|---|
| Authenticated suggestions | HTTP service mocks for reopen/resubmit; extensive direct-service lifecycle tests | Review payloads, queues/capabilities, bulk partial results, limiter outage mapping |
| Anonymous suggestions | HTTP mode/token/beacon/honeypot guards; direct-service beacon binding | Normal submit/discard and real token + real service non-beacon refusal cases |
| Human PR reviews | HTTP service mocks; direct-service roles/closed state | Private nonmember comment regression; allowed controls and terminal states |
| Persistence | Existing DB/Redis suites skipped in this sandbox | Commit-contained request-level isolation/control fixture on PostgreSQL |

The documented comment-review view predicate is still absent from `create_review`; U3 must demonstrate the failure before applying the narrowly authorized correction.

## Source availability

The chief campaign plan and mandate common file are absent from the supplied workspace; the prompt remains the authority excerpt. The October 5 handoff is also absent. The U9 reconciliation plan and October 20 handoff were inspected; commits `49ff0bea`, `38fc1307`, `83af4be6` remain ancestors. No remote, login, credential, or DEV access was attempted. These are controller evidence dependencies, not personal decisions for Damien.
