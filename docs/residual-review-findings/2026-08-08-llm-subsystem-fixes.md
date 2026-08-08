# 2026-08-08 LLM subsystem API fixes

Scope: API-side P0/P1 findings from
`2026-08-08-llm-subsystem-review.md`, based at `88de4244`. Web-only remedies
(P1-10, P1-13, P1-14, and P1-15) remain owned by the parallel web change.

## Disposition

| Finding | Fix commit(s) | Red evidence | Green evidence |
|---|---|---|---|
| P0-1 | `c3e1f936` | `b6d002e7`: live temp bare-repo save failed with `AttributeError: ... commit_to_branch` before the fix. | `test_p0_1_suggestion_save_commits_with_real_git_service`; real `BareGitRepositoryService`, no git mock. |
| P0-2 | `d796eabb` | `b6d002e7`: live migrated Postgres rejected `merged`, `rejected`, and `changes-requested` with `ck_suggestion_session_status`. | Parametrized live-CHECK test accepts all three review states; migration `y2z3a4b5c6d7` and model constraint agree. |
| P0-3 (shared contract) | `55fa0511` | At `88de4244`, `/llm/status` reported configured with a key but no selected model, while generation rejected the same config. | `tests/unit/test_llm_status_route.py` requires a selected model for readiness; web owns model selection. |
| P0-4 | `b6b27748`, `a48b211f` | At `88de4244`, `_approve_unchecked` caught merge `HTTPException` and continued to write `merged` plus an ACCEPTED outcome. | `test_p0_4_failed_merge_does_not_grant_trust_credit` uses live Postgres and proves the failed merge leaves `submitted` and zero outcome rows. |
| P0-5 | `167d8061`, `b43fc7ab`, `68da2d25` | At `88de4244`, both public routes accepted `OptionalUser` and reached unmetered owner-key embedding calls. | Live-Postgres `test_p0_5_public_project_embedding_spend_requires_membership` returns 403 for an authenticated non-member; paid provider calls now budget, price, audit, and attribute the caller. |
| P0-6 | `7b60405b`, `66ef1201`, `d96de506` | `b6d002e7`: a real pgvector row with an identical label/vector and no parent returned `pass` at the structural 0.80 ceiling. | `test_p0_6_identical_real_embedding_blocks_without_structure` returns `block` with score 1.0 both without a parent and with an unindexed parent; unavailable structural weight is renormalized without lowering thresholds, and an exact normalized label deterministically blocks even when richer embedding text dilutes semantic similarity. |
| P1-1 | `088d4682`, `b2c389c8`, `66ef1201` | Unknown models returned `(0, 0)` and pricing-fetch failures retried on every request at `88de4244`. | Unit pricing proofs plus live-budget `test_p1_1_unpriced_model_stops_before_provider_on_real_budget_rows` return 503 before paid-provider creation; local providers retain explicit zero-cost operation; fetch failures are negatively cached. |
| P1-2 | `698aa4ce`, `b43fc7ab`, `66ef1201`, `d96de506` | At `88de4244`, provider embedding calls had no budget/audit path and active-job uniqueness was branch-scoped in code only. | Paid embedding operations require a budget-bearing configuration, check budget before invocation, and write audit cost; the migration reconciles legacy duplicate active jobs before installing a project-wide partial unique index. |
| P1-3 | `4b0e9531` | Provider responses without `usage` returned zero tokens. | Provider tests assert deterministic token estimates for OpenAI-compatible and Anthropic responses with missing usage. |
| P1-4 | `7d447e0e`, `66ef1201`, `9cefd86d` | Generated parent/edge/annotation IRIs were accepted without syntax screening. | Generation parsing rejects whitespace, delimiters, and unsafe schemes while preserving HTTP(S), URN, and the ontology CURIE vocabularies before Turtle serialization. |
| P1-5 | `7d447e0e` | Prompts interpolated uncapped ontology strings and greedy prose salvage extracted injected JSON. | Prompt tests prove untrusted-data boundaries and caps; strict whole-response JSON parsing replaces greedy salvage, and typed/IRI validation constrains related output. |
| P1-6 | `4b0e9531` | Anthropic dropped `base_url` and constructed its own default transport. | Provider test proves configured base URL plus the SSRF-secure HTTP client reach `AsyncAnthropic`. |
| P1-7 | `35b97ef8`, `b6960cb3`, `66ef1201` | The client-controlled `mints_entity` flag was the only mint gate. | Real-git integration proves hidden minting is rejected before commit; save, beacon, and anonymous paths derive classes, properties, and named individuals from parsed branch content inside the cross-worker lock. |
| P1-8 | `35b97ef8`, `b6960cb3`, `d96de506` | Process-local branch locks could not serialize four production workers. | Git writes and submit-time validation/status transition take the same Postgres transaction advisory lock keyed by project and branch; writers refresh and recheck ACTIVE after lock acquisition. |
| P1-9 | `35b97ef8` | Counters/entity JSON were updated from a session snapshot loaded before locking. | Locked paths refresh the ORM session after the advisory lock before incrementing or updating entity metadata. |
| P1-11 | `698aa4ce`, `b6960cb3` | Suggestion writes and merges enqueued no embedding/index refresh. | Saves enqueue index plus entity embedding; submit and confirmed merge enqueue full branch/default-branch embedding, with failed Redis enqueue releasing the active-job slot. |
| P1-12 | `35b97ef8`, `b35d2947`, `66ef1201` | Arbitrary client Turtle was committed and submit did not gate duplicates or unknown parents. | Real-git malformed-Turtle proof leaves branch bytes unchanged; submit and anonymous-submit parse Turtle and fail closed through exact/composite duplicate and `ValidationService` gates for newly declared entities. |

## Integration harness

- `tests/conftest.py` and `tests/integration/conftest.py` read
  `TEST_DATABASE_URL` and `TEST_REDIS_URL`, defaulting to the supplied local
  Postgres/pgvector and Redis services.
- The session bootstrap runs `alembic upgrade head` before integration tests.
- Integration tests carry `@pytest.mark.integration`; use
  `pytest -m 'not integration'` for unit-only runs.

## Verification

- Full suite: `.venv/bin/pytest -q` — **2574 passed**, including 26 live
  integration tests, on 2026-08-08.
- Changed production/test files: Ruff clean and focused strict mypy clean.
- Repository-wide `.venv/bin/mypy .` is not green at this base: 402 existing
  errors in 33 test files, dominated by pre-existing test doubles and
  `pr_party` files outside this task's allowed mutation scope.
- Repository-wide `.venv/bin/ruff check .` is not green at this base: three
  pre-existing out-of-scope findings (an import-order finding in applied
  migration `t8u9v0w1x2y3` and two `F541` findings in
  `scripts/prepare-release.py`). The applied migration was intentionally not
  edited.

## Post-deploy monitoring and validation

For 24 hours after deployment, the API owner should watch logs for
`Pricing unavailable`, `Embedding budget exhausted`, failed embedding enqueue,
suggestion merge failures, and Postgres constraint violations. Healthy signals
are successful suggestion save/submit/merge flows, non-zero paid-provider audit
costs, and embedding jobs reaching `complete`. Roll back the API release (while
leaving additive migrations in place) if confirmed merges fail, trust outcomes
appear without merges, or active embedding jobs remain stuck for more than one
worker cycle.
