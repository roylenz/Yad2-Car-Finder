"""Licence plate per ad.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ads") as batch:
        batch.add_column(sa.Column("plate", sa.String(8)))
        batch.add_column(sa.Column("plate_status", sa.String(16)))
        batch.create_index("ix_ads_plate", ["plate"])


def downgrade() -> None:
    with op.batch_alter_table("ads") as batch:
        batch.drop_index("ix_ads_plate")
        batch.drop_column("plate_status")
        batch.drop_column("plate")
