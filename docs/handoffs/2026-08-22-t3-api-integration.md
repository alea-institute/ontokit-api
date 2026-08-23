# T3 API integration handoff

Date: 2026-08-22 (America/Chicago)

## Publication state

- Local branch: `upstream-queue/t3-api-integrated`
- Base: `738f20a6250602f2b7950d8346f5ad8355f241b7` (`fix/t3-security-api`)
- Integration tip before this handoff: `2cc69d06` (`fix(integration): harden T3 API lifecycle invariants`)
- Pushed: no
- Merged: no
- PR or issue created: no
- No CatholicOS, AWS, DNS, DEV, or PROD state was changed.

This branch is the local integration queue. Do not publish it piecemeal. The user asked to finish the remaining autonomous work first, then create issues, link them to PRs, and publish the resulting work as one reviewed batch.

## Integrated commits

- `f7333f53` `feat(embeddings): expose safe job status polling`
- `b2ad8e29` `feat(llm): expose paginated audit receipts`
- `f25f16da` `fix(pr): enforce open source-branch idempotency`
- `700bef42` `fix(review): close T3 synthesis findings`
- `2cc69d06` `fix(integration): harden T3 API lifecycle invariants`

Earlier synthesis commits `e7b5d3cd`, `0bc5f834`, and `afbae4c4` were superseded by the stronger `fix/t3-security-api` base and this integrated queue. Do not cherry-pick those older variants over this branch.

## What is complete

- Embedding-job polling exposes safe status and progress without leaking worker exception text.
- LLM call receipts are metadata-only, project-scoped, owner/admin-only, cursor-paginated, and reject malformed or cross-project cursors with typed 422 responses.
- Sensitive audit and embedding-job endpoints reject the explicit disabled-auth anonymous identity before project or database access.
- Optional authentication without Zitadel remains supported for browse-only surfaces. This is a settled requirement unless the user explicitly overrides it.
- One-open-PR-per-source-branch is enforced in the database and application paths, including reopen and webhook transitions.
- PR number allocation is serialized per project before branch locks. Suggestion submission acquires one canonical lock set and does not re-enter a non-reentrant branch lock.
- External GitHub creation/reopen work runs after local claims and locks. GitHub synchronization remains intentionally best-effort.
- Expected PR conflicts do not roll back unrelated caller state. Direct fallback allocation retries use savepoints.
- Reconciled PR races schedule the same auto-accept clock as ordinary submission.
- Untrusted-user verification remains outside the PR lock, while daily allowance is consumed only after the refreshed session and ontology content are eligible. Invalid requests and concurrent losers do not spend allowance.
- The open-PR migration bounds duplicate diagnostics to 20 displayed groups while reporting the total count.
- PostgreSQL concurrency coverage proves disjoint-branch PR numbering, one-pass suggestion locking, shared-session preservation, application reopen conflicts, and webhook reopen conflicts.

## Verification evidence

Final CI-equivalent run after all substantive review fixes:

- `pytest -q`: 2,855 passed, 36 warnings, 90% coverage in 36.47 seconds.
- Real PostgreSQL PR lifecycle/concurrency module: 5 passed and included in the full run.
- Focused review validation: 9 passed.
- Ruff format check: 21 changed files formatted.
- Ruff check: passed for all 21 changed files.
- Mypy: passed for 183 source files.
- Alembic: one head, `e4f5g6h7i8j9`.
- `uv lock --check`: passed with 157 packages resolved.
- `git diff --check`: passed.

The warnings are existing Starlette/Pydantic deprecations, duplicate FastAPI operation IDs, and AsyncMock/connection-cleanup warnings. No new test failure remains.

## Review evidence

Two structured `ce-code-review` passes were run:

1. `20260822-184145-f921d044` found seven application, transaction, concurrency, and cursor issues. Six were fixed. The proposed durable GitHub outbox was not applied because repository history confirms best-effort synchronization is the existing contract and guaranteed delivery requires a new architecture decision.
2. `20260822-200002-f23dc06a` used correctness, project-standards, testing, maintainability, security, performance, API-contract, data-migration, reliability, and adversarial reviewers. Its four surviving findings were applied and independently validated. Final verdict: **Ready to merge**, with no actionable findings remaining.

Cross-model review was not used. An approval request to send the private diff to an external model was denied, so the review used the local adversarial fallback. Do not retry private diff egress without specific user authorization.

## True decisions still required

### Duplicate-rejection semantics

The `duplicate_rejections` model and read-side behavior exist, but there is no write path. Before implementation, the user must decide:

- Which human action means "these entities are not duplicates"?
- Which roles may record that decision?
- Must the entity pair be canonicalized so A/B and B/A are identical?
- Is a reason required for the audit trail?
- Is the decision permanent, or does it expire after ontology changes or time?

This is a domain/product choice, not a safe mechanical fix.

### Guaranteed GitHub synchronization

The current contract is local-authoritative and best-effort GitHub synchronization. If guaranteed synchronization is desired, decide that explicitly. The implementation would need a durable outbox, idempotency keys, retry/backoff, reconciliation, observability, and a defined local-versus-GitHub authority rule.

## Operational validation for the eventual publication batch

Before merge or deployment:

- Confirm both manifest SHAs are published and the PR branch runs required lint, type, unit, and PostgreSQL integration checks.
- Review migration output for duplicate open source-branch groups. Stop if the migration reports any; repair data before retrying.
- Exercise an authenticated audit request, malformed cursor, cross-project cursor, and disabled-auth anonymous request. Healthy signals are 200, typed 422, typed 422, and 403 respectively, with no prompt or response body in receipts.
- Exercise embedding-job polling as a member, a non-member, and the disabled-auth anonymous identity. Healthy signals are safe progress, 403/404 as appropriate, and no raw worker error.
- Exercise simultaneous PR creation on disjoint branches and reopen with an existing open successor. Healthy signals are unique monotonic PR numbers and a typed 409 without a GitHub mutation.
- Exercise invalid and racing untrusted submissions. Healthy signals are no daily-limit decrement for invalid content or the losing request.
- Monitor `llm_audit_finalize_failed`, PR uniqueness/reopen conflicts, embedding polling authorization failures, and GitHub sync warnings during the release window.

Rollback triggers include migration failure, duplicate PR numbers, two open PRs for one source branch, anonymous access to audit/job endpoints, raw provider or worker error disclosure, or allowance consumption on rejected submissions. Roll back the application release first; do not downgrade the unique index until data invariants have been inspected. The release owner should remain present through migration, smoke checks, and the first monitored request window.

## Next safe action

Keep this branch local until the remaining autonomous queue is complete. Then prepare the user-approved batch: create the necessary issues, link each issue to its PR, review the complete publication map, and only then push/open PRs under the existing approval gates.
