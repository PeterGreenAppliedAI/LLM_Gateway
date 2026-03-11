"""initial schema

Revision ID: 56dc87704392
Revises:
Create Date: 2026-03-11 14:14:37.327222

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '56dc87704392'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create initial schema tables."""
    # audit_log
    op.create_table(
        'audit_log',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('request_id', sa.String(64), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('client_id', sa.String(128), nullable=False),
        sa.Column('user_id', sa.String(128), nullable=True),
        sa.Column('environment', sa.String(64), nullable=True),
        sa.Column('task', sa.String(32), nullable=False),
        sa.Column('model', sa.String(128), nullable=False),
        sa.Column('endpoint', sa.String(64), nullable=False),
        sa.Column('provider_type', sa.String(32), nullable=True),
        sa.Column('stream', sa.Boolean(), nullable=True),
        sa.Column('max_tokens', sa.Integer(), nullable=True),
        sa.Column('temperature', sa.Float(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('time_to_first_token_ms', sa.Float(), nullable=True),
        sa.Column('tokens_per_second', sa.Float(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('estimated_cost_usd', sa.Float(), nullable=True),
        sa.Column('request_body', sa.JSON(), nullable=True),
        sa.Column('response_body', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log')),
        sa.UniqueConstraint('request_id', name=op.f('uq_audit_log_request_id')),
    )
    op.create_index('ix_audit_log_timestamp', 'audit_log', ['timestamp'])
    op.create_index('ix_audit_log_client_id', 'audit_log', ['client_id'])
    op.create_index('ix_audit_log_user_id', 'audit_log', ['user_id'])
    op.create_index('ix_audit_log_model', 'audit_log', ['model'])
    op.create_index('ix_audit_log_endpoint', 'audit_log', ['endpoint'])
    op.create_index('ix_audit_log_status', 'audit_log', ['status'])
    op.create_index('ix_audit_log_environment', 'audit_log', ['environment'])
    op.create_index('ix_audit_log_client_timestamp', 'audit_log', ['client_id', 'timestamp'])

    # usage_daily
    op.create_table(
        'usage_daily',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('date', sa.DateTime(), nullable=False),
        sa.Column('client_id', sa.String(128), nullable=False),
        sa.Column('user_id', sa.String(128), nullable=True),
        sa.Column('environment', sa.String(64), nullable=True),
        sa.Column('endpoint', sa.String(64), nullable=False),
        sa.Column('model', sa.String(128), nullable=False),
        sa.Column('request_count', sa.Integer(), nullable=True),
        sa.Column('success_count', sa.Integer(), nullable=True),
        sa.Column('error_count', sa.Integer(), nullable=True),
        sa.Column('stream_count', sa.Integer(), nullable=True),
        sa.Column('total_prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('total_completion_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('total_cost_usd', sa.Float(), nullable=True),
        sa.Column('avg_latency_ms', sa.Float(), nullable=True),
        sa.Column('min_latency_ms', sa.Float(), nullable=True),
        sa.Column('max_latency_ms', sa.Float(), nullable=True),
        sa.Column('p50_latency_ms', sa.Float(), nullable=True),
        sa.Column('p95_latency_ms', sa.Float(), nullable=True),
        sa.Column('p99_latency_ms', sa.Float(), nullable=True),
        sa.Column('avg_ttft_ms', sa.Float(), nullable=True),
        sa.Column('avg_tokens_per_second', sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_usage_daily')),
    )
    op.create_index('ix_usage_daily_date', 'usage_daily', ['date'])
    op.create_index('ix_usage_daily_client_date', 'usage_daily', ['client_id', 'date'])
    op.create_index('ix_usage_daily_endpoint_date', 'usage_daily', ['endpoint', 'date'])
    op.create_index(
        'ix_usage_daily_unique', 'usage_daily',
        ['date', 'client_id', 'user_id', 'environment', 'endpoint', 'model'],
        unique=True,
    )

    # api_keys
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('key_hash', sa.String(128), nullable=False),
        sa.Column('key_prefix', sa.String(16), nullable=False),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('client_id', sa.String(128), nullable=False),
        sa.Column('environment', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('rate_limit_rpm', sa.Integer(), nullable=True),
        sa.Column('allowed_models', sa.JSON(), nullable=True),
        sa.Column('allowed_endpoints', sa.JSON(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_api_keys')),
        sa.UniqueConstraint('key_hash', name=op.f('uq_api_keys_key_hash')),
    )
    op.create_index('ix_api_keys_client_id', 'api_keys', ['client_id'])
    op.create_index('ix_api_keys_key_prefix', 'api_keys', ['key_prefix'])
    op.create_index('ix_api_keys_is_active', 'api_keys', ['is_active'])


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table('api_keys')
    op.drop_table('usage_daily')
    op.drop_table('audit_log')
