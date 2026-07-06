"""add security_scans and pii_events tables

These tables exist in gateway.storage.schema but were missing from the
initial migration — deployments relying on alembic (instead of the
metadata.create_all() startup fallback) had no security scan store or
PII audit trail.

Revision ID: 7a3f1c9e2b41
Revises: 56dc87704392
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a3f1c9e2b41'
down_revision: Union[str, Sequence[str], None] = '56dc87704392'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create security_scans and pii_events tables."""
    # security_scans (guard model training data collection)
    op.create_table(
        'security_scans',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('request_id', sa.String(64), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('client_id', sa.String(128), nullable=False),
        sa.Column('model', sa.String(128), nullable=True),
        sa.Column('task', sa.String(32), nullable=True),
        sa.Column('messages', sa.JSON(), nullable=False),
        sa.Column('regex_threat_level', sa.String(16), nullable=False),
        sa.Column('regex_match_count', sa.Integer(), nullable=True),
        sa.Column('regex_matches', sa.JSON(), nullable=True),
        sa.Column('guard_safe', sa.Boolean(), nullable=True),
        sa.Column('guard_skipped', sa.Boolean(), nullable=True),
        sa.Column('guard_category_code', sa.String(8), nullable=True),
        sa.Column('guard_category_name', sa.String(64), nullable=True),
        sa.Column('guard_confidence', sa.String(16), nullable=True),
        sa.Column('guard_inference_ms', sa.Float(), nullable=True),
        sa.Column('guard_raw_response', sa.Text(), nullable=True),
        sa.Column('guard_error', sa.String(128), nullable=True),
        sa.Column('label', sa.String(16), nullable=True),
        sa.Column('label_category', sa.String(64), nullable=True),
        sa.Column('labeled_by', sa.String(128), nullable=True),
        sa.Column('labeled_at', sa.DateTime(), nullable=True),
        sa.Column('label_notes', sa.Text(), nullable=True),
        sa.Column('is_disagreement', sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id'),
    )
    op.create_index('ix_security_scans_timestamp', 'security_scans', ['timestamp'])
    op.create_index('ix_security_scans_label', 'security_scans', ['label'])
    op.create_index('ix_security_scans_disagreement', 'security_scans', ['is_disagreement'])
    op.create_index('ix_security_scans_client_id', 'security_scans', ['client_id'])
    op.create_index('ix_security_scans_regex_threat', 'security_scans', ['regex_threat_level'])

    # pii_events (detection audit trail — never stores raw PII)
    op.create_table(
        'pii_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('request_id', sa.String(64), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('client_id', sa.String(128), nullable=False),
        sa.Column('model', sa.String(128), nullable=True),
        sa.Column('task', sa.String(32), nullable=True),
        sa.Column('pii_type', sa.String(32), nullable=False),
        sa.Column('message_index', sa.Integer(), nullable=True),
        sa.Column('message_role', sa.String(16), nullable=True),
        sa.Column('position_start', sa.Integer(), nullable=True),
        sa.Column('position_end', sa.Integer(), nullable=True),
        sa.Column('value_hash', sa.String(64), nullable=False),
        sa.Column('was_scrubbed', sa.Boolean(), nullable=True),
        sa.Column('scan_time_ms', sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_pii_events_timestamp', 'pii_events', ['timestamp'])
    op.create_index('ix_pii_events_request_id', 'pii_events', ['request_id'])
    op.create_index('ix_pii_events_client_id', 'pii_events', ['client_id'])
    op.create_index('ix_pii_events_pii_type', 'pii_events', ['pii_type'])
    op.create_index('ix_pii_events_value_hash', 'pii_events', ['value_hash'])


def downgrade() -> None:
    """Drop security_scans and pii_events tables."""
    op.drop_table('pii_events')
    op.drop_table('security_scans')
