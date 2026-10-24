"""Structural checks on the model metadata and migration (no database required)."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import apa.db.models  # noqa: F401
from apa.db.base import Base

EXPECTED = {
    # blueprint 5B
    "huawei_health_metrics", "sleep_sessions", "stress_readings", "workout_activities",
    "body_composition_logs", "meal_logs", "biomarker_metrics", "supplement_regimen",
    "health_warnings", "transaction_evidence_bundles", "transaction_evidence_files",
    "transaction_splits", "asset_warranties", "transaction_items", "cash_wallets", "transactions",
    # decisions.md section 4 + commodity_prices
    "explainability_log", "agent_debate_transcripts", "policy_rules", "merchant_cache",
    "budget_rules", "budget_rebalance_log", "price_basket", "commodity_prices",
    "macro_assumptions", "reconciliation_log",
    # decisions.md section 5
    "inbound_events", "telegram_messages", "oauth_tokens", "news_articles", "cyber_advisories",
    "homelab_watchlist", "research_reports", "savings_goals",
}


def test_all_expected_tables_exist():
    assert EXPECTED <= set(Base.metadata.tables), EXPECTED - set(Base.metadata.tables)


def test_every_table_compiles_for_postgres():
    for table in Base.metadata.sorted_tables:
        CreateTable(table).compile(dialect=postgresql.dialect())


def test_circular_fk_is_deferred_not_a_cycle():
    # transactions <-> evidence bundles must not break topological sorting
    names = [t.name for t in Base.metadata.sorted_tables]
    assert "transactions" in names and "transaction_evidence_bundles" in names


def test_transactions_has_soft_reversal_columns():
    cols = Base.metadata.tables["transactions"].c
    for c in ("status", "debate_id", "rolled_back_at", "rolled_back_via", "inbound_event_id"):
        assert c in cols


def test_child_tables_cascade_from_transactions():
    for t in ("transaction_items", "transaction_splits"):
        fk = next(iter(Base.metadata.tables[t].c.transaction_id.foreign_keys))
        assert fk.ondelete == "CASCADE"
