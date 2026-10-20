---
title: OntoKit API readiness for FOLIO, Catholic OS and the 2026-11-02 rehearsal
type: chore
date: 2026-10-20
topic: sweep-2026-10-20-api-readiness
---

# OntoKit API readiness - sweep plan

## Goal Capsule

- **Objective:** close the API's open correctness and security plan, make CI image
  publishing deployable, and finish the AWS migration readiness steps that need no
  account access.
- **Authority:** capacity sweep `sweep-2026-10-20`. Merge reviewed, tested work to `dev`.
  No deploys, no spend, no credential or package-visibility changes.
- **Open questions:** written to `docs/handoffs/2026-10-20-sweep-cloud-ontokit-api.md`.

## Re-verified state (`origin/dev` at `c682a2f`, 2026-10-20)

- **Correctness plan (2026-10-09-0647):** merged through #64 and #67. R1-R12 each have
  code and a test on `dev`; no `NotImplementedError` stubs remain. A cloud run of the
  full suite re-checks it (receipt in the PR and handoff).
- **OSV-Scanner:** red on every `dev` push. `uv.lock` pins mako 1.4.1 and
  virtualenv 21.7.4 (5 advisories, 3 High).
- **Image publishing:** `publish_docker` pushes `ghcr.io/alea-institute/ontokit` with a
  `sha-<40>` tag, OCI revision label and digest receipt on each `dev` push (last green
  2026-10-09). Gaps:
  - `Dockerfile.prod` (the published image) runs an unpinned `pip install .`, so the
    published image ignores `uv.lock`, its tested set and its security pins.
  - CI never smoke-tests the published image; only the DEV `Dockerfile` image.
  - Both GHCR packages refuse anonymous pulls; a pull-based host needs a decision.
  - ontokit-web publishes images only on `ontokit-web-*` tags (web lane).
  - No consumer exists: `release-manifest.json` is schema 1 (SHA-only), and the host
    builds from source with `uvicorn --reload`.
- **Deploy contract tests:** `deploy/tests/*.sh` pass locally but no CI job runs them.
  Nothing tests `deploy/validate_release_manifest.py`.

## Units

### U1. Patch the OSV advisories (PR: `fix/osv-mako-virtualenv-20261020`)

Upgrade mako and virtualenv in `uv.lock`. Verify with osv-scanner over the lock and the
full suite.

### U2. Dormant image-based DEV deploy contract (PR: `feat/image-deploy-contract-20261020`)

- `Dockerfile.prod` installs from `uv.lock` exactly as `Dockerfile` does, records the lock
  hash, and CI smoke-tests the published image before publication.
- `validate_release_manifest.py` accepts schema 2: schema 1 fields plus `api_image` and
  `web_image`, each `ghcr.io/alea-institute/<ontokit|ontokit-web>@sha256:<64 hex>`.
  It rejects tags, other registries, unknown keys and mixed pairs. Schema 1 stays valid.
- `deploy/compose.dev.images.yaml` overlays `compose.dev.yaml`: API, worker and web run
  the manifest digests; the API runs without `--reload`.
- `ontokit-deploy.sh` gains `deploy-images <api-sha> <web-sha> <api-digest> <web-digest>`.
  It pulls, refuses images whose OCI revision label differs from the SHA, then
  `up --no-build`. The rollback record carries the mode, so rollback restores an image
  pair or a source pair. `deploy` and `rollback` behave as before.
- `deploy-dev.yml` sends `deploy-images` only for a schema 2 manifest. `promote-prod.yml`
  refuses schema 2 until a PROD image consumer exists.
- CI runs `deploy/tests/*.sh`.
- `deploy/release-manifest.json` stays schema 1 and unchanged, so merging deploys nothing.

### U3. AWS migration readiness without access (PR: `docs/aws-readiness-20261020`)

- Inventory every DEV service, volume, port, secret name (names only) and external
  dependency from `deploy/compose.dev.yaml`, enforced by a test so it cannot drift.
- A host-side `deploy/backup-dev.sh`: consistent `pg_dumpall`, git repos, MinIO and
  Zitadel volumes as tarballs plus a SHA-256 manifest. It is read-only on the running
  stack and writes only to a target directory. Shell tests cover it with stubs.
- A restore and rehearsal runbook section: restore order, verification queries, the
  single-writer cutover, and the return path to EU.
- No AWS calls, no provisioning.

### U4. Handoff

`docs/handoffs/2026-10-20-sweep-cloud-ontokit-api.md`: receipts, open questions with
recommendations, and box-only steps (install the new forced-command script and overlay
on the host, GHCR pull access, the first schema 2 manifest).

## Verification

- `uv run pytest tests/` (full suite), `ruff check`, `ruff format --check`, mypy 3.11 in CI.
- `bash deploy/tests/*.sh`.
- `docker compose -f deploy/compose.dev.yaml -f deploy/compose.dev.images.yaml config -q`.
- PR CI green, then a code-review pass, then merge.
