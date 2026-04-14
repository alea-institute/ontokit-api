Post-merge cleanup after a PR has been merged to dev on GitHub.

Argument: $ARGUMENTS (optional: the branch name that was merged, e.g. "feat/my-feature")

Steps to perform:

1. **Detect the branch to delete** (before switching away): If no argument was provided, check the current branch name. If it is not `dev` or `main`, offer that branch as the candidate to delete. If an argument was provided, use that as the candidate.

2. **Checkout dev and pull**: Switch to the `dev` branch and pull the latest changes from origin.

3. **Delete the stale local branch**: Delete the candidate branch identified in step 1. If no candidate was identified (current branch was already `dev`/`main` and no argument given), list recent local branches (excluding dev and main) and ask which one to delete.

4. **Check if a Docker rebuild is needed**: Compare the pulled changes to see if any of these files changed: `Dockerfile`, `Dockerfile.prod`, `pyproject.toml`, `uv.lock`, `compose.yaml`. If any of them changed, run `docker compose up -d --build` to rebuild. If none changed, just run `docker compose restart api worker` to pick up the volume-mounted code changes and re-trigger the entrypoint (which runs alembic migrations on startup).

5. **Verify**: Run `docker compose ps` to confirm all services are healthy.

Important:
- The entrypoint.sh uses a marker file `/tmp/.migrations_done` to skip repeated migrations. A container restart clears this marker, so migrations will run automatically on restart.
- Always confirm before deleting a branch if it looks like it might have unmerged changes.
- Run all commands from the repository root (use `git rev-parse --show-toplevel` to find it).
