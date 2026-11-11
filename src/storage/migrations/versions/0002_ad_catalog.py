"""Global ad catalog: ads, history, photos, per-chat alert state and processor results.

Moves the per-chat `listings` rows into `ads` + `chat_ads` and drops `listings`.

Revision ID: 0002
Revises: 0001
"""

import datetime

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ads",
        sa.Column("token", sa.String(32), primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("price", sa.Integer),
        sa.Column("km", sa.Integer),
        sa.Column("hand", sa.Integer),
        sa.Column("year", sa.Integer),
        sa.Column("manufacturer", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("sub_model", sa.Text),
        sa.Column("attributes", sa.JSON),
        sa.Column("raw", sa.JSON),
        sa.Column("listed_at", sa.DateTime),
        sa.Column("first_seen_at", sa.DateTime, nullable=False),
        sa.Column("last_seen_at", sa.DateTime, nullable=False),
        sa.Column("removed_at", sa.DateTime),
        sa.Column("details_fetched_at", sa.DateTime),
    )
    op.create_table(
        "ad_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("token", sa.String(32), sa.ForeignKey("ads.token"), nullable=False),
        sa.Column("observed_at", sa.DateTime, nullable=False),
        sa.Column("price", sa.Integer),
        sa.Column("km", sa.Integer),
        sa.Column("changes", sa.JSON),
    )
    op.create_index("ix_ad_snapshots_token", "ad_snapshots", ["token", "observed_at"])
    op.create_table(
        "photos",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("token", sa.String(32), sa.ForeignKey("ads.token"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("path", sa.Text),
        sa.Column("size_bytes", sa.Integer),
        sa.Column("first_seen_at", sa.DateTime, nullable=False),
        sa.Column("removed_from_ad_at", sa.DateTime),
        sa.Column("download_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("token", "source_url", name="uq_photos_token_source"),
    )
    op.create_index("ix_photos_sha256", "photos", ["sha256"])
    op.create_table(
        "chat_ads",
        sa.Column("chat_id", sa.Integer, primary_key=True),
        sa.Column("token", sa.String(32), sa.ForeignKey("ads.token"), primary_key=True),
        sa.Column("last_price", sa.Integer),
        sa.Column("in_search", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "analysis_results",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("processor", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.JSON),
        sa.Column("error", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "target_type", "target_id", "processor", "version", name="uq_analysis_target_version"
        ),
    )

    bind = op.get_bind()
    # Formatted like SQLAlchemy stores DateTime in SQLite (also valid for Postgres).
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    rows = bind.execute(
        sa.text("SELECT chat_id, url, title, price, km, hand, year FROM listings")
    ).fetchall()
    ads, chat_ads = {}, []
    for chat_id, url, title, price, km, hand, year in rows:
        token = url.rstrip("/").rsplit("/", 1)[-1]
        ads.setdefault(
            token,
            {
                "token": token, "url": url, "title": title, "price": price, "km": km,
                "hand": hand, "year": year, "first_seen_at": now, "last_seen_at": now,
            },
        )
        chat_ads.append(
            {"chat_id": chat_id, "token": token, "last_price": price, "first_seen_at": now}
        )
    if ads:
        bind.execute(
            sa.text(
                "INSERT INTO ads(token,url,title,price,km,hand,year,first_seen_at,last_seen_at) "
                "VALUES (:token,:url,:title,:price,:km,:hand,:year,:first_seen_at,:last_seen_at)"
            ),
            list(ads.values()),
        )
        bind.execute(
            sa.text(
                "INSERT INTO ad_snapshots(token,observed_at,price,km) "
                "VALUES (:token,:first_seen_at,:price,:km)"
            ),
            list(ads.values()),
        )
    if chat_ads:
        bind.execute(
            sa.text(
                "INSERT INTO chat_ads(chat_id,token,last_price,in_search,first_seen_at) "
                "VALUES (:chat_id,:token,:last_price,TRUE,:first_seen_at)"
            ),
            chat_ads,
        )
    op.drop_table("listings")


def downgrade() -> None:
    raise NotImplementedError("The ad catalog cannot be converted back to per-chat listings.")
