"""Tests for TrustService (U2) — tier resolution, outcome log, promotion.

Every branch of the plan's tier-resolution flow gets a test: a second derivation
of tier anywhere in the codebase is a privilege-escalation bug, so the one
derivation has to be nailed down.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ontokit.core.auth import CurrentUser
from ontokit.models.suggestion_outcome import SuggestionOutcomeType
from ontokit.schemas.trust import TrustOverride, TrustTier
from ontokit.services.trust_service import TrustService, get_trust_service, is_anonymous_user_id

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _member(
    user_id: str = "u1",
    role: str = "suggester",
    *,
    is_trusted: bool = False,
    trust_override: str = "none",
) -> MagicMock:
    member = MagicMock()
    member.user_id = user_id
    member.role = role
    member.is_trusted = is_trusted
    member.trust_override = trust_override
    member.trust_granted_at = None
    member.trust_granted_by = None
    return member


def _project(
    members: list[MagicMock] | None = None,
    *,
    threshold: int = 5,
    auto_accept_enabled: bool = False,
    quiet_days: int = 7,
) -> MagicMock:
    project = MagicMock()
    project.id = PROJECT_ID
    project.name = "Test Project"
    project.members = members if members is not None else []
    project.trust_promotion_threshold = threshold
    project.auto_accept_enabled = auto_accept_enabled
    project.auto_accept_quiet_days = quiet_days
    return project


def _user(user_id: str = "u1", *, superadmin: bool = False) -> CurrentUser:
    user = CurrentUser(id=user_id, email="u@example.com", name="U", username="u")
    if superadmin:
        # is_superadmin is derived from settings; force it for the test.
        object.__setattr__(user, "roles", user.roles)
        user.__dict__["_forced_superadmin"] = True
    return user


def _session(
    user_id: str = "u1", *, is_anonymous: bool = False, is_llm_generated: bool = False
) -> MagicMock:
    session = MagicMock()
    session.id = uuid.uuid4()
    session.project_id = PROJECT_ID
    session.user_id = user_id
    session.user_name = "Account Name"
    session.user_email = "account@example.com"
    session.submitter_name = "Self-Reported Name"
    session.submitter_email = "self-reported@example.com"
    session.is_anonymous = is_anonymous
    session.is_llm_generated = is_llm_generated
    return session


def _db_with_count(count: int) -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    result = MagicMock()
    result.scalar.return_value = count
    db.execute = AsyncMock(return_value=result)
    return db


def _svc(db: Any = None) -> TrustService:
    return TrustService(db or _db_with_count(0))


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


class TestResolveTier:
    def test_unauthenticated_is_anonymous(self) -> None:
        assert _svc().resolve_tier(_project(), None) is TrustTier.ANONYMOUS

    def test_anonymous_pseudo_user_is_anonymous(self) -> None:
        user = _user("anonymous-abc123")
        assert _svc().resolve_tier(_project(), user) is TrustTier.ANONYMOUS

    def test_superadmin_without_membership_is_reviewer(self, monkeypatch: Any) -> None:
        user = _user("admin-user")
        monkeypatch.setattr(
            type(user), "is_superadmin", property(lambda _self: True), raising=False
        )
        assert _svc().resolve_tier(_project(), user) is TrustTier.REVIEWER

    @pytest.mark.parametrize("role", ["owner", "admin", "editor"])
    def test_staff_roles_are_reviewer_even_when_untrusted(self, role: str) -> None:
        """KTD4: the ladder never demotes staff."""
        project = _project([_member("u1", role, is_trusted=False)])
        assert _svc().resolve_tier(project, _user("u1")) is TrustTier.REVIEWER

    def test_signed_in_without_membership_is_untrusted(self) -> None:
        assert _svc().resolve_tier(_project(), _user("stranger")) is TrustTier.UNTRUSTED

    def test_suggester_with_is_trusted_is_trusted(self) -> None:
        project = _project([_member("u1", "suggester", is_trusted=True)])
        assert _svc().resolve_tier(project, _user("u1")) is TrustTier.TRUSTED

    def test_suggester_without_is_trusted_is_untrusted(self) -> None:
        project = _project([_member("u1", "suggester", is_trusted=False)])
        assert _svc().resolve_tier(project, _user("u1")) is TrustTier.UNTRUSTED

    def test_override_granted_beats_flag(self) -> None:
        project = _project([_member("u1", "suggester", is_trusted=False, trust_override="granted")])
        assert _svc().resolve_tier(project, _user("u1")) is TrustTier.TRUSTED

    @pytest.mark.parametrize("override", ["refused", "revoked"])
    def test_override_refused_or_revoked_beats_flag(self, override: str) -> None:
        """KTD1: an admin decision is sticky and outranks the materialized flag."""
        project = _project([_member("u1", "suggester", is_trusted=True, trust_override=override)])
        assert _svc().resolve_tier(project, _user("u1")) is TrustTier.UNTRUSTED

    def test_viewer_role_falls_through_to_the_ladder(self) -> None:
        project = _project([_member("u1", "viewer", is_trusted=True)])
        assert _svc().resolve_tier(project, _user("u1")) is TrustTier.TRUSTED


class TestCapabilityHelpers:
    @pytest.mark.parametrize(
        ("tier", "expected"),
        [
            (TrustTier.ANONYMOUS, False),
            (TrustTier.UNTRUSTED, False),
            (TrustTier.TRUSTED, True),
            (TrustTier.REVIEWER, True),
        ],
    )
    def test_can_mint_entities(self, tier: TrustTier, expected: bool) -> None:
        assert TrustService.can_mint_entities(tier) is expected

    def test_every_tier_may_suggest(self) -> None:
        assert all(TrustService.can_suggest(t) for t in TrustTier)

    def test_is_anonymous_user_id(self) -> None:
        assert is_anonymous_user_id("anonymous-deadbeef") is True
        assert is_anonymous_user_id("u1") is False
        assert is_anonymous_user_id(None) is False


# ---------------------------------------------------------------------------
# Outcome log
# ---------------------------------------------------------------------------


class TestRecordOutcome:
    async def test_authenticated_outcome_counts(self) -> None:
        db = _db_with_count(0)
        row = await _svc(db).record_outcome(
            PROJECT_ID, _session("u1"), SuggestionOutcomeType.ACCEPTED, "reviewer-1"
        )
        assert row.counts_toward_promotion is True
        assert row.is_anonymous is False
        assert row.outcome == "accepted"
        assert row.decided_by == "reviewer-1"
        db.add.assert_called_once_with(row)

    async def test_anonymous_outcome_is_credited_but_not_counted(self) -> None:
        """R7 as a data property, not a query-site convention."""
        db = _db_with_count(0)
        row = await _svc(db).record_outcome(
            PROJECT_ID,
            _session("anonymous-abc", is_anonymous=True),
            SuggestionOutcomeType.ACCEPTED,
            "reviewer-1",
        )
        assert row.counts_toward_promotion is False
        assert row.is_anonymous is True

    async def test_anonymous_detected_from_user_id_alone(self) -> None:
        """Even if the flag is unset, the pseudo-user ID is decisive."""
        db = _db_with_count(0)
        row = await _svc(db).record_outcome(
            PROJECT_ID,
            _session("anonymous-abc", is_anonymous=False),
            SuggestionOutcomeType.ACCEPTED,
            None,
        )
        assert row.counts_toward_promotion is False

    async def test_does_not_commit(self) -> None:
        """The caller commits the outcome with the status change, atomically."""
        db = _db_with_count(0)
        await _svc(db).record_outcome(
            PROJECT_ID, _session("u1"), SuggestionOutcomeType.REJECTED, "r"
        )
        db.commit.assert_not_called()

    async def test_rejected_outcome_snapshots_submitter_standing_and_identity(self) -> None:
        """Covers AE1 and authenticated attribution."""
        db = _db_with_count(0)
        project = _project([_member("u1", "suggester", is_trusted=True)])

        row = await _svc(db).record_outcome(
            PROJECT_ID,
            _session("u1"),
            SuggestionOutcomeType.REJECTED,
            "reviewer-1",
            project=project,
            decided_by_name="Review Person",
        )

        assert row.snapshot_tier == TrustTier.TRUSTED.value
        assert row.snapshot_role == "suggester"
        assert row.submitter_name == "Account Name"
        assert row.submitter_email == "account@example.com"
        assert row.decided_by_name == "Review Person"
        assert row.snapshot_captured_at is not None

    async def test_snapshot_does_not_follow_later_member_changes(self) -> None:
        """Covers AE2: the stored values reflect decision-time standing."""
        member = _member("u1", "suggester", is_trusted=True)
        row = await _svc().record_outcome(
            PROJECT_ID,
            _session("u1"),
            SuggestionOutcomeType.REJECTED,
            "reviewer-1",
            project=_project([member]),
        )

        member.role = "editor"
        member.is_trusted = False

        assert row.snapshot_tier == TrustTier.TRUSTED.value
        assert row.snapshot_role == "suggester"

    async def test_anonymous_snapshot_uses_self_reported_identity(self) -> None:
        """Covers AE4: anonymous standing is not invented."""
        row = await _svc().record_outcome(
            PROJECT_ID,
            _session("anonymous-abc", is_anonymous=True),
            SuggestionOutcomeType.DISMISSED,
            "reviewer-1",
            project=_project(),
            decided_by_name="Review Person",
        )

        assert row.is_anonymous is True
        assert row.snapshot_tier is None
        assert row.snapshot_role is None
        assert row.submitter_name == "Self-Reported Name"
        assert row.submitter_email == "self-reported@example.com"
        assert row.snapshot_captured_at is not None

    async def test_snapshot_resolution_failure_degrades_without_pii(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Covers AE7: audit capture never blocks the terminal outcome."""
        service = _svc()
        session = _session("u1")
        session.user_name = "Private Submitter"
        session.user_email = "private@example.com"
        monkeypatch.setattr(service, "resolve_tier", MagicMock(side_effect=RuntimeError("boom")))

        row = await service.record_outcome(
            PROJECT_ID,
            session,
            SuggestionOutcomeType.REJECTED,
            "reviewer-1",
            project=_project([_member("u1")]),
        )

        assert row.snapshot_tier is None
        assert row.snapshot_role is None
        assert row.snapshot_captured_at is not None
        assert "suggestion outcome snapshot resolution failed" in caplog.text
        assert "Private Submitter" not in caplog.text
        assert "private@example.com" not in caplog.text

    async def test_acceptance_snapshots_pre_promotion_tier(self) -> None:
        """AE6: capture precedes promotion evaluation in the transaction."""
        member = _member("u1", "suggester", is_trusted=False)
        project = _project([member], threshold=1)
        service = _svc(_db_with_count(1))

        row = await service.record_outcome(
            PROJECT_ID,
            _session("u1"),
            SuggestionOutcomeType.ACCEPTED,
            "reviewer-1",
            project=project,
        )
        promoted = await service.evaluate_promotion(project, "u1")

        assert promoted is True
        assert member.is_trusted is True
        assert row.snapshot_tier == TrustTier.UNTRUSTED.value
        assert row.snapshot_role == "suggester"

    async def test_count_accepted_reads_the_log(self) -> None:
        db = _db_with_count(4)
        assert await _svc(db).count_accepted(PROJECT_ID, "u1") == 4

    async def test_count_outcomes_reads_the_log(self) -> None:
        db = _db_with_count(2)
        assert await _svc(db).count_outcomes(PROJECT_ID, "u1") == 2

    async def test_count_handles_null_scalar(self) -> None:
        db = _db_with_count(None)  # type: ignore[arg-type]
        assert await _svc(db).count_accepted(PROJECT_ID, "u1") == 0

    async def test_count_accepted_by_user_groups_the_roster(self) -> None:
        db = AsyncMock()
        result = MagicMock()
        result.all.return_value = [("u1", 4), ("u3", 1)]
        db.execute.return_value = result
        assert await _svc(db).count_accepted_by_user(PROJECT_ID, ["u1", "u2", "u3"]) == {
            "u1": 4,
            "u3": 1,
        }
        db.execute.assert_awaited_once()

    async def test_outcome_counts_share_one_aggregate_query(self) -> None:
        db = AsyncMock()
        result = MagicMock()
        result.one.return_value = (3, 7)
        db.execute.return_value = result
        assert await _svc(db).get_outcome_counts(PROJECT_ID, "u1") == (3, 7)
        db.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


class TestEvaluatePromotion:
    async def test_promotes_on_reaching_threshold(self) -> None:
        """Covers AE3."""
        member = _member("u1")
        project = _project([member], threshold=5)
        db = _db_with_count(5)
        assert await _svc(db).evaluate_promotion(project, "u1") is True
        assert member.is_trusted is True
        assert member.trust_granted_at is not None
        assert member.trust_granted_by == "system:auto-promotion"

    async def test_does_not_promote_below_threshold(self) -> None:
        member = _member("u1")
        project = _project([member], threshold=5)
        db = _db_with_count(4)
        assert await _svc(db).evaluate_promotion(project, "u1") is False
        assert member.is_trusted is False

    async def test_already_trusted_returns_false_so_no_duplicate_notification(self) -> None:
        member = _member("u1", is_trusted=True)
        project = _project([member], threshold=5)
        db = _db_with_count(9)
        assert await _svc(db).evaluate_promotion(project, "u1") is False

    @pytest.mark.parametrize("override", ["refused", "revoked", "granted"])
    async def test_any_admin_override_blocks_auto_promotion(self, override: str) -> None:
        """KTD1 stickiness."""
        member = _member("u1", trust_override=override)
        project = _project([member], threshold=5)
        db = _db_with_count(50)
        assert await _svc(db).evaluate_promotion(project, "u1") is False
        assert member.is_trusted is False

    async def test_anonymous_never_promotes(self) -> None:
        """R7: promotion requires an account."""
        member = _member("anonymous-abc")
        project = _project([member], threshold=1)
        db = _db_with_count(50)
        assert await _svc(db).evaluate_promotion(project, "anonymous-abc") is False

    async def test_non_member_never_promotes(self) -> None:
        db = _db_with_count(50)
        assert await _svc(db).evaluate_promotion(_project([]), "stranger") is False


class TestSetTrustOverride:
    async def test_grant_sets_flag_and_provenance(self) -> None:
        member = _member("u1")
        project = _project([member])
        actor = _user("admin-1")
        db = _db_with_count(0)
        result = await _svc(db).set_trust_override(
            project, "u1", TrustOverride.GRANTED, actor
        )
        assert result.is_trusted is True
        assert result.trust_override == "granted"
        assert result.trust_granted_by == "admin-1"
        assert result.trust_granted_at is not None

    @pytest.mark.parametrize("override", [TrustOverride.REFUSED, TrustOverride.REVOKED])
    async def test_refuse_and_revoke_clear_trust(self, override: TrustOverride) -> None:
        member = _member("u1", is_trusted=True)
        project = _project([member])
        db = _db_with_count(0)
        result = await _svc(db).set_trust_override(project, "u1", override, _user("admin-1"))
        assert result.is_trusted is False
        assert result.trust_override == str(override)
        assert result.trust_granted_by is None

    async def test_clearing_to_none_reevaluates_promotion_immediately(self) -> None:
        member = _member("u1", trust_override="refused")
        project = _project([member], threshold=5)
        db = _db_with_count(5)
        result = await _svc(db).set_trust_override(
            project, "u1", TrustOverride.NONE, _user("admin-1")
        )
        assert result.trust_override == "none"
        assert result.is_trusted is True

    async def test_clearing_to_none_below_threshold_leaves_untrusted(self) -> None:
        member = _member("u1", trust_override="refused")
        project = _project([member], threshold=5)
        db = _db_with_count(2)
        result = await _svc(db).set_trust_override(
            project, "u1", TrustOverride.NONE, _user("admin-1")
        )
        assert result.is_trusted is False

    async def test_clearing_explicit_grant_below_threshold_revokes_materialized_flag(self) -> None:
        member = _member("u1", is_trusted=True, trust_override="granted")
        member.trust_granted_at = datetime.now(UTC)
        member.trust_granted_by = "admin-1"
        project = _project([member], threshold=5)
        result = await _svc(_db_with_count(2)).set_trust_override(
            project, "u1", TrustOverride.NONE, _user("admin-1")
        )
        assert result.is_trusted is False
        assert result.trust_granted_at is None
        assert result.trust_granted_by is None

    async def test_unknown_member_raises(self) -> None:
        with pytest.raises(ValueError, match="Member not found"):
            await _svc().set_trust_override(_project([]), "ghost", TrustOverride.GRANTED, _user())

    async def test_emits_audit_log_line(self, caplog: pytest.LogCaptureFixture) -> None:
        member = _member("u1")
        project = _project([member])
        with caplog.at_level("INFO", logger="ontokit.services.trust_service"):
            await _svc().set_trust_override(project, "u1", TrustOverride.GRANTED, _user("admin-1"))
        assert any("trust override set" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Auto-accept eligibility
# ---------------------------------------------------------------------------


class TestAutoAcceptEligibility:
    def test_trusted_human_on_enabled_project_is_eligible(self) -> None:
        project = _project(auto_accept_enabled=True)
        assert (
            TrustService.is_auto_accept_eligible(project, _session("u1"), TrustTier.TRUSTED) is True
        )

    def test_disabled_project_is_never_eligible(self) -> None:
        project = _project(auto_accept_enabled=False)
        assert (
            TrustService.is_auto_accept_eligible(project, _session("u1"), TrustTier.TRUSTED)
            is False
        )

    def test_llm_generated_is_never_eligible(self) -> None:
        """Covers AE5 — R13 holds at every tier, forever."""
        project = _project(auto_accept_enabled=True)
        session = _session("u1", is_llm_generated=True)
        assert TrustService.is_auto_accept_eligible(project, session, TrustTier.TRUSTED) is False

    def test_anonymous_is_never_eligible(self) -> None:
        """Covers AE4."""
        project = _project(auto_accept_enabled=True)
        session = _session("anonymous-abc", is_anonymous=True)
        assert TrustService.is_auto_accept_eligible(project, session, TrustTier.ANONYMOUS) is False

    @pytest.mark.parametrize(
        "tier", [TrustTier.ANONYMOUS, TrustTier.UNTRUSTED, TrustTier.REVIEWER]
    )
    def test_only_the_trusted_tier_is_eligible(self, tier: TrustTier) -> None:
        project = _project(auto_accept_enabled=True)
        assert TrustService.is_auto_accept_eligible(project, _session("u1"), tier) is False


class TestFactory:
    def test_get_trust_service_returns_bound_service(self) -> None:
        db = _db_with_count(0)
        service = get_trust_service(db)
        assert isinstance(service, TrustService)
        assert service.db is db
