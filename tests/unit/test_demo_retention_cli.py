"""Operator retention paths use fakes without database or Redis access."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from deploy import purge_demo_generations as cli
from ontokit.services.demo_project_provisioning import DemoProvisioningRefused
from ontokit.services.demo_retention import PurgeAttempt, PurgeReceipt
from tests.unit.test_demo_retention import NOW, Harness, generation


@pytest.fixture
def runtime(monkeypatch):
    """Reuse the service's in-memory database and deletion fakes."""
    h = Harness([generation(days=30), generation(days=20), generation("active")])
    engine = MagicMock()
    engine.dispose = AsyncMock()
    connection = object()
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    create_engine = Mock(return_value=engine)
    monkeypatch.setattr(cli, "create_async_engine", create_engine)
    session = MagicMock()
    session.return_value.__aenter__ = AsyncMock(return_value=h.db)
    monkeypatch.setattr(cli, "AsyncSession", session)

    def service(db, *, lease_factory):
        assert db is h.db
        h.service.lease_factory = lease_factory
        return h.service

    monkeypatch.setattr(cli, "DemoRetentionService", service)
    monkeypatch.setattr(cli.settings, "app_env", "development")
    return h, engine, create_engine, connection


def test_dry_run_prints_plan_without_changes(runtime, capsys):
    h, engine, _, _ = runtime
    before = {step: set(ids) for step, ids in h.contents.items()}
    with pytest.raises(SystemExit) as exc:
        cli.main(["--dry-run"])
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["eligible"][0]["generation_key"] == h.db.generations[0].generation_key
    assert result["eligible"][0]["reason"] == "eligible"
    assert {entry["reason"] for entry in result["retained"]} == {"keep", "active"}
    assert not h.db.commits and not h.calls
    assert h.contents == before
    engine.connect.assert_not_called()
    engine.dispose.assert_awaited_once()


def test_status_reads_latest_persisted_failure_even_after_success(runtime, capsys):
    h, engine, _, _ = runtime
    # Intentionally order the latest failure before the older one in the DB.
    for index, g in enumerate(h.db.generations[:2]):
        failed_at = NOW - timedelta(days=index + 1)
        receipt = PurgeReceipt(
            generation_id=g.id,
            generation_key=g.generation_key,
            project_ids=[],
            integration_ids=[],
            attempts=[
                PurgeAttempt(
                    started_at=failed_at,
                    finished_at=failed_at,
                    outcome="failed",
                    failure_class="OSError" if index == 0 else "RuntimeError",
                ),
                PurgeAttempt(started_at=NOW, finished_at=NOW, outcome="success"),
            ],
        )
        g.purge_receipt = receipt.model_dump_json()
        g.purged_at = NOW - timedelta(hours=index)
        g.last_failure_reason = "refresh failure must not be reported as a purge failure"
    with pytest.raises(SystemExit) as exc:
        cli.main(["--status"])
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "retained_count": 1,
        "purged_count": 2,
        "last_purged_at": NOW.isoformat(),
        "last_failure": {
            "generation_key": h.db.generations[0].generation_key,
            "failed_at": (NOW - timedelta(days=1)).isoformat(),
            "failure_class": "OSError",
        },
    }
    assert not h.db.commits and not h.calls
    engine.connect.assert_not_called()


def test_status_without_receipts(runtime, capsys):
    h, _, _, _ = runtime
    with pytest.raises(SystemExit) as exc:
        cli.main(["--status"])
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "retained_count": 3,
        "purged_count": 0,
        "last_purged_at": None,
        "last_failure": None,
    }
    assert not h.calls and not h.db.commits


@pytest.mark.parametrize("args", [[], ["--env", "production"], ["--env", "DEV"]])
def test_apply_refuses_missing_or_mismatched_environment(runtime, capsys, args):
    h, _, create_engine, _ = runtime
    with pytest.raises(SystemExit) as exc:
        cli.main(["--apply", *args])
    assert exc.value.code == 64
    assert "refused:" in capsys.readouterr().err
    create_engine.assert_not_called()
    assert not h.calls and not h.db.commits


def test_matching_apply_yields_held_lease_and_prints_receipt(runtime, monkeypatch, capsys):
    h, engine, _, connection = runtime

    @asynccontextmanager
    async def held_lease(actual_connection):
        assert actual_connection is connection
        raise DemoProvisioningRefused("held by refresh")
        yield  # pragma: no cover

    monkeypatch.setattr(cli, "demo_generation_attempt_lease", held_lease)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--apply", "--env", "development"])
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["yielded"] == [h.db.generations[0].generation_key]
    assert result["summary"]["purged"] == []
    assert result["receipts"][0]["attempts"][-1]["outcome"] == "yielded"
    assert h.db.generations[0].purge_receipt is not None
    assert not h.calls
    engine.connect.assert_called_once()
    engine.connect.return_value.__aexit__.assert_awaited_once()
    engine.dispose.assert_awaited_once()


def test_apply_prints_failed_receipt_and_exits_one(runtime, monkeypatch, capsys):
    h, _, _, connection = runtime

    @asynccontextmanager
    async def available_lease(actual_connection):
        assert actual_connection is connection
        h.held = True
        try:
            yield
        finally:
            h.held = False

    h.failure = "index"
    monkeypatch.setattr(cli, "demo_generation_attempt_lease", available_lease)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--apply", "--env", "development"])
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["failed"] == [h.db.generations[0].generation_key]
    assert result["receipts"][0]["attempts"][-1]["failure_class"] == "RuntimeError"
