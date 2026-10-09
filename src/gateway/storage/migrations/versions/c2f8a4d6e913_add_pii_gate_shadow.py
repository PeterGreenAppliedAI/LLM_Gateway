"""add pii_gate_shadow

Shadow-mode results of the ML PII gate (D-052): categories, probabilities,
counts and timings per request, never text or values.

Revision ID: c2f8a4d6e913
Revises: b7d3e9f14c62
Create Date: 2026-10-09

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from gateway.storage.migrations.guards import has_table


revision: str = "c2f8a4d6e913"
down_revision: Union[str, Sequence[str], None] = "b7d3e9f14c62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create pii_gate_shadow."""
    if has_table("pii_gate_shadow"):
        return
    op.create_table(
        "pii_gate_shadow",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("task", sa.String(32), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("text_chars", sa.Integer(), nullable=False),
        sa.Column("gate_probs", sa.JSON(), nullable=True),
        sa.Column("gate_categories", sa.JSON(), nullable=True),
        sa.Column("gate_error", sa.String(300), nullable=True),
        sa.Column("gate_ms", sa.Float(), nullable=True),
        sa.Column("finder_reason", sa.String(32), nullable=True),
        sa.Column("finder_categories", sa.JSON(), nullable=True),
        sa.Column("finder_hallucinated", sa.Integer(), nullable=True),
        sa.Column("finder_error", sa.String(300), nullable=True),
        sa.Column("finder_ms", sa.Float(), nullable=True),
        sa.Column("regex_types", sa.JSON(), nullable=True),
        sa.Column("gate_missed", sa.Boolean(), nullable=True),
    )
    op.create_index("ix_pii_gate_shadow_timestamp", "pii_gate_shadow", ["timestamp"])
    op.create_index("ix_pii_gate_shadow_request_id", "pii_gate_shadow", ["request_id"])


def downgrade() -> None:
    """Drop pii_gate_shadow."""
    op.drop_table("pii_gate_shadow")
