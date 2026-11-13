"""The seller's name per ad, as shown with the number.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ads", sa.Column("seller_name", sa.Text))


def downgrade() -> None:
    op.execute("ALTER TABLE ads DROP COLUMN seller_name")
