"""audit decision type for the data steward's repairs

Revision ID: 0006
Revises: 0005

Adds 'repair' to explainability_log.decision_type: every action the steward takes is one audited row. The log
stays append-only; only the CHECK is replaced.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

NAME = "ck_explainability_log_decision_type_valid"
BASE = ("ledger_commit", "quarantine", "undo", "release", "dismiss", "fusion", "budget_rebalance",
        "health_interlock", "reconciliation", "config_change", "edit", "budget_set")
NEW = (*BASE, "repair")


def _check(types: tuple[str, ...]) -> str:
    return "decision_type IN (" + ",".join(f"'{t}'" for t in types) + ")"


def upgrade() -> None:
    op.execute(f"ALTER TABLE explainability_log DROP CONSTRAINT {NAME}")
    op.execute(f"ALTER TABLE explainability_log ADD CONSTRAINT {NAME} CHECK ({_check(NEW)})")


def downgrade() -> None:
    op.execute(f"ALTER TABLE explainability_log DROP CONSTRAINT {NAME}")
    op.execute(f"ALTER TABLE explainability_log ADD CONSTRAINT {NAME} CHECK ({_check(BASE)})")
