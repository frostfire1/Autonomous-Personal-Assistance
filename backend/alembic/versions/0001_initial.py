"""initial schema (blueprint 5B + docs/decisions.md)

Revision ID: 0001
Revises:

The base tables are created from the model metadata as it stood at this revision, then the
DB-level guarantees that cannot live in models are applied: seed policy rules, the
append-only trigger on explainability_log, and the app-role revokes. Later revisions should
be explicit `op.*` migrations (generate with `alembic revision --autogenerate`).
"""

from alembic import op

import apa.db.models  # noqa: F401
from apa.db.base import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% on % is not allowed: table is append-only', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""

# Blueprint seed values (decisions.md, table 4.3)
POLICY_SEED = [
    ("velocity_max_tx", 5, "count"),
    ("velocity_window_min", 10, "minutes"),
    ("single_tx_ceiling", 2500000, "IDR"),
    ("liquidity_floor", 0, "IDR"),
    ("deviation_sigma", 3, "sigma"),
    ("caffeine_daily_mg", 400, "mg"),
    ("sugar_daily_g", 30, "g"),
    ("sodium_daily_mg", 2000, "mg"),
    ("early_warning_pct", 80, "pct"),
    ("fatigue_sleep_score", 70, "count"),
    ("fatigue_deep_sleep_pct", 15, "pct"),
    ("stress_shield_score", 75, "count"),
    ("stress_shield_hours", 2, "count"),
    ("undo_window_hours", 24, "count"),
]


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")  # gen_random_uuid() on PG < 13 safety
    Base.metadata.create_all(bind)

    op.execute(APPEND_ONLY_FN)
    op.execute(
        "CREATE TRIGGER explainability_log_append_only BEFORE UPDATE OR DELETE "
        "ON explainability_log FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER explainability_log_no_truncate BEFORE TRUNCATE "
        "ON explainability_log FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()"
    )

    # Defense in depth: the runtime role can never mutate the audit tables, trigger or not.
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'apa_app') THEN
            REVOKE UPDATE, DELETE, TRUNCATE ON explainability_log FROM apa_app;
            REVOKE UPDATE, DELETE, TRUNCATE ON agent_debate_transcripts FROM apa_app;
          END IF;
        END $$;
        """
    )

    for key, value, unit in POLICY_SEED:
        op.execute(
            "INSERT INTO policy_rules (rule_key, scope, scope_ref, value_numeric, unit, updated_by) "
            f"VALUES ('{key}', 'global', '', {value}, '{unit}', 'seed')"
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS explainability_log_no_truncate ON explainability_log")
    op.execute("DROP TRIGGER IF EXISTS explainability_log_append_only ON explainability_log")
    Base.metadata.drop_all(op.get_bind())
    op.execute("DROP FUNCTION IF EXISTS forbid_mutation()")
