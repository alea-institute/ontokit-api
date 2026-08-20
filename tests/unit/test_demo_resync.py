"""Unit proofs for the credential and atomic-swap demo resync boundary."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deploy.resync_demo_projects import (
    read_demo_token,
    refreshed_repository,
    validate_manifest,
)

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mirrors": [
            {
                "source_repository": "alea-institute/FOLIO",
                "destination_repository": "alea-institute/ontokit-demo-folio",
            },
            {
                "source_repository": "CatholicOS/ontology-semantic-canon",
                "destination_repository": "alea-institute/ontokit-demo-semantic-canon",
            },
        ],
    }


def test_manifest_contract_accepts_only_the_two_exact_pairs(tmp_path: Path) -> None:
    path = tmp_path / "mirrors.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    validate_manifest(path)

    changed = _manifest()
    mirrors = changed["mirrors"]
    assert isinstance(mirrors, list)
    first = mirrors[0]
    assert isinstance(first, dict)
    first["destination_repository"] = "operator/private-repo"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        validate_manifest(path)
    assert exc.value.code == 64


def test_token_file_must_be_private(tmp_path: Path) -> None:
    path = tmp_path / "demo-token"
    path.write_text("scoped-token\n", encoding="utf-8")
    path.chmod(0o600)
    assert read_demo_token(path) == "scoped-token"

    path.chmod(0o640)
    with pytest.raises(SystemExit) as exc:
        read_demo_token(path)
    assert exc.value.code == 64


def _fake_clone(_url: str, target: Path, _token: str) -> None:
    target.mkdir()
    (target / "generation").write_text("new", encoding="utf-8")


def test_failed_reindex_restores_previous_repository(tmp_path: Path) -> None:
    target = tmp_path / f"{PROJECT_ID}.git"
    target.mkdir()
    (target / "generation").write_text("old", encoding="utf-8")
    service = SimpleNamespace(base_path=tmp_path)

    with (
        patch(
            "deploy.resync_demo_projects.BareOntologyRepository.clone_bare",
            side_effect=_fake_clone,
        ),
        pytest.raises(RuntimeError, match="index failed"),
        refreshed_repository(
            service,  # type: ignore[arg-type]
            PROJECT_ID,
            "alea-institute/ontokit-demo-folio",
            "scoped-token",
        ),
    ):
        assert (target / "generation").read_text(encoding="utf-8") == "new"
        raise RuntimeError("index failed")

    assert (target / "generation").read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(f".{PROJECT_ID}.*"))


def test_success_discards_previous_repository(tmp_path: Path) -> None:
    target = tmp_path / f"{PROJECT_ID}.git"
    target.mkdir()
    (target / "generation").write_text("old", encoding="utf-8")
    service = SimpleNamespace(base_path=tmp_path)

    with (
        patch(
            "deploy.resync_demo_projects.BareOntologyRepository.clone_bare",
            side_effect=_fake_clone,
        ),
        refreshed_repository(
            service,  # type: ignore[arg-type]
            PROJECT_ID,
            "alea-institute/ontokit-demo-folio",
            "scoped-token",
        ),
    ):
        assert (target / "generation").read_text(encoding="utf-8") == "new"

    assert (target / "generation").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(f".{PROJECT_ID}.*"))
