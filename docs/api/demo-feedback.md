# Demo API feedback contract

Full-text search rejects invalid paging and empty queries before running search.
Selected suggestion and review refusals include recovery guidance. A separate
readiness endpoint reports current database and Redis connectivity.

## Search validation

`GET /api/v1/search` requires `q` to contain at least one character, `limit` to be
an integer from 1 through 100 (default 20), and `offset` to be a nonnegative
integer (default 0). Missing/empty queries, invalid numbers, and out-of-range
paging return the standard HTTP 422 `detail` array before the search service or
its SQL executes. Dependency construction may still occur.

Whitespace and punctuation-only queries retain their existing empty-result
behavior. Comma-separated ontology/entity filters, successful responses,
semantic search, and SPARQL behavior remain unchanged.

## Optional recovery guidance

Classified errors retain the existing HTTP status, `detail` value and type, and
supplied response headers. They add a sibling `error` object with four strings:

| Field | Meaning |
|---|---|
| `code` | Stable machine-readable classification |
| `message` | Curated explanation of the refusal |
| `recovery` | Safe next action; never an instruction for an automatic retry |
| `request_id` | The same correlation value as the `X-Request-ID` response header |

Guidance is optional even on endpoints/statuses that document it in OpenAPI.
Ordinary HTTP errors still have only `detail`; validation errors still have a
`detail` array. Existing domain and rate-limit errors retain their established
`error` bodies, which do not necessarily contain `recovery` or `request_id`.
Do not assume that the presence of `error` alone identifies this new contract.

| Condition | HTTP status | Guidance code | Next action |
|---|---|---|---|
| Authenticated submit in the wrong lifecycle state (including the locked recheck) | 400 | `suggestion_not_submittable` | Refresh and check the current suggestion state |
| Authenticated or anonymous submit with no saved changes | 400 | `suggestion_no_changes` | Make and save an edit |
| Untrusted-submission limiter unavailable | 503 | `submission_limiter_unavailable` | Check the suggestion state and try later |
| Untrusted-submission daily quota exhausted | 429 | `submission_limit_reached` | Wait for the daily reset |
| Review a closed or merged PR | 400 | `pull_request_not_open` | Refresh and check whether the PR is closed or merged |
| Merge without sufficient approvals | 400 | `approvals_required` | Obtain the required reviews |
| Git merge unsuccessful | 409 | `merge_failed` | Inspect the branches/result and resolve reported conflicts |

Merge guidance also applies when suggestion approval reaches the shared merge
implementation. Direct PR review and suggestion review retain their distinct
permission rules. These changes introduce no retries or whole-request timeout:
an error does not establish that no prior work, including paid validation,
occurred. Existing pricing/budget errors, bulk-review HTTP 200 partial results,
and generation's transient empty-suggestion fallback are unchanged.

## Web consumer compatibility fixtures

Use these synthetic payloads with the actual web parser before release. A
detail-first parser should produce the same legacy output for both; a parser
that gives `error` precedence must accept the new object and retain fallbacks
for the existing error families.

Old HTTP 429:

```json
{
  "detail": {
    "reason": "daily_limit_reached",
    "message": "You have reached today's suggestion limit. It resets at midnight UTC."
  }
}
```

New HTTP 429 (`code` is the stable discriminator):

```json
{
  "detail": {
    "reason": "daily_limit_reached",
    "message": "You have reached today's suggestion limit. It resets at midnight UTC."
  },
  "error": {
    "code": "submission_limit_reached",
    "message": "You have used today's suggestion submission allowance.",
    "recovery": "Wait until the daily limit resets at midnight UTC before submitting again.",
    "request_id": "example-request-123"
  }
}
```

For a guidance-aware display, check that the expected fields are strings before
displaying them, and retain the legacy `detail`/domain-error handling when they
are absent. Render all strings as text. Do not flatten nested `detail` objects
or infer a new cooldown from HTTP 429. Request IDs follow the existing middleware
behavior: a supplied nonempty ID is reused, otherwise a new ID is generated.
The CORS configuration and its exposed headers are unchanged; body correlation
does not require JavaScript access to the response header.

The compatibility check must cover string and nested details, standard 422
arrays, existing domain errors, pricing/budget errors, and success/204 responses.
API tests establish the server contract; they do not establish compatibility
with a web parser absent from this checkout. The controller's G2 receipt remains
required before release.

## Liveness and dependency readiness

`GET /health` remains HTTP 200 with exactly `{"status":"healthy"}` and performs
no dependency probes. Existing deployment and restart healthchecks keep using it.

`GET /health/ready` runs a read-only database `SELECT 1` and Redis `PING`
concurrently. It returns HTTP 200 only when both probes and their cleanup succeed;
otherwise it returns HTTP 503. Every response carries `Cache-Control: no-store`.

```json
{
  "scope": "dependencies",
  "status": "not_ready",
  "request_id": "example-request-456",
  "checks": {
    "database": {"status": "ok", "message": "Database connection is available."},
    "redis": {"status": "timeout", "message": "Redis connection check timed out."}
  }
}
```

Each check reports `ok`, `unavailable`, or `timeout` with a fixed safe message.
Connection or cleanup failures are `unavailable`; an exceeded operation budget
is `timeout`. Timers request cancellation after one second of operation and
250 ms of cleanup. These are cancellation thresholds, not total response-time
deadlines. The installed SQLAlchemy/asyncpg adapter can shield an additional
two-second graceful shutdown before the service regains control.

Tests through that adapter with synthetic query/transport stalls measured about
three seconds for a query or pooled connection check, and 2.25 seconds for
rollback cleanup. A second cancellation during shutdown propagates promptly,
but library-owned cleanup and the synthetic transport can remain for about two
more seconds. In the tested cases, pool checkouts were released and shutdown
eventually finished. Application probe completion does not certify that all
library cleanup has drained.

The plan's two-second controlled-stall target is met only at injected cooperative
probe boundaries. This documented timing limitation preserves the existing
shared engine/pool behavior; it does not establish a strict total deadline or
live PostgreSQL behavior. See `tests/unit/test_health_adapter.py` for the
installed-adapter characterizations and the build receipt for release gates.

Every request probes current connectivity with the existing database engine and
a temporary Redis client. It neither relies on nor changes the startup Redis
pool or ARQ pool. No addresses, credentials, SQL text, or exception details are
returned by the readiness contract.

Readiness does not certify authentication, worker/job execution, git storage,
MinIO, project indexes, or model providers. It is a diagnostic observation, not
a write gate or proof that a suggestion/review will succeed. Current DEV
acceptance remains separate from the automated tests recorded in
[the build receipt](../plans/evidence/oa-5/verification.md).
