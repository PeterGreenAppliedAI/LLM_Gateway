"""add runtime_settings table

Operator settings changed at runtime from the dashboard (first: PII
scrubbing), persisted so they survive restarts and override the
environment-variable defaults.

Revision ID: b4e2d9a71c35
Revises: 7a3f1c9e2b41
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op

from gateway.storage.migrations.guards import has_table
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e2d9a71c35'
down_revision: Union[str, Sequence[str], None] = '7a3f1c9e2b41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create runtime_settings table."""
    if not has_table('runtime_settings'):
        op.create_table(
            'runtime_settings',
            sa.Column('key', sa.String(64), nullable=False),
            sa.Column('value', sa.JSON(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(128), nullable=True),
            sa.PrimaryKeyConstraint('key'),
        )


def downgrade() -> None:
    """Drop runtime_settings table."""
    op.drop_table('runtime_settings')
