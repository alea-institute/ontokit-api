"""Tests for the PR Party reviewer registry and credential service (U2).

Three properties carry the weight here, and each one is a place where a quiet
regression would be invisible in production until it mattered:

- **Provisioning is config, not schema (KTD12).** The registry reconciles from
  ``PR_PARTY_REVIEWERS`` at startup. A login change invalidates the stored
  credential, a removal takes the credential with it, and an unreachable GitHub
  degrades node-id resolution to a warning rather than failing boot.
- **Validation happens before storage (KTD13).** A submitted PAT is proven to
  belong to the registered login *before* anything is written, so a failed
  rotation cannot destroy a working credential.
- **Encryption is MultiFernet with previous-key rotation, domain-separated from
  the LLM provider keys.** A retired ``SECRET_KEY`` must still decrypt, and a
  PR Party ciphertext must not be readable by the LLM key domain.

The DB is a hand-rolled fake rather than an ``AsyncMock`` because reconcile is a
*multi-statement* routine: what matters is which rows it added, deleted, and
mutated, and a mock that returns the same canned result for every ``execute``
cannot express that.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import InvalidToken

from ontokit.core.config import settings
from ontokit.models.pr_party import PRPartyCredential, PRPartyMergeDefault, PRPartyReviewer
from ontokit.services.pr_party_credentials import (
    LOGIN_CHANGED_ERROR,
    CredentialRejected,
    CredentialValidationUnavailable,
    PRPartyCredentialService,
    credential_health,
    decrypt_reviewer_token,
    encrypt_reviewer_token,
    get_generation_token_status,
    reconcile_reviewers,
    reset_generation_token_cache,
    rotate_reviewer_token,
)
from ontokit.services.pr_party_github import (
    AuthenticatedIdentity,
    GitHubAPIError,
    GitHubUserRef,
    TokenExpiredError,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """AsyncSession stand-in that routes a SELECT by its entity type."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows: list[Any] = list(rows or [])
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.commits = 0

    async def execute(self, stmt: Any) -> _FakeResult:
        entity = stmt.column_descriptions[0]["entity"]
        return _FakeResult([r for r in self.rows if isinstance(r, entity)])

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self.rows.append(obj)

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)
        if obj in self.rows:
            self.rows.remove(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        return None


class _FakeClient:
    """Stands in for a PRPartyGitHubClient in either mode."""

    def __init__(
        self,
        *,
        identity: AuthenticatedIdentity | None = None,
        users: dict[str, GitHubUserRef] | None = None,
        identity_error: Exception | None = None,
        user_error: Exception | None = None,
    ) -> None:
        self.identity = identity
        self.users = users or {}
        self.identity_error = identity_error
        self.user_error = user_error
        self.identity_calls = 0
        self.user_calls: list[str] = []

    async def get_authenticated_user(self) -> AuthenticatedIdentity:
        self.identity_calls += 1
        if self.identity_error is not None:
            raise self.identity_error
        assert self.identity is not None
        return self.identity

    async def get_user(self, login: str) -> GitHubUserRef:
        self.user_calls.append(login)
        if self.user_error is not None:
            raise self.user_error
        return self.users.get(login, GitHubUserRef(login=login, node_id=f"NODE_{login}"))


def _factory(client: _FakeClient) -> Any:
    def _make(_token: str, **_kwargs: Any) -> Any:
        return client

    return _make


def _reviewer(
    zitadel_user_id: str = "zit-1",
    github_login: str = "octocat",
    node_id: str | None = None,
) -> PRPartyReviewer:
    row = PRPartyReviewer(
        zitadel_user_id=zitadel_user_id,
        github_login=github_login,
        github_node_id=node_id,
        merge_default=PRPartyMergeDefault.MANUAL,
    )
    row.id = uuid.uuid4()
    return row


def _credential(reviewer: PRPartyReviewer, token: str = "ghp_old") -> PRPartyCredential:
    row = PRPartyCredential(
        reviewer_id=reviewer.id,
        encrypted_token=encrypt_reviewer_token(token),
        last_validated_at=datetime.now(UTC) - timedelta(days=1),
    )
    row.id = uuid.uuid4()
    return row


@pytest.fixture(autouse=True)
def _stable_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic, non-default SECRET_KEY for every test in this module."""
    monkeypatch.setattr(settings, "secret_key", "pr-party-unit-test-secret")
    monkeypatch.setattr(settings, "secret_key_previous", "")
    reset_generation_token_cache()


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


class TestReviewerTokenCrypto:
    def test_round_trip(self) -> None:
        assert decrypt_reviewer_token(encrypt_reviewer_token("ghp_secret")) == "ghp_secret"

    def test_previous_key_decrypts_after_rotation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A credential written under the old SECRET_KEY survives a rotation."""
        ciphertext = encrypt_reviewer_token("ghp_written_before_rotation")

        monkeypatch.setattr(settings, "secret_key", "the-new-secret")
        monkeypatch.setattr(settings, "secret_key_previous", "pr-party-unit-test-secret")

        assert decrypt_reviewer_token(ciphertext) == "ghp_written_before_rotation"

        # ...and rotate_reviewer_token migrates it onto the current key, after
        # which the retired key can be dropped.
        migrated = rotate_reviewer_token(ciphertext)
        monkeypatch.setattr(settings, "secret_key_previous", "")
        assert decrypt_reviewer_token(migrated) == "ghp_written_before_rotation"

    def test_default_secret_hard_fails_in_deployed_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "secret_key", "change-me-in-production")
        monkeypatch.setattr(settings, "app_env", "staging")
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            encrypt_reviewer_token("ghp_secret")

    def test_domain_separated_from_llm_provider_keys(self) -> None:
        """A PR Party ciphertext is not readable in the LLM key domain."""
        from ontokit.services.llm.crypto import decrypt_secret

        with pytest.raises(InvalidToken):
            decrypt_secret(encrypt_reviewer_token("ghp_secret"))


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestReviewerConfigParsing:
    def test_parses_pairs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pr_party_reviewers", " zit-1:octocat , zit-2:hubot ")
        assert settings.pr_party_reviewer_map == {"zit-1": "octocat", "zit-2": "hubot"}

    def test_ignores_malformed_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pr_party_reviewers", "zit-1:octocat,garbage,:x,zit-3:")
        assert settings.pr_party_reviewer_map == {"zit-1": "octocat"}

    def test_empty_is_empty_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pr_party_reviewers", "")
        assert settings.pr_party_reviewer_map == {}


# ---------------------------------------------------------------------------
# Startup reconcile (KTD12)
# ---------------------------------------------------------------------------


class TestReconcileReviewers:
    async def test_inserts_and_resolves_node_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pr_party_reviewers", "zit-1:octocat")
        monkeypatch.setattr(settings, "pr_party_readonly_token", "ghp_readonly")
        db = _FakeSession()
        client = _FakeClient(users={"octocat": GitHubUserRef(login="octocat", node_id="MDQ6VXNl")})

        result = await reconcile_reviewers(db, client_factory=_factory(client))  # type: ignore[arg-type]

        assert result.added == 1
        assert result.resolved == 1
        assert db.commits == 1
        [row] = db.added
        assert row.zitadel_user_id == "zit-1"
        assert row.github_login == "octocat"
        assert row.github_node_id == "MDQ6VXNl"

    async def test_tolerates_github_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Boot continues with an unresolved node id and a WARNING (retry next sweep)."""
        monkeypatch.setattr(settings, "pr_party_reviewers", "zit-1:octocat")
        monkeypatch.setattr(settings, "pr_party_readonly_token", "ghp_readonly")
        db = _FakeSession()
        client = _FakeClient(user_error=GitHubAPIError("boom", status_code=503))

        with caplog.at_level(logging.WARNING):
            result = await reconcile_reviewers(db, client_factory=_factory(client))  # type: ignore[arg-type]

        assert result.added == 1
        assert result.unresolved == 1
        assert db.added[0].github_node_id is None
        assert any("node id" in r.message.lower() for r in caplog.records)

    async def test_login_change_invalidates_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "pr_party_reviewers", "zit-1:new-login")
        monkeypatch.setattr(settings, "pr_party_readonly_token", "ghp_readonly")
        reviewer = _reviewer(github_login="old-login", node_id="OLD_NODE")
        credential = _credential(reviewer)
        credential.last_error = None
        db = _FakeSession([reviewer, credential])
        client = _FakeClient(
            users={"new-login": GitHubUserRef(login="new-login", node_id="NEW_NODE")}
        )

        result = await reconcile_reviewers(db, client_factory=_factory(client))  # type: ignore[arg-type]

        assert result.updated == 1
        assert reviewer.github_login == "new-login"
        assert reviewer.github_node_id == "NEW_NODE"
        assert credential.last_error == LOGIN_CHANGED_ERROR
        assert credential.last_validated_at is None
        # The secret itself is retained — re-validation, not destruction.
        assert credential.encrypted_token

    async def test_removed_reviewer_row_and_credential_go(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "pr_party_reviewers", "zit-keep:octocat")
        monkeypatch.setattr(settings, "pr_party_readonly_token", "")
        keep = _reviewer("zit-keep", "octocat", node_id="KEEP")
        drop = _reviewer("zit-drop", "hubot", node_id="DROP")
        db = _FakeSession([keep, drop])

        result = await reconcile_reviewers(db)

        assert result.removed == 1
        assert db.deleted == [drop]
        assert keep in db.rows

    async def test_empty_config_leaves_registry_untouched(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A lost env var must not silently delete every reviewer's credential."""
        monkeypatch.setattr(settings, "pr_party_reviewers", "")
        existing = _reviewer()
        db = _FakeSession([existing])

        with caplog.at_level(logging.WARNING):
            result = await reconcile_reviewers(db)

        assert result.skipped is True
        assert db.deleted == []
        assert db.commits == 0

    async def test_skips_resolution_without_readonly_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "pr_party_reviewers", "zit-1:octocat")
        monkeypatch.setattr(settings, "pr_party_readonly_token", "")
        db = _FakeSession()

        result = await reconcile_reviewers(db)

        assert result.added == 1
        assert result.resolved == 0
        assert db.added[0].github_node_id is None


# ---------------------------------------------------------------------------
# Credential save / rotate / delete (KTD13)
# ---------------------------------------------------------------------------


class TestSaveCredential:
    async def test_fine_grained_pat_without_scopes_validates(self) -> None:
        """A fine-grained PAT leaves x-oauth-scopes empty; that must not matter."""
        reviewer = _reviewer()
        db = _FakeSession([reviewer])
        expires = datetime.now(UTC) + timedelta(days=60)
        client = _FakeClient(
            identity=AuthenticatedIdentity(
                login="octocat", node_id="MDQ6VXNl", token_expires_at=expires
            )
        )
        service = PRPartyCredentialService(db, actuation_factory=_factory(client))  # type: ignore[arg-type]

        row = await service.save_credential(reviewer, "github_pat_11ABC")

        assert decrypt_reviewer_token(row.encrypted_token) == "github_pat_11ABC"
        assert row.expires_at == expires
        assert row.last_validated_at is not None
        assert row.last_error is None
        # The capability probe is a real read, not a scope inspection.
        assert client.user_calls == ["octocat"]
        # Node id backfills onto the registry row.
        assert reviewer.github_node_id == "MDQ6VXNl"
        assert db.commits == 1

    async def test_login_mismatch_stores_nothing(self) -> None:
        reviewer = _reviewer(github_login="octocat")
        db = _FakeSession([reviewer])
        client = _FakeClient(
            identity=AuthenticatedIdentity(login="someone-else", node_id="X", token_expires_at=None)
        )
        service = PRPartyCredentialService(db, actuation_factory=_factory(client))  # type: ignore[arg-type]

        with pytest.raises(CredentialRejected, match="octocat"):
            await service.save_credential(reviewer, "github_pat_wrong")

        assert db.added == []
        assert db.commits == 0
        assert client.user_calls == []

    async def test_rotation_failure_leaves_old_credential_intact(self) -> None:
        reviewer = _reviewer()
        existing = _credential(reviewer, "ghp_working")
        db = _FakeSession([reviewer, existing])
        client = _FakeClient(identity_error=TokenExpiredError("bad credentials", status_code=401))
        service = PRPartyCredentialService(db, actuation_factory=_factory(client))  # type: ignore[arg-type]

        with pytest.raises(CredentialRejected):
            await service.save_credential(reviewer, "github_pat_dead")

        assert decrypt_reviewer_token(existing.encrypted_token) == "ghp_working"
        assert existing.last_error is None
        assert db.commits == 0

    async def test_rotation_overwrites_only_after_validation(self) -> None:
        reviewer = _reviewer()
        existing = _credential(reviewer, "ghp_old")
        existing.last_error = "expired"
        db = _FakeSession([reviewer, existing])
        client = _FakeClient(
            identity=AuthenticatedIdentity(login="octocat", node_id="N", token_expires_at=None)
        )
        service = PRPartyCredentialService(db, actuation_factory=_factory(client))  # type: ignore[arg-type]

        row = await service.save_credential(reviewer, "ghp_new")

        assert row is existing
        assert decrypt_reviewer_token(existing.encrypted_token) == "ghp_new"
        assert existing.last_error is None
        assert db.added == []

    async def test_github_outage_is_not_a_user_error(self) -> None:
        reviewer = _reviewer()
        db = _FakeSession([reviewer])
        client = _FakeClient(identity_error=GitHubAPIError("upstream down", status_code=503))
        service = PRPartyCredentialService(db, actuation_factory=_factory(client))  # type: ignore[arg-type]

        with pytest.raises(CredentialValidationUnavailable):
            await service.save_credential(reviewer, "github_pat_ok")

        assert db.commits == 0

    async def test_blank_token_rejected(self) -> None:
        reviewer = _reviewer()
        db = _FakeSession([reviewer])
        client = _FakeClient(
            identity=AuthenticatedIdentity(login="octocat", node_id="N", token_expires_at=None)
        )
        service = PRPartyCredentialService(db, actuation_factory=_factory(client))  # type: ignore[arg-type]

        with pytest.raises(CredentialRejected):
            await service.save_credential(reviewer, "   ")

        assert client.identity_calls == 0


class TestDeleteCredential:
    async def test_removes_row(self) -> None:
        reviewer = _reviewer()
        existing = _credential(reviewer)
        db = _FakeSession([reviewer, existing])
        service = PRPartyCredentialService(db)  # type: ignore[arg-type]

        assert await service.delete_credential(reviewer) is True
        assert db.deleted == [existing]
        assert db.commits == 1

    async def test_missing_row_is_not_an_error(self) -> None:
        reviewer = _reviewer()
        db = _FakeSession([reviewer])
        service = PRPartyCredentialService(db)  # type: ignore[arg-type]

        assert await service.delete_credential(reviewer) is False


class TestResolveToken:
    """U6 actuates with this; a missing or unreadable PAT is degradation, not a 500."""

    async def test_returns_plaintext(self) -> None:
        reviewer = _reviewer()
        db = _FakeSession([reviewer, _credential(reviewer, "ghp_live")])
        service = PRPartyCredentialService(db)  # type: ignore[arg-type]

        assert await service.resolve_token(reviewer) == "ghp_live"

    async def test_missing_credential_is_none(self) -> None:
        reviewer = _reviewer()
        db = _FakeSession([reviewer])
        service = PRPartyCredentialService(db)  # type: ignore[arg-type]

        assert await service.resolve_token(reviewer) is None

    async def test_undecryptable_ciphertext_degrades(self) -> None:
        reviewer = _reviewer()
        credential = _credential(reviewer)
        credential.encrypted_token = "not-a-fernet-token"
        db = _FakeSession([reviewer, credential])
        service = PRPartyCredentialService(db)  # type: ignore[arg-type]

        assert await service.resolve_token(reviewer) is None


class TestUpdateSettings:
    async def test_writes_only_supplied_fields(self) -> None:
        reviewer = _reviewer()
        reviewer.ntfy_topic = "existing-topic"
        db = _FakeSession([reviewer])
        service = PRPartyCredentialService(db)  # type: ignore[arg-type]

        await service.update_settings(reviewer, {"merge_default": PRPartyMergeDefault.DASHBOARD})

        assert reviewer.merge_default == PRPartyMergeDefault.DASHBOARD
        assert reviewer.ntfy_topic == "existing-topic"
        assert db.commits == 1

    async def test_explicit_none_clears_topic(self) -> None:
        reviewer = _reviewer()
        reviewer.ntfy_topic = "existing-topic"
        db = _FakeSession([reviewer])
        service = PRPartyCredentialService(db)  # type: ignore[arg-type]

        await service.update_settings(reviewer, {"ntfy_topic": None})

        assert reviewer.ntfy_topic is None

    async def test_unknown_field_is_ignored(self) -> None:
        reviewer = _reviewer()
        db = _FakeSession([reviewer])
        service = PRPartyCredentialService(db)  # type: ignore[arg-type]

        await service.update_settings(reviewer, {"github_login": "attacker"})

        assert reviewer.github_login == "octocat"


class TestGetReviewer:
    async def test_returns_none_for_non_reviewer(self) -> None:
        db = _FakeSession()
        service = PRPartyCredentialService(db)  # type: ignore[arg-type]
        assert await service.get_reviewer("nobody") is None


# ---------------------------------------------------------------------------
# Credential health
# ---------------------------------------------------------------------------


class TestCredentialHealth:
    def test_none_credential_is_none(self) -> None:
        assert credential_health(None) is None

    def test_expired_expiry_surfaces(self) -> None:
        reviewer = _reviewer()
        cred = _credential(reviewer)
        cred.expires_at = datetime.now(UTC) - timedelta(days=1)

        health = credential_health(cred)

        assert health is not None
        assert health.expired is True
        assert health.expires_soon is True

    def test_expiry_within_thirty_days_warns_without_being_expired(self) -> None:
        reviewer = _reviewer()
        cred = _credential(reviewer)
        cred.expires_at = datetime.now(UTC) + timedelta(days=5)

        health = credential_health(cred)

        assert health is not None
        assert health.expired is False
        assert health.expires_soon is True

    def test_no_expiry_is_healthy(self) -> None:
        reviewer = _reviewer()
        cred = _credential(reviewer)
        cred.expires_at = None

        health = credential_health(cred)

        assert health is not None
        assert health.expired is False
        assert health.expires_soon is False


# ---------------------------------------------------------------------------
# Shared generation-token status (KTD13)
# ---------------------------------------------------------------------------


class TestGenerationTokenStatus:
    async def test_none_without_configured_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pr_party_readonly_token", "")
        assert await get_generation_token_status() is None

    async def test_reports_expiry_and_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pr_party_readonly_token", "ghp_readonly")
        expires = datetime.now(UTC) + timedelta(days=10)
        client = _FakeClient(
            identity=AuthenticatedIdentity(login="bot", node_id="B", token_expires_at=expires)
        )

        first = await get_generation_token_status(client_factory=_factory(client))
        second = await get_generation_token_status(client_factory=_factory(client))

        assert first is not None
        assert first.expires_at == expires
        assert first.last_error is None
        assert second == first
        # One live call powers both reads inside the TTL.
        assert client.identity_calls == 1

    async def test_records_error_instead_of_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pr_party_readonly_token", "ghp_readonly")
        client = _FakeClient(identity_error=TokenExpiredError("bad creds", status_code=401))

        status = await get_generation_token_status(client_factory=_factory(client))

        assert status is not None
        assert status.expires_at is None
        assert status.last_error is not None

    async def test_token_change_busts_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pr_party_readonly_token", "ghp_one")
        client = _FakeClient(
            identity=AuthenticatedIdentity(login="bot", node_id="B", token_expires_at=None)
        )
        await get_generation_token_status(client_factory=_factory(client))

        monkeypatch.setattr(settings, "pr_party_readonly_token", "ghp_two")
        await get_generation_token_status(client_factory=_factory(client))

        assert client.identity_calls == 2


# ---------------------------------------------------------------------------
# Error scrubbing (Fix 6)
# ---------------------------------------------------------------------------


#: A GitHub error body quotes the request, so it can carry both the reviewer's
#: prose and — on a misconfiguration — the credential that was just submitted.
_LEAKY_PROSE = (
    "Bad credentials for request Authorization: Bearer "
    "github_pat_11SUPERSECRETVALUE — see the PR description: 'ship the liturgy fix'"
)
_SECRET_FRAGMENT = "github_pat_11SUPERSECRETVALUE"


def _assert_scrubbed(text: str, *, expected: str) -> None:
    """Exactly the class name and status; no GitHub prose, no secret."""
    assert expected in text
    assert _SECRET_FRAGMENT not in text
    assert "Bad credentials" not in text
    assert "ship the liturgy fix" not in text


class TestGitHubErrorProseIsScrubbed:
    """Nothing GitHub said in prose may reach storage or a response (Fix 6).

    ``scrub_error`` is the single rule — class name plus HTTP status — and these
    are the three credential-service paths that used to interpolate the raw
    exception instead.
    """

    async def test_generation_status_last_error_is_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one ``last_error`` this module *stores* from a live GitHub failure."""
        monkeypatch.setattr(settings, "pr_party_readonly_token", "ghp_readonly")
        client = _FakeClient(identity_error=TokenExpiredError(_LEAKY_PROSE, status_code=401))

        status = await get_generation_token_status(client_factory=_factory(client))

        assert status is not None
        assert status.last_error == "TokenExpiredError (HTTP 401)"
        _assert_scrubbed(status.last_error, expected="TokenExpiredError (HTTP 401)")

    async def test_authenticate_failure_carries_no_prose(self) -> None:
        reviewer = _reviewer()
        db = _FakeSession([reviewer])
        client = _FakeClient(identity_error=GitHubAPIError(_LEAKY_PROSE, status_code=502))
        service = PRPartyCredentialService(db, actuation_factory=_factory(client))  # type: ignore[arg-type]

        with pytest.raises(CredentialValidationUnavailable) as excinfo:
            await service.save_credential(reviewer, "github_pat_11SUPERSECRETVALUE")

        _assert_scrubbed(str(excinfo.value), expected="GitHubAPIError (HTTP 502)")
        assert db.commits == 0

    async def test_capability_probe_failure_carries_no_prose(self) -> None:
        reviewer = _reviewer(github_login="octocat")
        db = _FakeSession([reviewer])
        client = _FakeClient(
            identity=AuthenticatedIdentity(login="octocat", node_id="N", token_expires_at=None),
            user_error=GitHubAPIError(_LEAKY_PROSE, status_code=403),
        )
        service = PRPartyCredentialService(db, actuation_factory=_factory(client))  # type: ignore[arg-type]

        with pytest.raises(CredentialValidationUnavailable) as excinfo:
            await service.save_credential(reviewer, "github_pat_11SUPERSECRETVALUE")

        _assert_scrubbed(str(excinfo.value), expected="GitHubAPIError (HTTP 403)")
        # The probe failed, so nothing was stored.
        assert db.added == []
        assert db.commits == 0

    async def test_transport_failure_scrubs_without_a_status(self) -> None:
        """No HTTP status exists for a transport fault; the rule stays total."""
        reviewer = _reviewer()
        db = _FakeSession([reviewer])
        client = _FakeClient(identity_error=RuntimeError(_LEAKY_PROSE))
        service = PRPartyCredentialService(db, actuation_factory=_factory(client))  # type: ignore[arg-type]

        with pytest.raises(CredentialValidationUnavailable) as excinfo:
            await service.save_credential(reviewer, "github_pat_11SUPERSECRETVALUE")

        message = str(excinfo.value)
        _assert_scrubbed(message, expected="RuntimeError")
        assert "HTTP" not in message
