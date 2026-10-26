# U9 acceptance reconciliation — oa-2

Reconciled: 2026-10-26. **U9 acceptance: BLOCKED.** The documentation is ready
for controller use; no current DEV deployment, runtime, FOLIO, hosted-identity,
or recovery obligation has been proved by this lane. The accepted release pair
and acceptance observation window remain **unknown**.

Authority: [the U9 plan](../plans/2026-09-30-fix-u9-evidence-reconciliation-plan.md),
requirements R1–R8 and decisions KTD1–KTD4. Campaign R-IDs are a separate
namespace. The supplied standing rules apply; the chief campaign excerpts still
need reconciliation under G1. No personal approval requirement has been established.

## Obligation ledger

`PASS` requires eligible dated proof; `FAIL` records an observed contradiction;
`NOT RUN` means unperformed without a demonstrated prerequisite blocker;
`BLOCKED` names a missing prerequisite. Historical claims remain historical.

| Obligation | Outcome | Evidence or missing proof | Owner and next action |
|---|---|---|---|
| R1 — original run, later deployments, release-bound CI | BLOCKED | [Local history](../plans/evidence/oa-2/2026-10-26-local-verification.md) identifies candidate pairs, not a successful deploy job. Original run attempt, job result, immutable manifest revision, later runs, and CI receipts are absent. | Controller: supply G2 using collection step 1 below. |
| R2 — API, worker, and web runtime identities | BLOCKED | No current image IDs, OCI revisions, registry digest observations, or accepted pair. Checkout SHAs alone are insufficient. | Authorized operator: supply G3, then compare all three services against the G2 pair. |
| R2 — service health and stable acceptance window | BLOCKED | No before/after states or restart counts for the nine services; no installed-script/Compose attestation. | Authorized operator: collect G3 before and after acceptance; investigate drift or unhealthy/missing services. |
| R2 — issue #43 runtime-file permissions | BLOCKED | The repair is in source and focused tests pass, but non-root readability/traversal on running API and worker is unobserved. Issue scope/state is not retrieved. | Controller and operator: combine G2 issue scope with G3 runtime access evidence. |
| R3 — FOLIO import and re-import | BLOCKED | Accepted-web companion criteria, dataset identity, expected/actual counts and deltas, failures, and visible results are absent. | Web lane and operator: supply G4; reuse eligible receipts or perform only missing acceptance after G3/G5. |
| R4 — hosted identity | BLOCKED | No accepted-web login/role expectations or dated sign-in, callback, authenticated-use, and sign-out/session evidence. API health does not establish login. | Web lane and operator: supply G4 separately from FOLIO, bound to G3; satisfy G5 before writes. |
| R5 — recovery of data and functioning services | BLOCKED | No eligible accepted-prior-pair exercise or current-data isolated restore receipt. Historical synthetic tests are not DEV recovery proof. | Controller and operator: reuse eligible recovery evidence or establish G5 and collect restored-state/service/user proof. |
| R6 — final authority and acceptance disposition | BLOCKED | G1 sources are unavailable and R1–R5 remain open. | Controller: resolve G1 and every mandatory row before recording U9 accepted. |

R7–R8 local execution boundaries were observed: this build changed only
documentation and sanitized evidence. No deployment, data write, migration,
credential discovery/change, service provisioning, external message, issue
change, push, or PR action occurred. This local boundary check is not a DEV PASS.

## Historical reconciliation

The [dated local receipt](../plans/evidence/oa-2/2026-10-26-local-verification.md)
records commands, source revisions, verification results, and evidence limits.

| Source | Supported statement | What remains unproved |
|---|---|---|
| Commit `455f706c000d87d3d87d7939cfeab470fe78cd2b` (#44) | Its message reports run `34129972766` crash-looping API/worker on unreadable `pyproject.toml`; its diff adds readable COPY modes and checkout umask repair. | This is a historical commit account, not a retrieved run receipt or live permission check. |
| Commit `6464f74c68fa942c96e5aa411b250254102c11e4` (#45) | The manifest selects the #44 API fix with the unchanged web SHA; its message says this would queue environment approval. | It proves neither approval nor executed deployment. |
| Run `34155698435` | The assigned plan names this run for reconciliation. | Attempt, date, head SHA, deploy-job conclusion, manifest revision/pair, and relationship to #45 have not been retrieved. Do not conflate it with `34129972766`. |
| Commit `dcbce302350cc0044aa624ffd2dad76915d93399` | A locally available September 21 candidate manifest names a different API/web pair. | The commit title's claim about a running EU pair is not a current host observation. |
| [October 20 handoff](../handoffs/2026-10-20-sweep-cloud-ontokit-api.md) | Records earlier local/CI results, including synthetic restore coverage, and explicitly says no deployment or host change occurred in that sweep. | It does not establish current DEV CI, runtime, FOLIO/login, or recoverability. |

Candidate pairs below are **not selected deployment targets or accepted releases**.
The manifest commit, API source commit, and web source commit are different identities.

| Candidate source | API SHA | Web SHA |
|---|---|---|
| Tracked schema 1 manifest at build start `e348f616edbd9e55b0ae6b8da5f1b3179b55c804` | `455f706c000d87d3d87d7939cfeab470fe78cd2b` | `4cbe4d4c17437660b70764acb364d24b20ef20ba` |
| Available candidate manifest at `dcbce302350cc0044aa624ffd2dad76915d93399` | `13ad2f35d55196274c5fec313bfb254a7cc80226` | `83eaf9574315b71a165b80370dd964572d2ccac9` |

The old “waiting on the approval click” premise cannot establish today's gate.
Neither can it be replaced with a success claim. A green workflow may have a
skipped deploy job. Select the pair only after reconciling immutable job/manifest
receipts with current runtime; do not replay deployment just to obtain evidence.

## Controller collection and open gates

The [collection checklist](../plans/evidence/oa-2/2026-10-26-controller-collection.md)
defines the allowlisted receipt fields and the acceptance order. All five gates
remain open as of this reconciliation.

| Gate | Missing input | Owner / next authorized action |
|---|---|---|
| G1 | Chief campaign Goal Capsule and cited R11/R15–R20; missing October 5 handoff | Controller: provide those excerpts, reconcile the common mandate, and supply the handoff or an explicit unavailable-source disposition. |
| G2 | Fresh upstream `dev`, run/job and CI receipts, issue #43 scope/state | Controller: collect read-only records; retain immutable links and distinguish skipped jobs. |
| G3 | Current DEV identities, health, permissions, installed-file attestations | Authorized operator: use existing access to return only sanitized observations. No credential handoff to this lane. |
| G4 | Companion runbook at the accepted web SHA, FOLIO criteria, hosted-identity expectations and receipts | Web lane: provide exact criteria and eligible evidence; do not guess counts or test roles. |
| G5 | Verified preservation covering affected stores, safe terminal state, eligible test session, and isolated recovery destination if a rehearsal is needed | Controller/operator: establish these on already authorized infrastructure before dependent writes or restore. Reuse valid proof where available. |

These are controller/operator dependencies, not demonstrated Damien-only decisions.
Do not route GHCR visibility, image-deploy activation, AWS provisioning, or mypy
drift into oa-2. If an actual personally restricted approval is found, record its
exact run/environment action and the campaign-required setting, minutes, and
unblocked obligation; do not infer one from the September runbook.

## Draft controller card — not sent

**Title:** Reconcile U9 deployment, acceptance, and rollback evidence against the current EU release

**Status:** Reconciliation prepared; U9 acceptance open.

Source/history and focused local contracts are verified. No current DEV release
pair has been accepted by this lane. Resolve G1–G5, collect only missing
release-bound evidence, and update the ledger. Close the U9 card only when all
mandatory deployment/CI, runtime/permissions/health, FOLIO, hosted-identity, and
recovery rows pass within a valid observation window. Preserve successful DEV;
a documentation merge does not close acceptance.

**Issue #43 disposition draft:** The source fix and manifest correction are
present (#44/#45). Preserve the issue's existing state until its actual scope
and current state are retrieved. If its scope is solely the historical
permissions defect, assess it using the relevant successful deployment and
runtime-access receipt. If that scope is already satisfied and closed, leave
it closed; track the broader U9 obligations on this card. Do not close or reopen
the issue solely from local tests or use issue closure as U9 acceptance.

## Verification and delivery boundary

The manifest validator passed and 38 existing manifest/Dockerfile contract tests
passed in isolation; see the local receipt for the exact invocation and warning.
No tests were added or weakened because this change has no executable behavior.
The full application suite, live acceptance, and Docker restore rehearsal were
not run. No frontend changed, so a changed-page screenshot is not applicable.
Controller publication and the campaign merge gate remain external to this lane.
