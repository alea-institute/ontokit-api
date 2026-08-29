"""Real-Postgres proof for the PR Party credential rewrap transaction."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.models.pr_party import PRPartyCredential, PRPartyReviewer
from ontokit.services.pr_party_credentials import (
    CredentialRewrapError,
    CredentialRewrapErrorCode,
    decrypt_reviewer_token,
    encrypt_reviewer_token,
    rewrap_reviewer_credentials,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rewrap_commits_a_current_key_ciphertext_on_real_postgres(
    real_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = PRPartyReviewer(
        id=uuid.uuid4(),
        zitadel_user_id=f"rewrap-{uuid.uuid4()}",
        github_login="rewrap-proof",
    )
    monkeypatch.setattr(settings, "secret_key", "integration-old-secret")
    monkeypatch.setattr(settings, "secret_key_previous", "")
    credential = PRPartyCredential(
        id=uuid.uuid4(),
        reviewer_id=reviewer.id,
        encrypted_token=encrypt_reviewer_token("github_pat_integration-proof"),
    )
    original_ciphertext = credential.encrypted_token
    real_db_session.add_all([reviewer, credential])
    await real_db_session.commit()

    try:
        monkeypatch.setattr(settings, "secret_key", "integration-current-secret")
        monkeypatch.setattr(settings, "secret_key_previous", "integration-old-secret")
        receipt = await rewrap_reviewer_credentials(real_db_session, dry_run=False)

        stored = await real_db_session.scalar(
            select(PRPartyCredential).where(PRPartyCredential.id == credential.id)
        )
        assert stored is not None
        assert stored.encrypted_token != original_ciphertext
        assert credential.id in receipt.credential_ids

        monkeypatch.setattr(settings, "secret_key_previous", "")
        assert decrypt_reviewer_token(stored.encrypted_token) == "github_pat_integration-proof"
    finally:
        stored_reviewer = await real_db_session.get(PRPartyReviewer, reviewer.id)
        if stored_reviewer is not None:
            await real_db_session.delete(stored_reviewer)
            await real_db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_corrupt_row_rolls_back_entire_batch_on_real_postgres(
    real_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_reviewer = PRPartyReviewer(
        id=uuid.uuid4(),
        zitadel_user_id=f"rewrap-rollback-{uuid.uuid4()}",
        github_login=f"rewrap-rollback-{uuid.uuid4()}",
    )
    corrupt_reviewer = PRPartyReviewer(
        id=uuid.uuid4(),
        zitadel_user_id=f"rewrap-corrupt-{uuid.uuid4()}",
        github_login=f"rewrap-corrupt-{uuid.uuid4()}",
    )
    monkeypatch.setattr(settings, "secret_key", "integration-old-secret")
    monkeypatch.setattr(settings, "secret_key_previous", "")
    valid = PRPartyCredential(
        id=uuid.uuid4(),
        reviewer_id=valid_reviewer.id,
        encrypted_token=encrypt_reviewer_token("github_pat_rollback-proof"),
    )
    original_ciphertext = valid.encrypted_token
    corrupt = PRPartyCredential(
        id=uuid.uuid4(),
        reviewer_id=corrupt_reviewer.id,
        encrypted_token="not-a-valid-fernet-token",
    )
    real_db_session.add_all([valid_reviewer, corrupt_reviewer, valid, corrupt])
    await real_db_session.commit()
    valid_id = valid.id
    reviewer_ids = (valid_reviewer.id, corrupt_reviewer.id)

    try:
        monkeypatch.setattr(settings, "secret_key", "integration-current-secret")
        monkeypatch.setattr(settings, "secret_key_previous", "integration-old-secret")

        with pytest.raises(CredentialRewrapError) as caught:
            await rewrap_reviewer_credentials(real_db_session, dry_run=False)

        assert caught.value.code is CredentialRewrapErrorCode.credential_not_decryptable
        real_db_session.expire_all()
        stored = await real_db_session.get(PRPartyCredential, valid_id)
        assert stored is not None
        assert stored.encrypted_token == original_ciphertext
    finally:
        for reviewer_id in reviewer_ids:
            stored_reviewer = await real_db_session.get(PRPartyReviewer, reviewer_id)
            if stored_reviewer is not None:
                await real_db_session.delete(stored_reviewer)
        await real_db_session.commit()
