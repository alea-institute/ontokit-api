"""Index change events by branch and entity in history order.

The changes feed groups a window's events per entity, oldest to newest. This
index hands them over in that order, with every column the grouping filters
and reads, so the feed scans it alone instead of sorting the window.

Revision ID: l0m1n2o3p4q5
Revises: 77a67de3294e
"""

from alembic import op

revision: str = "l0m1n2o3p4q5"
down_revision: str | None = "77a67de3294e"
branch_labels: str | None = None
depends_on: str | None = None

INDEX = "ix_change_events_project_branch_entity_time"
TABLE = "entity_change_events"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # An interrupted concurrent build leaves an invalid index for the rerun to replace.
        op.drop_index(INDEX, table_name=TABLE, postgresql_concurrently=True, if_exists=True)
        op.create_index(
            INDEX,
            TABLE,
            ["project_id", "branch", "entity_iri", "created_at", "id"],
            postgresql_include=["event_type", "origin", "recorded_xact"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(INDEX, table_name=TABLE, postgresql_concurrently=True, if_exists=True)
