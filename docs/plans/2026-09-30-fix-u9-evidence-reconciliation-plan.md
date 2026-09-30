---
title: Reconcile U9 deployment and acceptance evidence
type: fix
date: 2026-09-30
---

# Goal Capsule

Replace the stale approval-wait premise with a verified account of the current
DEV release, acceptance and recovery. Lineage: “OntoKit DEV: first dev-line deploy
(U9) — waiting on the dev-deploy approval click”; sweep
`weekly-sweep-20260928t120410-095f52`. This document is local preparation, not a
deployment receipt or a claim that U9 is complete.

## Product Contract

Preserve any successful deployment; execute only missing acceptance work after
reconciling existing evidence. Do not rerun run 34155698435 merely to make a new
receipt. Keep hosted identity acceptance distinct from deployment success.

## Planning Contract and local evidence

This execution branch starts at `773c51aa`, 254 commits ahead of cached
`origin/main` and zero behind. It is older than the reviewer's cached
`origin/dev` (`44b6dfd37732d3289e39ea6468977b6e2940a195`). No refs were refreshed.
The branch's manifest still names API `435dc393f52d2607a744fdc7c2320d706e47a0f3`
and web `83b62b0b47916736274bb48f87448c8dbc3fb09b`; neither is asserted live.

| Obligation | Local evidence | What remains unproved |
|---|---|---|
| Runtime-file permissions repair | `455f706c000d87d3d87d7939cfeab470fe78cd2b`, subject identifies #44 and #43 | Current runtime permissions and issue #43 disposition |
| Corrected U9 manifest | `6464f74c68fa942c96e5aa411b250254102c11e4`, subject identifies #45 | Successful deployment of that pair |
| Later EU release | `dcbce302350cc0044aa624ffd2dad76915d93399` changes candidate API to `13ad2f35d55196274c5fec313bfb254a7cc80226`, web to `83eaf9574315b71a165b80370dd964572d2ccac9` | Current running immutable images and their revisions |
| EU move and identity | `44b6dfd3:deploy/RUNBOOK.md` records 2026-09-21 move to EU CPX32; hosted identity acceptance remains separate | Current health and dated hosted identity UAT |
| Import, recovery | Same runbook warns checkout heads/saved pairs do not prove recovery | FOLIO acceptance and verified previous-pair recovery or independent restore |

The approval-click premise is stale; the whole item is **not obsolete**. Cached
Git history cannot establish a current GitHub environment restriction.

## Implementation Units

1. Reconcile remote history, read-only, in the orchestrator's network-enabled lane:
   `gh run view 34155698435 --repo alea-institute/ontokit-api --json status,conclusion,jobs,url,headSha`
   and `gh issue view 43 --repo alea-institute/ontokit-api --json state,closedAt,url,body`.
   Inspect later Deploy DEV runs and the current manifest at their immutable
   commits, not the newest branch head by assumption. Preserve links and dates.
2. Obtain restricted DEV `status` through the existing authorized operator path;
   record API, worker and web image IDs/digests and OCI source revisions, service
   health and observation time. Do not print environment or credential values.
3. Locate existing FOLIO re-import and UAT entries in the web companion
   `docs/roundup-2026-08/DEV-RUNBOOK.md` at its accepted revision. Compare import
   counts, failures and user-visible acceptance against the original obligation.
   Run only missing acceptance steps, with a verified backup before data writes.
4. Verify the accepted prior pair or an independent restore on disposable staging.
   Capture actual image/backup references and an eligible rollback receipt. A
   manifest SHA or syntax check cannot substitute for recovery evidence.
5. Update the card to “reconcile U9 deployment, acceptance, and rollback evidence
   against the current EU release.” Close #43/U9 only for proved obligations.
   If GitHub still requires Damien personally, report the exact pending run and
   environment action then; never approve the historical run blindly.

## Verification Contract

Locally inspect the immutable commits above and run
`python3 deploy/validate_release_manifest.py deploy/release-manifest.json`.
This verifies source evidence and manifest syntax only. Completion requires
successful CI/deploy evidence, matched running images, healthy API/worker/web,
FOLIO acceptance, dated UAT, and tested recovery. No new automated test is needed
for this documentation-only unit.

## Rollback and release

No runtime change is prepared here. Revert the documentation commit if needed.
Before any later deploy, discover a verified prior release/restore reference;
none is currently established. Do not backdate a deployment pre-receipt or file a
successful deployment receipt from these notes. Integration target: `dev`'s
operational documentation; not an independent default-branch application fix.

## Definition of Done

Each U9 obligation has a dated evidence link or an explicit remaining gate; the
on-deck card describes the actual residual work without replaying completed work.
