"""Keep deploy/MIGRATION.md and the backup scripts in step with compose.dev.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
COMPOSE_DEV = ROOT / "deploy" / "compose.dev.yaml"
MIGRATION = ROOT / "deploy" / "MIGRATION.md"
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
SCRIPTS = (ROOT / "deploy" / "backup-dev.sh", ROOT / "deploy" / "restore-dev.sh")


def _compose() -> dict[str, object]:
    return yaml.safe_load(COMPOSE_DEV.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _documented() -> set[str]:
    """Every backticked token in MIGRATION.md."""
    return set(re.findall(r"`([^`\s]+)`", MIGRATION.read_text(encoding="utf-8")))


def _script_volumes(script: Path) -> list[str]:
    match = re.search(r"^readonly VOLUMES=\(([^)]*)\)$", script.read_text(), re.MULTILINE)
    assert match, f"{script.name} must declare its VOLUMES array"
    return match.group(1).split()


def test_every_service_and_volume_is_inventoried() -> None:
    compose = _compose()
    documented = _documented()
    services = set(compose["services"])  # type: ignore[call-overload]
    volumes = set(compose["volumes"])  # type: ignore[call-overload]
    assert services - documented == set()
    assert volumes - documented == set()


def test_every_published_port_is_inventoried() -> None:
    documented = _documented()
    ports = {
        str(mapping).split(":")[0]
        for service in _compose()["services"].values()  # type: ignore[attr-defined]
        for mapping in service.get("ports", [])
    }
    assert ports
    assert ports - documented == set()


def test_every_variable_name_is_inventoried() -> None:
    documented = _documented()
    compose_vars = set(re.findall(r"\$\{([A-Z0-9_]+)", COMPOSE_DEV.read_text(encoding="utf-8")))
    env_vars = set(re.findall(r"^([A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), re.MULTILINE))
    assert compose_vars and env_vars
    assert (compose_vars | env_vars) - documented == set()


def test_backup_and_restore_cover_every_stateful_volume() -> None:
    volumes = set(_compose()["volumes"])  # type: ignore[call-overload]
    for script in SCRIPTS:
        # postgres_data travels as pg_dumpall, never as a raw cluster copy.
        assert set(_script_volumes(script)) == volumes - {"postgres_data"}, script.name
