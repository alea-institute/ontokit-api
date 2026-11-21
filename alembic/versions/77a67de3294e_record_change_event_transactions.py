"""Record the transaction that inserted each change event.

A feed cursor carries the snapshot its first page read. Later pages keep only
events whose inserting transaction that snapshot saw, so history recorded
mid-paging, whatever its timestamp, waits for the next fresh read.

Existing rows keep NULL: they were committed before any cursor that can read
the column, so every snapshot counts them.

Revision ID: 77a67de3294e
Revises: k9l0m1n2o3p4
"""

import sqlalchemy as sa

from alembic import op

revision: str = "77a67de3294e"
down_revision: str | None = "k9l0m1n2o3p4"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "entity_change_events"
COLUMN = "recorded_xact"

# Each ALTER needs an ACCESS EXCLUSIVE lock. Waiting for it behind a long
# reader would queue every later query on the table, so the transaction gives
# up instead and commits nothing; the deploy reruns it. SET LOCAL ends with the
# transaction, so later revisions keep their normal waits.
_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    # A nullable column without a default, then a default for new rows: neither
    # step rewrites the table.
    op.execute(_LOCK_TIMEOUT)
    op.add_column(TABLE, sa.Column(COLUMN, sa.BigInteger(), nullable=True))
    op.alter_column(
        TABLE,
        COLUMN,
        server_default=sa.text("CAST(CAST(pg_current_xact_id() AS text) AS bigint)"),
    )


def downgrade() -> None:
    op.execute(_LOCK_TIMEOUT)
    op.drop_column(TABLE, COLUMN)
