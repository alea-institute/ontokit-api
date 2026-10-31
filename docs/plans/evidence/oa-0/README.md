# oa-0 dependency remediation evidence

On 2026-10-26, the candidate `uv.lock` cleared all seven vulnerability groups
(eight advisory records across five packages) found in the committed baseline.
OSV-Scanner 2.5.0 extracted 164 packages in both runs, filtered one local project
package, and returned exit 1 before the update and exit 0 afterward.
There are no remaining findings in the explicit-lock scan.

| Package | Baseline | Candidate | Cleared advisory records |
|---|---|---|---|
| certifi | 2026.7.22 | 2026.10.19 | PYSEC-2026-4320; GHSA-6q48-6c7w-9gw2 (aliases) |
| filelock | 3.32.3 | 4.2.1 | GHSA-567q-5qvw-pf92; GHSA-9vhv-jxww-xfm5; GHSA-v4x8-3wr5-3wq8 |
| gitpython | 3.2.0 | 3.2.1 | GHSA-83vg-56qc-22m7 |
| setuptools | 84.0.0 | 84.1.0 | GHSA-xmj3-9gf7-h52w |
| virtualenv | 21.14.11 | 21.14.13 | GHSA-cx6f-vjhg-g3cc |

## Candidate identity and resolution

- Starting commit: `51bd6f2802344d4e45b426e87684d0d2730be11a`.
- Baseline lock SHA-256: `430fec1e7523f4a4a034a290c45eb29c0e3a86978cc7154bf027951d3eb84d5c`.
- Candidate lock SHA-256: `aa2adf74b9ceb0fbf00006338499ec10806ec1014bebf83fd13a3f962993c0a0`.
- The scan receipts identify the starting commit plus the lock hash; the candidate
  lock was uncommitted when scanned. Scanner source paths are normalized to
  `uv.lock`; temporary locations in verification commands use a placeholder.

The build began with a supplied, uncommitted five-package patch. It was preserved
and independently reproduced by resolving a scratch copy of the committed lock:

```sh
uv lock --upgrade-package certifi==2026.10.19 \
  --upgrade-package filelock==4.2.1 \
  --upgrade-package gitpython==3.2.1 \
  --upgrade-package setuptools==84.1.0 \
  --upgrade-package virtualenv==21.14.13
```

The five package entries, including artifact URLs and hashes, match uv's output.
Only those complete entries were applied. Resolution also rewrote unrelated
dependency markers in cuda-bindings, cuda-toolkit, and both scipy variants;
those changes were excluded. All other package entries and top-level lock
metadata remain byte-for-byte unchanged. Both online and offline freshness
checks accept the focused result. See `resolver-receipt.json`.

## Compatibility results

| Check | Result |
|---|---|
| `uv lock --check`, `uv lock --check --offline` | Pass; 164 packages |
| Docker-style `uv export --frozen --no-dev --no-hashes --no-emit-project` | Pass |
| Locked dev sync | Pass on Python 3.13.11 and separately on Python 3.11.14 |
| Full existing suite, with branch coverage and 6,144 MiB memory cap | 3,935 passed, 6 skipped, 28 warnings; 88% combined statement/branch coverage |
| Focused Docker, entrypoint, health, storage, LLM-provider and git tests | 51 passed |
| `ruff check ontokit/`; `ruff format --check ontokit/` | Pass; 193 files formatted |
| `mypy ontokit/` in the locked Python 3.11 environment | Pass; 192 source files |
| `pyright ontokit/` in the locked Python 3.13 environment | 0 errors, 0 warnings |
| Package smoke | All five versions/imports verified; certifi SSL context, file-lock contention/release, GitPython commit/read and virtualenv creation/execution pass |

The full suite ran with independently built, disposable PostgreSQL 17.11
(pgvector 0.8.7 and pg_trgm 1.6) and Redis 7.4.10 over loopback TCP. Only existing
migrations were applied, to a fresh test database. No deployed database or
existing service was changed. Docker's Unix socket was unavailable, so this is
not a container-build receipt. The memory wrapper used its existing `prlimit`
fallback because the systemd user scope was unavailable.

The six skipped cases in `tests/integration/test_notification_sockets.py` require
the B12 opt-in service variables and a Unix-domain listener. A direct listener
probe returned `PermissionError: [Errno 13] Permission denied`. Those cases
remain unverified in this sandbox; no test, skip condition, assertion, or
checker setting was changed. Exact case names and skip reasons are in
`verification.json`.

Python 3.11 selected NumPy 2.4.6; Python 3.13 selected NumPy 2.5.2. The successful
mypy result uses the CI lint job's Python 3.11 environment and does not claim to
repair the separate oa-3 local-environment issue.

No new test was added solely to repeat locked versions. The actual scanner,
installation, runtime smoke checks, and unchanged suite provide the evidence
required by the plan's configuration-only test strategy.

## Security receipts

- `baseline-osv.json` and `baseline-osv.log`: complete baseline result and
  extraction diagnostics; `baseline-scan-receipt.json` records times and exit code.
- `final-osv.json` and `final-osv.log`: clean candidate result and extraction
  diagnostics; `final-scan-receipt.json` records times and exit code.
- `scanner-provenance.json`: official release download and SHA-256, checked
  against the release asset digest. The binary reports commit
  `a258868211a57052da6bd323f758b8388dee02bb`.
- `verification.json`: commands, exit codes, environments, test counts, skips,
  type-check output, and smoke results.
- `review.json`: completed local correctness review with no findings. Its
  `Not ready` acceptance verdict keeps the controller checks below open;
  independent campaign reviews remain with the controller.

Both security runs used:

```sh
osv-scanner scan source --lockfile=uv.lock --format=json
```

## Controller acceptance checks

1. Compare with current upstream `dev`; the clone has no remotes and its local
   `dev` ref is older than the supplied branch. The October 20 changes are
   already present in ancestry and were not re-landed.
2. Reconcile the unavailable chief campaign Goal Capsule/R-ID excerpts and
   `docs/handoffs/2026-10-05-lock-drift-guard-pr63.md`, as the plan records.
3. Run the unchanged recursive OSV `scan-full` workflow on the candidate revision,
   plus normal CI and container checks. This explicit-lock scan is not a claim
   that the recursive workflow passed or that `dev` is already green.
4. Run the six B12 notification cases with their disposable-service variables
   in an environment permitting Unix sockets.

The publication and campaign review gates remain with the controller. No
person-only decision, new migration, release, deployment, outside service,
workflow edit, ignore rule, Python-target change, or unrelated upgrade is
included. A future rollout needs rebuilt API/worker images because the existing
development lock-drift check rejects images containing the old lock.
