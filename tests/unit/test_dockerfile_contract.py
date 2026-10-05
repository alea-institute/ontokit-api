"""Contracts that keep deploy source files readable inside the runtime image."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
DOCKERFILE = ROOT / "Dockerfile"
DEPLOY_SCRIPT = ROOT / "deploy" / "ontokit-deploy.sh"
COMPOSE = ROOT / "compose.yaml"

RUNTIME_FILES = {
    "pyproject.toml",
    "README.md",
    "alembic.ini",
    "ontokit/version.py",
    "scripts/entrypoint.sh",
}


def test_runtime_file_copies_have_explicit_readable_metadata() -> None:
    copy_instructions = [
        shlex.split(line)
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("COPY ")
    ]

    for source in RUNTIME_FILES:
        matching_copies = [
            instruction for instruction in copy_instructions if source in instruction
        ]
        assert len(matching_copies) == 1, f"expected exactly one COPY for {source}"

        options = {token for token in matching_copies[0][1:] if token.startswith("--")}
        has_readable_mode = "--chmod=0644" in options
        owned_by_runtime_user = "--chown=ontokit:ontokit" in options
        assert has_readable_mode or owned_by_runtime_user, (
            f"COPY for {source} must set --chmod or runtime-user --chown"
        )


def test_detached_checkouts_run_with_world_readable_umask() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    checkout_lines = [line.strip() for line in source.splitlines() if "checkout --detach" in line]

    assert len(checkout_lines) == 2
    expected_repositories = {"API_REPO", "WEB_REPO"}
    scoped_repositories = {
        match.group("repository")
        for line in checkout_lines
        if (
            match := re.fullmatch(
                r'\( umask 022; git -C "\$(?P<repository>API_REPO|WEB_REPO)" '
                r'checkout --detach "\$(?:api_sha|web_sha)" \) \|\| return',
                line,
            )
        )
    }

    assert scoped_repositories == expected_repositories


def test_dependency_layer_records_readable_lock_hash() -> None:
    source = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    dependency_layers = [
        line for line in source.splitlines() if line.startswith("RUN uv lock --check --offline")
    ]

    assert len(dependency_layers) == 1
    layer = dependency_layers[0]
    assert "sha256sum uv.lock | cut -d ' ' -f 1 > /home/ontokit/app/.uv-lock.sha256" in layer
    assert "chmod 0644 /home/ontokit/app/.uv-lock.sha256" in layer


def test_dev_services_mount_live_lock_and_entrypoint() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    for service in ("api", "worker"):
        volumes = compose["services"][service]["volumes"]
        assert "./uv.lock:/home/ontokit/app/uv.lock.live:ro" in volumes
        assert "./scripts/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro" in volumes
