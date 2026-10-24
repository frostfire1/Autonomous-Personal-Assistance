"""Spending categories. The debate must pick from the user's own list, because a free-text category
(the live model answered `food_beverage`, `coffee`, `Food & Beverage` for one coffee) matches no budget
row, so the spend silently never reaches a budget and merchant history fragments."""

import re
from collections.abc import Sequence

# Used only for months with no budget_rules rows. Real budgets define the user's own categories.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "Food & Dining",
    "Groceries",
    "Transport",
    "Shopping",
    "Bills & Utilities",
    "Entertainment",
    "Social",
    "Health",
    "Tech & Lab",
    "Education",
    "Other",
)


def _key(s: str) -> str:
    """'Food & Dining', 'food_dining', ' FOOD and dining ' all become 'food dining'."""
    words = re.sub(r"[^a-z0-9]+", " ", s.casefold().replace("&", " ")).split()
    return " ".join(w for w in words if w != "and")


def canonicalize(raw: str | None, allowed: Sequence[str]) -> str | None:
    """Map a model's category onto the allowed list, ignoring case, punctuation and '&' vs 'and'.

    Returns the allowed spelling, or None when it names no allowed category. It never guesses by
    similarity: `food_beverage` is not `Food & Dining`, and a wrong guess would charge the wrong
    budget. An unknown category simply does not vote for one.
    """
    if not raw or not raw.strip():
        return None
    k = _key(raw)
    return next((a for a in allowed if _key(a) == k), None) if k else None
