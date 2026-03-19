#!/bin/bash
set -e

# Only run migrations once per container start (uvicorn --reload re-invokes
# the entrypoint on each file change; the marker prevents repeated runs).
MARKER="/tmp/.migrations_done"
if [ ! -f "$MARKER" ]; then
    echo "Running database migrations..."
    python -m alembic upgrade head
    touch "$MARKER"
fi

echo "Starting application..."
exec "$@"
