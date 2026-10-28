# oa-6 implementation and verification — 2026-10-28 UTC

Source baseline `241a430b`; implementation commits `10b499d1` (U2), `fbb520e5` (U3), and `668a3794` (U4). The source plan remains unchanged at `docs/plans/2026-10-27-2050-test-suggestion-review-audit-plan.md` (SHA256 `eaaef810ea2ea31107691d9923bde4aa024179089a5344ca2b6c08d66678b209`). Native implementation, no external implementation route or model receipt; no model equivalence claimed. No settled-decision conflict.

## Results and limitations

| Check | Result | Evidence |
|---|---|---|
| Baseline subset, no configured services | PASS: 3,789 passed, 148 skipped | `baseline-pytest.log` |
| Integrated U2/U3 unit and companion tests | PASS: 789 passed | `u2-u3-verified.log` |
| New PostgreSQL boundary suite | PASS: 6 passed, no skips | `u4-verified.log` |
| Required persistence companion suites | PASS: 127 passed, no skips | `persistence-regressions.log` |
| Comparable final subset, no configured services | PASS: 3,855 passed, 154 skipped | `final-comparable-pytest.log` |
| Broad final suite with PostgreSQL/Redis and B12 configured | 4,003 passed; 6 FAIL at Unix-socket startup (environment BLOCKED), no skips | `final-services-pytest.log` |
| Entire unfiltered repository suite | NOT RUN: `tests/unit/test_migration_inventory.py` reads forbidden `deploy/.env.example` | Campaign R11; test file unchanged |
| Application and six changed/new test files, Ruff lint/format | PASS: all lint checks; 199 files already formatted | Commands below |
| Authoritative strict mypy in locked CPython 3.11.14 | PASS: 192 files | `final-mypy311.log` |
| Existing Python 3.13.11 environment mypy | FAIL baseline: NumPy 2.5.2 stub syntax vs Python 3.11 target | `baseline-mypy.log`; oa-3 owns environment drift |
| Advisory pyright 1.1.411 on application | PASS: zero errors/warnings | `final-pyright.log` |
| OSV and Semgrep | See current findings, rule/engine coverage and failed scanner attempts | `docs/audits/2026-10-27-oa-6-suggestion-review-security.md` |

The six broad-suite failures are the two WebSocket engines × three notification endpoints in the unchanged `test_notification_sockets.py`. All fail while starting Uvicorn on its Unix-domain socket, before assertions about the application execute. An independent AF_UNIX bind returns `PermissionError`, errno 13 (`unix-socket-probe.txt`). The controller must rerun these six cases in a socket-capable environment. No test, assertion, skip marker, workflow or scanner suppression was weakened to conceal the failures. These checks do not establish DEV acceptance.

## Test environment and reproduction

Project dependencies remain lock-resolved: `uv.lock`, `pyproject.toml`, Python targets and workflow/scanner configuration are unchanged. The project runtime is CPython 3.13.11. A separate environment created with `UV_PROJECT_ENVIRONMENT=<audit-cache>/venv311 uv sync --locked --python 3.11` runs authoritative mypy without changing the project environment or lock.

Disposable PostgreSQL 18.6 and pgvector 0.8.1 came from public Ubuntu packages extracted outside the checkout, with TCP bound only to loopback and Unix sockets disabled. Redis 8.0.5 similarly ran on loopback. These are local synthetic test services, not new deployed services. Runtime versions differ from the deployment's PostgreSQL 17 / Redis 7: controller CI remains the matching-version gate. Existing migrations reached `i7j8k9l0m1`; no migration was added. A custom-format `pg_dump` of the migrated synthetic database was verified with `pg_restore -l` before broad tests (SHA256 `c50bf8802345f6b0e2ea5ea424a6d1a0534ba6a2b2458908424a36f20de22b3d`). No live data was accessed or deleted.

For reproduction, configure `DATABASE_URL` and `REDIS_URL` to disposable migrated services, and `B12_TEST_DATABASE_URL` / `B12_TEST_REDIS_URL` for the socket tests. Never point these commands at shared or live data.

```bash
# Broad suite; retains the campaign's forbidden-file boundary.
bash scripts/memory-cap.sh .venv/bin/pytest tests/ \
  --ignore=tests/unit/test_migration_inventory.py -q \
  --cov=ontokit --cov-branch --cov-report=json:coverage.json

# Persisted request isolation and required companion regressions.
bash scripts/memory-cap.sh .venv/bin/pytest \
  tests/integration/test_suggestion_review_boundaries.py \
  tests/integration/test_suggestion_submission_recovery.py \
  tests/integration/test_llm_review_regressions.py \
  tests/integration/test_pull_request_concurrency.py \
  tests/integration/test_outcome_audit.py --no-cov -q

bash scripts/memory-cap.sh "$XDG_CACHE_HOME/oa6/venv311/bin/mypy" ontokit/
bash scripts/memory-cap.sh .venv/bin/pyright ontokit/
.venv/bin/ruff check ontokit/ tests/integration/test_suggestion_review_boundaries.py \
  tests/unit/test_suggestions_routes.py tests/unit/test_anonymous_suggestions_routes.py \
  tests/unit/test_suggestion_service.py tests/unit/test_pull_request_service.py \
  tests/unit/test_pull_requests_routes.py
# Repeat the same path list with `ruff format --check`.
```

## Measured coverage

Comparable runs use the same no-service configuration, subset and branch-coverage options. The service-enabled run supplies additional persistence evidence, separately from this comparison. `coverage-comparison.json` retains summaries and the complete changed-method coverage data.

| Target | Baseline lines | Final comparable lines | Baseline branches | Final branches |
|---|---:|---:|---:|---:|
| Suggestion routes | 50/70 | 66/70 | 0/0 | 0/0 |
| Anonymous routes | 51/54 | 54/54 | 5/6 | 6/6 |
| PR routes | 164/168 | 164/168 | 32/34 | 32/34 |
| `PullRequestService.create_review` | 31/34 | 33/36 | 12/14 | 14/16 |

PR validation adds contract assertions even where executed-line coverage was already present. The remaining `create_review` misses are its existing GitHub no-token/no-mirror and sync-exception paths, not an assertion that the method is completely covered.

## Scenario-to-test map

| Plan scenario | Added proof |
|---|---|
| U2.1 204 decisions and invalid feedback/note | `test_review_decisions_return_empty_204`, `test_invalid_review_payload_does_not_execute` |
| U2.2 queues and response serialization | `test_pending_queue_accepts_declared_values`, `test_pending_queue_rejects_unknown_value`, `test_capabilities_serializes_trust_and_verification`, `test_session_list_serializes_review_metadata` |
| U2.3 bulk results/validation | `test_bulk_review_preserves_partial_success`, `test_invalid_bulk_review_does_not_execute` |
| U2.4 limiter-pool outage | `test_submission_pool_failure_preserves_gates_and_service_refusal` |
| U2.5 anonymous normal responses/token errors | New submit/discard HTTP tests and `test_anonymous_missing_or_expired_token_does_not_execute`; existing garbage-token/honeypot/mode guards retained |
| U2.6 bound-session refusals and positive controls | `test_anonymous_http_refusal_has_no_write_effects`, matching-token service controls |
| U3.1 private nonmember | `test_private_nonmember_comment_review_http_denied_without_effects`: observed 201 before correction, 403 afterward; `u3-red.log` preserved |
| U3.2 allowed comments/side effects | `test_comment_review_access_controls_keep_notification_and_sync` |
| U3.3 decision roles | `test_review_decision_role_boundary`; existing suggestion editor-authority tests retained |
| U3.4 terminal and scoped lookup | `test_terminal_pr_review_has_no_write_effects`, `test_review_lookup_is_scoped_to_requested_project` |
| U3.5 HTTP schema/auth | New invalid-status/identifier/serialization cases; existing auth-disabled suite retained |
| U4.1 own-session/project/tier isolation | `test_own_sessions_and_reviewer_queues_are_isolated` |
| U4.2 denied-write invariance | `test_denied_suggestion_write_preserves_state` (wrong project and unauthorized reviewer) |
| U4.3 real review/notification/clock changes | `test_private_review_denial_and_allowed_control_persist` |
| U4.4 rollback then valid decision exactly once | `test_bulk_failure_does_not_rollback_following_dismiss` |
| U4.5 service commit containment and exception cleanup | Fixture rollback control after each case; `test_request_exception_restores_overrides_and_contains_commits` |

U2/U3 add 66 parameterized unit cases; U4 adds six executed database cases. The only production change is U3's existing `_can_view` predicate before PR lookup/writes. U2 mocked transport responses are not presented as authorization or persistence proof. U4 uses real routing, service authorization, SQL and notification persistence; only identity and external Git/GitHub collaborators are controlled.

The initial U4 draft incorrectly reused seeded ORM objects and expected a shorter 404 message. A fresh request session on the same outer transaction fixed the fixture's relationship-loader mismatch; expected text now matches the existing contract. No application change was made for that test-authoring failure. Real bulk partial-success behavior then passed.

## Remaining controller gates

- Reconcile current upstream `dev`, missing chief Goal Capsule/R-IDs/common mandate, and the unavailable October 5 handoff.
- Rerun the six Unix-socket cases on the permitted matching-version test environment. The forbidden-file inventory check remains subject to controller authority.
- Apply oa-0 dependency remediation and rerun OSV. Preserve Semgrep engine/version/registry limitations and run authenticated CI/Pro plus the campaign's Astra/Fable reviews.
- This branch has no remote and remains local; the controller owns push, PR against `dev`, and merge. No personally supplied input, held migration, release, provisioning, frontend screenshot or deployment is required.
