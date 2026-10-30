"""steward cleanup: close stuck events and unfixable photos so they never alert again

Revision ID: 0011
Revises: 0010

Idempotent: updates existing rows in inbound_events that are stuck or unfixable,
marking them failed and gave_up/reported in payload['steward'].
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Close all stuck inbound events older than 12 hours
    op.execute(
        """UPDATE inbound_events
           SET status = 'failed',
               error = COALESCE(error, 'stuck: closed by steward cleanup'),
               payload = jsonb_set(
                   COALESCE(payload, '{}'::jsonb),
                   '{steward}',
                   COALESCE(payload->'steward', '{}'::jsonb) || '{"gave_up": true, "reported": true}'::jsonb
               )
           WHERE status IN ('received', 'parsed', 'processing', 'extracting', 'reading', 'needs_llm')
             AND received_at < now() - INTERVAL '12 hours'"""
    )
    # 2. Close non-retryable photo failures (e.g. no_receipt_page)
    op.execute(
        """UPDATE inbound_events
           SET payload = jsonb_set(
                   COALESCE(payload, '{}'::jsonb),
                   '{steward}',
                   COALESCE(payload->'steward', '{}'::jsonb) || '{"gave_up": true, "reported": true}'::jsonb
               )
           WHERE source = 'telegram_photo'
             AND (payload->>'review_reason' = 'no_receipt_page'
                  OR payload->>'skip_reason' = 'no_receipt_page')"""
    )


def downgrade() -> None:
    pass
