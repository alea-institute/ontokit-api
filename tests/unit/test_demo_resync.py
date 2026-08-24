"""Unit proofs for the credential and atomic-swap demo resync boundary."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from rdflib import Graph

from deploy.resync_demo_projects import (
    _full_reindex_verified,
    read_demo_token,
    refreshed_repository,
    validate_manifest,
)
from ontokit.models.ontology_index import IndexingStatus
from ontokit.services.ontology_index import OntologyIndexService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
COMMIT_HASH = "a" * 40


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


def _partially_clone_then_fail(_url: str, target: Path, _token: str) -> None:
    target.mkdir()
    (target / "partial").write_text("incomplete", encoding="utf-8")
    raise RuntimeError("clone interrupted")


def test_failed_clone_removes_partial_staging_and_preserves_repository(tmp_path: Path) -> None:
    target = tmp_path / f"{PROJECT_ID}.git"
    target.mkdir()
    (target / "generation").write_text("old", encoding="utf-8")
    service = SimpleNamespace(base_path=tmp_path)

    with (
        patch(
            "deploy.resync_demo_projects.BareOntologyRepository.clone_bare",
            side_effect=_partially_clone_then_fail,
        ),
        pytest.raises(RuntimeError, match="clone interrupted"),
        refreshed_repository(
            service,  # type: ignore[arg-type]
            PROJECT_ID,
            "alea-institute/ontokit-demo-folio",
            "scoped-token",
        ),
    ):
        pytest.fail("a failed clone must not enter the repository swap")

    assert (target / "generation").read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(f".{PROJECT_ID}.*"))


async def _assert_index_rejection_restores_repository(
    tmp_path: Path,
    index_status: object,
    error_match: str,
    *,
    entity_count: int,
) -> None:
    target = tmp_path / f"{PROJECT_ID}.git"
    target.mkdir()
    (target / "generation").write_text("old", encoding="utf-8")
    git_service = SimpleNamespace(base_path=tmp_path)
    index_service = AsyncMock(spec=OntologyIndexService)
    index_service.full_reindex.return_value = entity_count
    index_service.get_index_status.return_value = index_status

    with (
        patch(
            "deploy.resync_demo_projects.BareOntologyRepository.clone_bare",
            side_effect=_fake_clone,
        ),
        pytest.raises(RuntimeError, match=error_match),
        refreshed_repository(
            git_service,  # type: ignore[arg-type]
            PROJECT_ID,
            "alea-institute/ontokit-demo-folio",
            "scoped-token",
        ),
    ):
        await _full_reindex_verified(
            index_service,
            PROJECT_ID,
            "main",
            Graph(),
            COMMIT_HASH,
        )

    assert (target / "generation").read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(f".{PROJECT_ID}.*"))
    index_service.full_reindex.assert_awaited_once()
    index_service.get_index_status.assert_awaited_once_with(PROJECT_ID, "main")


@pytest.mark.asyncio
async def test_busy_index_restores_previous_repository(tmp_path: Path) -> None:
    await _assert_index_rejection_restores_repository(
        tmp_path,
        SimpleNamespace(status=IndexingStatus.INDEXING.value, commit_hash="b" * 40),
        "is not ready",
        entity_count=0,
    )


@pytest.mark.asyncio
async def test_missing_index_status_restores_previous_repository(tmp_path: Path) -> None:
    await _assert_index_rejection_restores_repository(
        tmp_path,
        None,
        "status row is missing",
        entity_count=0,
    )


@pytest.mark.asyncio
async def test_mismatched_index_commit_restores_previous_repository(tmp_path: Path) -> None:
    await _assert_index_rejection_restores_repository(
        tmp_path,
        SimpleNamespace(status=IndexingStatus.READY.value, commit_hash="b" * 40),
        "commit does not match",
        entity_count=12,
    )


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
