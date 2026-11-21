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

## Build status (2026-11-20, evening)

All three units are built on `drain/1120-diff-generative`. Nothing is merged into `dev` and nothing is deployed. An evening review read the whole branch diff and closed the findings the web plan's build status left open.

| Finding | State |
|---|---|
| Pull-request merge attribution relied on a 30-second wait | Closed. The merge holds the target branch's write lock from the ref move through its commit, and the reconciler takes the same lock. The wait is gone. |
| A work branch's first landing could be missed | Closed. A work branch's history starts where it left the default branch. An inline save without a contiguous watermark queues reconciliation. The daily sweep covers every recorded branch. |
| Feed queries could disagree during a backfill | Closed. Each feed response reads one repeatable-read snapshot. |
| `other` in `field_changes` was ambiguous | Closed. See the contract changes below. |
| The ledger row was written after the response | Closed. It commits before the response leaves, bounded to five seconds; a ledger failure still never fails generation. |
| A landing made while a branch's job was finishing queued nothing | Found and closed. Job ids name the branch head. |
| A concurrent index build that was interrupted left the migration unable to rerun | Found and closed. Both migrations tolerate their own leftovers. |
| Subclass and sibling prompts omitted the class definition | Found and closed. |
| Import events named the importer under the `system` author | Reported by the web acceptance run; closed. |
| An exact-label duplicate passed when no embeddings exist | Reported by the web acceptance run; closed. |

Verified on PostgreSQL 17 (pgvector) and Redis 7 in throwaway containers: upgrade, downgrade, and upgrade again on an empty database and on 2,500 seeded events; both concurrent index builds run outside a transaction; two worker processes reconcile one branch of 60 commits and record each once; the ARQ worker reconciles two landings queued back to back; and generate, accept, edit, and reject read as `ai`, `ai_edited`, and absent through the HTTP application with the provider stubbed at the HTTP boundary. The tests live in `tests/integration/` and skip without `DATABASE_URL` and `REDIS_URL`.

Still open:

- ~~A heavily edited entity loads every event in its window to compute its net change, although the response shows 20.~~ Closed 2026-11-21; see Feed scale below.
- ~~Across pages of one feed, a backfill can still move an entity past the cursor. Each response is consistent; reload to see the settled order.~~ Closed 2026-11-21; see contract change 6.
- `ruff format --check` reports 16 unit-test files this branch does not touch; the same files fail on `dev`.
- Not exercised here: remote sync against a real GitHub repository, and the suggestion-session merge path through HTTP (covered by unit tests on real Git).

## API contract changes (evening)

These amend the contract in the web plan. Response shapes are unchanged except where noted.

1. **`other` is defined.** An event lists `other` in `changed_fields` when the entity's statements changed in a way its changed tracked fields do not show: a predicate outside the five tracked fields, an anonymous class expression such as a restriction, or the language tag or datatype of a tracked literal whose text stayed the same. It can now appear beside tracked fields (`["labels", "other"]`); such an event has `event_type` `update`, not `rename`. In `field_changes`, `{"field": "other", "before": null, "after": null}` appears when any event in the window lists `other` and the entity's `net_change` is `modified`. It carries no values, so the omit-when-equal rule does not apply to it.
2. **Author of system work.** An import or normalization a member ran carries that member's `user_id` and `user_name` with `origin` `system`. The `system` author always has `user_name` null.
3. **Filter limits.** `author`, `entity_type`, and `event_type` accept at most 50 values each, `origin` at most 4, each text value at most 255 characters, and `cursor` at most 4,096. Beyond that the feed returns 422.
4. **Duplicate verdicts without embeddings.** `generate-suggestions` returns `duplicate_verdict` `block` when a proposed subclass or sibling has the same label, ignoring case and spacing, as a class already under the same parent or as an earlier proposal in the same batch. The match leads `duplicate_candidates` with `score` 1.0; for a repeat within a batch the candidate's `iri` is the earlier proposal's.
5. **Work-branch feeds.** A work branch's feed now includes every commit since it left the default branch, not only commits after its first reconciliation.
6. **Cursor pages share the first page's history.** A feed's first page records the PostgreSQL snapshot it read, and its `next_cursor` carries that snapshot beside the window end. Every later page, and its `summary`, counts only events whose inserting transaction that snapshot saw. Events recorded while a reader pages through, including backfilled or reconciled events with old timestamps, wait for the next first page, so paging never skips or repeats an entity and the union of the pages equals the first page's feed. Every page of one walk returns the same `summary`. `next_cursor` stays opaque and under 4,096 characters for IRIs of ordinary length; a cursor whose snapshot does not parse, or from before this change, returns 422, and the client restarts from the first page.

## Feed scale (2026-11-21)

An item now reads at most its 20 newest events plus the few that decide it: the earliest and latest event touching each field, the earliest listing `other`, the newest carrying a label, and the newest per origin and per author. The database counts the rest. Net-change rules are unchanged; unit tests on SQLite and integration tests on PostgreSQL compare every item against the item built from all of its events over random histories.

A new index, `ix_change_events_project_branch_entity_time` on `(project_id, branch, entity_iri, created_at, id)` including `event_type`, `origin`, and `recorded_xact`, hands the grouping its events in history order, so the window no longer sorts on disk. Revision `77a67de3294e` adds the nullable `recorded_xact` column (the inserting transaction, filled by a column default; existing rows stay NULL and count as seen by every snapshot) without rewriting the table, and revision `l0m1n2o3p4q5` then builds the index concurrently.

Measured on PostgreSQL 17 with 48,066 events across 2,000 entities in one project (60 with 500 to 600 events each, all on the first page) beside 200,000 events in four other projects, on a shared box under heavy load:

| First page, 50 items | Before | After |
|---|---|---|
| Event rows the item query returns | 17,987 | 901 |
| Client CPU per request (median of 7) | 818 ms | 57 ms |
| Wall time per request (median of 7) | 2,176 ms | 1,069 ms |
| Grouping statement, `EXPLAIN ANALYZE` | 108 to 172 ms, external merge sort | 34 ms, index-only scan, no heap fetches |

Every statement uses an index (`ix_change_events_project_time` for the summary scans, the new index for grouping and item reads). The item query (about 110 ms with 60 heavy entities on the page) and the author summary, a window over every event in the window (about 90 to 160 ms), are now the slowest statements.

