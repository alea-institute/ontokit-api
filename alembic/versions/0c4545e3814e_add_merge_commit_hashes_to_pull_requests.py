"""add merge commit hashes to pull_requests

Revision ID: 0c4545e3814e
Revises: 2d41d93ea12f
Create Date: 2026-02-12 14:15:06.406292

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0c4545e3814e'
down_revision: Union[str, Sequence[str], None] = '2d41d93ea12f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('pull_requests', sa.Column('merge_commit_hash', sa.String(length=40), nullable=True))
    op.add_column('pull_requests', sa.Column('base_commit_hash', sa.String(length=40), nullable=True))
    op.add_column('pull_requests', sa.Column('head_commit_hash', sa.String(length=40), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('pull_requests', 'head_commit_hash')
    op.drop_column('pull_requests', 'base_commit_hash')
    op.drop_column('pull_requests', 'merge_commit_hash')
