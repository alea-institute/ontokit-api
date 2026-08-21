"""Proof for the operator-only PR Party credential rewrap path."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ontokit.core.config import settings
from ontokit.models.pr_party import PRPartyCredential
from ontokit.services.pr_party_credentials import (
    CredentialRewrapError,
    CredentialRewrapErrorCode,
    decrypt_reviewer_token,
    encrypt_reviewer_token,
    rewrap_reviewer_credentials,
)


class _Result:
    def __init__(self, rows: list[PRPartyCredential]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[PRPartyCredential]:
        return list(self._rows)


class _Session:
    def __init__(self, rows: list[PRPartyCredential]) -> None:
        self.rows = rows
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FailingSession(_Session):
    async def commit(self) -> None:
        self.commits += 1
        raise RuntimeError(f"driver parameters included {self.rows[0].encrypted_token}")

    async def rollback(self) -> None:
        self.rollbacks += 1
        raise RuntimeError("rollback connection failure")


def _credential(ciphertext: str) -> PRPartyCredential:
    row = PRPartyCredential(
        id=uuid.uuid4(),
        reviewer_id=uuid.uuid4(),
        encrypted_token=ciphertext,
    )
    return row


def _encrypt_under(monkeypatch: pytest.MonkeyPatch, secret: str, plaintext: str) -> str:
    monkeypatch.setattr(settings, "secret_key", secret)
    monkeypatch.setattr(settings, "secret_key_previous", "")
    return encrypt_reviewer_token(plaintext)


@pytest.mark.asyncio
async def test_apply_rewraps_every_locked_row_and_returns_only_safe_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "github_pat_secret-never-report"
    old_ciphertext = _encrypt_under(monkeypatch, "old-secret", token)
    row = _credential(old_ciphertext)
    session = _Session([row])

    monkeypatch.setattr(settings, "secret_key", "current-secret")
    monkeypatch.setattr(settings, "secret_key_previous", "old-secret")

    receipt = await rewrap_reviewer_credentials(session, dry_run=False)  # type: ignore[arg-type]

    assert session.statements[0]._for_update_arg is not None
    assert session.commits == 1
    assert session.rollbacks == 0
    assert row.encrypted_token != old_ciphertext

    monkeypatch.setattr(settings, "secret_key_previous", "")
    assert decrypt_reviewer_token(row.encrypted_token) == token

    payload = receipt.as_dict()
    assert payload == {
        "status": "completed",
        "dry_run": False,
        "credentials_scanned": 1,
        "credentials_verified": 1,
        "credentials_rewrapped": 1,
        "credential_ids": [str(row.id)],
    }
    assert token not in repr(payload)
    assert old_ciphertext not in repr(payload)
    assert row.encrypted_token not in repr(payload)


@pytest.mark.asyncio
async def test_dry_run_proves_rotation_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_ciphertext = _encrypt_under(monkeypatch, "old-secret", "github_pat_dry_run")
    row = _credential(old_ciphertext)
    session = _Session([row])
    monkeypatch.setattr(settings, "secret_key", "current-secret")
    monkeypatch.setattr(settings, "secret_key_previous", "old-secret")

    receipt = await rewrap_reviewer_credentials(session, dry_run=True)  # type: ignore[arg-type]

    assert receipt.dry_run is True
    assert receipt.credentials_verified == 1
    assert receipt.credentials_rewrapped == 0
    assert session.statements[0]._for_update_arg is None
    assert row.encrypted_token == old_ciphertext
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_one_corrupt_row_rolls_back_without_mutating_any_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_ciphertext = _encrypt_under(monkeypatch, "old-secret", "github_pat_first")
    first = _credential(first_ciphertext)
    corrupt = _credential("not-a-valid-fernet-token")
    session = _Session([first, corrupt])
    monkeypatch.setattr(settings, "secret_key", "current-secret")
    monkeypatch.setattr(settings, "secret_key_previous", "old-secret")

    with pytest.raises(CredentialRewrapError) as caught:
        await rewrap_reviewer_credentials(session, dry_run=False)  # type: ignore[arg-type]

    assert caught.value.code is CredentialRewrapErrorCode.credential_not_decryptable
    assert caught.value.credential_id == corrupt.id
    assert "not-a-valid-fernet-token" not in str(caught.value)
    assert first.encrypted_token == first_ciphertext
    assert corrupt.encrypted_token == "not-a-valid-fernet-token"
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_refuses_to_run_without_a_distinct_previous_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "secret_key", "current-secret")
    monkeypatch.setattr(settings, "secret_key_previous", "")
    session = _Session([])

    with pytest.raises(CredentialRewrapError) as caught:
        await rewrap_reviewer_credentials(session, dry_run=True)  # type: ignore[arg-type]

    assert caught.value.code is CredentialRewrapErrorCode.previous_key_not_configured
    assert session.statements == []
    assert session.commits == 0
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_driver_and_rollback_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_ciphertext = _encrypt_under(monkeypatch, "old-secret", "github_pat_driver-error")
    session = _FailingSession([_credential(old_ciphertext)])
    monkeypatch.setattr(settings, "secret_key", "current-secret")
    monkeypatch.setattr(settings, "secret_key_previous", "old-secret")

    with pytest.raises(CredentialRewrapError) as caught:
        await rewrap_reviewer_credentials(session, dry_run=False)  # type: ignore[arg-type]

    assert caught.value.code is CredentialRewrapErrorCode.transaction_failed
    assert old_ciphertext not in str(caught.value)
    assert "driver parameters" not in str(caught.value)
    assert session.commits == 1
    assert session.rollbacks == 1
