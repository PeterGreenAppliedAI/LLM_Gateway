"""add api_keys.max_concurrent and api_keys.priority

Per-key in-flight limit and scheduling class (D-034).

Revision ID: d8e2f3a91b57
Revises: c7d1e5f20a84
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e2f3a91b57'
down_revision: Union[str, Sequence[str], None] = 'c7d1e5f20a84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add max_concurrent and priority columns."""
    with op.batch_alter_table('api_keys') as batch:
        batch.add_column(sa.Column('max_concurrent', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('priority', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Drop max_concurrent and priority columns."""
    with op.batch_alter_table('api_keys') as batch:
        batch.drop_column('priority')
        batch.drop_column('max_concurrent')
