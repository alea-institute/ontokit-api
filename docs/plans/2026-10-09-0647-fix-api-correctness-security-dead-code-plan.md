---
title: API Correctness, Hardening and Dead Code - Plan
type: fix
date: 2026-10-09
topic: api-correctness-security-dead-code
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
execution: code
---

# API Correctness, Hardening and Dead Code - Plan

## Goal Capsule

- **Objective:** search, the class tree and GitHub push sync give the same, complete answer whichever code path serves them. A DEV deploy can't race itself into a failed migration, and no code path exists only to raise `NotImplementedError`.
- **Product authority:** the 2026-10-09 OntoKit drain tranche 1 brief. Web-side fixes live in the companion ontokit-web plan of the same date.
- **Open blockers:** none.

## Product Contract

### Summary

Make bare `rdf:Property` and `rdfs:Class` entities visible and correctly kinded on both the indexed and RDFLib paths. Implement the missing `pull_branch` used by the GitHub push webhook, and delete the stub service methods the legacy routers left behind. Remove the shadowed duplicate branch routes. Close the DEV worker migration race, and finish the #43 file-mode hardening in `Dockerfile.prod`.

### Problem Frame

A survey of `origin/dev` at `b14d30f1` found #212, #207 and alea#55 already fixed. These gaps remain:

- **Search and tree.** RDFLib `search_entities` ignores bare `rdf:Property` (CatholicOS#121). The index finds such properties but reports them as `object` properties. The RDFLib class tree considers only `owl:Class`, while the index tree also includes `rdfs:Class`.
- **Push sync.** `handle_github_push_webhook` calls `BareGitRepositoryService.pull_branch`, which does not exist. Every push webhook raises `AttributeError`, logs a warning and never syncs.
- **Dead code.** Sixteen `OntologyService` methods raise `NotImplementedError` and have had no callers since the legacy routers were removed.
- **Duplicate routes.** `POST /projects/{id}/branches` and a branch-checkout route are registered twice, so the second registration is dead code that can drift.
- **Migration race.** On DEV, `deploy/compose.dev.yaml` starts api and worker from the same image, and both run `alembic upgrade head` at once (`UniqueViolationError` on `pg_type_typname_nsp_index`).
- **Image file modes.** `Dockerfile.prod` still copies plain files without explicit modes, the residual of alea#43.
- **Inventory blind spot.** The disabled-mode write inventory test never sees PR Party routes, because they mount only when PR Party is enabled.

### Key Decisions

- **A bare `rdf:Property` has no property kind.** Reporting it as `object` asserts an OWL commitment the ontology never made, so the response carries `property_kind: null`. Governs R1, R2.
- **`pull_branch` reuses the `github_sync` fast-forward-or-merge logic** rather than writing a second sync algorithm, and honors `github_mirror_outbound_only`. Governs R5.
- **Stubs are deleted, not implemented.** The project-scoped API replaced them, and implementing an unused surface adds attack surface. Governs R7.
- **Migrations get one leader plus a lock.** The api container is the leader, and alembic takes a Postgres advisory lock as defense in depth for any second runner. Governs R9, R10.

### Requirements

**Search and tree parity**

- R1. RDFLib `search_entities` returns bare `rdf:Property` entities as properties with `property_kind` null. An entity that is also typed with an OWL property kind appears once, with the OWL kind.
- R2. Indexed search reports bare `rdf:Property` entities with `property_kind` null instead of `object`, and returns the same entity set and kinds as the RDFLib path for a shared fixture.
- R3. RDFLib root-class and child-class listing include `rdfs:Class` entities, so the tree matches the indexed tree for a shared fixture.
- R4. The annotation parity test also asserts that `skos:altLabel` values, including language tags, match between the warm and cold paths (#212 gap).

**GitHub push sync**

- R5. A GitHub push webhook for the integration's default branch updates the local default branch from the remote and records `last_sync_at`. It does this by fast-forward, or by the same merge policy `sync_github_project` uses, and it does nothing when `github_mirror_outbound_only` is on.
- R6. A push webhook whose sync fails logs the failure with project and branch context and returns the webhook's normal response, without leaving the branch partially updated.

**Dead code**

- R7. The `NotImplementedError` methods in `ontokit/services/ontology.py` are removed. So are their schemas that nothing else uses, and any newly uncalled helper such as `create_class`. Live behavior does not change.
- R8. Each duplicated branch route is registered exactly once, and a test fails if any method and path pair is registered twice.

**Deploy hardening**

- R9. In `deploy/compose.dev.yaml` the worker does not run migrations and starts only after the api is healthy, matching `compose.yaml`.
- R10. `alembic upgrade` holds a Postgres advisory lock for the duration of the migration, so two concurrent runners serialize instead of colliding.
- R11. `Dockerfile.prod` copies plain files with explicit readable modes, and the Dockerfile contract test covers both Dockerfiles.
- R12. The disabled-mode write inventory test runs with PR Party enabled as well, so PR Party write routes cannot slip in unauthenticated.

### Acceptance Examples

- AE1. **Covers R1, R2.** Given `:hasFoo a rdf:Property` and `:hasBar a rdf:Property, owl:ObjectProperty`, a search for `has` on either path returns `hasFoo` with kind null and `hasBar` once, with kind `object`.
- AE2. **Covers R5.** Given the remote default branch is two commits ahead, a push webhook leaves the local branch at the remote head and sets `last_sync_at`. With `github_mirror_outbound_only` on, the branch is unchanged.
- AE3. **Covers R10.** Given two processes run `alembic upgrade head` on an empty database at the same moment, both exit 0 and the schema is at head.

### Scope Boundaries

- Changing the host-installed copy of `deploy/compose.dev.yaml` on the DEV box. The runbook gets a note, and the copy is refreshed by the normal deploy procedure.
- Deploying to production.
- The Phase 17 BFS graph endpoint (CatholicOS PR #37), which is tranche 2 and conflicts with the legacy-router removal.
- A dedicated `ENTITY_TYPE` for bare `rdf:Property` beyond what R2 needs, if R2 can be met without a schema migration.

### Outstanding Questions

- Deferred to Planning: whether R2 needs an index schema change or a reindex, or can derive the kind from stored types.
- Deferred to Planning: which duplicate route registration is canonical; keep the one the router order actually serves today.

### Sources / Research

- `ontokit/services/ontology.py` (`search_entities`, stubs at lines 240-313, 378-390 and 709-743)
- `ontokit/services/ontology_index.py` (`RDF_TYPE_MAP`, `property_kind_map`)
- `ontokit/services/pull_request_service.py:2184-2215`, `ontokit/services/github_sync.py`, `ontokit/git/bare_repository.py`
- `ontokit/api/routes/projects.py:1096`, `ontokit/api/routes/pull_requests.py:417`
- `deploy/compose.dev.yaml`, `compose.yaml:311`, `scripts/entrypoint.sh`, `alembic/env.py`
- `Dockerfile.prod`, `tests/unit/test_dockerfile_contract.py`, `tests/unit/test_auth_disabled_routes.py`
- `tests/integration/test_annotation_classification_parity.py`

## Planning Contract

### Key Technical Decisions

- KTD1. **New index entity type `rdf_property`.** `entity_type` is a `String(30)` column, so no migration is needed. `RDF_TYPE_MAP` maps `RDF.Property` to it, and an entity that also carries an OWL property type is stored once, with the OWL type. `reverse_type_map` maps it to `property` and `property_kind_map` maps it to `None`. Every place that filters on the property entity types includes it. Existing indexes still say `object_property` for such entities until their next reindex, which is acceptable and noted in the PR. Covers R2.
- KTD2. **RDFLib parity mirrors the index.** `search_entities` adds `(RDF.Property, "property", None)` and `(RDFS.Class, "class", None)`, deduplicated by IRI with the OWL-typed result winning. Root and child class queries consider `owl:Class` or `rdfs:Class`. Covers R1, R3.
- KTD3. **`pull_branch` becomes a call to `sync_github_project`.** The webhook resolves the integration's PAT the same way `worker.py` `sync_github_projects` does, and passes `outbound_only=settings.github_mirror_outbound_only`. The service then sets `last_sync_at` and status as it already does for the worker. The `# type: ignore` is removed. Covers R5, R6.
- KTD4. **Duplicate routes: keep the `projects.py` registrations,** which router order serves today, and delete the shadowed copies in `pull_requests.py`. A new route-uniqueness test enumerates `app.routes` under the default settings and under PR Party enabled. Covers R8.
- KTD5. **The advisory lock lives in `alembic/env.py` `do_run_migrations`:** `SELECT pg_advisory_lock(<fixed 64-bit key>)` before `run_migrations`, with unlock in `finally`, on the same connection. It applies only when the dialect is postgresql. Covers R10.

### Sequencing

- **In parallel:** U1, U2, U3 and U4.
- **After U1:** U5, because both edit `ontokit/services/ontology.py`.

## Implementation Units

### U1. Entity discovery parity (rdf:Property, rdfs:Class) and the altLabel parity gap

- **Requirements:** R1, R2, R3, R4.
- **Files (owned):**
  - `ontokit/services/ontology.py` (only `search_entities` and the class root/children/count queries)
  - `ontokit/services/ontology_index.py`
  - `tests/unit/test_ontology_service_extended.py`
  - `tests/unit/test_ontology_index_service.py`
  - `tests/unit/test_entity_discovery_parity.py` (new)
  - `tests/integration/test_annotation_classification_parity.py`
- **Approach:** KTD1 and KTD2. Grep for every use of `ENTITY_TYPE_OBJECT_PROPERTY` and of the property-type list, so that filters (`entity_types` for `property`), counts and the tree include the new type.
- **Test scenarios:**
  - The AE1 fixture on both paths.
  - Dual-typed dedupe.
  - An `rdfs:Class`-only class appears in the RDFLib root classes and children.
  - `property_kind` is null in the schema response.
  - Parity: altLabel with `@de` and `@en-gb` tags is equal on the warm and cold paths.
- **Verification:** `uv run pytest tests/unit/test_ontology_service_extended.py tests/unit/test_ontology_index_service.py tests/unit/test_entity_discovery_parity.py tests/integration/test_annotation_classification_parity.py`.

### U2. GitHub push webhook sync (`pull_branch`)

- **Requirements:** R5, R6.
- **Files (owned):**
  - `ontokit/services/pull_request_service.py` (`handle_github_push_webhook` only)
  - `ontokit/services/github_sync.py` (only if a small seam is needed)
  - `tests/unit/test_pull_request_service_extended.py` (push-webhook tests)
  - `tests/unit/test_github_push_webhook_sync.py` (new)
- **Approach:** KTD3. Do not add `pull_branch` to `BareGitRepositoryService` unless reuse proves impossible; if so, report why. A missing PAT logs and returns.
- **Test scenarios:**
  - The remote is ahead and the sync fast-forwards, setting `last_sync_at` (AE2). This uses a real temporary bare repo pair where feasible, following `tests/unit/test_github_sync*.py` patterns.
  - Outbound-only leaves the branch untouched.
  - A ref that is not the default branch is ignored.
  - A sync exception is logged with project context, and the branch is unchanged.
- **Verification:** `uv run pytest tests/unit/test_pull_request_service_extended.py tests/unit/test_github_push_webhook_sync.py`. Then `uv run mypy ontokit/services/pull_request_service.py` under Python 3.11, or report whether the 3.13 mypy limitation still blocks it.

### U3. Route uniqueness and PR Party write inventory

- **Requirements:** R8, R12.
- **Files (owned):**
  - `ontokit/api/routes/pull_requests.py` (delete the shadowed duplicates only)
  - `tests/unit/test_route_uniqueness.py` (new)
  - `tests/unit/test_auth_disabled_routes.py`
- **Approach:** KTD4. Build the app with PR Party enabled through the same settings override the PR Party tests use, and run the existing exhaustive write-inventory assertion against it. Add genuinely protected PR Party routes, such as HMAC webhooks, to the allowlist with a reason.
- **Test scenarios:**
  - Uniqueness fails on any duplicate method and path pair, and passes after deletion.
  - The inventory with PR Party enabled lists no unauthenticated write route outside the allowlist.
- **Verification:** `uv run pytest tests/unit/test_route_uniqueness.py tests/unit/test_auth_disabled_routes.py tests/unit -k "branch or pull_request"`.

### U4. Deploy hardening: migration leader, advisory lock, Dockerfile.prod modes

- **Requirements:** R9, R10, R11.
- **Files (owned):**
  - `deploy/compose.dev.yaml`
  - `deploy/RUNBOOK.md`
  - `alembic/env.py`
  - `Dockerfile.prod`
  - `tests/unit/test_dockerfile_contract.py`
  - `tests/integration/test_migration_advisory_lock.py` (new)
- **Approach:**
  - **Compose:** in `deploy/compose.dev.yaml`, set the worker's `RUN_MIGRATIONS: "0"` and add `depends_on: api: condition: service_healthy`, adding an api healthcheck if one is absent, modeled on `compose.yaml`.
  - **Lock:** add the KTD5 advisory lock to `alembic/env.py`.
  - **Dockerfile.prod:** add `--chmod=0644` on the plain-file COPYs, and `0755` dirs where `Dockerfile` already does so.
  - **Runbook:** a note in `RUNBOOK.md` that the host copy of `compose.dev.yaml` must be refreshed.
  - **Do not touch `deploy/release-manifest.json`,** which triggers DEV deploys.
- **Test scenarios:**
  - The contract test yaml-parses `deploy/compose.dev.yaml`, checking the worker's `RUN_MIGRATIONS` "0" and its health dependency on the api.
  - The contract test checks the COPY flags in both Dockerfiles.
  - The integration test starts two `alembic upgrade head` subprocesses concurrently against a fresh schema in the test database and asserts both exit 0 (AE3). It may skip when `DATABASE_URL` is unset, like the other integration tests, and must not drop the shared test DB's public schema: use a dedicated temporary database or schema.
- **Verification:** `uv run pytest tests/unit/test_dockerfile_contract.py tests/integration/test_migration_advisory_lock.py`, then `docker build -f Dockerfile.prod .` as a smoke test, then `docker compose -f deploy/compose.dev.yaml config -q`.

### U5. Remove the `NotImplementedError` stubs (after U1)

- **Requirements:** R7.
- **Files (owned):**
  - `ontokit/services/ontology.py` (stub methods only)
  - `ontokit/schemas/__init__.py`
  - the schema modules defining `OntologyCreate`, `OntologyUpdate`, `OntologyListResponse` and `OWLProperty*`, only where nothing else uses them
  - tests that reference removed symbols
- **Approach:** delete each stub listed in the Problem Frame. Delete `create_class` and any other helper that becomes uncalled, and their tests. Verify with grep across `ontokit/`, `tests/`, `scripts/`, `alembic/` and `deploy/` before deleting each symbol, and keep anything referenced.
- **Test scenarios:** the full suite still passes, and `grep -rn "Database integration pending" ontokit` returns nothing.
- **Verification:** the full `uv run pytest tests/`, plus `ruff check ontokit/` and `ruff format --check ontokit/`.

## Verification Contract

- **Database prep:** `uv run alembic upgrade head` against the test database.
- **Full suite:** `uv run pytest tests/` passes, with at least the 3,763 tests of the baseline.
- **Lint and types:** `uv run ruff check ontokit/` and `uv run ruff format --check ontokit/` are clean. mypy under Python 3.11 matches CI; if the local venv cannot run it, CI's lint job is the gate.
- **PR CI:** green before merge (`release.yml`: lint, test, build, docker_preflight).
- **UAT:** a local API against synthetic data.
  - `GET /projects/{id}/ontology/search?q=` shows bare `rdf:Property` with kind null.
  - Two concurrent `alembic upgrade head` runs succeed.

## Definition of Done

- Every R has a test that fails without its fix.
- No `NotImplementedError("Database integration pending")` remains.
- `deploy/release-manifest.json` is untouched.
- Abandoned-attempt code is removed.
- CatholicOS issue numbers (#121, #212) are referenced in commits only. No CatholicOS PRs or comments.
