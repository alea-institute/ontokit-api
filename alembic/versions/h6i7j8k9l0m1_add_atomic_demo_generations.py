"""Add atomically published, auditable demo generations.

Revision ID: h6i7j8k9l0m1
Revises: g5h6i7j8k9l0
Create Date: 2026-08-28
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "h6i7j8k9l0m1"
down_revision: str | None = "g5h6i7j8k9l0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_DEMO_REPOSITORY_PAIRS = frozenset(
    {
        (
            "alea-institute",
            "folio",
            "alea-institute",
            "ontokit-demo-folio",
        ),
        (
            "catholicos",
            "ontology-semantic-canon",
            "alea-institute",
            "ontokit-demo-semantic-canon",
        ),
    }
)


def upgrade() -> None:
    op.create_table(
        "demo_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("generation_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="preparing", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("attempt_token", sa.Uuid(), nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_failure_reason", sa.Text(), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('preparing', 'active', 'failed', 'retired')",
            name="ck_demo_generations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_key", name="uq_demo_generations_generation_key"),
    )
    op.create_index(
        "uq_demo_generations_single_active",
        "demo_generations",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.add_column("projects", sa.Column("demo_generation_id", sa.Uuid(), nullable=True))
    op.add_column("projects", sa.Column("demo_commit_hash", sa.String(length=40), nullable=True))
    op.create_foreign_key(
        "fk_projects_demo_generation_id",
        "projects",
        "demo_generations",
        ["demo_generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    connection = op.get_bind()
    legacy_demos = connection.execute(
        sa.text(
            """
            SELECT
                lower(trim(source_integration.repo_owner)),
                lower(trim(source_integration.repo_name)),
                lower(trim(demo_integration.repo_owner)),
                lower(trim(demo_integration.repo_name)),
                demo.is_public
            FROM projects AS demo
            LEFT JOIN projects AS source
                ON source.id = demo.demo_source_project_id
            LEFT JOIN github_integrations AS source_integration
                ON source_integration.project_id = source.id
            LEFT JOIN github_integrations AS demo_integration
                ON demo_integration.project_id = demo.id
            WHERE demo.is_demo IS TRUE
            """
        )
    ).all()
    visibilities = {is_public for *_, is_public in legacy_demos}
    if len(visibilities) > 1:
        raise RuntimeError(
            "Cannot migrate a partially published legacy demo set; restore complete visibility "
            "or unpublish the set before upgrading"
        )
    observed_pairs = {tuple(row[:4]) for row in legacy_demos}
    if legacy_demos and (
        len(legacy_demos) != len(_LEGACY_DEMO_REPOSITORY_PAIRS)
        or observed_pairs != _LEGACY_DEMO_REPOSITORY_PAIRS
    ):
        raise RuntimeError(
            "Cannot migrate the legacy demo set unless it contains exactly the approved "
            "source/destination repository pairs with correlated GitHub integrations"
        )
    demo_count = len(legacy_demos)
    if demo_count:
        generation_id = uuid.uuid4()
        status = "active" if visibilities == {True} else "preparing"
        connection.execute(
            sa.text(
                """
                INSERT INTO demo_generations
                    (id, generation_key, status, attempt_count, attempt_token,
                     failure_count, activated_at)
                VALUES
                    (:id, :key, :status, 1, :attempt_token, 0,
                     CASE WHEN :status = 'active' THEN CURRENT_TIMESTAMP ELSE NULL END)
                """
            ),
            {
                "id": generation_id,
                "key": f"legacy-{generation_id.hex}"[:64],
                "status": status,
                "attempt_token": uuid.uuid4(),
            },
        )
        connection.execute(
            sa.text(
                "UPDATE projects SET demo_generation_id = :generation_id WHERE is_demo IS TRUE"
            ),
            {"generation_id": generation_id},
        )

    op.drop_constraint("uq_projects_demo_source_project_id", "projects", type_="unique")
    op.create_unique_constraint(
        "uq_projects_demo_source_generation",
        "projects",
        ["demo_source_project_id", "demo_generation_id"],
    )


def downgrade() -> None:
    duplicate_sources = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT demo_source_project_id
            FROM projects
            WHERE demo_source_project_id IS NOT NULL
            GROUP BY demo_source_project_id
            HAVING count(*) > 1
            LIMIT 1
            """
            )
        )
        .first()
    )
    if duplicate_sources is not None:
        raise RuntimeError(
            "Cannot downgrade atomic demo generations while multiple generations exist; "
            "archive retired/failed generations first"
        )

    op.drop_constraint("uq_projects_demo_source_generation", "projects", type_="unique")
    op.create_unique_constraint(
        "uq_projects_demo_source_project_id", "projects", ["demo_source_project_id"]
    )
    op.drop_constraint("fk_projects_demo_generation_id", "projects", type_="foreignkey")
    op.drop_column("projects", "demo_commit_hash")
    op.drop_column("projects", "demo_generation_id")
    op.drop_index("uq_demo_generations_single_active", table_name="demo_generations")
    op.drop_table("demo_generations")
