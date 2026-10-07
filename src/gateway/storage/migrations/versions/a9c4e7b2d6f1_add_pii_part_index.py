"""add pii_events.part_index

Which content part of a multimodal message a PII detection is in; its
positions are within that part (D-048).

Revision ID: a9c4e7b2d6f1
Revises: f5b2d8e61a39
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op

from gateway.storage.migrations.guards import has_column
import sqlalchemy as sa


revision: str = 'a9c4e7b2d6f1'
down_revision: Union[str, Sequence[str], None] = 'f5b2d8e61a39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add part_index."""
    if has_column('pii_events', 'part_index'):
        return
    with op.batch_alter_table('pii_events') as batch:
        batch.add_column(sa.Column('part_index', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop part_index."""
    with op.batch_alter_table('pii_events') as batch:
        batch.drop_column('part_index')
