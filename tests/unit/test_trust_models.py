"""Tests for the trust-ladder data model (U1).

These pin the defaults the whole feature reasons about: an existing deployment
must land on the untrusted rung with auto-accept OFF, and the promotion /
quiet-period defaults (KTD8) must be a visible test change if anyone edits them.

Defaults are asserted on the COLUMN, not on a freshly constructed instance:
SQLAlchemy applies Python-side defaults at flush, so an unflushed object still
carries ``None``. Asserting the column pins both the ORM default and the
server_default that governs rows already in the table.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import Table

from ontokit.models.project import Project, ProjectMember
from ontokit.models.suggestion_outcome import SuggestionOutcome, SuggestionOutcomeType
from ontokit.models.suggestion_session import SuggestionSession
from ontokit.models.user_commit_identity import UserCommitIdentity

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "w0x1y2z3a4b5_add_trust_ladder.py"
)


def _column_default(table: Table, name: str) -> Any:
    """The Python-side default value configured on a column."""
    column = table.columns[name]
    assert column.default is not None, f"{name} has no ORM default"
    return column.default.arg


def _server_default_text(table: Table, name: str) -> str:
    """The DDL-level default configured on a column."""
    column = table.columns[name]
    assert column.server_default is not None, f"{name} has no server_default"
    return str(column.server_default.arg)  # type: ignore[union-attr]


@pytest.fixture(scope="module")
def migration_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("trust_ladder_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestProjectTrustDefaults:
    """Project-level ladder settings."""

    def test_defaults_match_ktd8(self) -> None:
        table = Project.__table__
        assert _column_default(table, "trust_promotion_threshold") == 5
        assert _column_default(table, "auto_accept_enabled") is False
        assert _column_default(table, "auto_accept_quiet_days") == 7

    def test_server_defaults_cover_existing_rows(self) -> None:
        """A populated table must migrate to auto-accept OFF, not ON."""
        table = Project.__table__
        assert _server_default_text(table, "trust_promotion_threshold") == "5"
        assert _server_default_text(table, "auto_accept_enabled") == "false"
        assert _server_default_text(table, "auto_accept_quiet_days") == "7"

    def test_settings_are_settable(self) -> None:
        project = Project(
            name="P",
            owner_id="u1",
            trust_promotion_threshold=3,
            auto_accept_enabled=True,
            auto_accept_quiet_days=14,
        )
        assert project.trust_promotion_threshold == 3
        assert project.auto_accept_enabled is True
        assert project.auto_accept_quiet_days == 14


class TestProjectMemberTrustDefaults:
    """A member created with no trust arguments is untrusted, unoverridden."""

    def test_untrusted_by_default(self) -> None:
        table = ProjectMember.__table__
        assert _column_default(table, "is_trusted") is False
        assert _column_default(table, "trust_override") == "none"
        assert _server_default_text(table, "is_trusted") == "false"
        assert _server_default_text(table, "trust_override") == "none"

    def test_grant_provenance_columns_are_nullable(self) -> None:
        table = ProjectMember.__table__
        assert table.columns["trust_granted_at"].nullable is True
        assert table.columns["trust_granted_by"].nullable is True

    def test_existing_flag_untouched(self) -> None:
        """KTD5: the ladder generalizes can_self_merge_structural, not replaces it."""
        member = ProjectMember(user_id="u1", role="editor", can_self_merge_structural=True)
        assert member.can_self_merge_structural is True
        assert "can_self_merge_structural" in ProjectMember.__table__.columns


class TestSuggestionSessionTrustDefaults:
    """New session columns default to the safe (non-auto-accepting) state."""

    def test_defaults(self) -> None:
        table = SuggestionSession.__table__
        assert _column_default(table, "is_llm_generated") is False
        assert _column_default(table, "verification_passed") is False
        assert table.columns["auto_accept_after"].nullable is True
        assert table.columns["auto_accept_halted_at"].nullable is True

    def test_server_defaults(self) -> None:
        table = SuggestionSession.__table__
        assert _server_default_text(table, "is_llm_generated") == "false"
        assert _server_default_text(table, "verification_passed") == "false"


class TestSuggestionOutcome:
    """The append-only outcome log."""

    def test_outcome_type_values(self) -> None:
        """Exactly three terminal outcomes — a fourth is a schema change."""
        assert {t.value for t in SuggestionOutcomeType} == {
            "accepted",
            "rejected",
            "dismissed",
        }

    def test_defaults(self) -> None:
        table = SuggestionOutcome.__table__
        assert _column_default(table, "counts_toward_promotion") is True
        assert _column_default(table, "is_anonymous") is False

    def test_session_fk_survives_session_deletion(self) -> None:
        """Append-only: the log must outlive the session it describes."""
        fk = next(iter(SuggestionOutcome.__table__.columns["session_id"].foreign_keys))
        assert fk.ondelete == "SET NULL"
        assert SuggestionOutcome.__table__.columns["session_id"].nullable is True

    def test_repr_is_informative(self) -> None:
        row = SuggestionOutcome(user_id="u1", outcome=SuggestionOutcomeType.REJECTED.value)
        assert "rejected" in repr(row)

    def test_promotion_index_is_partial(self) -> None:
        """KTD2: the promotion count is the only hot query — keep it partial."""
        by_name = {ix.name: ix for ix in SuggestionOutcome.__table__.indexes}
        assert "ix_suggestion_outcomes_project_user" in by_name
        promo = by_name["ix_suggestion_outcomes_promotion"]
        where = promo.dialect_options["postgresql"]["where"]
        assert where is not None
        assert "counts_toward_promotion" in str(where)


class TestUserCommitIdentityDefaults:
    """Opt-in commit authoring is off, and unverified, by default."""

    def test_defaults(self) -> None:
        table = UserCommitIdentity.__table__
        assert _column_default(table, "commit_email_verified") is False
        assert _column_default(table, "use_verified_email") is False
        assert table.columns["commit_email"].nullable is True

    def test_user_id_is_unique(self) -> None:
        assert UserCommitIdentity.__table__.columns["user_id"].unique is True


class TestMigration:
    """Single-head and reversibility pins."""

    def test_migration_chains_onto_current_embedding_head(
        self, migration_module: ModuleType
    ) -> None:
        assert migration_module.revision == "w0x1y2z3a4b5"
        assert migration_module.down_revision == "c2d3e4f5g6h7"

    def test_migration_is_reversible(self, migration_module: ModuleType) -> None:
        assert callable(migration_module.upgrade)
        assert callable(migration_module.downgrade)

    def test_auto_accept_scan_has_matching_partial_index(self) -> None:
        source = MIGRATION_PATH.read_text()
        assert '"ix_suggestion_sessions_auto_accept_scan"' in source
        assert "status IN ('submitted', 'auto-submitted')" in source
        assert "auto_accept_after IS NOT NULL" in source
        assert "auto_accept_halted_at IS NULL" in source
        assert "AND NOT is_anonymous" in source
        assert "AND NOT is_llm_generated" in source
        assert 'op.drop_index("ix_suggestion_sessions_auto_accept_scan"' in source
