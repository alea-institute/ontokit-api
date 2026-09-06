# PR Party → dev API parity ledger

Date: 2026-09-06 (U4 audit; required artifact filename retains the plan date).

Refs compared:

- `feat/pr-party`: `435dc393f52d2607a744fdc7c2320d706e47a0f3` (frozen DEV-deployed history).
- `dev`: `3201775d00d7bddf4c673cce3c658513e0cd19d9` (working-tree base and single ALEA integration/deploy line).

## Method

KTD2: settle parity with explicit dispositions and minimal carries, not a branch merge.
The inventory is the exact local `git diff --name-status feat/pr-party dev`: 240 rows,
62 A, 19 D, 158 M, and one R097. A means dev-only; D means frozen-only. The rename
is one row with both paths. No network or other checkout was used.

Read frozen content with `git show feat/pr-party:<path>` and compare
`git diff feat/pr-party dev -- <path>`. For M rows, compare changed definitions,
removed assertions, callers, and behavior at the revised service/route boundaries;
formatting, new locking/metering/authorization implementations, and changed mocks
are not reasons to replace an entire dev file. Table pointers refer to the immutable
dev SHA above unless explicitly identified as a carry in this working tree.

An AST inventory covers module-level def/async def/class and public assignment names
(including annotated assignments, type aliases, lower-case migration metadata, and
conditional module assignments) for every D/M Python path under ontokit/, deploy/,
scripts/, and alembic/. Private helpers and class members are outside that required
symbol inventory but were also considered in behavior review. Route decorators were
compared by method and path and checked against registration prefixes and shared
constants. `git grep` on dev checks each removed name and its replacement behavior.
The symbol table conservatively retains four apparent source removals whose public
binding/URL still exists: Vector, worker.get_redis_settings, and two anonymous route
literals. Thus it has 14 source-removal candidates, of which 10 are actually absent
bindings and **zero are lost HTTP route paths**. Imported dependencies are not new
public definitions; public re-exports are explicitly checked for continued availability.

U5 ownership takes precedence for **all** deploy/** and .github/workflows/** rows,
including additions. Other A rows are dev-only additions (including the dev-only
.claude command; it is not frozen-line documentation being carried). Frozen scratch
and the dependency manifests are excluded as directed. Root compose/setup settings
are outside those U5 path patterns; their missing configurable behavior is carried
without executing the setup script or touching deploy/**.

The sandbox cannot run the suite. Carry decisions for deleted tests were made by
reading dev code, not by observing pytest failures. No pytest, uv, pip, installs,
network, Alembic CLI, or Git index writes were run. The host owns Ruff, full pytest
with real PostgreSQL/Redis, `alembic heads`, and CI/mypy. Local verification uses AST
parsing, shell syntax, YAML parsing, ledger completeness/uniqueness, literal migration
graph/status checks, and `git diff --check`. Ruff is not installed in this sandbox;
Python was laid out to the repository style without claiming a Ruff result.

## Summary counts by disposition

| Disposition | Name-status rows | Removed public symbol candidates |
| --- | --- | --- |
| carry | 13 | 0 |
| dev supersedes | 200 | 12 |
| superseded by U5 | 17 | 0 |
| drop | 10 | 2 |

Totals: **240 rows**, **14 symbol candidates**. Every entry has exactly
one disposition and a pointer/reason. Carry means only the slice described in Note;
all other dev behavior in that file remains. Recorded policy questions below are not
authority to alter dev policy.

## Name-status rows

| Kind | Path | Disposition | Pointer or reason | Note |
| --- | --- | --- | --- | --- |
| A | .claude/commands/post-merge.md | dev supersedes | dev-only addition |  |
| M | .env.example | dev supersedes | dev re-synthesis (same file, revised): .env.example | Adds exact private LLM origin allowlist configuration. |
| D | .fix-api-pr15.md | drop | frozen-line documentation/scratch; durable copies live on the documentation branch |  |
| M | .github/dependabot.yml | dev supersedes | dev re-synthesis (same file, revised): .github/dependabot.yml | Adds seven-day dependency update cooldown. |
| D | .github/workflows/deploy-dev.yml | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| M | .github/workflows/osv-scanner.yml | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| M | .github/workflows/release.yml | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| M | .github/workflows/semgrep.yml | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| M | AGENTS.md | drop | Frozen-line upstream contribution/remote workflow documentation is outside application parity; KTD2 and this packet govern integration. | Retain dev repository guidance; no frozen documentation carried. |
| M | Dockerfile | dev supersedes | dev re-synthesis (same file, revised): Dockerfile | Adds operator credential rewrap executable. |
| M | Dockerfile.prod | dev supersedes | dev re-synthesis (same file, revised): Dockerfile.prod | Adds operator credential rewrap executable. |
| M | alembic/versions/a4b5c6d7e8f9_add_embedding_budget_caps.py | dev supersedes | dev re-synthesis (same file, revised): alembic/versions/a4b5c6d7e8f9_add_embedding_budget_caps.py | Same revision id with different file content; see Migration lineage. |
| M | alembic/versions/b5c6d7e8f9a0_add_translation_records.py | dev supersedes | dev re-synthesis (same file, revised): alembic/versions/b5c6d7e8f9a0_add_translation_records.py | Same revision id with different file content; see Migration lineage. |
| A | alembic/versions/c2d3e4f5g6h7_harden_embedding_index_integrity.py | dev supersedes | dev-only addition |  |
| A | alembic/versions/d2e3f4g5h6i7_add_demo_project_isolation.py | dev supersedes | dev-only addition |  |
| A | alembic/versions/e3f4g5h6i7j8_expand_suggestion_session_status_constraint.py | dev supersedes | dev-only addition |  |
| A | alembic/versions/e4f5g6h7i8j9_add_open_pr_source_branch_index.py | dev supersedes | dev-only addition |  |
| A | alembic/versions/f4g5h6i7j8k9_add_anonymous_content_bytes.py | dev supersedes | dev-only addition |  |
| A | alembic/versions/g5h6i7j8k9l0_add_stale_anonymous_reaper_index.py | dev supersedes | dev-only addition |  |
| A | alembic/versions/g6h7i8j9k0l1_add_pull_request_github_sync_receipts.py | dev supersedes | dev-only addition |  |
| A | alembic/versions/h6i7j8k9l0m1_add_atomic_demo_generations.py | dev supersedes | dev-only addition |  |
| R097 | alembic/versions/x1y2z3a4b5c6_add_pr_party.py → alembic/versions/h7i8j9k0l1m2_add_pr_party.py | dev supersedes | alembic/versions/h7i8j9k0l1m2_add_pr_party.py:h7i8j9k0l1m2 | PR Party schema retained under new revision/parent; see reused-id warning in Migration lineage. |
| M | alembic/versions/w0x1y2z3a4b5_add_trust_ladder.py | dev supersedes | dev re-synthesis (same file, revised): alembic/versions/w0x1y2z3a4b5_add_trust_ladder.py | Same revision id with different file content; see Migration lineage. |
| A | alembic/versions/x1y2z3a4b5c6_add_distinct_entity_decisions.py | dev supersedes | dev-only addition |  |
| D | alembic/versions/y2z3a4b5c6d7_widen_suggestion_session_status_check.py | dev supersedes | alembic/versions/e3f4g5h6i7j8_expand_suggestion_session_status_constraint.py | All seven allowed status values match; dev downgrade additionally refuses populated reviewed states. |
| M | alembic/versions/z3a4b5c6d7e8_cap_active_embedding_jobs_per_project.py | dev supersedes | dev re-synthesis (same file, revised): alembic/versions/z3a4b5c6d7e8_cap_active_embedding_jobs_per_project.py | Same revision id with different file content; see Migration lineage. |
| M | compose.yaml | carry | feat/pr-party:compose.yaml → working tree; minimal behavior slice | Restore configurable Zitadel origins, credentials, expiry, and log level; retain dev demo-mirror token additions. |
| D | deploy/.env.example | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| M | deploy/RUNBOOK.md | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| A | deploy/__init__.py | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| D | deploy/compose.dev.yaml | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| A | deploy/demo-mirrors.json | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| A | deploy/demo-refresh.cron.example | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| D | deploy/firewall/ontokit-firewall.service | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| D | deploy/firewall/ontokit-firewall.sh | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| D | deploy/init-db.dev.sh | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| D | deploy/ontokit-deploy.sh | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| A | deploy/refresh_demo_repositories.py | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| A | deploy/resync_demo_projects.py | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| D | deploy/traefik/ontokit-dev.yaml | superseded by U5 | U5 deploy seam ownership (unit packet) | No carry in U4, including dev-only deployment additions. |
| A | docs/PR_PARTY_CREDENTIAL_REWRAP.md | dev supersedes | dev-only addition |  |
| D | docs/plans/2026-08-09-001-feat-translations-annotation-plan.md | drop | frozen-line documentation/scratch; durable copies live on the documentation branch |  |
| D | docs/plans/2026-08-09-003-feat-submission-audit-snapshot-plan.md | drop | frozen-line documentation/scratch; durable copies live on the documentation branch |  |
| D | docs/residual-review-findings/2026-07-28-pr-party-code-review.md | drop | frozen-line documentation/scratch; durable copies live on the documentation branch |  |
| D | docs/residual-review-findings/2026-08-08-llm-subsystem-fixes.md | drop | frozen-line documentation/scratch; durable copies live on the documentation branch |  |
| D | docs/residual-review-findings/2026-08-08-llm-subsystem-review.md | drop | frozen-line documentation/scratch; durable copies live on the documentation branch |  |
| D | docs/residual-review-findings/2026-08-08-llm-subsystem-verification-api.md | drop | frozen-line documentation/scratch; durable copies live on the documentation branch |  |
| M | ontokit/api/routes/__init__.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/__init__.py | Shared authenticated/reviewer-registry PR Party gate; anonymous route constants. |
| M | ontokit/api/routes/anonymous_suggestions.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/anonymous_suggestions.py | Literal routes moved to shared constants without URL changes. |
| M | ontokit/api/routes/duplicate_check.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/duplicate_check.py | Adds authenticated fingerprint-bound distinct-decision endpoints. |
| M | ontokit/api/routes/embeddings.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/embeddings.py | Maps invalid embedding configuration to HTTP 422. |
| M | ontokit/api/routes/generation.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/generation.py | Atomic paid-call metering, hidden-demo filtering, and sticky session provenance. |
| M | ontokit/api/routes/llm.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/llm.py | Atomic connection-test metering, origin policy, and sanitized provider errors. |
| M | ontokit/api/routes/pr_party.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/pr_party.py | Idempotent action-budget reservations and refunds. |
| M | ontokit/api/routes/pr_party_settings.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/pr_party_settings.py | Credential log omits reviewer identity. |
| M | ontokit/api/routes/projects.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/projects.py | Adds immutable source CAS, compensating rollback, and demo discovery filters; tree default-branch behavior retained. |
| M | ontokit/api/routes/pull_requests.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/pull_requests.py | Adds authenticated GitHub synchronization retry endpoint. |
| M | ontokit/api/routes/semantic_search.py | carry | feat/pr-party:ontokit/api/routes/semantic_search.py → working tree; minimal behavior slice | Restore membership requirement, billing_user_id attribution, and budget/pricing HTTP errors; preserve dev endpoint signatures. |
| M | ontokit/api/routes/suggestions.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/suggestions.py | Typed SuggestionQueue replaces regex-only queue parameter. |
| M | ontokit/api/routes/translation.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/translation.py | Atomic fan-out call reservations, queue receipts, and demo visibility gates. |
| M | ontokit/api/routes/trust.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/trust.py | Project-bound cursors, batched counts, and effective policy-change auditing. |
| M | ontokit/api/routes/user_settings.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/routes/user_settings.py | PATCH distinguishes omission from explicit null; unused token-preview helper removed. |
| M | ontokit/api/utils/redis.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/utils/redis.py | Uses ontokit/core/redis.py:get_redis_settings; avoids importing the worker. |
| M | ontokit/api/utils/ws_auth.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/api/utils/ws_auth.py | WebSocket auth follows required/optional/disabled HTTP modes while enforcing project access. |
| A | ontokit/core/api_paths.py | dev supersedes | dev-only addition |  |
| M | ontokit/core/auth.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/core/auth.py | Adds explicit anonymous identity rejection at sensitive boundaries. |
| M | ontokit/core/config.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/core/config.py | Shared PR Party enablement predicate and dedicated demo mirror credential; obsolete mirror username has no consumer. |
| M | ontokit/core/constants.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/core/constants.py | Adds credential rewrap operator task and receipt constants. |
| A | ontokit/core/demo_targets.py | dev supersedes | dev-only addition |  |
| A | ontokit/core/limits.py | dev supersedes | dev-only addition |  |
| M | ontokit/core/middleware.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/core/middleware.py | Adds bounded anonymous body ingestion using shared route patterns. |
| A | ontokit/core/redis.py | dev supersedes | dev-only addition |  |
| M | ontokit/git/__init__.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/git/__init__.py | Exports BranchHeadMismatchError for source CAS. |
| M | ontokit/git/bare_repository.py | carry | feat/pr-party:ontokit/git/bare_repository.py → working tree; minimal behavior slice | Restore BareOntologyRepository.get_default_branch symbolic-HEAD resolution; retain dev CAS and demo push authorization. |
| M | ontokit/main.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/main.py | Registers anonymous body limits; gates all PR Party startup hooks consistently. |
| M | ontokit/models/__init__.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/models/__init__.py | Registers demo-generation, distinct-decision, and staged embedding models. |
| A | ontokit/models/demo_generation.py | dev supersedes | dev-only addition |  |
| A | ontokit/models/distinct_entity_decision.py | dev supersedes | dev-only addition |  |
| M | ontokit/models/embedding.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/models/embedding.py | Required pgvector, dimension integrity, and job-private embedding staging. |
| M | ontokit/models/project.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/models/project.py | Adds demo provenance and trust state metadata. |
| M | ontokit/models/pull_request.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/models/pull_request.py | Adds durable GitHub sync receipts/generations and one-open-source-branch invariant. |
| M | ontokit/models/suggestion_outcome.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/models/suggestion_outcome.py | Enforces one outcome per suggestion session. |
| M | ontokit/models/suggestion_session.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/models/suggestion_session.py | Adds anonymous byte accounting and recoverable auto-accept lease; status CHECK managed by e3f4g5h6i7j8. |
| M | ontokit/pr_party_org_assets/claude-pr-answers.yml | dev supersedes | dev re-synthesis (same file, revised): ontokit/pr_party_org_assets/claude-pr-answers.yml | Pins the external action to an immutable commit. |
| M | ontokit/schemas/duplicate_check.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/schemas/duplicate_check.py | Adds fingerprint-bound distinct-decision requests/responses and suppressed decisions. |
| M | ontokit/schemas/embeddings.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/schemas/embeddings.py | Exposes embedding_text needed for candidate fingerprints. |
| M | ontokit/schemas/generation.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/schemas/generation.py | Shared base preserves flat wire representation while strict edge/annotation constructors require payloads. |
| M | ontokit/schemas/project.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/schemas/project.py | Adds demo provenance, required base_revision, and typed consistency/conflict responses. |
| M | ontokit/schemas/pull_request.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/schemas/pull_request.py | Exposes durable GitHub sync status and last-attempt receipt. |
| M | ontokit/schemas/suggestion.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/schemas/suggestion.py | Bounds Turtle payloads; client mint flag remains a legacy hint. |
| M | ontokit/schemas/user_settings.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/schemas/user_settings.py | Explicit-null commit email semantics; two unused token DTOs removed (symbol table). |
| M | ontokit/services/branch_lock.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/branch_lock.py | Adds deterministic multi-branch and PR allocation locks. |
| M | ontokit/services/commit_identity.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/commit_identity.py | Omitted preference differs from explicit null, which resets verification and opt-in. |
| A | ontokit/services/demo_project_provisioning.py | dev supersedes | dev-only addition |  |
| A | ontokit/services/demo_target_authorizer.py | dev supersedes | dev-only addition |  |
| M | ontokit/services/duplicate_check_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/duplicate_check_service.py | Retains exact-label dominance and missing-signal normalization; adds fingerprint-bound suppression and audited decisions. |
| M | ontokit/services/embedding_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/embedding_service.py | Atomic spend reservations, dimension validation, staged activation, and race-safe incremental upsert replace older paths. |
| M | ontokit/services/github_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/github_service.py | Adds optional 404-only PR lookup and explicit body clearing. |
| M | ontokit/services/github_sync.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/github_sync.py | Authorizes demo targets before outbound sync; preserves outbound-only behavior. |
| M | ontokit/services/join_request_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/join_request_service.py | Enforces hidden-demo visibility for direct access and membership requests. |
| M | ontokit/services/llm/__init__.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/llm/__init__.py | Exports reservation, budget-limit, and metered-provider APIs. |
| M | ontokit/services/llm/audit.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/llm/audit.py | Durable pre-call reservation and post-call reconciliation augment existing logging. |
| M | ontokit/services/llm/budget.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/llm/budget.py | Locks project budget before spend checks and accounts for projected cost. |
| M | ontokit/services/llm/cohere_provider.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/llm/cohere_provider.py | Forwards the output-token cap. |
| M | ontokit/services/llm/crypto.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/llm/crypto.py | Domain-separated HKDF writes with legacy SHA-256 and rotated-key decrypt fallback. |
| M | ontokit/services/llm/google_provider.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/llm/google_provider.py | Forwards the output-token cap. |
| A | ontokit/services/llm/metering.py | dev supersedes | dev-only addition |  |
| M | ontokit/services/llm/rate_limiter.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/llm/rate_limiter.py | Adds atomic multi-unit reservation and idempotent release. |
| M | ontokit/services/llm/registry.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/llm/registry.py | Exact operator-approved origins replace provider-label private-network exemptions. |
| M | ontokit/services/llm/ssrf.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/llm/ssrf.py | Pinned validated IPs at dial boundary; exact private-origin allowlist replaces broad bypass. |
| M | ontokit/services/mirror_credential.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/mirror_credential.py | Demo target authorization precedes credential selection; typed denial and redacted diagnostics. |
| M | ontokit/services/normalization_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/normalization_service.py | Acquires branch writer lock and uses expected-head CAS. |
| M | ontokit/services/ontology.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/ontology.py | Declaration-aware annotation classification includes local annotation properties. |
| M | ontokit/services/ontology_index.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/ontology_index.py | Uses the same annotation classifier as RDFLib fallback. |
| M | ontokit/services/pr_party_credentials.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/pr_party_credentials.py | Adds atomic credential rewrap and sanitized typed failures. |
| M | ontokit/services/pr_party_github.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/pr_party_github.py | Refuses unscoped demo targets before PR Party review, merge, and comment writes. |
| M | ontokit/services/pr_party_rate_limiter.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/pr_party_rate_limiter.py | Idempotent reservation receipts allow action replay at the final budget unit. |
| M | ontokit/services/pr_party_reconcile.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/pr_party_reconcile.py | Failed GitHub reads no longer roll back earlier repairs; test_pr_party_reconcile.py:test_later_unsettled_failure_does_not_erase_an_earlier_repair. |
| A | ontokit/services/project_access_policy.py | dev supersedes | dev-only addition |  |
| M | ontokit/services/project_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/project_service.py | Adds demo visibility/immutability and metadata writer CAS. |
| A | ontokit/services/pull_request_github_sync.py | dev supersedes | dev-only addition |  |
| M | ontokit/services/pull_request_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/pull_request_service.py | Atomic PR claims, durable sync/retry receipts, and explicit system auto-accept authorization; editor review policy requires host review (Findings). |
| M | ontokit/services/structural_similarity_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/structural_similarity_service.py | Same optional structural evidence behavior; import typing/docstring cleanup. |
| M | ontokit/services/suggestion_generation_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/suggestion_generation_service.py | Preserves prompt/output hardening; adds redacted fail-soft alerts and proposed-IRI attribution. |
| M | ontokit/services/suggestion_service.py | carry | feat/pr-party:ontokit/services/suggestion_service.py → working tree; minimal behavior slice | Restore commit_changes at all three write sites, Git-tree ontology-path resolution, missing-baseline error, unconditional Turtle parsing, and post-save/post-submit index refresh; retain dev distinct decisions, locks, quotas, and PR finalization. |
| M | ontokit/services/translation_backfill.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/translation_backfill.py | Preserves untagged RDF language identity separately from provider und fallback. |
| M | ontokit/services/translation_coverage.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/translation_coverage.py | Scopes entity-state reads to the requested entity instead of loading whole-project coverage. |
| M | ontokit/services/translation_jobs.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/translation_jobs.py | Deterministic project/branch job identity, atomic fan-out billing, and retry-aware queue slots. |
| M | ontokit/services/translation_review.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/translation_review.py | Locks/flushes review before Git, restores branch on database commit failure. |
| M | ontokit/services/translation_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/translation_service.py | MeteredLLMProvider replaces budget/audit callbacks; preserves untagged literal identity. |
| M | ontokit/services/trust_rate_limiter.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/trust_rate_limiter.py | Typed exhausted/unavailable decisions replace ambiguous boolean tuples. |
| M | ontokit/services/trust_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/trust_service.py | Batched member/outcome lookups and explicit trust grant reset. |
| M | ontokit/services/validation_service.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/services/validation_service.py | VALID-04 explicitly changes to project namespace ownership; pending host review, no policy carry (Findings). |
| M | ontokit/worker.py | dev supersedes | dev re-synthesis (same file, revised): ontokit/worker.py | Shared Redis DSN parser, bounded translation retries, unified PR Party gate, operator credential rewrap, and isolated sync failures. |
| M | pyproject.toml | drop | Dependency manifests are explicitly excluded from carry by the unit packet; retain dev dependency resolution. |  |
| A | railway.json | dev supersedes | dev-only addition |  |
| A | scripts/rewrap_pr_party_credentials.py | dev supersedes | dev-only addition |  |
| M | scripts/setup-zitadel.sh | carry | feat/pr-party:scripts/setup-zitadel.sh → working tree; minimal behavior slice | Restore custom web/admin/env-file settings and default secret masking with explicit --show-secrets opt-in. |
| M | tests/conftest.py | dev supersedes | dev re-synthesis (same file, revised): tests/conftest.py | Revised app-auth/session fixture setup for dev behavior. |
| M | tests/integration/conftest.py | dev supersedes | dev re-synthesis (same file, revised): tests/integration/conftest.py | Host-provided DATABASE_URL/REDIS_URL replace sandbox-managed migration fixture; host must migrate first. |
| A | tests/integration/test_annotation_classification_parity.py | dev supersedes | dev-only addition |  |
| A | tests/integration/test_demo_project_provisioning.py | dev supersedes | dev-only addition |  |
| A | tests/integration/test_distinct_decisions.py | dev supersedes | dev-only addition |  |
| A | tests/integration/test_embedding_integrity.py | dev supersedes | dev-only addition |  |
| M | tests/integration/test_git_operations.py | dev supersedes | dev re-synthesis (same file, revised): tests/integration/test_git_operations.py | Unused import cleanup; Git assertions retained. |
| D | tests/integration/test_llm_review_regressions.py | carry | feat/pr-party:tests/integration/test_llm_review_regressions.py → working tree; minimal behavior slice | Restore live regression coverage with dev billing, dimension, and locked PR-claim seams; omit the unresolved VALID-04 malformed-parent case (see Findings and case map). |
| M | tests/integration/test_outcome_audit.py | dev supersedes | dev re-synthesis (same file, revised): tests/integration/test_outcome_audit.py | Project-bound multi-page cursor walk and live index assertion. |
| A | tests/integration/test_pr_party_credential_rewrap.py | dev supersedes | dev-only addition |  |
| M | tests/integration/test_project_workflow.py | dev supersedes | dev re-synthesis (same file, revised): tests/integration/test_project_workflow.py | Unused import cleanup; workflow assertions retained. |
| A | tests/integration/test_pull_request_concurrency.py | dev supersedes | dev-only addition |  |
| A | tests/integration/test_source_revision_advisory_lock.py | dev supersedes | dev-only addition |  |
| M | tests/integration/test_translation_lifecycle.py | dev supersedes | dev re-synthesis (same file, revised): tests/integration/test_translation_lifecycle.py | Carries source revision and actor role through current save/job seams. |
| A | tests/unit/test_anonymous_limit_migrations.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_anonymous_suggestions_routes.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_anonymous_suggestions_routes.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| M | tests/unit/test_anonymous_token.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_anonymous_token.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| A | tests/unit/test_anonymous_write_limits.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_auth_disabled.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_auth_disabled.py | Revised tests; adds test_anonymous_user_is_explicitly_marked, test_sensitive_boundary_rejects_only_anonymous_identity. |
| M | tests/unit/test_auto_accept_worker.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_auto_accept_worker.py | Revised tests; adds test_authorization_failure_does_not_mark_the_session_merged, test_cancels_when_project_disabled_after_scheduling. |
| M | tests/unit/test_bare_repository.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_bare_repository.py | Revised tests; adds test_expected_head_rejects_interleaved_ref_advance. |
| M | tests/unit/test_bare_repository_service.py | carry | feat/pr-party:tests/unit/test_bare_repository_service.py → working tree; minimal behavior slice | Restore TestBranchDetection.test_get_default_branch_follows_symbolic_head. |
| M | tests/unit/test_commit_identity.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_commit_identity.py | Explicit null, omitted address, and opt-in reset supersede empty-address-only case. |
| M | tests/unit/test_config.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_config.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| A | tests/unit/test_demo_generation.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_demo_generation_migration_contract.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_demo_project_migration_contract.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_demo_project_provisioning.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_demo_project_route_visibility.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_demo_refresh.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_demo_resync.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_demo_target_authorizer.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_distinct_decision_migration_contract.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_distinct_decision_routes.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_distinct_decision_service.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_distinct_decision_submission_gate.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_duplicate_check.py | carry | feat/pr-party:tests/unit/test_duplicate_check.py → working tree; minimal behavior slice | Restore exact-label dominance with weak signals and missing-structure identity regressions; dev test_semantic_similarity_warn_range supersedes the removed semantic-only warning test. |
| M | tests/unit/test_duplicate_check_route.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_duplicate_check_route.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| A | tests/unit/test_embedding_integrity.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_embedding_service.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_embedding_service.py | Required pgvector replaces optional dependency tests; staged/upsert/dimension and reservation assertions replace ORM mutation mocks. |
| M | tests/unit/test_embeddings_routes.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_embeddings_routes.py | Revised tests; adds test_find_similar_rejects_anonymous_at_route, test_invalid_paid_provider_config_returns_typed_422 and related cases. |
| M | tests/unit/test_entity_validation.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_entity_validation.py | Owned/foreign namespace cases explicitly replace well-formed external-IRI cases; pending host review. |
| A | tests/unit/test_generation_provenance.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_generation_routes.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_generation_routes.py | Revised tests; adds test_generate_402_when_atomic_reservation_refuses_call, test_generation_without_matching_active_session_is_harmless and related cases. |
| M | tests/unit/test_generation_schemas.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_generation_schemas.py | Revised tests; adds test_annotation_suggestion_requires_property_and_value, test_edge_suggestion_requires_target_and_type. |
| A | tests/unit/test_github_pr_sync.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_github_service.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_github_service.py | Revised tests; adds test_optional_get_propagates_non_404_failures, test_optional_get_returns_none_only_for_404 and related cases. |
| A | tests/unit/test_github_sync_migration_contract.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_github_sync_outbound.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_github_sync_outbound.py | Revised tests; adds test_target_denial_remains_typed_for_the_caller. |
| M | tests/unit/test_join_request_service.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_join_request_service.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| M | tests/unit/test_llm_audit.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_llm_audit.py | Revised tests; adds test_budget_refusal_rolls_back_without_audit_row, test_finalize_preserves_failed_outcome_without_error_detail and related cases. |
| M | tests/unit/test_llm_budget.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_llm_budget.py | Revised tests; adds test_budget_reservation_locks_before_reading_spend, test_projected_cost_cannot_cross_monthly_budget. |
| M | tests/unit/test_llm_config.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_llm_config.py | Revised tests; adds test_legacy_sha256_ciphertext_still_decrypts, test_new_ciphertext_uses_domain_separated_kdf. |
| A | tests/unit/test_llm_metering.py | dev supersedes | dev-only addition |  |
| D | tests/unit/test_llm_pricing.py | carry | feat/pr-party:tests/unit/test_llm_pricing.py → working tree; minimal behavior slice | Restore unknown-model fail-closed and negative-cache regressions; pricing implementation is identical between refs. |
| D | tests/unit/test_llm_prompt_safety.py | carry | feat/pr-party:tests/unit/test_llm_prompt_safety.py → working tree; minimal behavior slice | Restore prompt delimiter/cap, strict JSON, and hostile-IRI regressions; dev retains the asserted hardening. |
| M | tests/unit/test_llm_providers.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_llm_providers.py | Revised tests; adds test_cohere_forwards_output_token_cap, test_custom_provider_does_not_bypass_private_network_guard and related cases. |
| M | tests/unit/test_llm_rate_limiter.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_llm_rate_limiter.py | Revised tests; adds test_multi_unit_reservation_is_atomic_and_can_be_idempotent, test_multi_unit_reservation_rejects_invalid_units_without_redis and related cases. |
| M | tests/unit/test_llm_routes.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_llm_routes.py | Revised tests; adds test_connection_budget_refusal_prevents_provider, test_connection_reserves_paid_call_before_provider and related cases. |
| M | tests/unit/test_llm_ssrf.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_llm_ssrf.py | Pinned-DNS dial assertions replace generic pass-through mock. |
| M | tests/unit/test_llm_status_route.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_llm_status_route.py | test_status_provider_without_model_is_unconfigured replaces selected-model case. |
| M | tests/unit/test_main.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_main.py | Revised tests; adds test_disabled_auth_skips_all_pr_party_startup_hooks. |
| M | tests/unit/test_native_reviewer.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_native_reviewer.py | Revised tests; adds test_confirmation_locks_and_flushes_state_before_git, test_database_commit_failure_restores_translation_branch and related cases. |
| M | tests/unit/test_pr_party_actions.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pr_party_actions.py | Revised tests; adds test_final_budget_unit_still_allows_same_action_replay, test_reservation_receipt_replays_at_the_limit_without_incrementing. |
| A | tests/unit/test_pr_party_credential_rewrap.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_pr_party_credentials.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pr_party_credentials.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| M | tests/unit/test_pr_party_intake.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pr_party_intake.py | Revised tests; adds test_disabled_auth_omits_the_sweep_cron, test_disabled_pr_party_tasks_refuse_stale_queue_jobs. |
| M | tests/unit/test_pr_party_models.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pr_party_models.py | PR Party migration now uses h7i8j9k0l1m2 after g6h7i8j9k0l1. |
| M | tests/unit/test_pr_party_qa.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pr_party_qa.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| M | tests/unit/test_pr_party_read_api.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pr_party_read_api.py | Revised tests; adds test_routes_are_mounted_when_pr_party_is_configured. |
| M | tests/unit/test_pr_party_reconcile.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pr_party_reconcile.py | Revised tests; adds test_later_unsettled_failure_does_not_erase_an_earlier_repair. |
| M | tests/unit/test_pr_party_settings_routes.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pr_party_settings_routes.py | Revised tests; adds test_not_mounted_without_a_valid_reviewer_registry. |
| M | tests/unit/test_pr_party_webhooks.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pr_party_webhooks.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| M | tests/unit/test_project_service.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_project_service.py | Revised tests; adds test_delete_active_demo_denied_for_owner, test_get_hidden_demo_generation_denied_even_for_source_owner and related cases. |
| M | tests/unit/test_projects_routes_coverage.py | carry | feat/pr-party:tests/unit/test_projects_routes_coverage.py → working tree; minimal behavior slice | Restore non-main default-branch tree regression; dev source-revision and demo tests remain. |
| M | tests/unit/test_projects_routes_extended.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_projects_routes_extended.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| A | tests/unit/test_pull_request_idempotency.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_pull_request_service.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pull_request_service.py | Atomic allocation/system auto-accept and reviewer-objection assertions; removed editor-authorized merge case follows policy change in Findings. |
| M | tests/unit/test_pull_request_service_extended.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pull_request_service_extended.py | Revised tests; adds test_github_approval_does_not_halt_linked_suggestion, test_github_objection_halts_linked_suggestion and related cases. |
| M | tests/unit/test_pull_requests_routes.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_pull_requests_routes.py | Revised tests; adds test_anonymous_user_rejected_before_service_access. |
| A | tests/unit/test_release_workflow.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_rewrap_pr_party_credentials_script.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_source_revision_cas.py | dev supersedes | dev-only addition |  |
| A | tests/unit/test_structural_similarity_service.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_suggestion_generation.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_suggestion_generation.py | Retains generation assertions; adds redacted validation/dedup alert coverage. |
| M | tests/unit/test_suggestion_service.py | carry | feat/pr-party:tests/unit/test_suggestion_service.py → working tree; minimal behavior slice | Adapt existing write mocks to the carried commit_changes contract and use valid Turtle in tests of downstream failures. |
| A | tests/unit/test_suggestion_submission_content.py | dev supersedes | dev-only addition |  |
| M | tests/unit/test_suggestion_trust_integration.py | carry | feat/pr-party:tests/unit/test_suggestion_trust_integration.py → working tree; minimal behavior slice | Adapt existing write mocks/assertions to the carried commit_changes contract. |
| M | tests/unit/test_translation_backfill.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_translation_backfill.py | New-language case retains scope assertions and adds untagged source identity. |
| M | tests/unit/test_translation_config.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_translation_config.py | Revised tests; adds test_admin_can_clear_verifier_provider_and_model. |
| M | tests/unit/test_translation_coverage.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_translation_coverage.py | Revised tests; adds test_entity_state_load_is_scoped_to_the_requested_entity. |
| M | tests/unit/test_translation_jobs.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_translation_jobs.py | Actor/role attribution moves into atomic fan-out job reservations; adds deterministic identity and retry receipts. |
| M | tests/unit/test_translation_models.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_translation_models.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| M | tests/unit/test_translation_service.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_translation_service.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| M | tests/unit/test_trust_models.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_trust_models.py | Trust migration now follows c2d3e4f5g6h7 and exposes auto-accept scan index. |
| M | tests/unit/test_trust_rate_limiter.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_trust_rate_limiter.py | Revised fixtures/signatures/assertions follow dev behavior in the corresponding route, model, or service; retained test contracts. |
| M | tests/unit/test_trust_routes.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_trust_routes.py | Three-page project-bound cursor walk supersedes earlier two-page case. |
| M | tests/unit/test_trust_service.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_trust_service.py | Revised tests; adds test_clearing_explicit_grant_below_threshold_revokes_materialized_flag, test_count_accepted_by_user_groups_the_roster and related cases. |
| M | tests/unit/test_user_settings_routes.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_user_settings_routes.py | Revised tests; adds test_patch_explicit_null_clears_email_verification_and_opt_in, test_patch_omitted_email_preserves_address. |
| M | tests/unit/test_worker.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_worker.py | Revised tests; adds test_batch_reuses_one_eager_loaded_target_context_query, test_credential_lookup_failure_does_not_abort_later_integrations and related cases. |
| M | tests/unit/test_ws_auth.py | dev supersedes | dev re-synthesis (same file, revised): tests/unit/test_ws_auth.py | Revised tests; adds test_disabled_mode_no_token_succeeds_anonymous, test_disabled_mode_still_enforces_project_access and related cases. |
| M | uv.lock | drop | Dependency manifests are explicitly excluded from carry by the unit packet; retain dev dependency resolution. |  |

## Removed public symbols

| Path | Symbol | Disposition | Pointer or reason |
| --- | --- | --- | --- |
| alembic/versions/y2z3a4b5c6d7_widen_suggestion_session_status_check.py | branch_labels | dev supersedes | alembic/versions/e3f4g5h6i7j8_expand_suggestion_session_status_constraint.py:branch_labels; equivalent migration role, revised chain; status sets below |
| alembic/versions/y2z3a4b5c6d7_widen_suggestion_session_status_check.py | depends_on | dev supersedes | alembic/versions/e3f4g5h6i7j8_expand_suggestion_session_status_constraint.py:depends_on; equivalent migration role, revised chain; status sets below |
| alembic/versions/y2z3a4b5c6d7_widen_suggestion_session_status_check.py | down_revision | dev supersedes | alembic/versions/e3f4g5h6i7j8_expand_suggestion_session_status_constraint.py:down_revision; equivalent migration role, revised chain; status sets below |
| alembic/versions/y2z3a4b5c6d7_widen_suggestion_session_status_check.py | downgrade | dev supersedes | alembic/versions/e3f4g5h6i7j8_expand_suggestion_session_status_constraint.py:downgrade; equivalent migration role, revised chain; status sets below |
| alembic/versions/y2z3a4b5c6d7_widen_suggestion_session_status_check.py | revision | dev supersedes | alembic/versions/e3f4g5h6i7j8_expand_suggestion_session_status_constraint.py:revision; equivalent migration role, revised chain; status sets below |
| alembic/versions/y2z3a4b5c6d7_widen_suggestion_session_status_check.py | upgrade | dev supersedes | alembic/versions/e3f4g5h6i7j8_expand_suggestion_session_status_constraint.py:upgrade; equivalent migration role, revised chain; status sets below |
| ontokit/api/routes/anonymous_suggestions.py | ROUTE POST '/{project_id}/suggestions/anonymous/beacon' | dev supersedes | ontokit/core/api_paths.py:ANONYMOUS_BEACON_PATH; same method/URL still registered in ontokit/api/routes/anonymous_suggestions.py |
| ontokit/api/routes/anonymous_suggestions.py | ROUTE PUT '/{project_id}/suggestions/anonymous/sessions/{session_id}/save' | dev supersedes | ontokit/core/api_paths.py:ANONYMOUS_SAVE_PATH; same method/URL still registered in ontokit/api/routes/anonymous_suggestions.py |
| ontokit/models/embedding.py | Vector | dev supersedes | ontokit/models/embedding.py:Vector imports pgvector.sqlalchemy.Vector and remains in __all__; optional None fallback deliberately removed |
| ontokit/schemas/user_settings.py | GitHubTokenCreate | drop | Unused token DTO: git grep on both refs finds no route or consumer; live token status remains GitHubTokenStatus and /api/v1/users/me/github-token |
| ontokit/schemas/user_settings.py | GitHubTokenResponse | drop | Unused token DTO: git grep on both refs finds no route or consumer; live token status remains GitHubTokenStatus and /api/v1/users/me/github-token |
| ontokit/services/translation_service.py | AuditLogger | dev supersedes | ontokit/services/translation_service.py:MeteredProviderFactory / TranslationService._call → ontokit/services/llm/metering.py:MeteredLLMProvider (atomic reservation and audit) |
| ontokit/services/translation_service.py | BudgetChecker | dev supersedes | ontokit/services/translation_service.py:MeteredProviderFactory / TranslationService._call → ontokit/services/llm/metering.py:MeteredLLMProvider (atomic reservation and audit) |
| ontokit/worker.py | get_redis_settings | dev supersedes | ontokit/core/redis.py:get_redis_settings; imported into ontokit/worker.py, preserving worker.get_redis_settings and credential/database/TLS parsing |

### Deleted regression file case map

The two unit files are carried unchanged. `ontokit/services/llm/pricing.py` is byte-identical
between refs and still raises PricingUnavailableError and negative-caches failed fetches.
`ontokit/services/llm/prompts/__init__.py` retains the 12,000-character cap and untrusted
wrapper for every PROMPT_BUILDER; SuggestionGenerationService._parse_json_safe and
_is_safe_iri retain strict JSON and generated-IRI checks. The missing artifact is test coverage.

For tests/integration/test_llm_review_regressions.py, the following accounts for every
frozen test function (parametrizations remain inside their case):

| Frozen test function | Disposition | Behavior / adaptation |
| --- | --- | --- |
| test_p0_1_suggestion_save_commits_with_real_git_service | carry | SuggestionService.save → BareGitRepositoryService.commit_changes (carried repair). |
| test_f5_storage_key_path_saves_to_existing_root_ontology | carry | Carried Git-tree path resolution prevents storage-key shadow files. |
| test_f5_missing_resolved_baseline_names_existing_ontology_path | carry | Carried baseline-path inconsistency error; add dev billing_user_id argument. |
| test_f5_submit_rejects_oversized_new_entity_sweep_before_duplicate_checks | carry | dev MAX_NEW_ENTITIES_PER_SUBMISSION=25 retained; add dev billing_user_id argument. |
| test_r2_1_saved_entity_embedding_does_not_block_its_own_submit | carry | dev excludes submitting branch/new IRIs; provider.dimensions=3; patch locked claim seam. |
| test_r3_restriction_parent_baseline_allows_mint_save_and_submit | carry | URIRef parent filtering remains; patch locked claim seam without changing blank-node fixture. |
| test_f3_folio_parent_mint_submits_outside_project_namespace | carry | Current dev submit duplicate gate still permits this graph; patch locked claim seam. Generation VALID-04 conflict recorded separately. |
| test_f3_malformed_parent_422_names_rule_and_carries_errors | dev supersedes | Pending host review: dev ValidationService._check_namespace and SuggestionService._validate_submission_content replace the frozen validation policy; omitted from carry, not skipped or weakened. See Findings. |
| test_f3_existing_label_still_returns_duplicate_409 | carry | dev exact baseline label guard remains with distinct-decision exception. |
| test_p0_2_database_accepts_review_action_statuses | carry | e3f4g5h6i7j8 permits all three reviewed statuses. |
| test_p0_6_identical_real_embedding_blocks_without_structure | carry | dev exact-label dominance remains; explicit entity/provider dimensions=3 satisfy dev integrity contract. |
| test_p0_5_public_project_embedding_spend_requires_membership | carry | Carried semantic_search._verify_access membership gate. |
| test_p1_1_unpriced_model_stops_before_provider_on_real_budget_rows | carry | dev generation.get_model_pricing still precedes get_provider. |
| test_p1_7_p1_12_server_gates_content_before_real_git_commit | carry | Carried unconditional parse restores trusted malformed-Turtle 422; dev mint gate retains suggester 403. |
| test_p0_4_failed_merge_keeps_real_session_and_trust_ledger_unchanged | carry | dev _approve_unchecked propagates failed merge before status/trust writes. |

M-file test review also resolves removed cases: semantic-only warning coverage remains
in test_duplicate_check.py:test_semantic_similarity_warn_range; optional-pgvector
failures are replaced by the required Vector import and embedding integrity tests;
explicit-null preference tests supersede empty-string clearing; pinned-DNS tests
supersede pass-through transport mocking; configured-registry fixtures supersede
unconditionally mounted PR Party tests; three-page project-bound cursors supersede
two-page walks; atomic PR claim/savepoint tests supersede rollback-expiry mocks;
translation job identity/reservation tests supersede pre-enqueue single-unit billing.
The editor-authorized merge and external-IRI validation cases are accounted for as
policy differences under Findings.

## Migration lineage

Every frozen migration is listed below, including unchanged files outside the
name-status inventory: **41 frozen revisions**. Content verdicts:
**35 identical**, **5 same-id/different-content**, **1 superseded**.
Every lineage disposition is dev supersedes; no frozen migration is carried.

| Frozen migration path | revision | down_revision | Disposition | Content verdict / dev pointer |
| --- | --- | --- | --- | --- |
| alembic/versions/0c4545e3814e_add_merge_commit_hashes_to_pull_requests.py | 0c4545e3814e | 2d41d93ea12f | dev supersedes | identical content; alembic/versions/0c4545e3814e_add_merge_commit_hashes_to_pull_requests.py |
| alembic/versions/295fa3db0e38_add_author_name_and_email_to_pull_.py | 295fa3db0e38 | 0c4545e3814e | dev supersedes | identical content; alembic/versions/295fa3db0e38_add_author_name_and_email_to_pull_.py |
| alembic/versions/2d41d93ea12f_add_author_name_and_author_email_to_.py | 2d41d93ea12f | c8d9e0f1a2b3 | dev supersedes | identical content; alembic/versions/2d41d93ea12f_add_author_name_and_author_email_to_.py |
| alembic/versions/47cc27515626_add_subject_type_to_lint_issues.py | 47cc27515626 | 94afeba9ab5c | dev supersedes | identical content; alembic/versions/47cc27515626_add_subject_type_to_lint_issues.py |
| alembic/versions/5f63c89c3669_add_projects_and_project_members_tables.py | 5f63c89c3669 | None | dev supersedes | identical content; alembic/versions/5f63c89c3669_add_projects_and_project_members_tables.py |
| alembic/versions/94afeba9ab5c_add_project_lint_configs.py | 94afeba9ab5c | r1s2t3u4v5w6 | dev supersedes | identical content; alembic/versions/94afeba9ab5c_add_project_lint_configs.py |
| alembic/versions/a0b1c2d3e4f5_add_translation_jobs.py | a0b1c2d3e4f5 | f9a0b1c2d3e4 | dev supersedes | identical content; alembic/versions/a0b1c2d3e4f5_add_translation_jobs.py |
| alembic/versions/a1b2c3d4e5f6_add_import_fields_to_projects.py | a1b2c3d4e5f6 | 5f63c89c3669 | dev supersedes | identical content; alembic/versions/a1b2c3d4e5f6_add_import_fields_to_projects.py |
| alembic/versions/a3b4c5d6e7f8_add_normalization_report.py | a3b4c5d6e7f8 | 295fa3db0e38 | dev supersedes | identical content; alembic/versions/a3b4c5d6e7f8_add_normalization_report.py |
| alembic/versions/a4b5c6d7e8f9_add_embedding_budget_caps.py | a4b5c6d7e8f9 | z3a4b5c6d7e8 | dev supersedes | same revision id, different content; alembic/versions/a4b5c6d7e8f9_add_embedding_budget_caps.py; import spacing only, same parent/DDL |
| alembic/versions/b1c2d3e4f5g6_add_outcome_snapshot_columns.py | b1c2d3e4f5g6 | a0b1c2d3e4f5 | dev supersedes | identical content; alembic/versions/b1c2d3e4f5g6_add_outcome_snapshot_columns.py |
| alembic/versions/b5c6d7e8f9a0_add_translation_records.py | b5c6d7e8f9a0 | a4b5c6d7e8f9 | dev supersedes | same revision id, different content; alembic/versions/b5c6d7e8f9a0_add_translation_records.py; dev down_revision=h7i8j9k0l1m2, DDL preserved |
| alembic/versions/b7c9d8e1f2a3_add_pr_workflow_tables.py | b7c9d8e1f2a3 | e68f6b98b09b | dev supersedes | identical content; alembic/versions/b7c9d8e1f2a3_add_pr_workflow_tables.py |
| alembic/versions/c6d7e8f9a0b1_add_project_translation_configs.py | c6d7e8f9a0b1 | b5c6d7e8f9a0 | dev supersedes | identical content; alembic/versions/c6d7e8f9a0b1_add_project_translation_configs.py |
| alembic/versions/c8d9e0f1a2b3_add_lint_tables.py | c8d9e0f1a2b3 | b7c9d8e1f2a3 | dev supersedes | identical content; alembic/versions/c8d9e0f1a2b3_add_lint_tables.py |
| alembic/versions/d7e8f9a0b1c2_add_primary_translation_provider_and_model.py | d7e8f9a0b1c2 | c6d7e8f9a0b1 | dev supersedes | identical content; alembic/versions/d7e8f9a0b1c2_add_primary_translation_provider_and_model.py |
| alembic/versions/e68f6b98b09b_add_label_preferences_to_projects.py | e68f6b98b09b | a1b2c3d4e5f6 | dev supersedes | identical content; alembic/versions/e68f6b98b09b_add_label_preferences_to_projects.py |
| alembic/versions/e8f9a0b1c2d3_add_translation_record_values.py | e8f9a0b1c2d3 | d7e8f9a0b1c2 | dev supersedes | identical content; alembic/versions/e8f9a0b1c2d3_add_translation_record_values.py |
| alembic/versions/f4a5b6c7d8e9_add_preferred_branch_to_project_members.py | f4a5b6c7d8e9 | a3b4c5d6e7f8 | dev supersedes | identical content; alembic/versions/f4a5b6c7d8e9_add_preferred_branch_to_project_members.py |
| alembic/versions/f9a0b1c2d3e4_add_native_reviewer_languages.py | f9a0b1c2d3e4 | e8f9a0b1c2d3 | dev supersedes | identical content; alembic/versions/f9a0b1c2d3e4_add_native_reviewer_languages.py |
| alembic/versions/g5b6c7d8e9f0_add_branch_metadata_table.py | g5b6c7d8e9f0 | f4a5b6c7d8e9 | dev supersedes | identical content; alembic/versions/g5b6c7d8e9f0_add_branch_metadata_table.py |
| alembic/versions/h6c7d8e9f0g1_add_user_github_tokens_and_pat_auth.py | h6c7d8e9f0g1 | g5b6c7d8e9f0 | dev supersedes | identical content; alembic/versions/h6c7d8e9f0g1_add_user_github_tokens_and_pat_auth.py |
| alembic/versions/i7d8e9f0g1h2_add_github_sync_fields.py | i7d8e9f0g1h2 | h6c7d8e9f0g1 | dev supersedes | identical content; alembic/versions/i7d8e9f0g1h2_add_github_sync_fields.py |
| alembic/versions/j8e9f0g1h2i3_add_turtle_file_path.py | j8e9f0g1h2i3 | i7d8e9f0g1h2 | dev supersedes | identical content; alembic/versions/j8e9f0g1h2i3_add_turtle_file_path.py |
| alembic/versions/k9f0g1h2i3j4_add_join_requests_table.py | k9f0g1h2i3j4 | j8e9f0g1h2i3 | dev supersedes | identical content; alembic/versions/k9f0g1h2i3j4_add_join_requests_table.py |
| alembic/versions/l0g1h2i3j4k5_add_suggestion_sessions_table.py | l0g1h2i3j4k5 | k9f0g1h2i3j4 | dev supersedes | identical content; alembic/versions/l0g1h2i3j4k5_add_suggestion_sessions_table.py |
| alembic/versions/m1n2o3p4q5r6_add_entity_change_events.py | m1n2o3p4q5r6 | l0g1h2i3j4k5 | dev supersedes | identical content; alembic/versions/m1n2o3p4q5r6_add_entity_change_events.py |
| alembic/versions/n2o3p4q5r6s7_add_embedding_tables.py | n2o3p4q5r6s7 | m1n2o3p4q5r6 | dev supersedes | identical content; alembic/versions/n2o3p4q5r6s7_add_embedding_tables.py |
| alembic/versions/o3p4q5r6s7t8_add_notifications_table.py | o3p4q5r6s7t8 | n2o3p4q5r6s7 | dev supersedes | identical content; alembic/versions/o3p4q5r6s7t8_add_notifications_table.py |
| alembic/versions/p4q5r6s7t8u9_add_upstream_sync_tables.py | p4q5r6s7t8u9 | o3p4q5r6s7t8 | dev supersedes | identical content; alembic/versions/p4q5r6s7t8u9_add_upstream_sync_tables.py |
| alembic/versions/q5r6s7t8u9v0_add_suggestion_review_fields.py | q5r6s7t8u9v0 | p4q5r6s7t8u9 | dev supersedes | identical content; alembic/versions/q5r6s7t8u9v0_add_suggestion_review_fields.py |
| alembic/versions/r1s2t3u4v5w6_rename_upstream_to_remote_sync.py | r1s2t3u4v5w6 | s7t8u9v0w1x2 | dev supersedes | identical content; alembic/versions/r1s2t3u4v5w6_rename_upstream_to_remote_sync.py |
| alembic/versions/r6s7t8u9v0w1_add_github_hook_id_to_integrations.py | r6s7t8u9v0w1 | q5r6s7t8u9v0 | dev supersedes | identical content; alembic/versions/r6s7t8u9v0w1_add_github_hook_id_to_integrations.py |
| alembic/versions/s7t8u9v0w1x2_add_ontology_index_tables.py | s7t8u9v0w1x2 | r6s7t8u9v0w1 | dev supersedes | identical content; alembic/versions/s7t8u9v0w1x2_add_ontology_index_tables.py |
| alembic/versions/t8u9v0w1x2y3_add_anonymous_suggestion_fields.py | t8u9v0w1x2y3 | v9w0x1y2z3a4 | dev supersedes | identical content; alembic/versions/t8u9v0w1x2y3_add_anonymous_suggestion_fields.py |
| alembic/versions/u9v0w1x2y3a4_add_llm_config_audit_tables_and_member_flag.py | u9v0w1x2y3a4 | 47cc27515626 | dev supersedes | identical content; alembic/versions/u9v0w1x2y3a4_add_llm_config_audit_tables_and_member_flag.py |
| alembic/versions/v9w0x1y2z3a4_add_hnsw_index_and_duplicate_rejections.py | v9w0x1y2z3a4 | u9v0w1x2y3a4 | dev supersedes | identical content; alembic/versions/v9w0x1y2z3a4_add_hnsw_index_and_duplicate_rejections.py |
| alembic/versions/w0x1y2z3a4b5_add_trust_ladder.py | w0x1y2z3a4b5 | t8u9v0w1x2y3 | dev supersedes | same revision id, different content; alembic/versions/w0x1y2z3a4b5_add_trust_ladder.py; parent c2d3e4f5g6h7, auto_accept_claimed_until, partial scan index, unique session outcome |
| alembic/versions/x1y2z3a4b5c6_add_pr_party.py | x1y2z3a4b5c6 | w0x1y2z3a4b5 | dev supersedes | same revision id, different content; alembic/versions/x1y2z3a4b5c6_add_distinct_entity_decisions.py; DIFFERENT SCHEMA (distinct entity decisions). PR Party behavior moved to h7i8j9k0l1m2_add_pr_party.py |
| alembic/versions/y2z3a4b5c6d7_widen_suggestion_session_status_check.py | y2z3a4b5c6d7 | x1y2z3a4b5c6 | dev supersedes | superseded; alembic/versions/e3f4g5h6i7j8_expand_suggestion_session_status_constraint.py:e3f4g5h6i7j8 |
| alembic/versions/z3a4b5c6d7e8_cap_active_embedding_jobs_per_project.py | z3a4b5c6d7e8 | y2z3a4b5c6d7 | dev supersedes | same revision id, different content; alembic/versions/z3a4b5c6d7e8_cap_active_embedding_jobs_per_project.py; dev down_revision=t8u9v0w1x2y3, DDL preserved |

The critical collision is `x1y2z3a4b5c6`: frozen add_pr_party versus dev
add_distinct_entity_decisions. Dev retains PR Party DDL at
`alembic/versions/h7i8j9k0l1m2_add_pr_party.py`, revision h7i8j9k0l1m2, parent
g6h7i8j9k0l1. Its upgrade/downgrade behavior matches the renamed frozen file;
revision/parent metadata and formatting differ. The other content differences and
reparenting are explicit in the table, even where DDL is unchanged.

**U9 prerequisite:** reset the DEV application database before the first dev deploy.
Do not remap or stamp the reused revisions and do not upgrade the frozen database
in place. A matching revision id is not proof of a matching schema. This unit only
records that already-settled decision; it performs no database operation.

### Suggestion-session status constraint, value by value

Frozen y2z3a4b5c6d7 `_ALL_STATUSES`:
`active, submitted, auto-submitted, discarded, merged, rejected, changes-requested`.

Dev e3f4g5h6i7j8 `_ORIGINAL_STATUSES + _REVIEWED_STATUSES`:
`active, submitted, auto-submitted, discarded, merged, rejected, changes-requested`.

| Status | Frozen permits | Dev permits |
| --- | --- | --- |
| active | yes | yes |
| submitted | yes | yes |
| auto-submitted | yes | yes |
| discarded | yes | yes |
| merged | yes | yes |
| rejected | yes | yes |
| changes-requested | yes | yes |

The sets are equal; **no missing status value**. Both upgrade functions replace
ck_suggestion_session_status with these values. Dev downgrade additionally rejects
reviewed rows before restoring the four original statuses; no migration is needed.

### Dev head from the literal chain

Reading all 49 dev revision/down_revision pairs yields exactly one head,
**h6i7j8k9l0m1** (`alembic/versions/h6i7j8k9l0m1_add_atomic_demo_generations.py`). Every parent exists,
there are no duplicate revision ids within dev, no cycle, and every revision lies
on the single root-to-head chain. `alembic heads` remains orchestrator-owned.

```text
5f63c89c3669
a1b2c3d4e5f6
e68f6b98b09b
b7c9d8e1f2a3
c8d9e0f1a2b3
2d41d93ea12f
0c4545e3814e
295fa3db0e38
a3b4c5d6e7f8
f4a5b6c7d8e9
g5b6c7d8e9f0
h6c7d8e9f0g1
i7d8e9f0g1h2
j8e9f0g1h2i3
k9f0g1h2i3j4
l0g1h2i3j4k5
m1n2o3p4q5r6
n2o3p4q5r6s7
o3p4q5r6s7t8
p4q5r6s7t8u9
q5r6s7t8u9v0
r6s7t8u9v0w1
s7t8u9v0w1x2
r1s2t3u4v5w6
94afeba9ab5c
47cc27515626
u9v0w1x2y3a4
v9w0x1y2z3a4
t8u9v0w1x2y3
z3a4b5c6d7e8
a4b5c6d7e8f9
c2d3e4f5g6h7
w0x1y2z3a4b5
x1y2z3a4b5c6
e4f5g6h7i8j9
g6h7i8j9k0l1
h7i8j9k0l1m2
b5c6d7e8f9a0
c6d7e8f9a0b1
d7e8f9a0b1c2
e8f9a0b1c2d3
f9a0b1c2d3e4
a0b1c2d3e4f5
b1c2d3e4f5g6
d2e3f4g5h6i7
e3f4g5h6i7j8
f4g5h6i7j8k9
g5h6i7j8k9l0
h6i7j8k9l0m1
```

## Carried in this change

- `compose.yaml` — Restore configurable Zitadel origins, credentials, expiry, and log level; retain dev demo-mirror token additions.
- `ontokit/api/routes/semantic_search.py` — Restore membership requirement, billing_user_id attribution, and budget/pricing HTTP errors; preserve dev endpoint signatures.
- `ontokit/git/bare_repository.py` — Restore BareOntologyRepository.get_default_branch symbolic-HEAD resolution; retain dev CAS and demo push authorization.
- `ontokit/services/suggestion_service.py` — Restore commit_changes at all three write sites, Git-tree ontology-path resolution, missing-baseline error, unconditional Turtle parsing, and post-save/post-submit index refresh; retain dev distinct decisions, locks, quotas, and PR finalization.
- `scripts/setup-zitadel.sh` — Restore custom web/admin/env-file settings and default secret masking with explicit --show-secrets opt-in.
- `tests/integration/test_llm_review_regressions.py` — Restore live regression coverage with dev billing, dimension, and locked PR-claim seams; omit the unresolved VALID-04 malformed-parent case (see Findings and case map).
- `tests/unit/test_bare_repository_service.py` — Restore TestBranchDetection.test_get_default_branch_follows_symbolic_head.
- `tests/unit/test_duplicate_check.py` — Restore exact-label dominance with weak signals and missing-structure identity regressions; dev test_semantic_similarity_warn_range supersedes the removed semantic-only warning test.
- `tests/unit/test_llm_pricing.py` — Restore unknown-model fail-closed and negative-cache regressions; pricing implementation is identical between refs.
- `tests/unit/test_llm_prompt_safety.py` — Restore prompt delimiter/cap, strict JSON, and hostile-IRI regressions; dev retains the asserted hardening.
- `tests/unit/test_projects_routes_coverage.py` — Restore non-main default-branch tree regression; dev source-revision and demo tests remain.
- `tests/unit/test_suggestion_service.py` — Adapt existing write mocks to the carried commit_changes contract and use valid Turtle in tests of downstream failures.
- `tests/unit/test_suggestion_trust_integration.py` — Adapt existing write mocks/assertions to the carried commit_changes contract.

The ledger itself is the only additional audit artifact. No dependency manifest, deploy/** or .github/workflows/** file,
frozen documentation, or migration is carried. Root compose retains both dev
GITHUB_DEMO_MIRROR_TOKEN additions. The setup script is restored without executing it.
The integration file adapts only billing arguments, explicit embedding dimensions,
PR claim mocking, and formatting; its unresolved malformed-parent case is omitted
and explicitly accounted for above. Existing dev suggestion tests now mock the real
commit_changes API and pass valid Turtle when exercising later failures. Restored
refresh calls run after dev PR lock release rather than undoing the claim/finalize split.

## Findings

1. **VALID-04 policy requires host review; no policy carry.** Frozen
   `ontokit/services/validation_service.py:ValidationService._check_namespace`
   accepts external HTTP(S)/URN IRIs and rejects malformed entity/parent IRIs.
   Dev's same symbol explicitly requires project namespace ownership and no longer
   checks parent syntax. `tests/unit/test_entity_validation.py` replaces the
   external-IRI cases with owned/foreign namespace cases. Meanwhile dev
   `ontokit/services/suggestion_service.py:_validate_submission_content` no longer
   calls ValidationService at submission. The frozen integration case
   `test_f3_malformed_parent_422_names_rule_and_carries_errors` therefore has no
   equivalent at submit. Leave validation_service.py and test_entity_validation.py
   as dev supersedes pending host review, retain dev policy, and do not reintroduce
   the incompatible frozen test as a failing/disabled test. The FOLIO/blank-node
   submission cases still assert existing dev submit behavior, not an inferred
   resolution of this generation-versus-submission policy difference.

2. **Editor review authorization requires host review; no authorization carry.**
   Frozen `ontokit/services/pull_request_service.py:merge_pull_request` accepts
   suggestion_review_authorized from the reviewer-authorized SuggestionService;
   `tests/unit/test_pull_request_service.py:TestMergePullRequest.test_merge_pr_editor_allowed_after_suggestion_review_authorization`
   proves that bypass. Dev replaces it with `system_auto_accept`, restricted to
   SYSTEM_AUTO_ACCEPT_ACTOR, and interactive merges require owner/admin. Dev
   SuggestionService.approve still accepts editors through _verify_reviewer_access,
   so an editor can enter that endpoint but be denied by the merge service. Both
   authorization alternatives are explicit; the unit does not choose between them.
   pull_request_service.py and its unit-test row remain dev supersedes pending review.

3. **Entity-kind policy requires host review; no declaration-set carry.** Frozen
   `feat/pr-party:ontokit/services/suggestion_service.py:SuggestionService._declared_entities`
   includes OWL.NamedIndividual. Dev replaces it with
   `dev:ontokit/services/suggestion_service.py:_ENTITY_DECLARATION_TYPES`
   shared by the declaration helpers for classes/properties, adding
   RDFS.Class and property characteristics but omitting OWL.NamedIndividual.
   That changes which declarations trigger mint/submit gates. The carried
   suggestion_service.py slice preserves dev's shared declaration set; host review
   must decide whether individuals belong in those gates before restoring that slice.
