---
title: Prepare the conditional OntoKit DEV move to ALEA AWS
type: chore
date: 2026-09-30
---

# Goal Capsule

Keep EU serving now and prepare a reversible migration once ALEA access and
resource approval exist. Preserve the exact card “OntoKit DEV: move from Hetzner
EU to ALEA AWS once Mike grants access,” Waiting; sweep
`weekly-sweep-20260928t120410-095f52`.

## Product Contract

Damien's 2026-09-27 answer, reproduced in the supplied review, is “Keep EU for
now, still move to AWS later.” The preference is settled. A completed preference
answer does not retire the migration, grant a budget, or prove AWS access.
No new hosting question or message to Mike is prepared for sending.

Update, 2026-09-30 (Damien): Mike agrees the DEV server should move to an
ALEA-controlled AWS account. This supersedes the earlier idea of a scoped IAM key
in another account; the target is an ALEA-owned account. It still does not by
itself grant access, approve a budget, or schedule the cutover.

## Planning Contract

Read-only cached evidence: `44b6dfd3:deploy/RUNBOOK.md` records the EU CPX32 move
on 2026-09-21. This plan was drafted on an older `773c51aa` baseline and delivered
on top of `44b6dfd3`; runtime host truth still comes from the running host, not
from repository fallbacks. The supplied card says
Helsinki CPX32 is serving; runtime was not queried. No credential directories,
credential values, env files or other repositories' credentials were inspected.

| Prerequisite | Evidence available in this lane | Required next evidence |
|---|---|---|
| Destination/timing preference | Supplied dated answer | None; keep this choice |
| ALEA account/region and operator role | Not verified | Approved target account and region; read-only identity/permission confirmation through existing authorized access |
| Mike's access grant | Card says outstanding | Verify current grant status; do not assume it is still missing or mint credentials |
| Capacity and spend | No approval provided | Approved sizing/budget and authority before provisioning |
| Current matched EU release | Later source manifest assertion only | Running API/worker/web image digests, source revisions and health |
| Backup/restore and identity | No verified recovery/UAT receipt supplied | Successful disposable restore, callback acceptance, rollback reference |

## Implementation Units

1. **Readiness (network-enabled orchestrator).** Confirm the access grant with
   existing access records first. With the already authorized ALEA profile, use
   read-only `aws sts get-caller-identity --profile <approved-profile>` and compare
   the account/role to the approved destination. Verify the selected region,
   available target capacity, network boundaries and required service access.
   A successful STS call alone is not provisioning permission. Record only
   non-secret readiness evidence; never dump credential/config files. If access
   is absent, keep Waiting and identify the exact outstanding Mike-owned grant.
   If new spend or a credential/security change lacks prior authorization, name
   that specific action for Damien; no such action is established by this lane.
2. **Inventory and recovery.** Observe the accepted EU pair and collect a
   consistent DB backup plus git repository, media and identity storage backups.
   From `deploy/compose.dev.yaml`, inventory `postgres_data` (OntoKit and Zitadel
   databases), `git_repos`, `minio_data`, `zitadel_data`, and Redis persistence/job
   state. Decide queue drain/replay from observed pending work; do not blindly
   duplicate workers. Transfer credentials only through the authorized operator
   secret mechanism, never through reports or code workers. Record backup
   identifiers/checksums and restoration results, not contents.
3. **Parallel target and rehearsal (after access/budget gates).** Provision the
   approved AWS target without changing EU routing. Match database extensions,
   image revisions and deployment auth/network settings. Restore backups into
   isolated storage; keep background jobs and outbound integrations disabled
   until validated to avoid duplicate work. Exercise migrations, image startup,
   API/worker/web health, FOLIO import integrity, object and git history parity,
   hosted login/logout and callbacks using an approved staging hostname. Rehearse
   returning to the accepted EU pair; record timings and all failure findings.
4. **Cutover plan, then execution.** The orchestrator records actual target,
   accepted digests, backup IDs, routing/TTL, write-freeze procedure and recovery
   procedure before scheduling. Drain jobs and freeze writes on EU, capture and
   restore the final consistent delta, validate on AWS, then switch routing.
   Keep one writable site and one active queue processor set. If verification
   fails before AWS accepts writes, route back to unchanged EU. If AWS has
   accepted writes, freeze AWS and restore/reconcile the accepted delta to EU
   before re-enabling writes there; DNS reversal alone risks lost updates.
5. **Acceptance window.** Keep EU and its verified backups intact through the
   recorded acceptance window. Check runtime images, health, identity, imported
   data, git/media consistency and background jobs. Do not decommission, delete
   or purge EU resources under this preparation assignment. Any later deletion
   needs the verified-backup and authorization requirements satisfied separately.

## Verification Contract

Local preparation verifies the cached EU statement and the service/volume
inventory against repository source. No behavior changes, so no new test suite
is needed. Network acceptance requires the correct ALEA account/region and
permissions, approved budget/capacity, successful full restore, matched immutable
images, identity callbacks, import/data checks, single-writer cutover and a
rehearsed recovery with a resolvable `deploy-rollback-ref`.

## Rollback and release

Before cutover retain accepted EU images, storage backups and routing state.
Discover actual rollback references during rehearsal; no candidate source SHA
is a substitute. Reverting this plan is a documentation-only rollback.
Integration target: current `dev` operational docs, not an independent `main`
application change. Keep the card Waiting until access prerequisites are proved.

## Definition of Done

Readiness evidence permits a parallel target; migration/restore rehearsal and
cutover/rollback instructions are verified before any routing change. The move
itself remains blocked on external prerequisites and is not claimed here.
