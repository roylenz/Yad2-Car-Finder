"""Add past photo swaps to the ad history (the overview's recent changes), which only records
them from now on: each time a seller's photos were replaced becomes a "photos" change.

Revision ID: 0013
Revises: 0012
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    swaps = db.execute(
        sa.text(
            "SELECT token, removed_from_ad_at AS at, COUNT(*) AS removed FROM photos "
            "WHERE removed_from_ad_at IS NOT NULL GROUP BY token, removed_from_ad_at"
        )
    ).all()
    for swap in swaps:
        added = db.execute(
            sa.text("SELECT COUNT(*) FROM photos WHERE token = :token AND first_seen_at = :at"),
            {"token": swap.token, "at": swap.at},
        ).scalar()
        ad = db.execute(sa.text("SELECT price, km FROM ads WHERE token = :token"), {"token": swap.token}).first()
        if ad is None:
            continue
        db.execute(
            sa.text(
                "INSERT INTO ad_snapshots (token, observed_at, price, km, changes) "
                "VALUES (:token, :at, :price, :km, :changes)"
            ),
            {
                "token": swap.token, "at": swap.at, "price": ad.price, "km": ad.km,
                "changes": json.dumps({"photos": [swap.removed, added]}),
            },
        )


def downgrade() -> None:
    pass
