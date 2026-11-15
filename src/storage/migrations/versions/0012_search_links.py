"""Search names, and extra Yad2 links under a search: a link is a search row pointing at its
parent search.

Each link is scanned on its own (so ads leaving one link's results are tracked per link), and
the dashboard shows a search together with its links.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("searches") as batch:
        batch.add_column(sa.Column("parent_id", sa.Integer))
        batch.add_column(sa.Column("name", sa.Text))
        batch.create_index("ix_searches_parent", ["parent_id"])


def downgrade() -> None:
    with op.batch_alter_table("searches") as batch:
        batch.drop_index("ix_searches_parent")
        batch.drop_column("parent_id")
        batch.drop_column("name")
