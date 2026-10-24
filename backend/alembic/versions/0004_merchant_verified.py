"""merchant_cache.last_verified_at: when a debate or the owner last confirmed a merchant's category

Revision ID: 0004
Revises: 0003

Blueprint v2 4.2: the fingerprint cache entry is trusted for 30 days after its last verification, and only a
full debate or an owner correction verifies. Idempotent (a database created at 0001 already has the column).
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE merchant_cache ADD COLUMN IF NOT EXISTS last_verified_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE merchant_cache DROP COLUMN IF EXISTS last_verified_at")
