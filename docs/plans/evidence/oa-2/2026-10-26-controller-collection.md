# oa-2 controller collection checklist

Prepared: 2026-10-26. **Instructions only; no live observation is recorded here.**
Follow the [U9 plan](../../2026-09-30-fix-u9-evidence-reconciliation-plan.md)
and update the [acceptance ledger](../../../releases/oa-2-u9-acceptance.md).
Use existing authorized access outside this clone. Return sanitized results,
not credentials or backups. This checklist authorizes no infrastructure or
security-setting change.

## Receipt envelope

Each receipt must name: obligation/R-ID, UTC observation start/end, environment,
collector role, exact procedure, expected result, actual result, outcome,
immutable source/artifact link, accepted manifest revision and API/web pair,
and the runtime observations bracketing it. If the accepted pair is unresolved,
record `unknown` and keep the affected current-release conclusion open.

Use PASS/FAIL/NOT RUN/BLOCKED as defined in the ledger. Keep historical evidence
dated. Record deployment/data changes during the window; reassess affected
receipts rather than combining observations from different releases.

## 1. Resolve authority, release history, and criteria — G1/G2/G4

- Supply the chief campaign Goal Capsule and cited Campaign R11/R15–R20 from
  `docs/plans/2026-10-26-0705-feat-codex-drawdown-campaign-plan.md`, reconcile
  `ops/drawdown/mandates/_common.md`, and supply
  `docs/handoffs/2026-10-05-lock-drift-guard-pr63.md` or explicitly record its
  unavailability. These are external source identifiers, absent from this clone.
- Retrieve [run 34155698435](https://github.com/alea-institute/ontokit-api/actions/runs/34155698435)
  and relevant later Deploy DEV runs. Record run ID/URL, attempt, UTC date, event,
  head SHA, workflow revision, **deploy job** result, immutable manifest revision
  and pair, and separate release-bound CI results. A skipped deploy is not PASS.
- Retrieve fresh upstream `dev` and [issue #43](https://github.com/alea-institute/ontokit-api/issues/43).
  Return its scope, state, and relevant resolution links without copying private
  matter text. Explain how the permission-failure account for run `34129972766`
  and #44/#45 relate to the retrieved runs; do not assume that mapping.
- Obtain `ontokit-web`'s `docs/roundup-2026-08/DEV-RUNBOOK.md` at the accepted web
  SHA. Record the exact FOLIO import/re-import expectations, input revision or
  checksum, and hosted-login/authenticated-role expectations. Missing criteria
  block the corresponding acceptance; do not substitute synthetic smoke results.

## 2. Bind the current runtime and permission repair — G3

Use the existing restricted `status` procedure in
[the runbook](../../../../deploy/RUNBOOK.md#deployment-key-and-host-verification).
Record exit status and distinguish transport/authentication failure from a
dispatched command reporting service/revision failure. `status` takes a lock
and writes an audit outcome. Do not replay a deploy or alter keys to collect it.

For each API, worker, and web container, allowlist only container identity,
immutable image ID, OCI revision from that image, registry digests when present,
state/health, restart count, and observation time. Bind container selection to
the intended Compose project; missing or ambiguous containers block PASS.
A source-built image may have no registry digest: explicitly record that absence
and its actual image ID and OCI revision. Compare API and worker to the accepted
API SHA, and web to the accepted web SHA. Investigate an API/worker image-ID
difference even when both labels match.

Record before/after health and restart counts for `postgres`, `redis`, `minio`,
`api`, `worker`, `web`, `mailpit`, `zitadel`, and `login`. Worker health only checks
for `arq` in PID 1's command line; it is not evidence of completed background work.
Capture the service/user outcomes required by the companion acceptance criteria.

Return installed deployment-script and Compose revision/hash attestations against
their intended source revisions. A checked-out source revision does not attest
to separately installed copies. If copies differ, record the difference and its
effect; do not silently refresh them during evidence collection.

For issue #43, record the non-root runtime UID/GID and ownership, modes,
parent-directory traversal and read/execute access results for the affected
files in **both** API and worker. Derive paths from the accepted image's
Dockerfile: `pyproject.toml`, `README.md`, `ontokit/version.py`, `alembic.ini`,
the entrypoint, and the credential-rewrap script. Read no file contents or
credential material; check metadata and access only. Local COPY declarations
are supporting source evidence, not the runtime check.

Do not return full Docker inspection, environment dumps, raw logs, tokens,
credential paths/values, browser state, private account details, or ontology text.

## 3. Establish preservation before any acceptance write — G5

Prefer existing eligible receipts. Before a new import, re-import, or other
acceptance write, require verified preservation of affected stores and a safe
terminal state. Record backup identity, capture time, checksum/readability
verification, source release/schema, covered stores, capture consistency, and
preservation owner; keep archive contents outside the lane. A live cross-store
backup may be inconsistent. Writer-pausing capture requires a documented
service-preservation window. No deletion is permitted without verified backup.

For any new recovery exercise, require an already authorized isolated destination
and eligible test session. Demonstrate that its stores, queues, identity setup,
and service routing cannot affect successful DEV. Do not provision paid resources
or perform host migration for this item.

## 4. Collect only missing FOLIO and hosted-identity acceptance — R3/R4

FOLIO receipt: accepted source/dataset revision or checksum, expected versus
observed import counts, expected versus observed re-import counts/deltas,
failures, and the companion's user-visible browse/search result. Report failed
imports as FAIL; healthy endpoints do not supersede them.

Separate identity receipt: accepted web criteria and test role, sign-in,
callback completion, expected authenticated behavior, sign-out, and session
termination. Supply only public-safe structured results or sanitized screenshots.
Do not include identities, cookies, private ontology text, or bearer credentials.

`deploy/smoke-release.sh` is supplementary: it writes and deletes a synthetic
project. Its `revision_contract=immutable` text does not establish runtime
identity, FOLIO acceptance, or hosted login. Its write/delete path is subject to
the same preservation prerequisites.

## 5. Prove recovery and finish the observation window — R5/R6

Reuse an eligible accepted-prior-pair recovery receipt if available. Otherwise
restore to the G5 isolated destination. Verify expected versus restored
application data, bare git repository state, object storage, identity data,
and relevant queue state, then start the intended pair and verify service
health plus applicable FOLIO/login acceptance. Record the source/destination
release and schema identities, safe terminal state, and preservation of DEV.

The restore script's completion line and matching schema revision are
insufficient: SQL import errors are suppressed. Require restored-data checks
and working services. A previous-pair file or backup checksum alone is also
insufficient. Historical synthetic round trips remain supporting evidence only;
`SKIP docker unavailable` is NOT RUN even if the script returns zero.

Capture the closing runtime observation. If a release/data change invalidates a
receipt, retain its history and repeat only affected acceptance after prerequisites
hold. Update each ledger row, then apply the controller card/issue disposition.
Keep U9 open while any mandatory row lacks PASS.
