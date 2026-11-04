---
title: "fix: translation entity-state no longer blocks the event loop"
date: 2026-11-04
status: active
type: fix
---

# Translation entity-state: stop parsing the whole ontology on every click

## Evidence (live BAIF demo, 2026-11-04, signed-in owner, Playwright)

Every class click fires `GET /translation/entity-state`. It took 4.6–7.5 s
(Canon ~4.7–5.1 s, FOLIO ~6.6–7.5 s). The class-detail GET fired at the same
moment finished within 10 ms of it, every time, although it takes ~200 ms when
anonymous users (who never call entity-state) click. The detail panel heading
therefore appeared 4.7–7.5 s after the click.

Cause: `TranslationCoverageService._load_branch_graph`
(`ontokit/services/translation_coverage.py`) runs a synchronous
`Graph().parse()` of the full branch ontology file inside an async request
handler. The parse holds the event loop, so every request the same uvicorn worker
holds waits behind it.

The parsed graph only feeds `_is_verified` (provenance annotation check), which
returns False unless a record's state is `verified`.

## Changes

1. **Skip the parse when unneeded.** `_load_entity` loads the branch graph only
   if one of the entity's `TranslationRecord`s has state `verified`; otherwise it
   passes an empty `Graph`. Behaviour stays identical because `_is_verified`
   returns False before touching the graph for non-verified records.
2. **Never parse on the event loop.** `_load_branch_graph` reads the file and runs
   `Graph().parse` via `asyncio.to_thread` (covers `coverage`, `classify` and
   entity-state paths). The git read moves into the thread too.
3. Keep the "fresh from branch head" semantics (no cross-request cache) — the
   docstring's correctness rule stands.

## Tests

- Unit: entity-state with no verified record never calls `_load_branch_graph`.
- Unit: entity-state with a verified record still loads the graph and reports
  `verified` when the annotation digest matches (existing tests cover this).
- Unit: `_load_branch_graph` runs parse off-loop (patch `asyncio.to_thread` or
  assert the loop stays responsive).
- Full `pytest tests/unit`, `ruff check`, `ruff format --check` on touched files,
  `pyright`.

## Verification after deploy

Re-run the signed-in Playwright click measurement: entity-state < 300 ms for
entities without verified records; class detail no longer finishes in lockstep
with entity-state; detail heading < 600 ms after click.

## Follow-ups (not in this change)

- Web: fire references/history/similar in parallel with class detail (today they
  wait for it, adding a second ~300–1000 ms round trip), and render tree-known
  label/IRI instantly as placeholder data.
- API: a commit-keyed provenance index if verified records become common.
