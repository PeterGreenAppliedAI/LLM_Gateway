"""add budget_usage

Token budget usage per day, client and tier, so budgets survive restarts
and are shared by every gateway process (D-037).

Revision ID: e3a7c1d94f20
Revises: d8e2f3a91b57
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op

from gateway.storage.migrations.guards import has_table
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3a7c1d94f20'
down_revision: Union[str, Sequence[str], None] = 'd8e2f3a91b57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create budget_usage."""
    if not has_table('budget_usage'):
        op.create_table(
            'budget_usage',
            sa.Column('day', sa.String(length=10), nullable=False),
            sa.Column('client_id', sa.String(length=128), nullable=False),
            sa.Column('tier', sa.String(length=64), nullable=False),
            sa.Column('weighted_tokens', sa.BigInteger(), nullable=False),
            sa.Column('raw_tokens', sa.BigInteger(), nullable=False),
            sa.Column('requests', sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint('day', 'client_id', 'tier', name=op.f('pk_budget_usage')),
        )


def downgrade() -> None:
    """Drop budget_usage."""
    op.drop_table('budget_usage')
