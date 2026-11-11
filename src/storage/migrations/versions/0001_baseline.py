"""Baseline: the tables the bot created before Alembic (kept as-is on existing databases).

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "searches" not in existing:
        op.create_table(
            "searches",
            sa.Column("chat_id", sa.Integer, primary_key=True),
            sa.Column("url", sa.Text, nullable=False),
            sa.Column("interval_minutes", sa.Integer, nullable=False),
        )
    if "listings" not in existing:
        op.create_table(
            "listings",
            sa.Column("chat_id", sa.Integer, primary_key=True),
            sa.Column("url", sa.Text, primary_key=True),
            sa.Column("title", sa.Text, nullable=False),
            sa.Column("price", sa.Integer),
            sa.Column("km", sa.Integer),
            sa.Column("hand", sa.Integer),
            sa.Column("year", sa.Integer),
        )
    else:
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("listings")}
        for column in ("hand", "year"):
            if column not in columns:
                op.add_column("listings", sa.Column(column, sa.Integer))
    if "access" not in existing:
        op.create_table(
            "access",
            sa.Column("user_id", sa.Integer, primary_key=True),
            sa.Column("allowed", sa.Integer, nullable=False),
        )
    if "admins" not in existing:
        op.create_table("admins", sa.Column("user_id", sa.Integer, primary_key=True))
    if "access_requests" not in existing:
        op.create_table(
            "access_requests",
            sa.Column("user_id", sa.Integer, primary_key=True),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("status", sa.Text, nullable=False),
        )
    if "access_meta" not in existing:
        op.create_table("access_meta", sa.Column("key", sa.Text, primary_key=True))


def downgrade() -> None:
    for table in ("access_meta", "access_requests", "admins", "access", "listings", "searches"):
        op.drop_table(table)
