#!/bin/bash
set -euo pipefail

# Compare baked dependencies with the checkout only when the dev lock is mounted.
LOCK_HASH_FILE="${ONTOKIT_LOCK_HASH_FILE:-/home/ontokit/app/.uv-lock.sha256}"
LIVE_LOCK="${ONTOKIT_LIVE_LOCK:-/home/ontokit/app/uv.lock.live}"
if [ "${ONTOKIT_SKIP_LOCK_CHECK:-0}" = "1" ]; then
    echo "Skipping dependency lock check (ONTOKIT_SKIP_LOCK_CHECK=1)" >&2
elif [ -f "$LIVE_LOCK" ]; then
    LIVE_LOCK_HASH="$(sha256sum "$LIVE_LOCK" | cut -d ' ' -f 1)"
    if [ ! -f "$LOCK_HASH_FILE" ]; then
        echo "ERROR: Image predates dependency-lock recording; rebuild its dependencies." >&2
        echo "Live uv.lock SHA-256:  $LIVE_LOCK_HASH" >&2
        echo "Rebuild dependencies with: docker compose build api worker" >&2
        exit 1
    fi
    IMAGE_LOCK_HASH="$(cat "$LOCK_HASH_FILE")"
    if [ "$IMAGE_LOCK_HASH" != "$LIVE_LOCK_HASH" ]; then
        echo "ERROR: Image dependencies are stale: uv.lock differs from the checkout." >&2
        echo "Image uv.lock SHA-256: $IMAGE_LOCK_HASH" >&2
        echo "Live uv.lock SHA-256:  $LIVE_LOCK_HASH" >&2
        echo "Rebuild dependencies with: docker compose build api worker" >&2
        exit 1
    fi
fi

# Only run migrations once per container start (uvicorn --reload re-invokes
# the entrypoint on each file change; the marker prevents repeated runs).
# Set RUN_MIGRATIONS=0 to skip (e.g. for replica instances where only
# a single leader should run migrations).
MARKER="/tmp/.migrations_done"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-1}"
if [ "$RUN_MIGRATIONS" = "1" ] && [ ! -f "$MARKER" ]; then
    echo "Running database migrations..."
    python -m alembic upgrade head
    touch "$MARKER"
elif [ "$RUN_MIGRATIONS" != "1" ]; then
    echo "Skipping migrations (RUN_MIGRATIONS=$RUN_MIGRATIONS)"
fi

echo "Starting application..."
exec "$@"
