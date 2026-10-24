"""transactions.batch_id: entries the owner confirmed together from one chat draft

Revision ID: 0003
Revises: 0002

The velocity breaker counts a batch once (see pipeline/commit.py `gather`). Idempotent, because a database
created at 0001 from the current models already has the column.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS batch_id uuid")
    op.execute("CREATE INDEX IF NOT EXISTS ix_transactions_batch_id ON transactions (batch_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_transactions_batch_id")
    op.execute("ALTER TABLE transactions DROP COLUMN IF EXISTS batch_id")
