"""pii_gate_shadow.scrubbed_categories

Which categories had their values replaced in stored copies under the
per-category ML PII policy (D-052).

Revision ID: d5a1c3e7f209
Revises: c2f8a4d6e913
Create Date: 2026-10-09

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from gateway.storage.migrations.guards import has_column


revision: str = "d5a1c3e7f209"
down_revision: Union[str, Sequence[str], None] = "c2f8a4d6e913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not has_column("pii_gate_shadow", "scrubbed_categories"):
        op.add_column("pii_gate_shadow", sa.Column("scrubbed_categories", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("pii_gate_shadow", "scrubbed_categories")
