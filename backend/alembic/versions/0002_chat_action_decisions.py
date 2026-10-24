"""audit decision types for owner-confirmed chat actions

Revision ID: 0002
Revises: 0001

Adds 'edit' (a confirmed transaction edit) and 'budget_set' (a confirmed budget change) to the
explainability_log.decision_type check. The log stays append-only: only the CHECK is replaced.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

NAME = "ck_explainability_log_decision_type_valid"
BASE = ("ledger_commit", "quarantine", "undo", "release", "dismiss", "fusion", "budget_rebalance",
        "health_interlock", "reconciliation", "config_change")
NEW = (*BASE, "edit", "budget_set")


def _check(types: tuple[str, ...]) -> str:
    return "decision_type IN (" + ",".join(f"'{t}'" for t in types) + ")"


def upgrade() -> None:
    op.execute(f"ALTER TABLE explainability_log DROP CONSTRAINT {NAME}")
    op.execute(f"ALTER TABLE explainability_log ADD CONSTRAINT {NAME} CHECK ({_check(NEW)})")


def downgrade() -> None:
    op.execute(f"ALTER TABLE explainability_log DROP CONSTRAINT {NAME}")
    op.execute(f"ALTER TABLE explainability_log ADD CONSTRAINT {NAME} CHECK ({_check(BASE)})")
