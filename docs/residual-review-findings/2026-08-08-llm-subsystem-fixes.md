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

## Round 2

| Finding | Fix commit | Red evidence | Green evidence |
|---|---|---|---|
| Defect 1 — submit self-dedup | `b9832da3` | Live save/embed/submit regression reproduced the branch entity as its own blocking candidate before exclusions. | `test_r2_1_saved_entity_embedding_does_not_block_its_own_submit` saves through real bare git, writes the real pgvector row through `embed_single_entity`, and submits successfully. |
| Defect 2 — PR Party pricing | `1830ffaf` | `test_unknown_model_pricing_degrades_without_failing_brief` raised `PricingUnavailableError` from `_run_llm`. | The same test reaches `ready`; unavailable pricing logs and displays zero cost without aborting generation. |
| Defect 3 — local-provider drift | `ea6bac6e` | Generation used a hard-coded set lacking `custom`. | Generation now consumes `llm._LOCAL_PROVIDERS`, the single provider classification source. |
| Defects 4 and 7 — imported parents / refresh logging | `ad1f8111` | A well-formed FOLIO parent was rejected as unknown; active-job uniqueness skips returned silently. | The FOLIO-parent submit proof passes, malformed absolute IRIs remain rejected, and uniqueness skips emit an INFO record. |
| Defect 5 — advisory lock lifetime | `0a4ffe94` | Paid embedding audit committed the caller session inside submit validation. | Audit writes use an independent session, so the caller transaction and `pg_advisory_xact_lock` remain open through PR creation. |
| Defect 6 — embedding-only budget | `7c487904` | Paid embeddings required a `ProjectLLMConfig` row. | Embedding configuration owns monthly/daily caps (migration `a4b5c6d7e8f9`) and is the fallback budget source. |
| P0-6 warn tier / P1-5 delimiter | `5797e46e` | Semantic-only near matches could not exceed 0.50; a literal closing delimiter survived in payload. | `test_semantic_only_near_duplicate_can_warn` reaches `warn` at 0.86; delimiter tokens are escaped before wrapping. |
| P0-5 route gaps | `419bcf6a` | Only helper-level semantic access was exercised. | Anonymous route requests are 401 for semantic search and duplicate check before either service is invoked. |

Round-2 verification commands: `.venv/bin/pytest -q`, `.venv/bin/mypy ontokit`, and
`.venv/bin/ruff check ontokit tests`. The first command includes the live Postgres/pgvector
and Redis integration suite using the round-1 harness variables.

## Round 3

| Finding | Fix commit | Red evidence | Green evidence |
|---|---|---|---|
| Restriction blank-node parents blocked every real FOLIO submit | `45b82a42` | Before the fix, `test_r3_restriction_parent_baseline_allows_mint_save_and_submit` performed a real bare-git mint save and submit over restriction-bearing Turtle, then failed with 422 `Suggestion references a malformed parent IRI`. Independent rdflib parses assigned different blank-node identifiers, so the whole-graph `subClassOf` triple subtraction treated the unchanged restriction parent as added. | The same live integration test now submits successfully. Parent validation iterates only `rdfs:subClassOf` objects on newly declared URIRef entities, ignores legitimate blank-node restriction parents, and `test_blocks_malformed_parent_on_minted_entity` proves an `ftp://` URIRef parent still returns the exact malformed-parent 422. |

### Blank-node identity audit

| Round 1–2 path checked | Comparison shape | Verdict / action |
|---|---|---|
| `_validate_submission_content` parent validation | Raw `subClassOf` triple-set difference across independent baseline/proposal parses | **Unsafe; fixed in `45b82a42`.** Replaced the graph-wide triple diff with parent inspection scoped to `new_entities`; only malformed URIRef parents are rejected and blank-node restriction parents are legitimate. |
| `_validate_turtle_and_detect_mint` | `_declared_entities(proposed) - _declared_entities(current)` | **Safe.** `_declared_entities` admits only URIRef subjects with named declaration types, so parse-local blank-node identifiers cannot enter either set. |
| `_validate_submission_content` mint detection | `_declared_entities(proposed) - _declared_entities(baseline)` | **Safe.** This is the same URIRef-only entity-set comparison; restriction blank nodes are excluded by construction. |
| `_validate_submission_content` duplicate-label gate | Labels collected only for baseline/new declared URIRef entities | **Safe.** It compares normalized literal label text and entity URIRefs, not RDF triples or blank-node identifiers. |
| All other production changes from Round 1 start (`b35d2947`) through Round 2 disposition (`3dc64817`) | Reviewed the production diff for `Graph`, `triples`, baseline/proposal, and set-difference comparisons | **No additional cross-parse RDF triple comparison found.** The malformed-parent block was the only Round 1–2 path whose result depended on blank-node identity. |

Round-3 verification on 2026-08-08: `.venv/bin/pytest -q` — **2580 passed**,
including 31 live integration tests; `.venv/bin/mypy ontokit` — **Success: no issues
found in 168 source files**; `.venv/bin/ruff check ontokit tests` — **All checks
passed**.
