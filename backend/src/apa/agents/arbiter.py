"""A-10 Arbiter: deterministic weighted consensus (formula id `weighted_v1`).

The LLM only phrases the Telegram summary; the commit decision is this arithmetic so it is
reproducible and auditable from explainability_log.agent_scores.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

FORMULA_ID = "weighted_v1"

# Domain weights; A-02 (CFO) has the heaviest say on money, A-03 on health items.
DEFAULT_WEIGHTS: dict[str, Decimal] = {
    "A-02": Decimal("0.30"),
    "A-03": Decimal("0.25"),
    "A-05": Decimal("0.15"),
    "A-04": Decimal("0.15"),
    "A-07": Decimal("0.15"),
}

# Below this weighted approval the entry is still recorded (zero-manual policy) but flagged.
FLAG_THRESHOLD = Decimal("0.40")


@dataclass(frozen=True)
class AgentVote:
    agent_id: str
    approve_score: Decimal  # 0..1, higher = more in favour of the proposed classification
    confidence: Decimal  # 0..1
    category: str | None = None  # the category this agent proposes


@dataclass(frozen=True)
class Consensus:
    formula: str
    final_score: Decimal
    category: str | None
    flagged: bool
    scores: dict[str, dict[str, str]] = field(default_factory=dict)
    lead_agent: str | None = None  # most influential agent behind the winning category


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def resolve(
    votes: list[AgentVote],
    proposed_category: str | None,
    weights: dict[str, Decimal] | None = None,
) -> Consensus:
    """Confidence-weighted mean of approve scores; category by weighted plurality.

    Agents that failed to answer are simply absent from `votes`; weights renormalise.
    """
    weights = weights or DEFAULT_WEIGHTS
    if not votes:
        return Consensus(FORMULA_ID, Decimal("0"), proposed_category, True, {})

    num = Decimal("0")
    den = Decimal("0")
    category_mass: dict[str, Decimal] = {}
    influence: dict[str, tuple[Decimal, str]] = {}  # category -> (best weight, agent)
    scores: dict[str, dict[str, str]] = {}
    for v in votes:
        w = weights.get(v.agent_id, Decimal("0.10")) * v.confidence
        num += w * v.approve_score
        den += w
        if v.category:
            category_mass[v.category] = category_mass.get(v.category, Decimal("0")) + w
            if v.category not in influence or w > influence[v.category][0]:
                influence[v.category] = (w, v.agent_id)
        scores[v.agent_id] = {
            "score": str(_q(v.approve_score)),
            "confidence": str(_q(v.confidence)),
            "weight": str(_q(weights.get(v.agent_id, Decimal("0.10")))),
        }

    final = _q(num / den) if den > 0 else Decimal("0")
    category = max(category_mass, key=category_mass.get) if category_mass else proposed_category
    lead = influence[category][1] if category in influence else None
    return Consensus(FORMULA_ID, final, category, final < FLAG_THRESHOLD, scores, lead)
