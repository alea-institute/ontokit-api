"""Tests for the explicit operator enqueue boundary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts import rewrap_pr_party_credentials as command


@pytest.mark.asyncio
async def test_dry_run_enqueues_fixed_id_and_waits_for_safe_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = {
        "status": "completed",
        "dry_run": True,
        "credentials_scanned": 1,
        "credentials_verified": 1,
        "credentials_rewrapped": 0,
        "credential_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
    }
    job = AsyncMock()
    job.result.return_value = receipt
    pool = AsyncMock()
    pool.enqueue_job.return_value = job
    monkeypatch.setattr(command, "create_pool", AsyncMock(return_value=pool))

    result = await command.enqueue_rewrap(apply=False, timeout=12.0)

    pool.enqueue_job.assert_awaited_once_with(
        command.TASK_NAME,
        False,
        None,
        _job_id=command.DRY_RUN_JOB_ID,
    )
    job.result.assert_awaited_once_with(timeout=12.0)
    pool.close.assert_awaited_once()
    assert result == receipt
    assert "token" not in repr(result).lower()


@pytest.mark.asyncio
async def test_duplicate_apply_job_fails_without_starting_another_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AsyncMock()
    pool.enqueue_job.return_value = None
    monkeypatch.setattr(command, "create_pool", AsyncMock(return_value=pool))

    with pytest.raises(command.OperatorRewrapError, match="already queued"):
        await command.enqueue_rewrap(apply=True, timeout=12.0)

    pool.enqueue_job.assert_awaited_once_with(
        command.TASK_NAME,
        True,
        command.CONFIRMATION,
        _job_id=command.APPLY_JOB_ID,
    )
    pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmed_apply_returns_consistent_safe_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = {
        "status": "completed",
        "dry_run": False,
        "credentials_scanned": 1,
        "credentials_verified": 1,
        "credentials_rewrapped": 1,
        "credential_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
    }
    job = AsyncMock()
    job.result.return_value = receipt
    pool = AsyncMock()
    pool.enqueue_job.return_value = job
    monkeypatch.setattr(command, "create_pool", AsyncMock(return_value=pool))

    result = await command.enqueue_rewrap(apply=True, timeout=12.0)

    pool.enqueue_job.assert_awaited_once_with(
        command.TASK_NAME,
        True,
        command.CONFIRMATION,
        _job_id=command.APPLY_JOB_ID,
    )
    assert result == receipt


def test_apply_requires_exact_confirmation_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = AsyncMock()
    monkeypatch.setattr(command, "create_pool", connect)

    with pytest.raises(SystemExit) as caught:
        command.main(["--apply"])

    assert caught.value.code == 2
    connect.assert_not_called()


@pytest.mark.parametrize(
    "updates",
    [
        {"credentials_verified": 0},
        {"credentials_rewrapped": 1},
        {"credential_ids": []},
        {
            "credentials_scanned": 2,
            "credentials_verified": 2,
            "credential_ids": [
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            ],
        },
    ],
)
def test_receipt_validator_rejects_inconsistent_proof(updates: dict[str, object]) -> None:
    receipt: dict[str, object] = {
        "status": "completed",
        "dry_run": True,
        "credentials_scanned": 1,
        "credentials_verified": 1,
        "credentials_rewrapped": 0,
        "credential_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
    }
    receipt.update(updates)

    with pytest.raises(command.OperatorRewrapError, match="inconsistent receipt counts"):
        command._validated_receipt(receipt, apply=False)


def test_deployed_worker_images_ship_the_documented_operator_command() -> None:
    root = Path(__file__).resolve().parents[2]
    destination = "/usr/local/bin/rewrap-pr-party-credentials"

    for dockerfile in (root / "Dockerfile", root / "Dockerfile.prod"):
        contents = dockerfile.read_text()
        assert "scripts/rewrap_pr_party_credentials.py" in contents
        assert destination in contents

    runbook = (root / "deploy" / "RUNBOOK.md").read_text()
    assert runbook.count(f"python {destination}") == 2
