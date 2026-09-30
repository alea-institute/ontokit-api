---
title: Publish and deploy matched DEV image digests
type: feat
date: 2026-09-30
---

# Goal Capsule

Complete “OntoKit: GHCR publish triggers + image-based dev compose” (Later),
sweep `weekly-sweep-20260928t120410-095f52`. Publish the API and web separately,
then deploy an explicitly selected matched pair of immutable registry digests.

## Product Contract

PRs validate without publishing. Pushes to `dev` publish the API after lint,
tests and Docker preflight. Existing `ontokit-*` tag publishing remains; DEV
must not replace `latest` or trigger PyPI/GitHub releases. Worker and API must
consume exactly the same digest. No implicit deployment of the newest head.

## Planning Contract

The local publisher change extends `.github/workflows/release.yml`, keeps its
existing `ghcr.io/<owner>/ontokit` repository, adds full `sha-<40-character SHA>`
tags and explicit OCI revision/source labels, and records the actual pushed
registry digest in the job summary. The SHA tag is a discovery aid, **not an
immutable reference**: deployment must use `repository@sha256:<digest>`.

This branch begins at `773c51aa`, before the runtime-permission repair #44 and
later EU deployment work in cached `origin/dev` (`44b6dfd3`). Integrate onto the
current dev line before publication. Do not release an image built from this
old worktree as the current EU release. No real digest is available offline.

The existing manifest is schema 1, SHA-only. `deploy/ontokit-deploy.sh` calls
`docker compose build api` and `build --no-cache web`; `deploy/compose.dev.yaml`
also uses source checkouts for bootstrap mounts. Replacing only its `image:`
fields would leave a broken or misleading deployment contract. The active
manifest, compose and host command remain unchanged until the coordinated
consumer work below is ready. This item is **not complete** after API publication.

## Implementation Units

1. **API publisher (this local change).** Extend the existing tag publisher with
   the guarded DEV path, source labels, SHA discovery tag and digest receipt.
   Inspect `tests/unit/test_release_workflow.py` for guard, release-isolation,
   prerequisite, least-privilege and provenance contracts.
2. **Web publisher (separate ontokit-web assignment).** Produce its DEV image
   with the same revision/digest receipt and push-only controls. Retain the
   currently required web build inputs: API/WS URLs, AUTH_MODE, Zitadel issuer
   and client ID. Verify which are baked versus runtime inputs. Never copy
   secrets into image layers. Confirm the actual registry repository rather
   than inventing a digest or assuming a matching API registry naming scheme.
3. **Coordinated API consumer change (orchestrator integration on current dev).**
   Update `deploy/validate_release_manifest.py` and its CLI consumers together:
   `.github/workflows/deploy-dev.yml`, `.github/workflows/promote-prod.yml`,
   `deploy/ontokit-deploy.sh` and `deploy/compose.dev.yaml`. Use schema 2 with
   existing repository/SHA fields plus `api_image` and `web_image`, each a
   validated allowed repository plus lowercase 64-hex sha256 digest. Reject
   missing/mixed pairs, mutable tags, unknown fields and unexpected registries.
   Preserve the SHA association and verify OCI revision labels against it after
   pulling, before migration or replacement. Schema 1 remains usable only on
   the existing source-build deployment contract during rollout; never silently
   reinterpret it as an image pair.
4. **Image-based composition and recovery.** Remove API/web `build` entries;
   require `ONTOKIT_API_IMAGE` for API and worker and `ONTOKIT_WEB_IMAGE` for web.
   Switch the host path from `build` to explicit `pull`, then `up --no-build`.
   Keep deployment-owned init scripts available even without application source
   builds. Since Dockerfile.prod auto-migrates, run migrations once and configure
   the worker with `RUN_MIGRATIONS=0`; verify API startup and worker startup.
   Store the previously *accepted* image/SHA pair atomically and preserve it on
   failed retries and same-pair deploys. Update forced-command argument validation
   and rollback format together; do not insert digest strings into the current
   SHA-only SSH command. Carry current EU auth/firewall/checkout recovery fixes
   forward, rather than replacing them with this branch's older implementation.
5. **Activation (network lane).** After review and integration, push the API change
   to `dev` through the normal PR path; inspect Distribution's successful
   `publish_docker` digest receipt. Obtain the web receipt from its separately
   assigned publisher. Review a matched schema-2 manifest populated with those
   actual digests, stage it with the coordinated consumer implementation, and
   exercise protected DEV deployment only after the verification below.

## Verification Contract

Local publisher check:
`python3 -m pytest --noconftest -o addopts='' tests/unit/test_release_workflow.py -q`.
These YAML contract tests do not require app fixtures, credentials or network;
`--noconftest` intentionally avoids importing application configuration.

Consumer acceptance must cover schema 2 success and malformed/mutable/mismatched
pair rejection; API/worker digest equality; PR and main-push non-publication;
dev push and release-tag publication; no DEV latest/PyPI/release mutation; a
Compose configuration containing no source build for API/web; and no build
invocation on deployment or rollback. Run the existing deployment/promotion
suites in a disposable test lane with synthetic configuration only. Those
suites source temporary env fixtures, so this worker's blanket env-read ban
prevents executing them here.

Network staging must pull both declared digests on a clean host without building
application source, verify OCI provenance, migrate a disposable restored DB,
check API/worker/web health, identity callbacks and FOLIO acceptance, then restore
the accepted previous pair. Repeat failed-deploy and failed-rollback recovery
cases. Keep image digests, run links and restore evidence in the receipt; local
workflow syntax does not prove a successful registry build or deployment.

## Rollback and release

Revert the publisher commit to stop DEV publication; no deployment is coupled to
that change. Before switching consumers, capture the verified existing release,
data backup and routing state. Image rollback does not reverse database changes:
validate migration compatibility or restore the rehearsed backup before routing
traffic. Do not delete prior images or data while acceptance remains open.

Integration target: current **dev** first. The publisher itself is portable to
`main` independently if desired, but the operational consumer change belongs on
dev and must later accompany normal promotion. Do not cherry-pick this old
branch wholesale or activate its stale release manifest.

## Definition of Done

Both publishers produce proven digests; matched manifest validation and all
consumers agree; disposable staging deploys and restores the pair without source
builds; runtime and hosted acceptance receipts exist. API publisher preparation
alone is not completion of the on-deck item.
