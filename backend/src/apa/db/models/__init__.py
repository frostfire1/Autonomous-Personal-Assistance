"""Import every model module so Base.metadata is complete (Alembic autogenerate, tests)."""

from apa.db.models import audit, budget, evidence, health, intel, ledger, ops  # noqa: F401
from apa.db.models.audit import AgentDebateTranscript, ExplainabilityLog, ReconciliationLog
from apa.db.models.budget import (
    BudgetRebalanceLog,
    BudgetRule,
    CommodityPrice,
    MacroAssumption,
    MerchantCache,
    PolicyRule,
    PriceBasket,
    SavingsGoal,
)
from apa.db.models.evidence import EvidenceBundle, EvidenceFile
from apa.db.models.intel import AppUsage, MemoryItem, ProfileTrait
from apa.db.models.ledger import (
    AssetWarranty,
    CashWallet,
    Transaction,
    TransactionItem,
    TransactionSplit,
)
from apa.db.models.ops import InboundEvent, OAuthToken, TelegramMessage

__all__ = [
    "AppUsage",
    "MemoryItem",
    "ProfileTrait",
    "AgentDebateTranscript",
    "AssetWarranty",
    "BudgetRebalanceLog",
    "BudgetRule",
    "CashWallet",
    "CommodityPrice",
    "EvidenceBundle",
    "EvidenceFile",
    "ExplainabilityLog",
    "InboundEvent",
    "MacroAssumption",
    "MerchantCache",
    "OAuthToken",
    "PolicyRule",
    "PriceBasket",
    "ReconciliationLog",
    "SavingsGoal",
    "TelegramMessage",
    "Transaction",
    "TransactionItem",
    "TransactionSplit",
]
