"""add audit_journal

Positions of the audit intent log drainers (D-038).

Revision ID: f5b2d8e61a39
Revises: e3a7c1d94f20
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op

from gateway.storage.migrations.guards import has_table
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5b2d8e61a39'
down_revision: Union[str, Sequence[str], None] = 'e3a7c1d94f20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create audit_journal."""
    if not has_table('audit_journal'):
        op.create_table(
            'audit_journal',
            sa.Column('instance', sa.String(length=64), nullable=False),
            sa.Column('segment', sa.String(length=32), nullable=False),
            sa.Column('byte_offset', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('instance', name=op.f('pk_audit_journal')),
        )


def downgrade() -> None:
    """Drop audit_journal."""
    op.drop_table('audit_journal')
