"""telegram_messages.inbound_event_id: which message an answer belongs to

Revision ID: 0005
Revises: 0004

Lets the inspector show "you wrote X, the system answered Y". Idempotent (a database created at 0001 from the
current models already has the column).
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE telegram_messages ADD COLUMN IF NOT EXISTS inbound_event_id bigint "
               "REFERENCES inbound_events(id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_telegram_messages_inbound_event_id "
               "ON telegram_messages (inbound_event_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_telegram_messages_inbound_event_id")
    op.execute("ALTER TABLE telegram_messages DROP COLUMN IF EXISTS inbound_event_id")
