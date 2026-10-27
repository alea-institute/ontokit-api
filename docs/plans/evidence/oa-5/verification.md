# oa-5 build verification

Date: 2026-10-27. Branch: `drawdown/oa-5`. Kind: expansion.
Plan: `docs/plans/2026-10-27-0933-feat-oa5-demo-api-feedback-plan.md`.
Plan SHA-256: `2dd14de5bde513d2c35c156763f3560ba83df0534c2557018682c92bc405107c`.

## Scope and baseline

The clean starting revision was `32bfbfc12e57c78045761f9681a6347f97becda9`.
The selected plan was already committed. Local `dev` remains `62469e0c`, without
a configured remote or upstream refs; it is not a freshly verified upstream.
The October 20 fixes and image/readiness work remain in ancestry and were not
re-landed or changed. The chief campaign plan, common mandate file, and requested
October 5 handoff remain unavailable in this supplied checkout. The controller's
supplied rules governed independent implementation.

The untouched tracked-test baseline completed with **3,793 passed, 148 skipped**.
Tracked test paths were enumerated with `git ls-files tests/` to exclude new tests
being authored concurrently, then passed to `uv run --locked pytest` with `-v
--cov=ontokit`. An earlier exploratory full-suite run included new failing tests
and was interrupted; it is not the baseline receipt.

Dependencies were installed using `uv sync --locked` and the configured cache.
The runtime is Python 3.13.11; the repository's Python 3.11 type-checking target
and dependency files are unchanged.

## Implementation and proof

The initial integrated implementation revision is `7533b92a` (U1–U4).
Documentation followed in `fbe3a796`. Subsequent simplification and review
corrections are described below; they preserve the response and permission
contracts.

| Unit | Change and proof |
|---|---|
| U1, `31c88c7a` | Opt-in `GuidedHTTPException`, optional OpenAPI schema, and dedicated HTTP handler. New tests first failed on the absent exception; subsequent full ASGI tests prove exact string/nested detail, headers, CORS, supplied/generated request correlation, and unchanged ordinary/domain/validation/204 bodies. Existing exception/main tests remain unchanged. |
| U2, `15a05708` | Full-text query constraints moved to request validation. Before implementation four HTTP cases returned 500 instead of 422; eight characterization cases passed. After implementation 24 new/existing search tests passed, proving zero service/SQL work for invalid inputs and unchanged filters, bounds, defaults, tokenless queries, and SPARQL service behavior. |
| U3, `7533b92a` | Only KTD2's guards receive guidance. Actual suggestion/PR services are exercised through ASGI, with exact legacy details and side-effect ordering checked. Ten HTTP and six schema cases failed before implementation; bulk handling characterized successfully. Two existing shared-approval tests were strengthened without removing their assertions. Focused regression run: 633 passed. |
| U4, `2c6dd045` | Concurrent current database/Redis probes, bounded operations/cleanup, cancellation propagation, and separate readiness HTTP route. Service tests first failed on the missing module; 11 HTTP cases failed on the absent route/schema. False PING and invalidation-failure tests also reproduced failures before fixes. Integrated health/adapter/main/exception run: 69 passed. |
| U5 | Consumer contract and synthetic old/new fixtures in `docs/api/demo-feedback.md`, linked from README. No separate documentation test; HTTP/OpenAPI tests supply the contract evidence. Actual companion parser verification is still G2. |

Readiness tests include controlled acquisition/query/ping stalls, cleanup stalls
and failures, independent dependency results, fresh Redis recovery, cancellation,
sanitized failure responses/logs, and no surviving application-owned probe tasks.
Tests using the installed Redis connection/pool and a synthetic stalled writer
verify actual transport close/cancellation behavior. Database cleanup avoids
SQLAlchemy's context-manager shielded close task and invalidates a failed pooled
connection. This does not eliminate SQLAlchemy's separate shielded invalidation
path, described in the review qualification below. If both graceful close and
invalidation fail, the result is `unavailable` with a fixed safe warning; actual
driver resource release in that exceptional case is not proved by mock tests.

## Verification results

| Command/check | Observed outcome |
|---|---|
| `uv run --locked pytest tests/ -v --cov=ontokit -ra` | Initial implementation: **3,862 passed, 148 skipped**, 20 warnings, 90% rounded coverage; 37.07 seconds. Final reviewed tree: **3,866 passed, 148 skipped**, 20 warnings, 90% rounded coverage; 46.27 seconds. No existing test removed or weakened. |
| `uv run --locked ruff check ontokit/ tests/` | Pass. |
| `uv run --locked ruff format --check ontokit/ tests/` | Fails on the same **20 existing test files** present before implementation. Changed/new files pass scoped formatting. No unrelated reformat. |
| `uv run --locked mypy ontokit/` | Blocked before checking application code: installed `numpy/__init__.pyi:737` uses a type statement requiring Python 3.12+, while the repository targets 3.11. Same failure before and after. This is the separate `oa-3` mandate; no target/dependency change here. |
| `uv run --locked pyright ontokit/` and `.venv/bin/pyright ontokit/` | **0 errors, 0 warnings**. |
| `git diff --check` | Pass. |

The 148 skips are **141** requiring `DATABASE_URL`, **6** requiring disposable
B12 PostgreSQL and Redis, and **1** requiring `REDIS_URL`. This includes all eight
suggestion-submission recovery cases and seven PR-concurrency cases. These are
existing prerequisite skips, not successful integration checks. No credentials
were sought and no live DEV state or provider was contacted.

## Review and browser evidence

`ce-simplify-code` completed all five lenses. It flattened nested exception
handling, removed a redundant cancellation branch and redundant summaries, and
preserved all proof tests. Its 86 focused tests, Ruff, scoped formatting, and
Pyright passed; mypy retained the baseline blocker.

`ce-code-review` run `20261027-100522-72e33b01` completed eight lenses:
correctness, project standards, testing, maintainability, security, API contract,
reliability, and adversarial review. Its verdict was **Ready with fixes**, with
one confirmed P2 finding: qualify readiness timing/cleanup claims and add actual
adapter characterization. Correctness, reliability, and adversarial reviewers
independently identified it; a separate validator confirmed it. The fix adds
`tests/unit/test_health_adapter.py` and corrects the service comment, consumer
contract, and this receipt. No existing tests were removed or weakened.

**KTD4/U4 timing qualification:** the one-second operation and 250 ms cleanup
timers initiate cancellation; they do not bound the entire driver shutdown.
On SQLAlchemy 2.0.52 and asyncpg 0.31.0, installed-adapter reproductions measured
3.004 seconds for SELECT and pooled pre-ping, and 2.253 seconds for rollback
cleanup. With a second cancellation during shielded termination, the probe
returned at 1.002 seconds while a library task and synthetic transport remained
until 3.005 seconds. Pool checkouts were zero; cleanup eventually completed.
These are synthetic query/transport observations, not a live connection or
production-incidence claim.

The accepted implementation deviation retains the shared engine/pool and
records those limits, instead of claiming the plan's two-second target for
actual adapter cancellation or a complete driver-task drain before return.
That target remains proved only for injected cooperative probe boundaries.
Four added adapter tests cover SELECT, pre-ping, rollback, and repeated
cancellation, preserving all previous lifecycle tests. The correction addresses
the review's evidence/documentation finding; it does not repair a strict total
deadline. Live-service timing and cancellation remain controller evidence gaps.

No actionable review finding remains unapplied after this correction. No
session-settled decision was changed. Cross-model review is disabled by checkout
configuration; these were native review lenses, not the campaign Astra/Fable
reviews. Those reviews and required CI remain controller-owned. No separate
learning document is needed because the rationale is captured here, in the
consumer contract, and in the adapter tests.

No frontend files changed. API request/response checks use ASGITransport; there
is no changed-page screenshot. Actual `ontokit-web` parser/display checks and
live dependency/driver acceptance are not claimed by these tests.

## Controller release gates

| Gate | Outstanding evidence | Owner and boundary |
|---|---|---|
| G1 | Campaign Goal Capsule/R11/R15–R20 and unavailable-source disposition for the October 5 handoff | Controller, before publication. |
| G2 | Actual web parser consumes old/new fixtures, including any branch selected by presence of `error` | Companion web lane, before release. |
| G3 | Current upstream `dev`, required checks/reviews, integration services, and freeze eligibility | Controller/campaign merge gate. Local tests do not prove current upstream or DEV acceptance. |

No person-only decision, held migration, data rewrite, package release, or new
outside service was introduced. No workflows, deployment policy, dependency
versions, or credential/security settings changed. The controller owns scanning,
push, PR creation, and merging; this lane delivers local commits only. The
November 3–6 expansion merge freeze remains in force.
