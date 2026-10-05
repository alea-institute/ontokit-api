---
title: Fail fast when the local dev image falls behind uv.lock
type: fix
date: 2026-10-05
---

# Goal

The local compose stack bind-mounts live source (`./ontokit`) over the
`ontokit/api:latest` image, so containers run current code against whatever
dependencies were baked in at build time. The local image was built on
2026-03-03; `pgvector` arrived on 2026-03-05 (`1a654b9f`). For about seven
months `ontokit-worker` crash-looped (1,900+ restarts) and `ontokit-api` sat
unhealthy on `ModuleNotFoundError: No module named 'pgvector'`. Nothing told
anyone why.

Two outcomes:

1. **Restore the local stack** (operational, no code change): pull `dev`,
   rebuild the image, recreate `api` and `worker`, and let the entrypoint run
   migrations. The running Postgres already ships the `vector` extension
   (`pg_available_extensions`), so the Postgres image does not change.
2. **Make drift loud** (this branch): when the image's dependency lock no
   longer matches the checkout, stop at startup with a message naming the fix,
   instead of failing later on an arbitrary import.

# Design

- **Record at build time.** The `Dockerfile` already copies `uv.lock` for
  `uv export`. In the same layer, write its SHA-256 to a file in the image
  (`/home/ontokit/app/.uv-lock.sha256`).
- **Compare at start.** `scripts/entrypoint.sh` hashes the live lock when
  `/home/ontokit/app/uv.lock.live` exists. If the hashes differ, it prints
  both hashes and `docker compose build api worker`, then exits non-zero.
  `ONTOKIT_SKIP_LOCK_CHECK=1` bypasses the check deliberately.
- **Dev-only activation.** Only the root `compose.yaml` mounts
  `./uv.lock:/home/ontokit/app/uv.lock.live:ro` on `api` and `worker`.
  Release images and `deploy/compose.dev.yaml` never mount it, so the check is
  inert in deployed environments.
- **Worker gets the live entrypoint.** `worker` currently uses the baked
  entrypoint; mount `./scripts/entrypoint.sh` the same way `api` does so
  entrypoint fixes reach both services without a rebuild.
- **Fail rather than warn.** A warning in a crash-looping container's log is
  what we already had in effect. Exiting before Python imports anything puts
  the cause on the first log line. The opt-out covers lock changes that touch
  only dev dependencies.

# Units

- **U1 Dockerfile:** record the lock hash during the dependency layer.
- **U2 Entrypoint:** the drift check, its message and the opt-out.
- **U3 Compose:** the live-lock mount for `api` and `worker`, plus the worker
  entrypoint mount.
- **U4 Tests:** extend `tests/unit/test_dockerfile_contract.py` (or a sibling)
  to pin the contract, and test the entrypoint behavior with a shell harness:
  match, mismatch, missing live file, opt-out.
- **U5 Docs:** a short README note under the Docker section.

# Verification

- **Unit and lint:** `pytest tests/unit`, `ruff`, `mypy ontokit/`.
- **Real stack:** rebuild, confirm `api` healthy and `worker` up with a stable
  restart count. Then change the live `uv.lock` hash on a scratch copy, confirm
  the drift message and non-zero exit, and restore.

# Rollback

Revert the PR. The check activates only through the dev compose mount.
