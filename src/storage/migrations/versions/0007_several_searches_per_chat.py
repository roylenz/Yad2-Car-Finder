"""Several saved searches per chat.

`searches` gets its own id (it was keyed by chat), `search_ads` tracks which ads are in each
search's results (moved out of `chat_ads.in_search`), and scans point at their search.
`chat_ads` keeps what each chat was told, so an ad found by two searches alerts once.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "searches_new",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.Integer, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("interval_minutes", sa.Integer, nullable=False),
        sa.Column("next_scan_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime),
        sa.Column("added_from", sa.String(16)),
        sa.Column("announced_at", sa.DateTime),
        sa.UniqueConstraint("chat_id", "url", name="uq_searches_chat_url"),
    )
    op.execute(
        "INSERT INTO searches_new (chat_id, url, interval_minutes, next_scan_at, added_from) "
        "SELECT chat_id, url, interval_minutes, next_scan_at, 'telegram' FROM searches"
    )
    op.drop_table("searches")
    op.rename_table("searches_new", "searches")
    op.create_index("ix_searches_chat", "searches", ["chat_id"])

    op.create_table(
        "search_ads",
        sa.Column("search_id", sa.Integer, sa.ForeignKey("searches.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("token", sa.String(32), sa.ForeignKey("ads.token"), primary_key=True),
        sa.Column("in_search", sa.Boolean, nullable=False),
        sa.Column("first_seen_at", sa.DateTime, nullable=False),
    )
    op.execute(
        "INSERT INTO search_ads (search_id, token, in_search, first_seen_at) "
        "SELECT searches.id, chat_ads.token, chat_ads.in_search, chat_ads.first_seen_at "
        "FROM chat_ads JOIN searches ON searches.chat_id = chat_ads.chat_id"
    )
    with op.batch_alter_table("chat_ads") as batch:
        batch.drop_column("in_search")

    with op.batch_alter_table("scans") as batch:
        batch.add_column(sa.Column("search_id", sa.Integer))
        batch.create_index("ix_scans_search", ["search_id", "queued_at"])
    op.execute(
        "UPDATE scans SET search_id = (SELECT searches.id FROM searches "
        "WHERE searches.chat_id = scans.chat_id AND searches.url = scans.search_url)"
    )


def downgrade() -> None:
    with op.batch_alter_table("scans") as batch:
        batch.drop_index("ix_scans_search")
        batch.drop_column("search_id")
    with op.batch_alter_table("chat_ads") as batch:
        batch.add_column(sa.Column("in_search", sa.Boolean, nullable=False, server_default=sa.true()))
    op.drop_table("search_ads")
    op.create_table(
        "searches_old",
        sa.Column("chat_id", sa.Integer, primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("interval_minutes", sa.Integer, nullable=False),
        sa.Column("next_scan_at", sa.DateTime),
    )
    op.execute(
        "INSERT INTO searches_old (chat_id, url, interval_minutes, next_scan_at) "
        "SELECT chat_id, url, interval_minutes, next_scan_at FROM searches "
        "WHERE id IN (SELECT max(id) FROM searches GROUP BY chat_id)"
    )
    op.drop_index("ix_searches_chat", "searches")
    op.drop_table("searches")
    op.rename_table("searches_old", "searches")
