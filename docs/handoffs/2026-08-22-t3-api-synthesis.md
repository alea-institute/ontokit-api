# T3 API synthesis — complete locally, verified, not published

Date: 2026-08-22

## Authoritative continuation point

- Repository: `ontokit-api`
- Isolated worktree: `/tmp/ontokit-api-t3-synthesis`
- Branch: `upstream-queue/t3-api-synthesis`
- Base: `1b8bde287279a942dd37c59d3546cdba3a903e94`
- Verified code HEAD: `054bbf1f`
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
9. `04774d7b` — preserve the pre-T3 verification handoff
10. `fa999e4b` — expose member-safe embedding-job polling
11. `448f862f` — expose privacy-preserving paginated LLM audit receipts
12. `e7b5d3cd` — bound project-wide quality-job admission
13. `0bc5f834` — reconcile ambiguous quality-job enqueue outcomes
14. `be1c4c16` — enforce idempotent open pull requests by source branch
15. `afbae4c4` — centralize quality admission contracts
16. `054bbf1f` — close all five T3 code-review findings

## What is complete on this branch

- Optional auth without Zitadel remains supported for browse-only surfaces.
- Privileged and paid LLM/embedding operations require an authenticated identity.
- Provider base URLs and custom-provider redirects are constrained against SSRF and DNS-rebinding paths.
- Budget reservation, pricing, metering, failure audit, and provider-default behavior are fail-closed or explicitly observable.
- Embedding generation, activation, model changes, dimension integrity, and ANN index recovery are serialized and checked.
- Ontology validation and duplicate checks use the requested branch and preserve exact-match protection when ANN is unavailable.
- Git/RDF/storage dual writes and pull-request/branch lifecycle mutations use database-backed branch locks.
- Paid semantic search and direct duplicate checks enforce LLM-capable project roles, daily rate limits, project budgets, and caller-attributed audit receipts.
- `daily_remaining` reads the live Redis counter when available, with the established fail-open allowance when Redis is unavailable.
- Editors may toggle `auto_embed_on_save`; only owners/admins may change embedding providers, models, credentials, or budget caps.
- Members can poll an accepted embedding job by job ID without receiving worker exception text.
- Owners/admins can page through metadata-only individual LLM audit receipts with a project-bound cursor.
- Consistency and duplicate-detection jobs share one ownership-safe project admission lock, reconcile ambiguous enqueue outcomes, and expose terminal polling state.
- Open pull-request creation is idempotent by source branch at the service, database, and concurrent suggestion-submit seams.
- The open-PR uniqueness migration now fails before DDL with an actionable list of legacy duplicate groups rather than a cryptic database error or silent history mutation.

## Review and verification

- `ce-simplify-code` reuse, quality, and efficiency passes completed before review. They centralized quality lock naming and removed redundant membership/list materialization.
- `ce-code-review` completed in agent mode as run `20260822-171213-d5b2b39a`, reviewing `04774d7b..afbae4c4` with correctness, standards, security, testing, maintainability, performance, API-contract, data-migration, reliability, adversarial, and agent-native lenses.
- The review retained five findings: one P1 migration-safety issue and four P2 typing/performance/testing/concurrency issues. Independent validation confirmed the migration, polling, and concurrent-submit findings. All five were fixed in `054bbf1f`.
- The configured cross-model pass was not run because approval to export the private diff to Anthropic was denied. The adversarial lens ran locally instead.
- Disposable PostgreSQL rehearsal proved the duplicate-group migration preflight fails with the exact project/branch/count diagnostic, preserves the rows, cleans up the synthetic fixture, and then upgrades cleanly to head.
- Full suite against PostgreSQL 17 + pgvector: `1913 passed, 12 warnings` in 18.40 seconds.
- Full mypy: `Success: no issues found in 148 source files`.
- Changed-scope Ruff lint: passed.
- Changed-scope Ruff format: 13 files already formatted.
- `git diff --check`: passed.
- Alembic: one head, `e4f5g6h7i8j9`.
- Pyright is advisory. The isolated-worktree run required an alternate environment configuration and timed out at 180 seconds without emitting a code-specific diagnostic; mypy remains the repository's authoritative type gate.
- Review receipt was also written to `/tmp/compound-engineering-1000/ce-code-review/20260822-171213-d5b2b39a/review.json`. That path is temporary; this handoff is the durable evidence summary.

## Post-deploy monitoring & validation

- Before migration, run the duplicate-group query embedded in `e4f5g6h7i8j9`; any returned group is a deployment stop until deliberately resolved.
- After deployment, exercise one embedding job and both quality job types through accepted, polling, and terminal states. Healthy signals are stable job IDs, monotonic progress, one active quality job per project, and no raw worker exception text.
- Search API/worker logs for `quality_job_active`, enqueue reconciliation, lock renewal/release failures, embedding terminal-state reads, LLM audit cursor decode errors, and open-source-branch conflicts.
- Watch 409 rates for quality admission and suggestion submission. Repeated suggestion-submit 409s for an already-created branch PR are a regression signal.
- Validate LLM audit pagination across at least two pages and confirm receipts contain metadata only, remain project-scoped, and terminate with a null cursor.
- Roll back the application commit if polling, authorization, or enqueue reconciliation regresses. Do not downgrade the unique index until confirming no duplicate open source branches were created after deployment.
- Validation window: first deploy plus 24 hours. Owner: the operator performing the eventual approved DEV/upstream publication batch.

## Constraints preserved

- No writes or messages were sent to CatholicOS, AWS, DNS, DEV, or PROD.
- No credentials, secrets, or domain settings were created or changed.
- No push, PR, merge, issue creation, or issue linking was performed; publication remains reserved for the user's requested final batch.

## Remaining queue

### Requires a product decision

- `duplicate_rejections` has a model/table but no write path. Before implementing one, decide which user action means “not a duplicate,” which project roles may record it, and whether the rejection is permanent or expires. This is a genuine workflow/product-policy choice, not a safe implementation inference.

### Autonomous or publication-batch follow-up

- The API exposes these capabilities through authenticated REST/OpenAPI, but the repository has no explicit MCP/tool registry or runtime prompt layer. Treat agent-native discovery as a separate architecture unit rather than expanding this completed T3 branch.
- Reconcile this branch with the separate `fix/t3-security-api` work before publication so the final upstream tranche has one coherent history.
- At the final publication batch, create/link the agreed issues and PRs, then push only after the existing approval gate is explicitly opened.

External product gates — CatholicOS delivery, PROD/AWS, DNS, and domain-name coordination — remain unchanged and must not be crossed from this handoff without explicit approval.
