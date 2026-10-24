"""briefings: composed voice briefings for the Android app

Revision ID: 0007
Revises: 0006

Idempotent: a database created at 0001 from the current models already has the table.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS briefings (
            id BIGSERIAL PRIMARY KEY,
            kind VARCHAR(8) NOT NULL,
            text TEXT NOT NULL,
            sections JSONB NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            played_at TIMESTAMPTZ,
            CONSTRAINT ck_briefings_kind_valid CHECK (kind IN ('short','full'))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS briefings")
