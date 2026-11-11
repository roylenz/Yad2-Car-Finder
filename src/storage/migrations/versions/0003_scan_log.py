"""Scan log and next scan time per search.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scans",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.Integer, nullable=False),
        sa.Column("search_url", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("progress", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("queued_at", sa.DateTime, nullable=False),
        sa.Column("started_at", sa.DateTime),
        sa.Column("finished_at", sa.DateTime),
        sa.Column("results", sa.Integer),
        sa.Column("new_ads", sa.Integer),
        sa.Column("price_drops", sa.Integer),
        sa.Column("removed", sa.Integer),
        sa.Column("details_fetched", sa.Integer),
    )
    op.create_index("ix_scans_chat_queued", "scans", ["chat_id", "queued_at"])
    with op.batch_alter_table("searches") as batch:
        batch.add_column(sa.Column("next_scan_at", sa.DateTime))


def downgrade() -> None:
    with op.batch_alter_table("searches") as batch:
        batch.drop_column("next_scan_at")
    op.drop_index("ix_scans_chat_queued", "scans")
    op.drop_table("scans")
