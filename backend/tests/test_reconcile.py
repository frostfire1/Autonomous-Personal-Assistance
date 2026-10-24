"""A-11 pure logic: the three matching tiers, the arithmetic gate on a statement, and caption rules."""

import json
from datetime import date
from decimal import Decimal

import pytest

from apa.ingestion.llm_extract import LLMUnavailable
from apa.integrations.llm import LLMResult
from apa.reconcile.apply import clean_name
from apa.reconcile.intake import looks_like_statement, wants_full_import
from apa.reconcile.match import MAX_CATCHUP, Entry, Line, is_fee_line, plan
from apa.reconcile.statement import (
    StatementRead,
    StatementRefused,
    check_arithmetic,
    read_statement,
    render_pdf,
    wallet_for,
)

D = Decimal


def line(day, amount, description="BELI KOPI", direction="debit"):
    return Line(date(2026, 10, day), description, D(amount), direction)


def entry(id_, day, total, direction="out", merchant="kopi"):
    return Entry(id_, date(2026, 10, day), D(total), direction, merchant)


# ---- tier 1: exact ---------------------------------------------------------------------------------------------------


def test_the_same_amount_within_two_days_is_an_exact_match():
    p = plan([line(10, 18000)], [entry(1, 10, 18000)])
    (m,) = p.matched
    assert (m.entry_id, m.tier, m.kind, m.date_diff) == (1, 1, "exact", 0)
    assert not p.catchup and not p.fees


@pytest.mark.parametrize(("ledger_day", "matches"), [(8, True), (12, True), (13, False), (7, False)])
def test_the_clearing_tolerance_is_exactly_two_days(ledger_day, matches):
    p = plan([line(10, 18000)], [entry(1, ledger_day, 18000)])
    assert bool(p.matched) is matches and bool(p.catchup) is not matches


def test_a_different_amount_or_direction_never_matches():
    assert not plan([line(10, 18000)], [entry(1, 10, 18001)]).matched
    assert not plan([line(10, 18000, direction="credit")], [entry(1, 10, 18000, direction="out")]).matched


def test_matching_is_one_to_one_so_one_ledger_entry_cannot_cover_two_statement_lines():
    p = plan([line(10, 18000), line(10, 18000)], [entry(1, 10, 18000)])
    assert len(p.matched) == 1 and len(p.catchup) == 1  # the second identical payment was missed by the ledger


def test_the_closest_date_wins_and_ties_go_to_the_earlier_entry():
    p = plan([line(10, 18000)], [entry(7, 12, 18000), entry(3, 9, 18000), entry(5, 9, 18000)])
    assert p.matched[0].entry_id == 3 and p.matched[0].date_diff == 1


# ---- tier 2: fees -----------------------------------------------------------------------------------------------------


def test_a_known_fee_folded_into_a_payment_is_split_out():
    p = plan([line(10, 22500, "TRF BI-FAST")], [entry(1, 10, 20000)])
    (m,) = p.matched
    assert (m.tier, m.kind, m.fee) == (2, "fuzzy_fee", D(2500)) and p.folded_fees == [m]
    assert not p.catchup


def test_an_unknown_difference_is_not_taken_for_a_fee():
    p = plan([line(10, 20777)], [entry(1, 10, 20000)])
    assert not p.matched and len(p.catchup) == 1  # 777 is not a known fee: a different payment


def test_a_separate_small_fee_line_next_to_a_matched_payment_becomes_a_fee_entry():
    p = plan([line(10, 20000, "TRF KE BUDI"), line(10, 2500, "BIAYA ADMIN BI-FAST")], [entry(1, 10, 20000)])
    assert len(p.matched) == 1 and len(p.fees) == 1
    assert p.fees[0].near_entry_id == 1 and p.fees[0].line.amount == D(2500) and not p.catchup


def test_a_fee_that_is_already_in_the_ledger_matches_exactly_instead_of_being_added_again():
    p = plan([line(10, 2500, "BIAYA ADMIN")], [entry(9, 10, 2500, merchant="Biaya bank")])
    assert len(p.matched) == 1 and not p.fees  # a second run does not duplicate the fee


def test_a_large_payment_with_a_fee_word_is_a_payment_not_a_fee():
    big = line(10, 900_000, "PEMBAYARAN BIAYA SEKOLAH")
    assert not is_fee_line(big)
    assert len(plan([big], []).catchup) == 1


@pytest.mark.parametrize("d", ["BIAYA ADMIN", "Admin fee", "PAJAK BUNGA", "biaya materai", "BIAYA LAYANAN"])
def test_fee_wording(d):
    assert is_fee_line(line(10, 2500, d))


def test_a_credit_is_never_a_fee():
    assert not is_fee_line(line(10, 2500, "BUNGA TABUNGAN", direction="credit"))


# ---- tier 3: catch-up and the safety stop --------------------------------------------------------------------------------


def test_a_movement_the_ledger_never_heard_about_becomes_a_catchup_in_either_direction():
    p = plan([line(10, 50000, "TRF DARI ANDI", direction="credit"), line(11, 30000, "WARUNG")], [])
    assert [(ln.direction, ln.amount) for ln in p.catchup] == [("credit", D(50000)), ("debit", D(30000))] or len(p.catchup) == 2
    assert not p.suspicious  # two lines: too few to call a run suspicious


def test_a_statement_that_mostly_does_not_match_is_not_injected():
    lines = [line(d, 10000 * d, f"X{d}") for d in range(1, 7)]
    p = plan(lines, [entry(1, 1, 10000)])  # 1 of 6 matches: 5 of 6 unmatched (83%)
    assert p.suspicious and p.catchup == [] and len(p.unmatched) == 5


def test_the_owner_saying_import_everything_lifts_the_stop():
    lines = [line(d, 10000 * d, f"X{d}") for d in range(1, 7)]
    p = plan(lines, [], allow_inject=True)
    assert not p.suspicious and len(p.catchup) == 6


def test_a_mostly_matching_statement_with_a_few_misses_is_injected():
    lines = [line(d, 10000 * d, f"X{d}") for d in range(1, 11)]
    p = plan(lines, [entry(d, d, 10000 * d) for d in range(1, 9)])  # 8 of 10 match
    assert not p.suspicious and len(p.catchup) == 2


def test_one_run_never_injects_more_than_the_cap():
    lines = [line(1 + i % 28, 1000 + i, f"Y{i}") for i in range(MAX_CATCHUP + 10)]
    p = plan(lines, [], allow_inject=True)
    assert MAX_CATCHUP == 40  # the literal, so loosening the constant fails here
    assert len(p.catchup) == 40 and len(p.unmatched) == 10


# ---- the arithmetic gate ----------------------------------------------------------------------------------------------------


def read(**kw):
    base = dict(bank="BCA", opening_balance=D(100000), closing_balance=D(70000),
                lines=[
                    {"date": "2026-10-01", "description": "A", "amount": 50000, "direction": "debit", "balance": 50000},
                    {"date": "2026-10-02", "description": "B", "amount": 20000, "direction": "credit", "balance": 70000},
                ])
    return StatementRead.model_validate({**base, **kw})


def test_a_statement_that_adds_up_passes():
    assert check_arithmetic(read()) is None


def test_opening_plus_movements_must_equal_closing_exactly():
    assert "saldo akhir" in check_arithmetic(read(closing_balance=D(70001)))


def test_a_running_balance_that_does_not_follow_is_refused():
    bad = read(lines=[{"date": "2026-10-01", "description": "A", "amount": 50000, "direction": "debit", "balance": 49999},
                      {"date": "2026-10-02", "description": "B", "amount": 20000, "direction": "credit", "balance": 69999}])
    assert "saldo berjalan" in check_arithmetic(bad)


def test_a_statement_with_no_balances_at_all_cannot_be_checked_and_is_refused():
    r = StatementRead.model_validate({"lines": [
        {"date": "2026-10-01", "description": "A", "amount": 5, "direction": "debit"}]})
    assert "tidak ada saldo" in check_arithmetic(r)


@pytest.mark.parametrize("bank,wallet", [("Bank Central Asia (BCA)", "bank_bca"), ("PT Bank Mandiri", "bank_mandiri"),
                                         ("Bank Jago", "bank_jago"), ("Some New Bank", "bank_some_new_bank"),
                                         (None, "bank_unknown"), ("", "bank_unknown")])
def test_the_wallet_comes_from_the_bank_and_never_collides_with_a_cash_wallet(bank, wallet):
    w = wallet_for(bank)
    assert w == wallet and w != "cash_wallet" and len(w) <= 24


@pytest.mark.parametrize(
    "bad",
    [{"amount": -5}, {"amount": 0}, {"direction": "sideways"}, {"date": "not a date"}, {"amount": 10**15}],
)
def test_implausible_statement_lines_are_rejected_by_the_schema(bad):
    line_ = {"date": "2026-10-01", "description": "A", "amount": 5, "direction": "debit", **bad}
    with pytest.raises(ValueError):
        StatementRead.model_validate({"lines": [line_]})


# ---- reading through a scripted vision model -----------------------------------------------------------------------------------


class Vision:
    def __init__(self, *bodies):
        self.bodies, self.calls = list(bodies), 0

    async def complete(self, route, system, user, **kw):
        assert route == "router-vision-ocr" and kw["images"]
        body = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        if isinstance(body, Exception):
            raise body
        return LLMResult(body if isinstance(body, str) else json.dumps(body), "fake", 100, 100, 0)


PAGES = [(b"x", "image/png")]


def stmt(**kw):
    return {"bank": "BCA", "opening_balance": 100000, "closing_balance": 70000, "lines": [
        {"date": "2026-10-01", "description": "BELI KOPI 081234567890", "amount": 50000, "direction": "debit", "balance": 50000},
        {"date": "2026-10-02", "description": "TRF DARI ANDI", "amount": 20000, "direction": "credit", "balance": 70000}], **kw}


async def test_a_clean_statement_is_read_with_its_wallet_and_scrubbed_descriptions():
    s = await read_statement(Vision(stmt()), PAGES)
    assert (s.wallet, s.bank, len(s.lines), s.opening, s.closing) == ("bank_bca", "BCA", 2, D(100000), D(70000))
    assert "[PHONE]" in s.lines[0].description and "0812" not in s.lines[0].description


async def test_a_statement_that_does_not_add_up_is_refused_whole():
    with pytest.raises(StatementRefused, match="saldo"):
        await read_statement(Vision(stmt(closing_balance=1)), PAGES)


@pytest.mark.parametrize("body", ["not json", "{}", '{"lines": []}', '{"lines": "x"}', "[]", ""])
async def test_unusable_model_output_is_refused_not_guessed(body):
    with pytest.raises(StatementRefused):
        await read_statement(Vision(body), PAGES)


async def test_a_fenced_answer_is_accepted():
    s = await read_statement(Vision("```json\n" + json.dumps(stmt()) + "\n```"), PAGES)
    assert len(s.lines) == 2


async def test_pages_are_read_a_few_at_a_time_and_merged_with_the_boundary_line_counted_once():
    first = {"bank": "BCA", "opening_balance": 100000, "closing_balance": None, "lines": stmt()["lines"][:1]}
    second = {"bank": None, "opening_balance": None, "closing_balance": 70000, "lines": stmt()["lines"]}  # repeats line 1
    v = Vision(first, second)
    s = await read_statement(v, [(b"p", "image/png")] * 4)  # 4 pages = 2 calls of 3 + 1
    assert v.calls == 2 and len(s.lines) == 2 and s.closing == D(70000)


async def test_a_model_outage_is_reported_as_unavailable_not_as_a_bad_statement():
    with pytest.raises(LLMUnavailable):
        await read_statement(Vision(RuntimeError("down")), PAGES)


# ---- files ----------------------------------------------------------------------------------------------------------------------


def make_pdf(pages=1):
    import io

    from PIL import Image

    imgs = [Image.new("RGB", (400, 600), "white") for _ in range(pages)]
    b = io.BytesIO()
    imgs[0].save(b, format="PDF", save_all=True, append_images=imgs[1:])
    return b.getvalue()


def test_a_pdf_is_rendered_one_png_per_page():
    pages = render_pdf(make_pdf(3))
    assert len(pages) == 3 and all(m == "image/png" and b[:4] == b"\x89PNG" for b, m in pages)


@pytest.mark.parametrize("junk", [b"", b"not a pdf", b"%PDF-1.4 truncated", b"\x00" * 100])
def test_a_file_that_is_not_a_pdf_is_refused(junk):
    with pytest.raises(StatementRefused):
        render_pdf(junk)


def test_a_pdf_with_too_many_pages_is_refused():
    with pytest.raises(StatementRefused, match="terlalu panjang"):
        render_pdf(make_pdf(4), max_pages=3)


# ---- captions and names ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("c,yes", [("mutasi bca oktober", True), ("Rekening Koran", True), ("e-statement", True),
                                   ("statement BNI", True), ("struk warteg", False), ("", False), (None, False)])
def test_a_photo_caption_decides_receipt_or_statement(c, yes):
    assert looks_like_statement(c) is yes


@pytest.mark.parametrize("c,yes", [("mutasi, catat semua", True), ("IMPORT", True), ("mutasi bca", False), (None, False)])
def test_catat_semua_is_the_only_way_to_lift_the_safety_stop(c, yes):
    assert wants_full_import(c) is yes


@pytest.mark.parametrize("raw,clean", [("TRSF E-BANKING DB 2310/FTSCY/WS95031 KOPI KENANGAN", "KOPI KENANGAN"),
                                       ("QRIS WARUNG BU SITI", "WARUNG BU SITI"), ("12345", "Mutasi bank")])
def test_a_counterparty_name_is_read_out_of_bank_noise(raw, clean):
    assert clean_name(raw) == clean
