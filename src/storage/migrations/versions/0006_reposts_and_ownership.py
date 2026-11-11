"""Repost grouping, ownership history and sale follow-up; fill make/model of old ads.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ads") as batch:
        batch.add_column(sa.Column("car_id", sa.String(32)))
        batch.add_column(sa.Column("ownership", sa.JSON))
        batch.add_column(sa.Column("ownership_checked_at", sa.DateTime))
        batch.add_column(sa.Column("sale_status", sa.String(16)))
        batch.add_column(sa.Column("sold_month", sa.String(7)))
        batch.add_column(sa.Column("sold_to", sa.Text))
        batch.create_index("ix_ads_car_id", ["car_id"])
    op.create_table(
        "ad_links",
        sa.Column("token", sa.String(32), sa.ForeignKey("ads.token"), primary_key=True),
        sa.Column("other", sa.String(32), sa.ForeignKey("ads.token"), primary_key=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    _fill_make_and_model()


def _fill_make_and_model() -> None:
    """Ads saved by the old card scraper have only a "<make> <model>" title."""
    from car.catalog import split_title

    connection = op.get_bind()
    known = {
        " ".join(f"{make} {model}".replace("׳", "'").split()).casefold(): (make, model)
        for make, model in connection.execute(
            sa.text("SELECT DISTINCT manufacturer, model FROM ads WHERE manufacturer IS NOT NULL AND model IS NOT NULL")
        )
    }
    rows = connection.execute(
        sa.text("SELECT token, title FROM ads WHERE manufacturer IS NULL OR model IS NULL")
    ).all()
    for token, title in rows:
        split = split_title(title, known)
        if split:
            connection.execute(
                sa.text("UPDATE ads SET manufacturer = :make, model = :model WHERE token = :token"),
                {"make": split[0], "model": split[1], "token": token},
            )


def downgrade() -> None:
    op.drop_table("ad_links")
    with op.batch_alter_table("ads") as batch:
        batch.drop_index("ix_ads_car_id")
        for column in ("sold_to", "sold_month", "sale_status", "ownership_checked_at", "ownership", "car_id"):
            batch.drop_column(column)
