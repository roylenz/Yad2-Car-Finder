"""Live status of the background pipeline.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "background_status",
        sa.Column("worker", sa.String(32), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("done", sa.Integer),
        sa.Column("total", sa.Integer),
        sa.Column("started_at", sa.DateTime),
        sa.Column("next_run_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("background_status")
