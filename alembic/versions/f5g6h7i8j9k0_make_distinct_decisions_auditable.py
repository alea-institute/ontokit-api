"""Make distinct-entity decisions auditable and fingerprint-bound.

Revision ID: f5g6h7i8j9k0
Revises: e4f5g6h7i8j9
Create Date: 2026-08-23
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "f5g6h7i8j9k0"
down_revision = "e4f5g6h7i8j9"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    invalid_rows = list(
        bind.execute(
            sa.text(
                "SELECT id FROM duplicate_rejections "
                "WHERE rejected_iri = canonical_iri ORDER BY rejected_at DESC LIMIT 20"
            )
        ).scalars()
    )
    invalid_count = int(
        bind.execute(
            sa.text("SELECT count(*) FROM duplicate_rejections WHERE rejected_iri = canonical_iri")
        ).scalar_one()
    )
    if invalid_count:
        raise RuntimeError(
            "Cannot canonicalize duplicate_rejections containing identical IRI pairs: "
            f"count={invalid_count}, sample_ids={invalid_rows}. "
            "Correct or remove those invalid legacy rows before retrying the migration."
        )
    legacy_count = int(
        bind.execute(sa.text("SELECT count(*) FROM duplicate_rejections")).scalar_one()
    )
    if legacy_count:
        sample_ids = list(
            bind.execute(
                sa.text("SELECT id FROM duplicate_rejections ORDER BY rejected_at DESC LIMIT 20")
            ).scalars()
        )
        logger.warning(
            "Deactivating %d legacy duplicate_rejections rows without input fingerprints; "
            "sample_ids=%s",
            legacy_count,
            sample_ids,
        )

    op.drop_index("ix_duplicate_rejections_lookup", table_name="duplicate_rejections")
    op.alter_column("duplicate_rejections", "rejected_iri", new_column_name="iri_a")
    op.alter_column("duplicate_rejections", "canonical_iri", new_column_name="iri_b")
    op.alter_column("duplicate_rejections", "rejection_reason", new_column_name="reason")
    op.alter_column("duplicate_rejections", "rejected_by", new_column_name="marked_by")
    op.alter_column("duplicate_rejections", "rejected_at", new_column_name="marked_at")

    op.add_column("duplicate_rejections", sa.Column("fingerprint_a", sa.String(64), nullable=True))
    op.add_column("duplicate_rejections", sa.Column("fingerprint_b", sa.String(64), nullable=True))
    op.add_column(
        "duplicate_rejections", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("duplicate_rejections", sa.Column("revoked_by", sa.String(255), nullable=True))
    op.add_column("duplicate_rejections", sa.Column("superseded_by_id", sa.UUID(), nullable=True))

    # Canonicalize the unordered pair before adding its invariant. Legacy rows
    # cannot safely suppress because they predate detector-input fingerprints,
    # so retain them as explicitly revoked audit history.
    op.execute(
        """
        UPDATE duplicate_rejections
        SET iri_a = LEAST(iri_a, iri_b),
            iri_b = GREATEST(iri_a, iri_b),
            reason = COALESCE(NULLIF(BTRIM(reason), ''),
                              'Legacy distinct decision (inactive pending re-validation)'),
            fingerprint_a = md5(id::text || '-a') || md5(id::text || '-a-2'),
            fingerprint_b = md5(id::text || '-b') || md5(id::text || '-b-2'),
            revoked_at = COALESCE(marked_at, now()),
            revoked_by = 'system:migration'
        """
    )
    op.alter_column("duplicate_rejections", "reason", nullable=False)
    op.alter_column("duplicate_rejections", "fingerprint_a", nullable=False)
    op.alter_column("duplicate_rejections", "fingerprint_b", nullable=False)
    op.create_foreign_key(
        "fk_duplicate_rejections_superseded_by",
        "duplicate_rejections",
        "duplicate_rejections",
        ["superseded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_duplicate_rejections_canonical_pair",
        "duplicate_rejections",
        "iri_a < iri_b",
    )
    op.create_check_constraint(
        "ck_duplicate_rejections_reason",
        "duplicate_rejections",
        "length(trim(reason)) > 0",
    )
    op.create_index(
        "uq_duplicate_rejections_active_pair",
        "duplicate_rejections",
        ["project_id", "iri_a", "iri_b"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_duplicate_rejections_history",
        "duplicate_rejections",
        ["project_id", "iri_a", "iri_b", "marked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_duplicate_rejections_history", table_name="duplicate_rejections")
    op.drop_index("uq_duplicate_rejections_active_pair", table_name="duplicate_rejections")
    op.drop_constraint("ck_duplicate_rejections_reason", "duplicate_rejections", type_="check")
    op.drop_constraint(
        "ck_duplicate_rejections_canonical_pair", "duplicate_rejections", type_="check"
    )
    op.drop_constraint(
        "fk_duplicate_rejections_superseded_by", "duplicate_rejections", type_="foreignkey"
    )
    op.drop_column("duplicate_rejections", "superseded_by_id")
    op.drop_column("duplicate_rejections", "revoked_by")
    op.drop_column("duplicate_rejections", "revoked_at")
    op.drop_column("duplicate_rejections", "fingerprint_b")
    op.drop_column("duplicate_rejections", "fingerprint_a")
    op.alter_column("duplicate_rejections", "reason", nullable=True)
    op.alter_column("duplicate_rejections", "marked_at", new_column_name="rejected_at")
    op.alter_column("duplicate_rejections", "marked_by", new_column_name="rejected_by")
    op.alter_column("duplicate_rejections", "reason", new_column_name="rejection_reason")
    op.alter_column("duplicate_rejections", "iri_b", new_column_name="canonical_iri")
    op.alter_column("duplicate_rejections", "iri_a", new_column_name="rejected_iri")
    op.create_index(
        "ix_duplicate_rejections_lookup",
        "duplicate_rejections",
        ["project_id", "rejected_iri"],
    )
