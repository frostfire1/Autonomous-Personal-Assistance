"""briefings: the language each one was written in

Revision ID: 0008
Revises: 0007

Idempotent: a database created at 0001 from the current models already has the column.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE briefings ADD COLUMN IF NOT EXISTS lang VARCHAR(2) NOT NULL DEFAULT 'id'")


def downgrade() -> None:
    op.execute("ALTER TABLE briefings DROP COLUMN IF EXISTS lang")
