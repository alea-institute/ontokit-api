# B12 API socket lane completion

Lane: ontokit-api B12. Branch: `feat/drain-b12-sockets`. Base/PR target: `dev` only.
API scope is PR-ready; commits remain local. No push, PR, GitHub call, or deployment.

## Delivered

- All three existing notification sockets accept base64url credentials offered as
  `ontokit.bearer.v1` plus `ontokit.token.<credential>`; the server selects only
  `ontokit.bearer.v1`. Legacy query-token clients remain compatible.
- Uvicorn access/error filters redact plain and percent-encoded query parameter
  names and credential subprotocols, including debug headers and exception text.
- Shared Redis forwarding filters projects, checks authorization every 60 seconds
  even on quiet/stalled channels, closes on expiry/revocation, logs unexpected
  errors with tracebacks and closes 1011, and cleans subscriptions on cancellation.
- Reauthorization preserves the identity admitted at handshake. Optional auth
  cannot downgrade an authenticated connection when its token expires immediately
  after acceptance. Originally anonymous optional/disabled connections stay anonymous.
- Local compose allows 180 seconds for migrations before healthcheck failures count.
  The inherited timezone repair in unused presence code is maintenance only.
- A normal merge retains local `dev`'s memory-cap protections (62469e0c).

Presence/sync are **NOT built**. There is no new collaboration endpoint.

## Verification receipts

All commands used `timeout` and `scripts/memory-cap.sh` (the main checkout's
fallback script before merging `dev`), with a 6144 MiB ceiling.

- Focused post-merge suite: **128 passed, 0 failed**. Files: `test_ws_auth`,
  `test_ws_forward`, `test_logging_filters`, `test_index_ws`, `test_lint_ws`,
  `test_quality_ws`, `test_collab_presence`, `test_dockerfile_contract`, `test_memory_caps`.
- Adjacent API regression suite: **515 passed, 0 failed**. Files: `test_auth_core`,
  `test_auth_disabled`, `test_auth_disabled_routes`, `test_lint_routes`,
  `test_lint_routes_extended`, `test_quality_routes`, `test_quality_worker`,
  `test_projects_routes`, `test_projects_routes_extended`, `test_projects_routes_coverage`,
  `test_ontology_index_service`, `test_project_service`.
- Wire suite: **6 passed, 0 failed** (`tests/integration/test_notification_sockets.py`),
  across three routes and both Uvicorn `websockets` / `websockets-sansio` backends.
  Real PostgreSQL project membership and Redis pub/sub run offline over Unix sockets.
  RSA test keys are generated in memory; only the identity provider's JWKS fetch is
  substituted. JWT signature, issuer, audience and expiry verification remain real.
- Wire scenarios prove missing/garbage/expired/unrelated/unknown-project handshake
  closes; valid header and query auth; two-client fan-out and project isolation;
  live member revocation/expiry; shutdown close 1012, reconnect after Uvicorn
  stop/start, and REST refetch of results persisted while pub/sub had no subscribers.
  Raw and encoded credentials are absent from captured server logs at DEBUG level.
- Ruff checks and formatting: pass. mypy with Python 3.11: **192 files, 0 errors**.
  Advisory Pyright: **0 errors, 0 warnings**. `git diff --check`: pass.
- Initially inherited 120 tests passed. Added first-check expiry and two redaction
  regressions failed before their fixes and passed afterward. Other inherited
  changes were characterized after implementation; no invented before-fix receipt.
- Broader runs inside the sandbox timed out at 150s and 45s in Starlette TestClient
  on `test_get_lint_rules_returns_list`. The same suite outside its thread/socket
  restrictions passed all 515 tests in 2.35s. The Python 3.13 mypy attempt hit a
  dependency-stub syntax mismatch; the authoritative Python 3.11 run is clean.

Self-review checked auth-mode parity, identity retention, project filtering,
timer/send ordering, task cancellation, pub/sub cleanup, protocol selection,
redaction preserving Uvicorn formatter arguments, and merge scope. No outstanding findings.

## Replay the wire proof

Use **disposable** PostgreSQL 17 with pgvector and Redis 7 instances. Do not target
production data: the fixture creates test schema and rows. Set `B12_TEST_DATABASE_URL`
and `B12_TEST_REDIS_URL`; Redis accepts `unix:///path/to/redis.sock`. Then run:

```sh
timeout 90 bash scripts/memory-cap.sh .venv/bin/pytest -o addopts='' -q \
  tests/integration/test_notification_sockets.py
```

The proof's app mounts the actual route modules without unrelated application startup
integrations. Reauthorization intervals are shortened to 0.1s for tests; production
keeps 60s. Uvicorn listeners stop/start within the test process. This is API wire
evidence, not a browser, deployment, identity-provider, or container-restart receipt.

## Orchestrator next steps

Publish/review this local branch with PR target `dev`. The separate web lane owns
browser retry policy, terminal auth closes, REST-refetch callbacks, the honest
connection indicator, and the full browser-stack/container restart acceptance.
No database migration or deployment credential change is required here. Rollback
is reverting the socket hardening commits; legacy query clients remain supported.

BLOCKERS: none for the API lane. NEEDS-DAMIEN: none.
