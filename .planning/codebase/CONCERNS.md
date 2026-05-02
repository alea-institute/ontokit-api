# Codebase Concerns

**Analysis Date:** 2026-05-02

## Tech Debt

**Incomplete Ontology Service (16 NotImplementedError stubs):**
- Issue: `OntologyService` class in `ontokit/services/ontology.py` (lines 195–216, 258–269, 336, 341, 352, 664, 671, 678, 685, 690) has critical methods stubbed with `raise NotImplementedError()`
- Files: `ontokit/services/ontology.py`
- Impact: Cannot create/list/update/delete ontologies, serialize history, compute diffs, update/delete classes, retrieve class hierarchy, or manage properties
- Fix approach: Implement database integration for CRUD ops, Git integration for versioning, and RDF diff semantics. These are foundational service methods blocking API functionality.

**Deprecated Git Repository Module Still In Codebase:**
- Issue: `ontokit/git/repository.py` mentioned in CLAUDE.md as "Legacy GitPython implementation (deprecated)" but may still be referenced
- Files: Potential lingering imports in git module or dependency graph
- Impact: Code confusion, maintenance burden from dual implementations
- Fix approach: Confirm all imports are migrated to `ontokit/git/bare_repository.py` (pygit2-based), then delete `repository.py` entirely

**Bare Repository Implementation Complexity:**
- Issue: `ontokit/git/bare_repository.py` is 1315 lines, providing low-level git operations via pygit2
- Files: `ontokit/git/bare_repository.py`, used by `ontokit/services/pull_request_service.py` (line 1601 TODO)
- Impact: Large, monolithic file difficult to test and maintain; hard to add new git features
- Fix approach: Break into focused modules (e.g., `commit_ops.py`, `diff_ops.py`, `branch_ops.py`), add docstring examples for each public method

**Synchronous-to-Async Bridge Issues:**
- Issue: `asyncio.to_thread()` wraps blocking operations but only used in 4 places; potential for more missed cases
- Files: `ontokit/api/dependencies.py:93`, `ontokit/services/project_service.py:362`, `ontokit/services/embedding_providers/local_provider.py:54`
- Impact: Unknown blocking calls in pygit2 or RDFLib could starve event loop; local embedding provider forces thread pool
- Fix approach: Audit all RDFLib graph operations, pygit2 calls, and file I/O in async context. Document sync call locations with `# BLOCKING I/O` marker

## Known Bugs

**Multiple Exception Handlers Swallowing Errors:**
- Symptoms: Silent failures in user settings, search, and analytics endpoints
- Files: `ontokit/api/routes/user_settings.py:38,75`, `ontokit/api/routes/search.py:76,81`, `ontokit/api/routes/analytics.py:33`
- Trigger: Errors in GitHub token decryption, search query parsing, or analytics aggregation are caught and suppressed
- Workaround: Enable debug logging to see swallowed exceptions; check server logs for stack traces
- Fix approach: Log exception details before returning error response; replace bare `except Exception:` with specific exception types and proper logging

**Token Decryption Fallback Returns Masked String:**
- Symptoms: User settings endpoint returns `****` for any decryption failure without distinguishing between missing config and corrupted ciphertext
- Files: `ontokit/api/routes/user_settings.py:31–39` (function `_token_preview`)
- Trigger: Invalid GitHub token encryption key, changed secret, or corrupted stored token
- Workaround: Re-save GitHub token if encryption key changed
- Fix approach: Raise `HTTPException` with clear message instead of returning masked placeholder; distinguish "key not set" from "decryption failed"

**Database Transaction Rollbacks After Async Gaps:**
- Symptoms: Failed embedding jobs and batch operations may leave inconsistent state if rollback happens after async checkpoint
- Files: `ontokit/services/embedding_service.py`, recent commits mention "Rollback failed transaction before updating job status" (line 6422198, 45f6a39)
- Trigger: Long-running embedding job fails; database transaction committed partially before error
- Workaround: Manual cleanup via direct SQL updates or re-trigger full re-index
- Fix approach: Wrap all async operations in explicit transaction boundaries; use `savepoint` for partial rollback on job sub-steps

## Security Considerations

**Default Secret Key in Config:**
- Risk: `secret_key` defaults to `"change-me-in-production"` in `ontokit/core/config.py:24`
- Files: `ontokit/core/config.py`, `ontokit/services/embedding_service.py:43–48` (uses secret_key for encryption)
- Current mitigation: Documentation warns to set `SECRET_KEY` env var; deployment scripts should enforce this
- Recommendations: Fail at startup if `app_env == "production"` and `secret_key` is default value; never ship with hardcoded defaults

**GitHub Token Encryption Key Not Enforced:**
- Risk: `github_token_encryption_key` can be empty string; `ontokit/core/encryption.py:15–20` raises `HTTPException` at call time, not boot time
- Files: `ontokit/core/encryption.py`, `ontokit/core/config.py`
- Current mitigation: Runtime exception if user tries to store a GitHub token without key configured
- Recommendations: Validate at server startup (like secret_key); ensure all encrypted-token endpoints have explicit auth checks

**JWT/JWKS Cache Uses Global Mutable State:**
- Risk: `_jwks_cache` and `_jwks_cache_time` are module-level globals in `ontokit/core/auth.py:71–73`
- Files: `ontokit/core/auth.py:71–73, 86–100, 138–141`
- Current mitigation: Double-checked locking pattern with `asyncio.Lock()` prevents cache stampede
- Recommendations: Move to class-based cache (e.g., `JWKSCache` singleton) for testability and cleaner state management

**Encryption Function Duplicated in Two Modules:**
- Risk: `_get_fernet()`, `_encrypt_secret()`, `_decrypt_secret()` defined in both `ontokit/services/embedding_service.py:43–58` and `ontokit/core/encryption.py:9–43`
- Files: `ontokit/services/embedding_service.py`, `ontokit/core/encryption.py`
- Current mitigation: Both use Fernet symmetric encryption; embedding service uses application secret, core uses GitHub token key
- Recommendations: Consolidate into shared utility; make key derivation explicit per use case

**Superadmin IDs Stored in Environment Variable:**
- Risk: `superadmin_user_ids` parsed from CSV string in config; no validation that IDs are well-formed UUIDs
- Files: `ontokit/core/config.py:94–98`
- Current mitigation: Checked against user IDs at runtime
- Recommendations: Parse and validate superadmin IDs on startup; reject malformed values rather than silently ignoring

## Performance Bottlenecks

**Ontology Index Batch Processing May Stall on Large Graphs:**
- Problem: `OntologyIndexService` in `ontokit/services/ontology_index.py` uses `BATCH_SIZE = 1000` for bulk inserts; memory usage unbounded on multi-MB ontologies
- Files: `ontokit/services/ontology_index.py:40`
- Cause: Graph is loaded entirely into memory, then indexed in chunks; no streaming/chunked parsing
- Improvement path: Implement streaming RDF parser; index entities as parsed rather than all-at-once. Monitor memory usage under 50MB+ ontologies.

**Entity Graph BFS Traversal Unbounded by Default:**
- Problem: `build_entity_graph()` in `ontokit/services/ontology.py` lacks depth limit for seeAlso reverse links; can traverse entire graph
- Files: `ontokit/services/ontology.py` (recent fixes address per-node budgets, but global traversal strategy unclear)
- Cause: External namespace heuristics filter some results, but user-defined seeAlso links can chain deeply
- Improvement path: Add configurable max-nodes and max-edges parameters; track visited nodes across BFS; document traversal limits

**Embedding Service Sequential Processing:**
- Problem: `EmbeddingService.embed_ontology()` processes entities sequentially; no parallelization even for independent embeddings
- Files: `ontokit/services/embedding_service.py`
- Cause: Library constraints (sentence-transformers) and batch size coordination
- Improvement path: Use asyncio task groups for batching; profile CPU vs. I/O bottleneck. Consider GPU acceleration if feasible.

**Pull Request Service 1874 lines, 50+ methods:**
- Problem: Monolithic service handling PR CRUD, reviews, comments, merges, GitHub sync
- Files: `ontokit/services/pull_request_service.py:1874`
- Cause: Accretion of features without refactoring into focused domain objects
- Improvement path: Extract `ReviewService`, `CommentService`, `GitHubSyncHandler` into separate classes; keep `PullRequestService` as orchestrator

## Fragile Areas

**OntologyService Untested RDF Operations:**
- Files: `ontokit/services/ontology.py` (tested in `tests/unit/test_ontology_service.py` but coverage of stubbed methods is 0%)
- Why fragile: Most class/property methods (`get_class`, `list_classes`, `create_class`) are lightly tested; changes to RDF property handling break silently
- Safe modification: Add integration tests for RDF graph mutations; use `rdflib.compare.isomorphic_graphs()` to validate results
- Test coverage: Stubs have no tests; tested methods have ~70% line coverage

**Suggestion Service (900 lines) Low Test Isolation:**
- Files: `ontokit/services/suggestion_service.py`, tested in `tests/unit/test_suggestion_service.py:2157`
- Why fragile: Depends on ontology indexing, embeddings, and change events; mocking incomplete in unit tests
- Safe modification: Extract suggestion ranking logic into pure functions; mock external service calls explicitly
- Test coverage: 2157-line test file suggests broad coverage, but integration gaps with `EmbeddingService` and `OntologyIndexService`

**Pull Request Service Merge Logic Edge Cases:**
- Files: `ontokit/services/pull_request_service.py:83–150` (merge sync), complex branch/commit reconciliation
- Why fragile: Git state transitions (merge commit detection, PR status sync) have multiple branches and exception handlers
- Safe modification: Write scenario-based tests (e.g., "merge via GitHub, then check PR status locally"); separate git logic from database updates
- Test coverage: `tests/unit/test_pull_request_service.py` + extended, but merge sync and GitHub integration untested in isolation

**Embedding Provider Registry Pattern:**
- Files: `ontokit/services/embedding_providers/` directory and `ontokit/services/embedding_service.py` (line 34)
- Why fragile: Dynamic provider loading (`get_embedding_provider()`) not protected against missing implementations; OpenAI/Voyage providers hardcoded
- Safe modification: Define provider interface contract; add plugin loader with validation; test all provider paths
- Test coverage: `tests/unit/test_embedding_service.py` tests provider switching, but provider implementations (`openai_provider.py`, `voyage_provider.py`) have minimal test coverage

## Scaling Limits

**PostgreSQL Row-Level Locking on Concurrent Index Updates:**
- Current capacity: Tested with single concurrent embedding job per project; multiple simultaneous jobs contend for `OntologyIndexStatus` rows
- Limit: >10 concurrent index jobs per project will see significant contention; see recent commits about rollback handling
- Scaling path: Use advisory locks (`pg_advisory_lock`) or job queue (ARQ) to serialize index jobs per project; add queue depth monitoring

**Redis Pub/Sub for Ontology Index Updates:**
- Current capacity: Broadcast channel `"ontology_index:updates"` has no backpressure or message queue
- Limit: High-volume index updates (>1000/min) may lose messages if subscriber lags
- Scaling path: Switch from pub/sub to Redis streams; maintain consumer groups and offset tracking per subscriber

**MinIO S3 Client Blocking on Network Timeouts:**
- Current capacity: No timeout configuration for MinIO client; default system TCP timeout (~120 sec)
- Limit: Slow network (>10MB files, poor connectivity) blocks async tasks
- Scaling path: Set explicit socket timeout in MinIO config; use asyncio.timeout() around S3 calls; add circuit breaker for storage failures

## Dependencies at Risk

**GitPython Dependency Still Listed (Deprecated):**
- Risk: `gitpython>=3.1.0` in `pyproject.toml:dependencies` but codebase uses `pygit2>=1.13.0`
- Impact: Unused dependency increases lock file size and attack surface; confusion for contributors
- Migration plan: Remove `gitpython` from dependencies once migration script is deprecated (keep script, just mark as historical)

**OWLReady2 ORM Limitations:**
- Risk: `owlready2>=0.47` provides ORM-like interface for OWL, but not used (codebase uses RDFLib Graph directly)
- Impact: Dead dependency; maintenance burden if newer versions break compatibility
- Migration plan: Either commit to OWLReady2 for entity modeling or remove and document why RDFLib was chosen

**Sentence-Transformers Local Embedding Provider:**
- Risk: `sentence-transformers>=3.0.0` loads 100MB+ model files to disk; blocks embedding setup
- Impact: Slow container startup; can exhaust disk on ephemeral compute
- Migration plan: Move to lazy loading (download on first use); or switch to cloud embedding (OpenAI/Voyage) as default

## Missing Critical Features

**No Ontology CRUD Enforcement:**
- Problem: Cannot create, update, or delete ontologies via API due to `NotImplementedError` stubs
- Blocks: Fundamental user workflows; ontologies can only be imported, not managed
- Fix approach: Implement `create()`, `update()`, `delete()` with database backing; coordinate with git branching strategy

**No Semantic Diff Implementation:**
- Problem: `diff()` method in `ontokit/services/ontology.py:266–269` raises `NotImplementedError`
- Blocks: PR review feature (showing what changed semantically) cannot function
- Fix approach: Use RDFLib `graph_diff()` and `to_isomorphic()` (already imported in `bare_repository.py:17`); map changes to high-level ontology operations

**No Pull Branch Implementation on BareGitRepositoryService:**
- Problem: `ontokit/services/pull_request_service.py:1601` has `TODO: implement pull_branch on BareGitRepositoryService`
- Blocks: Cannot pull remote branches into local bare repo
- Fix approach: Implement pygit2-based remote tracking and fetch; test with GitHub integration

## Test Coverage Gaps

**Ontology Service CRUD/Mutation Methods (16 stubs):**
- What's not tested: `create()`, `list_all()`, `get()`, `update()`, `delete()`, `serialize()`, `import_from_file()`, `get_history()`, `diff()`, `update_class()`, `delete_class()`, `get_class_hierarchy()`, `list_properties()`, `create_property()`, `update_property()`, `delete_property()`
- Files: `ontokit/services/ontology.py` (lines 193–216, 258–269, 331–352, 663–690)
- Risk: Breaking changes to RDF graph operations go undetected; ontology persistence logic untested
- Priority: **High** — these are foundational APIs

**Pull Request Merge Synchronization Logic:**
- What's not tested: `_sync_merge_commits_to_prs()`, merge commit detection, PR status rollback on sync failure
- Files: `ontokit/services/pull_request_service.py:83–150`
- Risk: Git state inconsistency between PR records and actual commits
- Priority: **High** — affects data integrity

**Embedding Provider Failover and Fallback:**
- What's not tested: OpenAI provider timeout recovery, Voyage API fallback, local provider graceful degradation
- Files: `ontokit/services/embedding_providers/openai_provider.py`, `voyage_provider.py`, `local_provider.py`
- Risk: Embedding failures cascade to index staleness
- Priority: **Medium** — impacts search quality but not critical path

**GitHub App Installation and Webhook Integration:**
- What's not tested: GitHub App auth flow, webhook verification, install/uninstall handlers
- Files: `ontokit/services/github_service.py:593`, `ontokit/api/routes/auth.py`
- Risk: Unauthorized webhook processing, ghost installations
- Priority: **High** — security-critical

**Cryptographic Key Management:**
- What's not tested: Key rotation, encryption key changes, corrupted ciphertext handling
- Files: `ontokit/core/encryption.py`, `ontokit/services/embedding_service.py:43–58`
- Risk: Decryption failures on key rollover; silent data corruption
- Priority: **Medium** — affects token and credential handling

---

*Concerns audit: 2026-05-02*
