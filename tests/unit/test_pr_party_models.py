"""Tests for the PR Party data model (U1).

These pin the *structural* guarantees the rest of the feature leans on, so a
later unit cannot quietly reintroduce a carried finding:

- KTD15/KTD16: one live action row per ``(reviewer, PR, head_sha, action_kind)``
  — enforced by a partial unique index, not by a service-layer convention.
- R24: two reviewers hold genuinely independent action rows on the same PR
  (the fingerprint leads with ``reviewer_id``).
- KTD20: a ``pr_party_ready`` notification is once-per-revision, and a PR Party
  notification / LLM audit row can exist with no project at all.

Defaults are asserted on the COLUMN, not on a freshly constructed instance:
SQLAlchemy applies Python-side defaults at flush, so an unflushed object still
carries ``None``. Asserting the column pins both the ORM default and the
``server_default`` that governs rows already in the table.

The constraint tests are metadata-level. Unit tests in this repo run without a
live Postgres (see ``tests/conftest.py`` — the DB session is an ``AsyncMock``),
and a partial unique index is a Postgres-dialect construct, so the honest thing
to assert here is the DDL the migration will emit. The live round-trip
(``alembic upgrade head`` / ``downgrade -1``) is the migration's own gate.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from sqlalchemy import Column, Index, Table, UniqueConstraint, create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ontokit.core.database import Base
from ontokit.models.llm_config import LLMAuditLog
from ontokit.models.notification import Notification
from ontokit.models.pr_party import (
    PRPartyAction,
    PRPartyActionKind,
    PRPartyActionStatus,
    PRPartyAuthorKind,
    PRPartyBriefStatus,
    PRPartyCredential,
    PRPartyMergeDefault,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.services.llm.audit import log_llm_call

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "x1y2z3a4b5c6_add_pr_party.py"
)

CURRENT_HEAD_BEFORE_PR_PARTY = "w0x1y2z3a4b5"


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


def _index(table: Table, name: str) -> Index:
    by_name = {ix.name: ix for ix in table.indexes}
    assert name in by_name, f"{name} missing; have {sorted(by_name)}"
    return by_name[name]


def _where_text(index: Index) -> str:
    where = index.dialect_options["postgresql"]["where"]
    assert where is not None, f"{index.name} is not a partial index"
    return str(where)


@pytest.fixture
def pr_party_session() -> Iterator[Session]:
    """A throwaway SQLite database holding just the four PR Party tables.

    Cascade behaviour is the one guarantee in this file that metadata cannot
    show: whether the ORM defers a parent delete to the database depends on the
    relationship's ``passive_deletes``, and the consequence only appears in the
    SQL actually emitted. SQLite enforces ``ON DELETE CASCADE`` once
    ``PRAGMA foreign_keys`` is on, which is enough to observe it.
    """
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(
        engine,
        tables=[
            PRPartyReviewer.__table__,
            PRPartyCredential.__table__,
            PRPartyPR.__table__,
            PRPartyAction.__table__,
        ],
    )
    with Session(engine) as session:
        yield session
    engine.dispose()


def _record_sql(engine: Engine, statements: list[str]) -> Any:
    """Append every statement the engine executes to ``statements``."""

    def _before_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    return _before_cursor_execute


@pytest.fixture(scope="module")
def migration_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pr_party_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorded_ops(
    migration_module: ModuleType, direction: str
) -> list[tuple[str, tuple[Any, ...]]]:
    """Run upgrade()/downgrade() against a recording stand-in for ``op``.

    Returns the ordered ``(method_name, positional_args)`` pairs, which is what
    lets us assert the downgrade truly reverses the upgrade.
    """
    recorder = MagicMock()
    original = migration_module.op
    migration_module.op = recorder
    try:
        getattr(migration_module, direction)()
    finally:
        migration_module.op = original
    return [(call[0], call[1]) for call in recorder.mock_calls if call[0] and "." not in call[0]]


# ── Reviewer registry (KTD12) ────────────────────────────────────────────────


class TestPRPartyReviewer:
    def test_zitadel_user_id_is_the_registry_key(self) -> None:
        table = PRPartyReviewer.__table__
        assert table.columns["zitadel_user_id"].unique is True
        assert table.columns["zitadel_user_id"].nullable is False

    def test_merge_default_is_manual(self) -> None:
        """R11: nobody gets dashboard-merge without opting in."""
        table = PRPartyReviewer.__table__
        assert _column_default(table, "merge_default") == PRPartyMergeDefault.MANUAL
        assert _server_default_text(table, "merge_default") == "manual"

    def test_merge_default_values(self) -> None:
        assert {m.value for m in PRPartyMergeDefault} == {"dashboard", "manual"}

    def test_node_id_and_ntfy_topic_are_optional(self) -> None:
        """Node id is GitHub enrichment; ntfy topic is an opt-in secret."""
        table = PRPartyReviewer.__table__
        assert table.columns["github_node_id"].nullable is True
        assert table.columns["ntfy_topic"].nullable is True

    def test_repr_never_leaks_the_ntfy_topic(self) -> None:
        """KTD12: ntfy_topic is a secret — never in the capability payload or a log line."""
        reviewer = PRPartyReviewer(
            zitadel_user_id="z1", github_login="octocat", ntfy_topic="s3cret-topic"
        )
        assert "s3cret-topic" not in repr(reviewer)
        assert "octocat" in repr(reviewer)


# ── Credential (KTD13) ───────────────────────────────────────────────────────


class TestPRPartyCredential:
    def test_one_credential_per_reviewer(self) -> None:
        table = PRPartyCredential.__table__
        assert table.columns["reviewer_id"].unique is True

    def test_credential_dies_with_its_reviewer(self) -> None:
        fk = next(iter(PRPartyCredential.__table__.columns["reviewer_id"].foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_lifecycle_columns_are_nullable(self) -> None:
        """A freshly saved PAT has no expiry header, no validation, no error yet."""
        table = PRPartyCredential.__table__
        for name in ("expires_at", "last_validated_at", "last_error"):
            assert table.columns[name].nullable is True, name

    def test_repr_never_leaks_the_token(self) -> None:
        cred = PRPartyCredential(encrypted_token="gAAAAAB-not-a-real-token")
        assert "gAAAAAB" not in repr(cred)


# ── PR row (KTD15) ───────────────────────────────────────────────────────────


class TestPRPartyPR:
    def test_repo_and_number_are_the_natural_key(self) -> None:
        constraints = {
            c.name: c for c in PRPartyPR.__table__.constraints if isinstance(c, UniqueConstraint)
        }
        assert "uq_pr_party_pr_repo_number" in constraints
        assert [c.name for c in constraints["uq_pr_party_pr_repo_number"].columns] == [
            "repo_full_name",
            "pr_number",
        ]

    def test_author_kind_is_per_pr_not_per_reviewer(self) -> None:
        """R19: classification belongs to the PR, and defaults to the least-trusted kind."""
        assert {k.value for k in PRPartyAuthorKind} == {
            "counterpart",
            "own",
            "third_party",
            "bot",
        }
        table = PRPartyPR.__table__
        assert _column_default(table, "author_kind") == PRPartyAuthorKind.THIRD_PARTY
        assert _server_default_text(table, "author_kind") == "third_party"

    def test_brief_starts_brewing(self) -> None:
        assert {s.value for s in PRPartyBriefStatus} == {
            "brewing",
            "ready",
            "ready_with_warning",
            "failed",
        }
        table = PRPartyPR.__table__
        assert _column_default(table, "brief_status") == PRPartyBriefStatus.BREWING
        assert _server_default_text(table, "brief_status") == "brewing"

    def test_brief_truncated_defaults_false(self) -> None:
        table = PRPartyPR.__table__
        assert _column_default(table, "brief_truncated") is False
        assert _server_default_text(table, "brief_truncated") == "false"

    def test_null_mergeable_state_means_computing(self) -> None:
        """KTD15: GitHub returns null while it computes — that is not 'unmergeable'."""
        table = PRPartyPR.__table__
        assert table.columns["mergeable_state"].nullable is True
        assert table.columns["checks_rollup"].nullable is True

    def test_brief_content_is_plain_json_not_markup(self) -> None:
        """R21: decisions/links are JSON lists of plain strings."""
        table = PRPartyPR.__table__
        assert table.columns["brief_decisions"].nullable is True
        assert table.columns["brief_links"].nullable is True
        pr = PRPartyPR(
            repo_full_name="catholicos/ontokit-api",
            pr_number=7,
            head_sha="a" * 40,
            brief_decisions=["kept the sweep idempotent"],
            brief_links=["docs/plans/011.md"],
        )
        assert pr.brief_decisions == ["kept the sweep idempotent"]

    def test_missing_since_tracks_disappearance(self) -> None:
        """C7: a PR that vanishes from the sweep is aged out, not deleted on sight."""
        assert PRPartyPR.__table__.columns["missing_since"].nullable is True

    def test_node_id_is_enrichment_only(self) -> None:
        assert PRPartyPR.__table__.columns["pr_node_id"].nullable is True

    def test_title_is_an_optional_poller_owned_fact(self) -> None:
        """The card names itself with GitHub's title rather than with brief prose.

        Nullable on purpose: a payload that omitted the title, or a row written
        before the column existed, leaves the client on the
        ``{repo}#{number}`` fallback rather than on invented text.
        """
        column = PRPartyPR.__table__.columns["title"]
        assert column.nullable is True
        assert column.type.length == 512


# ── Action rows (KTD15/KTD16, R24, R25) ──────────────────────────────────────


class TestPRPartyAction:
    def test_action_kinds_and_statuses(self) -> None:
        assert {k.value for k in PRPartyActionKind} == {"review", "merge", "question"}
        assert {s.value for s in PRPartyActionStatus} == {
            "pending",
            "succeeded",
            "failed",
            "degraded_intent",
            "degraded_confirmed",
        }

    def test_actions_start_pending_without_override(self) -> None:
        """KTD16: the row is inserted *before* the GitHub call, in pending."""
        table = PRPartyAction.__table__
        assert _column_default(table, "status") == PRPartyActionStatus.PENDING
        assert _server_default_text(table, "status") == "pending"
        assert _column_default(table, "override") is False
        assert _server_default_text(table, "override") == "false"

    def test_one_live_action_per_fingerprint(self) -> None:
        """KTD16: a second non-failed action with the same fingerprint is rejected.

        Structural, not conventional — a partial UNIQUE index over
        ``(reviewer_id, pr_id, head_sha, action_kind)``.
        """
        index = _index(PRPartyAction.__table__, "uq_pr_party_action_live_fingerprint")
        assert index.unique is True
        assert [c.name for c in index.columns] == [
            "reviewer_id",
            "pr_id",
            "head_sha",
            "action_kind",
        ]

    def test_failed_rows_fall_outside_the_live_index(self) -> None:
        """The predicate excludes ``failed`` so a dead attempt never blocks a retry.

        Note the division of labour: the index *permits* a failed row and a
        fresh pending row to coexist; KTD16's endpoint contract (U6) is what
        re-opens the failed row instead of inserting a second one. Encoding
        're-open, not insert' in the index would make retry impossible.
        """
        where = _where_text(_index(PRPartyAction.__table__, "uq_pr_party_action_live_fingerprint"))
        assert "status" in where
        assert "failed" in where

    def test_two_reviewers_hold_independent_rows_on_one_pr(self) -> None:
        """R24: the fingerprint leads with reviewer_id, so it never cross-blocks."""
        index = _index(PRPartyAction.__table__, "uq_pr_party_action_live_fingerprint")
        assert index.columns[0].name == "reviewer_id"
        assert "pr_id" in {c.name for c in index.columns}

    def test_idempotency_key_is_indexed_but_not_unique(self) -> None:
        """KTD16: a replay must be *findable*; uniqueness lives on the fingerprint."""
        index = _index(PRPartyAction.__table__, "ix_pr_party_action_idempotency_key")
        assert index.unique is False
        assert [c.name for c in index.columns] == ["idempotency_key"]

    def test_verdict_binds_to_a_head_sha_and_a_review_id(self) -> None:
        """R25: the verdict, its GitHub review id, and the SHA are one atomic row."""
        table = PRPartyAction.__table__
        assert table.columns["head_sha"].nullable is False
        assert table.columns["github_review_id"].nullable is True

    def test_actions_cascade_from_both_parents(self) -> None:
        table = PRPartyAction.__table__
        for column in ("reviewer_id", "pr_id"):
            fk = next(iter(table.columns[column].foreign_keys))
            assert fk.ondelete == "CASCADE", column

    def test_actions_are_indexed_by_pr(self) -> None:
        """The card read (and the FK cascade) walks pr_id — Postgres indexes no FK on its own."""
        index = _index(PRPartyAction.__table__, "ix_pr_party_action_pr_id")
        assert index.unique is False
        assert [c.name for c in index.columns] == ["pr_id"]

    def test_both_parents_defer_deletion_to_the_database(self) -> None:
        """``passive_deletes`` is what makes the FK's ON DELETE CASCADE authoritative.

        Without it the ORM would load the children and UPDATE their parent FK to
        NULL — and both FK columns are NOT NULL, so that is an IntegrityError.
        """
        assert PRPartyPR.actions.property.passive_deletes is True
        assert PRPartyReviewer.actions.property.passive_deletes is True

    def test_deleting_a_pr_cascades_its_actions_in_the_database(
        self, pr_party_session: Session
    ) -> None:
        """The parent delete emits no UPDATE-to-NULL, and the rows still go."""
        reviewer = PRPartyReviewer(zitadel_user_id="z1", github_login="octocat")
        pr = PRPartyPR(repo_full_name="catholicos/ontokit-api", pr_number=7, head_sha="a" * 40)
        pr_party_session.add_all([reviewer, pr])
        pr_party_session.flush()
        pr_party_session.add(
            PRPartyAction(
                reviewer_id=reviewer.id,
                pr_id=pr.id,
                head_sha="a" * 40,
                action_kind=PRPartyActionKind.REVIEW,
                idempotency_key="idem-1",
            )
        )
        pr_party_session.commit()

        engine = pr_party_session.get_bind()
        assert isinstance(engine, Engine)
        statements: list[str] = []
        listener = _record_sql(engine, statements)
        try:
            pr_party_session.delete(pr)
            pr_party_session.commit()
        finally:
            event.remove(engine, "before_cursor_execute", listener)

        assert not [s for s in statements if "UPDATE pr_party_action" in s], statements
        assert pr_party_session.scalars(select(PRPartyAction)).all() == []
        # The other parent is untouched: a PR going away is not a de-registration.
        assert pr_party_session.scalars(select(PRPartyReviewer)).all() == [reviewer]

    def test_deleting_a_reviewer_cascades_actions_and_credential(
        self, pr_party_session: Session
    ) -> None:
        reviewer = PRPartyReviewer(zitadel_user_id="z2", github_login="hubot")
        reviewer.credential = PRPartyCredential(encrypted_token="gAAAAAB-not-a-real-token")
        pr = PRPartyPR(repo_full_name="catholicos/ontokit-api", pr_number=8, head_sha="b" * 40)
        pr_party_session.add_all([reviewer, pr])
        pr_party_session.flush()
        pr_party_session.add(
            PRPartyAction(
                reviewer_id=reviewer.id,
                pr_id=pr.id,
                head_sha="b" * 40,
                action_kind=PRPartyActionKind.MERGE,
                idempotency_key="idem-2",
            )
        )
        pr_party_session.commit()

        engine = pr_party_session.get_bind()
        assert isinstance(engine, Engine)
        statements: list[str] = []
        listener = _record_sql(engine, statements)
        try:
            pr_party_session.delete(reviewer)
            pr_party_session.commit()
        finally:
            event.remove(engine, "before_cursor_execute", listener)

        assert not [s for s in statements if "UPDATE pr_party_action" in s], statements
        assert pr_party_session.scalars(select(PRPartyAction)).all() == []
        # KTD13: de-registering a reviewer never leaves the secret behind.
        assert pr_party_session.scalars(select(PRPartyCredential)).all() == []
        assert pr_party_session.scalars(select(PRPartyPR)).all() == [pr]


# ── Nullable project columns (KTD20) ─────────────────────────────────────────


class TestNullableProjectColumns:
    def test_notification_project_columns_are_nullable(self) -> None:
        """A pr_party_ready notification belongs to no OntoKit project."""
        table = Notification.__table__
        assert table.columns["project_id"].nullable is True
        assert table.columns["project_name"].nullable is True

    def test_pr_party_ready_is_once_per_revision(self) -> None:
        """R22: (user, type, target_id) unique, scoped to the pr_party_ready type."""
        index = _index(Notification.__table__, "uq_notification_pr_party_ready")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["user_id", "type", "target_id"]
        where = _where_text(index)
        assert "pr_party_ready" in where

    def test_audit_log_project_is_nullable(self) -> None:
        assert LLMAuditLog.__table__.columns["project_id"].nullable is True

    async def test_log_llm_call_accepts_a_null_project(self) -> None:
        """The brief worker bills to no project; the audit row must still write."""
        db = AsyncMock()
        db.add = Mock()
        entry = await log_llm_call(
            db=db,
            project_id=None,
            user_id="system:pr-party",
            model="gpt-4o",
            provider="openai",
            endpoint="pr-party/brief",
            input_tokens=10,
            output_tokens=20,
            cost_estimate_usd=0.01,
        )
        assert entry.project_id is None
        db.add.assert_called_once()
        db.flush.assert_awaited_once()


# ── Migration (single head, reversible) ──────────────────────────────────────


class TestMigration:
    def test_chains_onto_the_current_single_head(self, migration_module: ModuleType) -> None:
        assert migration_module.revision == "x1y2z3a4b5c6"
        assert migration_module.down_revision == CURRENT_HEAD_BEFORE_PR_PARTY

    def test_upgrade_creates_all_four_tables(self, migration_module: ModuleType) -> None:
        ops = _recorded_ops(migration_module, "upgrade")
        created = [args[0] for name, args in ops if name == "create_table"]
        assert created == [
            "pr_party_reviewer",
            "pr_party_credential",
            "pr_party_pr",
            "pr_party_action",
        ]

    def test_pr_table_ddl_matches_the_model(self, migration_module: ModuleType) -> None:
        """The PR row is the table the feature keeps growing columns on.

        There is one PR Party migration and it is amended in place, so a column
        added to the model and forgotten in the DDL would pass every other test
        in this file and only fail against a real database.
        """
        ops = _recorded_ops(migration_module, "upgrade")
        args = next(a for name, a in ops if name == "create_table" and a[0] == "pr_party_pr")
        ddl_columns = {c.name for c in args[1:] if isinstance(c, Column)}
        assert ddl_columns == set(PRPartyPR.__table__.columns.keys())
        assert "title" in ddl_columns

    def test_action_ddl_indexes_match_the_model(self, migration_module: ModuleType) -> None:
        """Same trap as the column test: this migration is amended in place.

        An index added to ``__table_args__`` and forgotten in the DDL would pass
        every metadata assertion above and only fail against a real database.
        """
        ops = _recorded_ops(migration_module, "upgrade")
        ddl_indexes = {
            args[0] for name, args in ops if name == "create_index" and args[1] == "pr_party_action"
        }
        assert ddl_indexes == {ix.name for ix in PRPartyAction.__table__.indexes}
        assert "ix_pr_party_action_pr_id" in ddl_indexes

    def test_upgrade_relaxes_the_three_project_columns(self, migration_module: ModuleType) -> None:
        ops = _recorded_ops(migration_module, "upgrade")
        altered = {(args[0], args[1]) for name, args in ops if name == "alter_column"}
        assert altered == {
            ("notifications", "project_id"),
            ("notifications", "project_name"),
            ("llm_audit_logs", "project_id"),
        }

    def test_upgrade_creates_the_two_partial_unique_indexes(
        self, migration_module: ModuleType
    ) -> None:
        ops = _recorded_ops(migration_module, "upgrade")
        indexes = [args[0] for name, args in ops if name == "create_index"]
        assert "uq_pr_party_action_live_fingerprint" in indexes
        assert "uq_notification_pr_party_ready" in indexes
        assert "ix_pr_party_action_idempotency_key" in indexes

    def test_downgrade_reverses_the_upgrade_exactly(self, migration_module: ModuleType) -> None:
        up = _recorded_ops(migration_module, "upgrade")
        down = _recorded_ops(migration_module, "downgrade")

        created = [args[0] for name, args in up if name == "create_table"]
        dropped = [args[0] for name, args in down if name == "drop_table"]
        assert dropped == list(reversed(created))

        up_indexes = [args[0] for name, args in up if name == "create_index"]
        down_indexes = [args[0] for name, args in down if name == "drop_index"]
        assert down_indexes == list(reversed(up_indexes))

        # Nullability is restored on the same three columns.
        down_altered = {(args[0], args[1]) for name, args in down if name == "alter_column"}
        assert down_altered == {
            ("notifications", "project_id"),
            ("notifications", "project_name"),
            ("llm_audit_logs", "project_id"),
        }

    def test_migration_is_reversible(self, migration_module: ModuleType) -> None:
        assert callable(migration_module.upgrade)
        assert callable(migration_module.downgrade)
