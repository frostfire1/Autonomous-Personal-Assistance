"""A-11 matching (blueprint 4.6): the ledger against a bank statement, in three tiers. Pure code, no database.

  Tier 1  exact: same wallet and direction, same amount, date within +-2 days (bank clearing), one-to-one.
  Tier 2  fee: the statement shows a known bank fee that the ledger does not, either as its own small line next to
          a matched payment, or folded into a payment (statement = ledger + a known fee). The fee is split out as
          its own "Bank Fees" entry.
  Tier 3  catch-up: a legitimate statement line the phone listener and the manual entry both missed becomes a
          synthetic transaction flagged reconciliation_catchup.

Safety: a statement whose lines mostly do not match the ledger is not injected (it is probably the wrong wallet or
period); a hard cap limits one run; every decision keeps its reason."""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

DATE_TOLERANCE_DAYS = 2
KNOWN_FEES = (Decimal(1000), Decimal(2500), Decimal(3000), Decimal(5000), Decimal(6500), Decimal(7500))
MAX_FEE_LINE = Decimal(50_000)  # a "fee" larger than this is a real payment with a rude description
MAX_CATCHUP = 40
SUSPICIOUS_UNMATCHED_SHARE = Decimal("0.6")
SUSPICIOUS_MIN_LINES = 5
_FEE_WORDS = re.compile(r"(?i)\b(biaya|admin|adm|fee|pajak|bunga|materai|biaya layanan|provisi)\b")


@dataclass(frozen=True)
class Line:
    on: date
    description: str
    amount: Decimal  # always positive
    direction: Literal["debit", "credit"]  # debit = money out


@dataclass(frozen=True)
class Entry:
    id: int
    on: date  # the ledger transaction's local date
    total: Decimal
    direction: Literal["out", "in"]
    merchant: str | None = None


@dataclass(frozen=True)
class Matched:
    line: Line
    entry_id: int
    tier: int
    kind: Literal["exact", "fuzzy_fee"]
    date_diff: int
    fee: Decimal | None = None  # tier 2 folded fee: amount to record as its own entry


@dataclass(frozen=True)
class Fee:
    line: Line  # the statement's own fee line
    near_entry_id: int | None


@dataclass
class Plan:
    matched: list[Matched] = field(default_factory=list)
    fees: list[Fee] = field(default_factory=list)
    folded_fees: list[Matched] = field(default_factory=list)
    catchup: list[Line] = field(default_factory=list)
    unmatched: list[Line] = field(default_factory=list)  # not injected (suspicious run, cap, or not allowed)
    suspicious: bool = False

    @property
    def injected(self) -> int:
        return len(self.catchup)


def is_fee_line(line: Line) -> bool:
    return (
        line.direction == "debit"
        and line.amount <= MAX_FEE_LINE
        and bool(_FEE_WORDS.search(line.description))
    )


def _direction(line: Line) -> str:
    return "out" if line.direction == "debit" else "in"


def _pick(line: Line, entries: list[Entry], taken: set[int], amount: Decimal) -> tuple[Entry, int] | None:
    best: tuple[Entry, int] | None = None
    for e in entries:
        if e.id in taken or e.direction != _direction(line) or e.total != amount:
            continue
        diff = abs((e.on - line.on).days)
        if diff <= DATE_TOLERANCE_DAYS and (
            best is None or diff < best[1] or (diff == best[1] and e.id < best[0].id)
        ):
            best = (e, diff)
    return best


def plan(lines: list[Line], ledger: list[Entry], *, allow_inject: bool = False) -> Plan:
    """`allow_inject` lifts the "mostly unmatched" safety stop (the owner said to import everything)."""
    p, taken = Plan(), set[int]()
    rest: list[Line] = []

    # tier 1: exact, one to one, payments before fee-looking lines so a real payment is never mistaken for a fee
    for line in sorted(lines, key=lambda ln: (is_fee_line(ln), ln.on, -ln.amount)):
        hit = _pick(line, ledger, taken, line.amount)
        if hit:
            taken.add(hit[0].id)
            p.matched.append(Matched(line, hit[0].id, 1, "exact", hit[1]))
        else:
            rest.append(line)

    # tier 2a: the statement amount folds a known fee into a ledger payment
    still: list[Line] = []
    for line in rest:
        done = False
        if not is_fee_line(line):
            for fee in KNOWN_FEES:
                if line.amount > fee and (hit := _pick(line, ledger, taken, line.amount - fee)):
                    taken.add(hit[0].id)
                    m = Matched(line, hit[0].id, 2, "fuzzy_fee", hit[1], fee)
                    p.matched.append(m)
                    p.folded_fees.append(m)
                    done = True
                    break
        if not done:
            still.append(line)

    # tier 2b: a small line that looks like a bank fee, next to a payment that did match
    rest2: list[Line] = []
    for line in still:
        if is_fee_line(line):
            near = next((m.entry_id for m in p.matched if abs((m.line.on - line.on).days) <= 1), None)
            p.fees.append(Fee(line, near))
        else:
            rest2.append(line)

    # tier 3: what is left is a real movement the ledger never heard about
    candidates = rest2
    if candidates and not allow_inject:
        share = Decimal(len(candidates)) / Decimal(len(lines)) if lines else Decimal(0)
        if len(lines) >= SUSPICIOUS_MIN_LINES and share > SUSPICIOUS_UNMATCHED_SHARE:
            p.suspicious = True
    if p.suspicious:
        p.unmatched = candidates
    else:
        p.catchup, p.unmatched = candidates[:MAX_CATCHUP], candidates[MAX_CATCHUP:]
    return p
