# T3 API synthesis — reviewed, verified, not published

Date: 2026-08-22

## Authoritative continuation point

- Repository: `ontokit-api`
- Isolated worktree: `/tmp/ontokit-api-t3-synthesis`
- Branch: `upstream-queue/t3-api-synthesis`
- Base: `1b8bde287279a942dd37c59d3546cdba3a903e94`
- Code HEAD before this handoff: `c00b54a7`
- Publication: **not pushed, not merged, and no PR opened**

Continue from this branch. Do not reconstruct these changes in another checkout.

## Local commit stack

1. `5d80cce0` — harden authentication and provider trust boundaries
2. `83e62cd5` — align budget and readiness contracts
3. `60032812` — correct ontology duplicate and validation results
4. `60e737ca` — serialize embedding activation and harden index migration
5. `9f879a89` — bound provider failures and generation work
6. `72f3dec4` — align branch-lock route fixtures with database locks
7. `7db9be70` — close reviewed concurrency and readiness gaps
8. `c00b54a7` — secure paid embedding query boundaries

## What is complete on this branch

- Optional auth without Zitadel remains supported for browse-only surfaces.
- Privileged and paid LLM/embedding operations require an authenticated identity.
- Provider base URLs and custom-provider redirects are constrained against SSRF and DNS-rebinding paths.
- Budget reservation, pricing, metering, failure audit, and provider-default behavior are fail-closed or explicitly observable.
- Embedding generation, activation, model changes, dimension integrity, and ANN index recovery are serialized and checked.
- Ontology validation and duplicate checks use the requested branch and preserve exact-match protection when ANN is unavailable.
- Git/RDF/storage dual writes and pull-request/branch lifecycle mutations use database-backed branch locks.
- Paid semantic search and direct duplicate checks now enforce LLM-capable project roles, daily rate limits, project budgets, and caller-attributed audit receipts.
- `daily_remaining` now reads the live Redis counter when available, with the established fail-open allowance when Redis is unavailable.
- Editors may still toggle `auto_embed_on_save`, but only owners/admins may change embedding providers, models, credentials, or budget caps.

## Review and verification

- `ce-code-review` was run against the synthesis stack. Eight confirmed concurrency/readiness defects were fixed; false positives were discarded after source-level validation.
- A second diff-scoped review of the paid-query residual batch found no remaining actionable defect.
- `ce-simplify-code` reuse, quality, and efficiency passes were run. They consolidated Redis discovery and removed a redundant membership query.
- Full test suite: `1879 passed, 2 skipped, 12 warnings`.
- Skips: the two real-PostgreSQL embedding-integrity tests are environment-gated; earlier focused execution had passed against disposable PostgreSQL, but this final checkout had no PostgreSQL service available.
- Full mypy: `Success: no issues found in 147 source files`.
- Changed-scope Ruff lint and format checks: passed.
- `git diff --check`: passed.
- Project-wide Ruff still has three pre-existing errors outside this branch's changed scope: one old migration import-order issue and two `F541` findings in `scripts/prepare-release.py`.

## Constraints preserved

- No writes or messages were sent to CatholicOS, AWS, DNS, DEV, or PROD.
- No credentials, secrets, or domain settings were created or changed.
- No push, PR, merge, issue creation, or issue linking was performed; publication remains reserved for the user's requested final batch.

## Remaining queue

These are not blockers to the verified code stack, but remain useful autonomous follow-ups:

1. Add a member-readable endpoint for polling an accepted embedding job by job ID; the worker and job records exist, but the API currently exposes only aggregate branch status.
2. Add a privacy-preserving per-call LLM audit-history endpoint; aggregate usage exists, but automated clients cannot inspect individual audit receipts.
3. Re-run the two real-PostgreSQL embedding-integrity tests and migration upgrade/downgrade rehearsal when a disposable PostgreSQL service is available.
4. Re-audit the remaining lower-priority findings from the 2026-08-08 LLM residual review before deciding which P2/P3 items warrant implementation.
5. At the final publication batch, create/link the agreed issues and PRs, then push only after the existing approval gate is explicitly opened.

External product gates (CatholicOS delivery, PROD/AWS, DNS, and domain-name coordination) remain unchanged and must not be crossed from this handoff without explicit approval.
