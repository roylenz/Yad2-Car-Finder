"""The seller's phone number per ad (Yad2's forwarding number), and lookup attempts.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


# Plain ADD/DROP COLUMN: a batch rebuild of `ads` trips the foreign keys that point at it.
_COLUMNS = ("seller_phone", "seller_phone_checked_at", "seller_phone_attempts")


def upgrade() -> None:
    op.add_column("ads", sa.Column("seller_phone", sa.String(16)))
    op.add_column("ads", sa.Column("seller_phone_checked_at", sa.DateTime))
    op.add_column("ads", sa.Column("seller_phone_attempts", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE ads DROP COLUMN {column}")
