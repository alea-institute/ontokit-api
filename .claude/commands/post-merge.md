Post-merge cleanup after a PR has been merged to dev on GitHub.

Argument: $ARGUMENTS (optional: the branch name that was merged, e.g. "feat/my-feature")

Steps to perform:

1. **Detect the branch to delete** (before switching away): If no argument was provided, check the current branch name. If it is not `dev` or `main`, offer that branch as the candidate to delete. If an argument was provided, use that as the candidate.

2. **Checkout dev and pull**: Switch to the `dev` branch and pull the latest changes from origin.

3. **Delete the stale local branch**: Delete the candidate branch identified in step 1. If no candidate was identified (current branch was already `dev`/`main` and no argument given), list recent local branches (excluding dev and main) and ask which one to delete.

4. **Check if a Docker rebuild is needed**: Compare the pulled changes against `Dockerfile`, `Dockerfile.prod`, `pyproject.toml`, `uv.lock`, `compose.yaml`, and `alembic/`, then run the first matching command:
   - `Dockerfile`, `Dockerfile.prod`, `pyproject.toml`, `uv.lock`, or `compose.yaml` changed → `docker compose up -d --build` (rebuilds images, recreates containers, re-runs migrations)
   - Only `alembic/` changed (new migrations) → `docker compose up -d --force-recreate api worker` (recreates containers so migrations run on startup)
   - None of the above changed → `docker compose restart api worker` (picks up volume-mounted code changes)

5. **Restart the login container if api/worker were recreated**: If step 4 ran the rebuild or force-recreate path (i.e., the api/worker containers were recreated rather than just restarted), also run `docker compose restart login`. Recreating api/worker can sever the `ontokit-zitadel-login` container's persistent gRPC channel to Zitadel without it auto-reconnecting — see issue #130. Skip this if step 4 only restarted (no recreation).

6. **Verify**: Run `docker compose ps` to confirm all services are healthy.

Important:
- The "no infra changes → restart" branch relies on the `./ontokit:/home/ontokit/app/ontokit:ro` volume mount in `compose.yaml` (api + worker services). If that mount is ever removed, restart will silently keep running the old code baked into the image and you'll need `docker compose up -d --build api worker` instead. Before relying on the restart path, confirm the mount still exists with `grep -A 1 "Mount source code" compose.yaml`.
- The entrypoint.sh uses a marker file `/tmp/.migrations_done` to skip repeated migrations. This marker lives inside the container filesystem, so `docker compose restart` preserves it and migrations will NOT re-run. Only container recreation (`--build` or `--force-recreate`) clears the marker.
- Always confirm before deleting a branch if it looks like it might have unmerged changes.
- Run all commands from the repository root (use `git rev-parse --show-toplevel` to find it).
