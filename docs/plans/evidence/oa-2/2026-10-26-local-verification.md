# oa-2 local verification receipt

Observed: 2026-10-26T15:50:25Z. Collector: drawdown build lane.
Environment: isolated local clone, not DEV. Scope: U1–U5 preparation, plan R1–R8.
Outcome: local source/contract verification **PASS**; current DEV acceptance
**BLOCKED** as itemized in the [ledger](../../../releases/oa-2-u9-acceptance.md).
This is not a live deployment or acceptance receipt.

## Repository state

| Observation | Result | Limit |
|---|---|---|
| `git status --short --branch` at intake | Clean `drawdown/oa-2` | Local working tree only |
| `git rev-parse HEAD` | `e348f616edbd9e55b0ae6b8da5f1b3179b55c804` | Existing committed oa-2 plan; parent is `83af4be6cdd9f3027e3319eadbd1c1ccfb54c55a` |
| `git rev-parse dev`; `git rev-list --count dev..HEAD` | `62469e0c4cb2d0d322b6fcaec9c1acbdd5e6ae15`; six commits | Local `dev` is not fresh upstream evidence |
| `git remote -v`; `git branch -a` | No remotes or `origin/*`; only local `dev` and `drawdown/oa-2` | No fetch, credential search, or login attempted |
| `git log` / `git show` on named source commits | #63 lock guard, #69 dependency fixes, #70 image contract, #71 readiness are present in available history | The three named October 20 remote branches are unavailable; their contents are not live receipts |
| Explicit campaign/common-mandate/handoff path inventory | Chief repository sources unavailable in this sandbox; October 5 handoff absent, including available path history | G1 remains open; the existing lock-guard plan is a source reference, not a replacement handoff |

The build reverified the plan-stage baseline rather than assuming its observations
were fresh. No application or deployment implementation was changed.

## Source evidence

Inspected the tracked manifest, `deploy/RUNBOOK.md`, `.github/workflows/deploy-dev.yml`,
`deploy/ontokit-deploy.sh`, `deploy/compose.dev.yaml`, `Dockerfile`,
`deploy/restore-dev.sh`, `deploy/tests/backup-restore-test.sh`, the focused tests,
the [lock-guard plan](../../2026-10-05-fix-dev-image-lock-drift-guard-plan.md), and
the [October 20 handoff](../../../handoffs/2026-10-20-sweep-cloud-ontokit-api.md).

- Manifest SHA-256: `61f20e5a2128e5b444e3649d2412ebe8ed7688b1226e33567fb9ea1b9e380f9d`.
  It remains schema 1 with the September #44 API fix and unchanged web SHA.
- `git show` of `455f706c000d87d3d87d7939cfeab470fe78cd2b` and
  `6464f74c68fa942c96e5aa411b250254102c11e4` supports the historical repair and
  manifest statements in the ledger. Their commit messages are claims about
  operations, not independently retrieved operational receipts.
- `git show dcbce302350cc0044aa624ffd2dad76915d93399:deploy/release-manifest.json`
  returns the alternate pair recorded in the ledger. Neither pair was assumed live.
- The workflow's `deploy` job is conditional on preflight configuration. A green
  overall workflow does not establish that this job executed.
- `status` reads revision labels through running containers' image IDs, reports
  checkout drift and nine service states, and fails for missing/unhealthy/drifting
  services. It does not emit image IDs, registry digests, or restart counts.
  Its entrypoint takes a lock and appends an audit outcome; it does not load the
  deployment environment for `status`.
- Restore suppresses SQL import errors and checks the schema revision afterward.
  A completion line alone cannot prove restored data or functioning services.

Public retrieval of the run and issue pages returned `Failed to fetch: Cache miss`
for both on 2026-10-26. No record contents were obtained. This retrieval failure
establishes neither resource absence nor a need for new credentials. Controller
collection still needs the run and issue records.

## Executed local checks

| Procedure | Expected | Observed | Outcome / limit |
|---|---|---|---|
| `python3 deploy/validate_release_manifest.py deploy/release-manifest.json` | Accept the declared schema 1 pair | Exit 0; API `455f706c000d87d3d87d7939cfeab470fe78cd2b`, web `4cbe4d4c17437660b70764acb364d24b20ef20ba`; empty registry digests | PASS for syntax/contract only, not fetchability or runtime |
| Focused pytest invocation below | Existing manifest and readable-runtime-file source contracts pass | Exit 0; 38 passed in 0.24s; one configuration warning | PASS for isolated source contracts only |

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest --noconftest -o addopts='' \
  tests/unit/test_release_manifest.py tests/unit/test_dockerfile_contract.py -q
```

These two self-contained test modules use no application fixtures. Application
conftest loading, automatic plugins, and repository-wide coverage addopts were
disabled for this focused run; all 38 test bodies ran unchanged. Pytest warned
that `asyncio_mode` was unknown because the asyncio plugin was not loaded.
This invocation is not equivalent to the full CI suite. It made no DEV calls.

Documentation verification after authoring passed: all 12 local Markdown links
in the four changed documents resolve; the ledger covers R1–R6 and G1–G5;
new receipts contain no machine-local paths or detected credential markers;
`git diff --check` is clean. A diff against the build-start commit confirms that
`tests/`, `ontokit/`, `alembic/`, and `deploy/release-manifest.json` are unchanged.
The source-attribution review corrected the reviewer-policy account to its
verified 2026-08-28 merge date, rather than inferring an August 10 observation.

## Unperformed checks and their limits

| Check | Outcome | Reason / owner |
|---|---|---|
| Full application suite, mypy, security audit | NOT RUN | No application change; distinct mandate items own broader drift/audit work |
| Deployment shell mock suite | NOT RUN | Plan limits a rerun to separately authorized implementation changes; none occurred |
| Docker backup/restore round trip | BLOCKED | Sandbox denies Unix sockets; no authorized disposable Docker target supplied. No test run or successful restore is claimed |
| Current DEV deployments, status, permissions, FOLIO, identity, recovery | BLOCKED | G1–G5 missing inputs; controller/operator collection required |
| Frontend screenshot | Not applicable | No frontend change |

No existing test was edited or deleted. No backup contents, runtime environment,
credential file, cookie, account details, or private ontology content were read
or retained. The controller should append new dated receipts and reevaluate
the ledger; it must not relabel these local checks as DEV proof.
