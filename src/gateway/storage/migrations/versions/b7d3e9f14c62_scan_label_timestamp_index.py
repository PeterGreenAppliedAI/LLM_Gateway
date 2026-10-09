"""index security_scans on (label, timestamp)

The labeling view lists the newest unlabeled scans. With only an index on
label, the planner matched every unlabeled row (all ~714k in the reference
deployment) and sorted them all to return 50: 7 s per page load (D-057).
The composite index walks straight to the newest rows for a label.

Revision ID: b7d3e9f14c62
Revises: a9c4e7b2d6f1
Create Date: 2026-10-09

"""
from typing import Sequence, Union

from alembic import op

from gateway.storage.migrations.guards import has_index


revision: str = 'b7d3e9f14c62'
down_revision: Union[str, Sequence[str], None] = 'a9c4e7b2d6f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the composite index."""
    if has_index('security_scans', 'ix_security_scans_label_timestamp'):
        return
    op.create_index(
        'ix_security_scans_label_timestamp', 'security_scans', ['label', 'timestamp']
    )


def downgrade() -> None:
    """Drop the composite index."""
    op.drop_index('ix_security_scans_label_timestamp', table_name='security_scans')
