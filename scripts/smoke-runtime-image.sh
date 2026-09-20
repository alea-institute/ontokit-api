#!/usr/bin/env bash
# Usage: bash scripts/smoke-runtime-image.sh IMAGE
# Build IMAGE from Dockerfile first. No services, mounts, or credentials required.
set -euo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
    echo "Usage: $0 IMAGE" >&2
    exit 2
fi

# Retain the image's USER and WORKDIR; overriding either can hide packaging bugs.
# Bypass only the migration entrypoint, since this probe has no database.
docker run --rm --network none --entrypoint python -i "$1" - <<'PY'
import importlib
import os
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

assert os.geteuid() != 0, "Image must configure a non-root runtime user"
root = Path.cwd().resolve()
for name in ("ontokit", "ontokit.core.config", "ontokit.main", "ontokit.worker"):
    module = importlib.import_module(name)
    source = Path(module.__file__).resolve()
    assert source.is_relative_to(root / "ontokit"), (
        f"{name} must load from the image application tree, got {source}"
    )
    source.read_bytes()
    print(f"Imported {name}")

# Read the actual migration files and load their revision graph without running
# env.py or opening a database connection.
config_path = root / "alembic.ini"
config_path.read_text()
scripts = ScriptDirectory.from_config(Config(str(config_path)))
Path(scripts.env_py_location).read_text()
revisions = list(scripts.walk_revisions())
assert revisions, "Image must contain migration revisions"
assert scripts.get_heads(), "Image must contain migration heads"
for revision in revisions:
    Path(revision.path).read_text()
print(f"Runtime smoke passed as uid={os.geteuid()}: {len(revisions)} migrations readable")
PY
