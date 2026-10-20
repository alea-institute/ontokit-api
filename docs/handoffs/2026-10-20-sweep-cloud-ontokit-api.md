# Sweep handoff: ontokit-api (cloud lane, `sweep-2026-10-20`)

Mandate: make the OntoKit API ready for FOLIO and Catholic OS work and the
2026-11-02 rehearsal. Plan: `docs/plans/2026-10-20-sweep-api-readiness-plan.md`.

## Done-when status

| Criterion | Status | Evidence |
|---|---|---|
| API correctness and security plan merged | Met | 2026-10-09-0647 plan merged via #64 and #67. R1-R12 each have code and tests on `dev`. Full suite on `dev` + #69: 3,899 passed, 6 skipped, 0 failed |
| Image publishing works in CI | Met for the API | `publish_docker` pushes `ghcr.io/alea-institute/ontokit` with SHA tag, revision label and digest receipt on each `dev` push. #70 makes the published image install `uv.lock` and smoke-tests it, and adds a dormant digest consumer |
| AWS readiness steps that need no access | Met | #71: inventory, backup and restore scripts with a Docker round-trip test, and the rehearsal, cutover and return runbook |

## Pull requests

| PR | Content | State |
|---|---|---|
| #69 | mako 1.4.3 and virtualenv 21.14.11: clears 5 OSV advisories (3 High) that turned every `dev` push red | merged 2026-10-20 (`49ff0be`), CI green |
| #70 | Dormant image-based DEV deploy: lockfile-pinned `Dockerfile.prod`, schema 2 manifest, `deploy-images` forced-command verb, CI job for `deploy/tests` | merged 2026-10-20 (`38fc130`), CI green including the prod-image smoke test |
| #71 | AWS migration readiness that needs no account access, plus this handoff | merged after CI, which runs the Docker round-trip test |

## Receipts

- Full suite, cloud container (PostgreSQL 16 with pgvector 0.8 built from source; Docker Hub
  returned 429 for `pgvector/pgvector`): 3,899 passed, 6 skipped, 0 failed, 91% coverage.
- `osv-scanner` 2.2.3 over `uv.lock`: 5 vulnerabilities before #69, none after.
- `deploy/tests/ontokit-deploy-test.sh`: 18/18 ok. Of its 8 new tests, 5 fail on the old
  script, and the legacy-retry regression test fails without its fix. Firewall 7/7 and PROD
  promotion 6/6 also pass.
- `deploy/tests/backup-restore-test.sh`: 3/3 ok on Docker 29 / Compose 5.6. Both databases,
  table ownership, git, MinIO and Zitadel files and a queued Redis job survive a backup with
  `--stop-writers` and a restore into a fresh project.
- `docker compose -f deploy/compose.dev.yaml -f deploy/compose.dev.images.yaml config -q`
  passes.
- A code-review pass on #70 raised 10 findings: 6 fixed (with tests where behavior changed),
  1 resolved by correcting the RUNBOOK wording, and 3 declined as refactors (below).

## Open questions (for Damien)

1. **GHCR pull access for image deploys.** Both packages
   (`ghcr.io/alea-institute/ontokit`, `ghcr.io/alea-institute/ontokit-web`) refuse
   anonymous pulls, so a host cannot run `deploy-images` yet.
   *Recommendation:* make both packages public. Both source repositories are public, and
   the images hold no secrets: the web image bakes only the public Zitadel issuer and
   client ID. Check that last point on the first web DEV image before flipping
   visibility. The alternative is a read-only PAT in `docker login ghcr.io` on the host,
   which is one more credential to rotate.
   *Setting:* browser-with-logins (GitHub, org packages, each package's settings,
   "Change visibility"). Quick decision.
2. **When to switch DEV to image deploys.**
   *Recommendation:* after the 2026-11-02 rehearsal, not before. The source-build path is
   proven on EU, and the switch also needs the web lane's DEV publisher and question 1.
   *Setting:* quick decision.
3. **AWS gates** (unchanged from the 2026-09-30 plan): Mike's grant on the ALEA-owned
   account, the region, and the budget and size approval. `deploy/MIGRATION.md`, "Gates",
   lists the evidence each needs. No message is drafted, per that plan.
   *Setting:* on-site or message-to-send, Damien's call.

## Steps that need the home box or the DEV host

1. **After #70 merges, refresh the host copies** (RUNBOOK, "Image-based deploy", step 3):
   `/usr/local/sbin/ontokit-deploy` from `deploy/ontokit-deploy.sh`, and
   `/opt/ontokit/compose.yaml` from `deploy/compose.dev.yaml`, which still carries #67's
   migration-leader change. Add `/opt/ontokit/compose.images.yaml` only when activating.
   Then run `ontokit-deploy status` over the deploy key. The refreshed forced command
   keeps `deploy` and `rollback` unchanged, and it starts writing `.deploy-current`.
2. **First EU backup** (live, no downtime) with
   `deploy/backup-dev.sh /root/ontokit-backup-<date>`. Record the `SHA256SUMS` line for
   `postgres.sql.gz` and the output size, which feeds AWS sizing. The output contains
   credentials; keep it on the host or move it only through the secret mechanism.
3. **Measure EU usage for sizing:** `docker stats --no-stream` and `docker system df -v`
   on the EU host.

## For the ontokit-web lane

- `deploy-images` expects `ghcr.io/alea-institute/ontokit-web@sha256:<digest>`, labelled
  `org.opencontainers.image.revision=<web sha>`. Today ontokit-web publishes only on
  `ontokit-web-*` tags, with `AUTH_MODE=optional` and none of the DEV build arguments
  (`NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_WS_URL`, Zitadel issuer and client ID).
- The API side is ready once the host steps above are done. A schema 2 manifest then
  needs both digest receipts.

## Declined review findings (recorded, not built)

- Building the prod image once and pushing the smoke-tested image (instead of a cached
  rebuild in `publish_docker`). Worth doing when the publish job is next reworked.
- Folding `deploy_pair` and `deploy_images` into shared helpers, and merging the two
  Dockerfiles into one multi-stage file. These are refactors with no defect behind them;
  the contract tests already police both copies.

## Not done

- No deploy, no host change, no AWS call, no package visibility change.
- `deploy/release-manifest.json` is unchanged (schema 1, EU pair).
