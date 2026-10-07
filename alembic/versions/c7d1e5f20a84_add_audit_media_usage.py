"""add audit_log.media_usage

Native media units for voice/image/video requests (characters, audio
seconds, bytes, voice, format). Metadata only; media content is never
stored in the audit trail (D-023).

Revision ID: c7d1e5f20a84
Revises: b4e2d9a71c35
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d1e5f20a84'
down_revision: Union[str, Sequence[str], None] = 'b4e2d9a71c35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add media_usage column."""
    with op.batch_alter_table('audit_log') as batch:
        batch.add_column(sa.Column('media_usage', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop media_usage column."""
    with op.batch_alter_table('audit_log') as batch:
        batch.drop_column('media_usage')
