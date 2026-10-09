"""Existence checks so migrations can adopt databases made without them (D-047).

Before migrations ran at startup, the gateway built its schema with
`create_all`, which creates missing tables but never adds columns. A database
from that era can have a later revision's tables and lack an earlier one's
columns. Migrations skip what already exists, so adopting such a database is
"stamp the initial revision, upgrade".
"""

import sqlalchemy as sa
from alembic import op


def has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))
