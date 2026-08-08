# LLM-Subsystem Fix Verification (API) — 2026-08-08

Adversarial verification of the fix round (88de4244..5dc8dd07) against
`2026-08-08-llm-subsystem-review.md`. Fresh-context Opus verifier; skeptical default.

## Original findings
- VERIFIED-FIXED against live seams: P0-1 (real `commit_changes` contract), P0-2
  (CHECK widened via migration y2z3a4b5c6d7, single head), P0-4 (no swallow; trust
  only on confirmed merge), P0-5 (RequiredUser + membership + metered spend), P1-1,
  P1-2, P1-3, P1-4, P1-6, P1-7, P1-9, P1-11, P1-12.
- PARTIAL: P0-3 (API now truthful, but unreachable until web picker ships — web
  round fixed in parallel, cross-check pending); P0-6 (exact-match blocks, but
  renormalization caps non-exact composite at 0.50 so the warn tier is unreachable);
  P1-5 (delimiter not escaped in payload; no output-relatedness check); P1-8
  (advisory lock added but reopened by new defect 5).

## NEW defects introduced by the fix round
1. HIGH — suggestions self-block at submit: save() embeds the new entity on the
   suggestion branch; `semantic_search_all_branches` has no branch/IRI exclusion;
   submit's dedup gate exact-matches its own label → composite forced 1.0 → 409.
   (suggestion_service.py:590-593, embedding_service.py:710-720, :274-284)
2. HIGH — `pr_party_brief.py:786` calls `get_model_pricing` with no handler; P1-1's
   raise now escapes brief generation for any model absent from LiteLLM pricing.
3. MEDIUM — `generation.py:172` local-provider set omits `custom` (llm.py:72-77
   includes it) → self-hosted gateways 503 on every generate.
4. MEDIUM — `suggestion_service.py:206-214` 422s any subClassOf target not declared
   in the project's own .ttl — rejects FOLIO/imported/external parents (the main
   product case).
5. MEDIUM — `embedding_service.py:150` commits the caller's transaction (audit
   write), releasing the pg_advisory_xact_lock mid-submit; P1-8 race reopens when
   the embedding provider is paid.
6. LOW — paid embeddings hard-require a ProjectLLMConfig row; embedding-only
   projects fail every job.
7. LOW — `_enqueue_branch_refresh` swallows the new unique-index IntegrityError,
   silently skipping post-merge re-embed.

## Test gaps
- P0-4 test re-mocks the seam (MagicMock PR service); does not prove editors/system
  actor can now merge via `suggestion_review_authorized`.
- P0-5 test calls `_verify_access` directly; no route-level anonymous-rejection
  test; `duplicate_check.py` has no integration test.

## Verdict
NOT yet DEV-deployable. Required before deploy: self-branch/self-IRI dedup
exclusion; imported-parent acceptance; PricingUnavailableError handled in
pr_party_brief; local-provider set aligned. pr_party files untouched at file level
(diff empty); defect 2 breaks it behaviorally from outside.
