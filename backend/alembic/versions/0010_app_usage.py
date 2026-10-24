"""app_usage: how long each app was used per day on each device (from ActivityWatch or anything that can push the same shape)

Revision ID: 0010
Revises: 0009

One row per day, device and app: only the app's name and the seconds it was in use (never window titles, URLs or document
names). The profile uses it for screen time, the busiest apps and when the owner is up late. Idempotent.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS app_usage (
            id BIGSERIAL PRIMARY KEY,
            day DATE NOT NULL,
            device VARCHAR(40) NOT NULL,
            kind VARCHAR(12) NOT NULL DEFAULT 'other',
            app VARCHAR(120) NOT NULL,
            seconds INTEGER NOT NULL,
            first_at TIMESTAMPTZ,
            last_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_app_usage UNIQUE (day, device, app)
        )"""
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_app_usage_day ON app_usage (day)")
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'apa_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON app_usage TO apa_app;
                GRANT USAGE, SELECT ON SEQUENCE app_usage_id_seq TO apa_app;
            END IF;
        END $$"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_usage")
