"""Look up seller contacts again for ads that got none: dealer ads were skipped until their
"יצירת קשר" box was read too.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE ads SET seller_phone_attempts = 0 WHERE seller_phone IS NULL")


def downgrade() -> None:
    pass
