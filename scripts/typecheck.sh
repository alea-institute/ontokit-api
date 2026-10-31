#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

export UV_PROJECT_ENVIRONMENT="$repo_root/.venv-mypy"
# An activated venv or ambient uv settings must not redirect or skip the sync.
unset VIRTUAL_ENV UV_PROJECT UV_WORKING_DIR UV_NO_SYNC UV_FROZEN UV_NO_DEV UV_NO_GROUP

exec uv run --python 3.11 --locked --group dev mypy ontokit/
