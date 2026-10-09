"""Contracts that keep deploy source files readable inside the runtime image."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERFILE_PROD = ROOT / "Dockerfile.prod"
DOCKERFILES = (DOCKERFILE, DOCKERFILE_PROD)
DEPLOY_SCRIPT = ROOT / "deploy" / "ontokit-deploy.sh"
COMPOSE = ROOT / "compose.yaml"
COMPOSE_DEV = ROOT / "deploy" / "compose.dev.yaml"

RUNTIME_FILES = {
    "pyproject.toml",
    "README.md",
    "alembic.ini",
    "ontokit/version.py",
    "scripts/entrypoint.sh",
}


def _copy_instructions(dockerfile: Path) -> list[list[str]]:
    return [
        shlex.split(line)
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.startswith("COPY ")
    ]


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.name)
def test_runtime_file_copies_have_explicit_readable_mode(dockerfile: Path) -> None:
    """Plain runtime files get an explicit 0644 mode (alea#43), in both images.

    Runtime-user ownership alone is not enough: a root-only host file mode
    would still leave the file unreadable to other users of the image.
    """
    copy_instructions = _copy_instructions(dockerfile)

    for source in RUNTIME_FILES:
        matching_copies = [
            instruction for instruction in copy_instructions if source in instruction
        ]
        assert len(matching_copies) == 1, (
            f"{dockerfile.name}: expected exactly one COPY for {source}"
        )

        options = {token for token in matching_copies[0][1:] if token.startswith("--")}
        assert "--chmod=0644" in options, (
            f"{dockerfile.name}: COPY for {source} must set --chmod=0644"
        )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.name)
def test_package_directory_is_traversable_before_nested_copy(dockerfile: Path) -> None:
    """A nested ``COPY --chmod=0644`` must not create a non-traversable parent."""
    lines = dockerfile.read_text(encoding="utf-8").splitlines()
    mkdir_index = next(
        (index for index, line in enumerate(lines) if line == "RUN mkdir -m 0755 ontokit"),
        None,
    )
    nested_index = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("COPY ") and "./ontokit/version.py" in line
    )

    assert mkdir_index is not None, f"{dockerfile.name}: missing RUN mkdir -m 0755 ontokit"
    assert mkdir_index < nested_index


def test_dev_deploy_worker_defers_migrations_to_healthy_api() -> None:
    """DEV api is the sole migration leader; the worker waits for it (R9)."""
    services = yaml.safe_load(COMPOSE_DEV.read_text(encoding="utf-8"))["services"]

    worker = services["worker"]
    assert worker["environment"]["RUN_MIGRATIONS"] == "0"
    assert worker["depends_on"]["api"] == {"condition": "service_healthy"}

    api = services["api"]
    assert str(api["environment"].get("RUN_MIGRATIONS", "1")) == "1"
    assert api["healthcheck"]["test"][0] in {"CMD", "CMD-SHELL"}


def _duration_seconds(value: str) -> float:
    """Parse a compose duration such as ``180s``, ``3m`` or ``1m30s``."""
    units = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", str(value))
    assert parts and "".join(n + u for n, u in parts) == str(value), value
    return sum(float(n) * units[u] for n, u in parts)


def test_dev_deploy_api_healthcheck_allows_migration_time() -> None:
    """The migration leader gets a start period long enough to run migrations.

    The api runs migrations before serving and the worker waits on its health,
    so a short start_period marks it unhealthy mid-migration. The deploy's
    ``--wait-timeout 240`` must still exceed this grace period.
    """
    api = yaml.safe_load(COMPOSE_DEV.read_text(encoding="utf-8"))["services"]["api"]
    start_period = _duration_seconds(api["healthcheck"]["start_period"])
    assert start_period >= 120

    wait = re.search(r"--wait-timeout (\d+)", DEPLOY_SCRIPT.read_text(encoding="utf-8"))
    assert wait is not None
    assert int(wait.group(1)) > start_period


def test_local_compose_api_healthcheck_matches_deploy_migration_time() -> None:
    """Local API startup gets the same 180s migration grace period as deploy."""
    local_api = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["api"]
    deploy_api = yaml.safe_load(COMPOSE_DEV.read_text(encoding="utf-8"))["services"]["api"]

    assert _duration_seconds(local_api["healthcheck"]["start_period"]) == 180
    assert local_api["healthcheck"]["start_period"] == deploy_api["healthcheck"]["start_period"]


def test_local_compose_worker_defers_migrations_to_healthy_api() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]

    assert services["worker"]["environment"]["RUN_MIGRATIONS"] == "0"
    assert services["worker"]["depends_on"]["api"] == {"condition": "service_healthy"}


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
