"""Remove Yad2's ranking score from the change history.

`final_score` drifts every time Yad2 re-ranks its feed, so it filled "Recent changes" with
noise. It is no longer recorded as a change; this removes the entries already logged, and
deletes snapshots that recorded nothing else (an empty change would read as "First seen").

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_DROPPED = ("final_score",)


def upgrade() -> None:
    db = op.get_bind()
    snapshots = sa.table(
        "ad_snapshots", sa.column("id", sa.Integer), sa.column("changes", sa.JSON)
    )
    rows = db.execute(
        sa.select(snapshots.c.id, snapshots.c.changes).where(
            sa.or_(*(sa.cast(snapshots.c.changes, sa.Text).like(f'%"{key}"%') for key in _DROPPED))
        )
    ).all()
    for row in rows:
        changes = {key: value for key, value in (row.changes or {}).items() if key not in _DROPPED}
        if changes:
            db.execute(sa.update(snapshots).where(snapshots.c.id == row.id).values(changes=changes))
        else:
            db.execute(sa.delete(snapshots).where(snapshots.c.id == row.id))


def downgrade() -> None:
    pass  # The removed entries were noise; nothing to restore.
