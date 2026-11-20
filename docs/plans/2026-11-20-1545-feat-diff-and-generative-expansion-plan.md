---
title: Diff view and generative expansion (API pointer) - Plan
type: feat
date: 2026-11-20
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# Diff view and generative expansion (API pointer) - Plan

The full plan, including the API contract both repositories build against, lives in `ontokit-web` at `docs/plans/2026-11-20-1545-feat-diff-and-generative-expansion-plan.md`. That document is authoritative; this file only names the API's share.

## API units

- **U1. Change-origin storage, definition-aware diff, and the changes feed.** One migration (`j8k9l0m1n2o3`) adds `origin`, `origin_model`, and `origin_batch_id` to `entity_change_events` and creates `llm_generated_entities`. The change recorder compares skos:definition, keeps values on create and delete, and accepts an origin resolver. New routes: `GET /projects/{id}/analytics/changes` and `GET /projects/{id}/analytics/changed-entities`.
- **U2. LLM ledger, batch ids, and the AI-origin resolver.** `POST /projects/{id}/llm/generate-suggestions` returns `batch_id` and records each proposed subclass or sibling; `AIOriginService.resolve` tags created entities `ai` or `ai_edited`.
- **U3 (second wave). Wire origin into every landing path and backfill from git.** Source save, pull-request merge, suggestion approval, and remote sync record events with origin; a reconciler fills history from git commits that have no events.

U1 and U2 run in parallel on disjoint files. U3 waits on both.

## Verification

`pytest --no-cov -q tests/unit` (baseline 4115 passed, 1 skipped), `ruff check ontokit tests/unit`, and a migration upgrade and downgrade on a scratch database.
