---
title: Changes feed second pass (API) - handoff
date: 2026-11-21
branch: drain/1120-diff-generative
head: 79c1466f
---

# Changes feed second pass (API) - handoff

## State
Branch `drain/1120-diff-generative`, pushed. Nothing is merged into `dev`, no PR is open, and nothing is deployed: the owner reviews this stream before it lands. `drain/1120-feed-scale` is merged into it.

## Done
1. **Real paths proven** (tests only; no fix was needed): the real pull-request merge holds the target branch lock through its commit (874e1560); a suggestion-session merge over HTTP keeps `ai` and `ai_edited` origin in the feed (255b9189); remote sync against a local bare remote, including force-push and two rewrites between syncs, records exact history (fd6fcc33).
2. **Feed scale** (f178191d): per-item reads keep only deciding events; new covering index. On 48k events, rows fetched per page fell from 17,987 to 901, and client CPU from 818 ms to about 60 ms.
3. **Cross-page consistency** (469667a8): cursors carry the first page's PostgreSQL snapshot; new `recorded_xact` column.
4. **Security review fixes**: verified-email credit limited to members (f3290bb2); reconciler off the event loop, with size, commit, and chain caps (25bb35a5); generation request limits (41d3e3d0); migration lock timeouts (06a04b77, 79c1466f); remote sync now takes the branch write lock, a race that already existed on `dev` (80984754).
5. **Format check**: the 16 files that fail `ruff format --check` are all untouched by this branch, and all fail on `dev`.

Gates at 79c1466f: unit 4321 passed; integration 185 passed, 6 skipped (PostgreSQL 17, Redis 7); ruff, format, and `scripts/typecheck.sh` clean; migrations upgrade, downgrade, and upgrade cleanly. API contract changes 6 to 8 are in the pointer plan.

## Remaining
- **Owner decision:** whether anonymous visitors see author names in the feed, filed through the decision pipeline (`changes-feed-anon-authors`). Today's behavior stays until then.
- **Suggester generation:** whether suggesters may generate is existing policy, reported but not filed.
- **Not load-tested:** the reconciler caps and threading on a large real repository.
- **Cursor length:** the cursor can pass 4,096 characters with near-2,000-character IRIs and many concurrent transactions.
- **Feed latency:** wall time is still 0.7 to 1.1 s per page on a loaded box; the slowest parts are the item query (about 110 ms) and the author summary.
- **Flaky test:** `test_history_enqueue_timeout_does_not_block_landing` (10 ms timeout) failed once.
- **mypy:** bare `.venv/bin/mypy` fails on numpy stubs under Python 3.13; use `scripts/typecheck.sh`.
- **Intermediate commits:** 41d3e3d0 and 06a04b77 were not gated on their own tree; the head is.
