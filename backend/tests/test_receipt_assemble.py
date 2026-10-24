from decimal import Decimal

import pytest

from apa.ingestion.receipt_assemble import (
    TIP_LINE_NAME,
    MultiRead,
    max_overlap,
    overlap_candidates,
    stitch,
)
from apa.ingestion.receipt_assemble import assemble as assemble_multi
from apa.ingestion.receipt_extract import ReceiptItem

D = Decimal


def it(name, qty, total, unit=None):
    return {"name": name, "quantity": qty, "unit_price": unit if unit is not None else total // qty,
            "total_price": total}


def receipt_page(index, items, **kw):
    return {"index": index, "kind": "receipt", "items": items, **kw}


def slip(index, total, **kw):
    return {"index": index, "kind": "slip", "slip_total": total, "slip_time": "2026-10-23T12:41",
            "card_last4": "4321", "batch": "000123", "slip_kind": "edc", **kw}


def album(images, **kw):
    return MultiRead.model_validate({"is_receipt": True, "merchant": "WARUNG SEDERHANA", "purchased_at": "2026-10-23",
                                     "payment_method": "card", "images": images, "confidence": 0.93, **kw})


def run(images, **kw):
    return assemble_multi(album(images, **kw))


BERAS, MINYAK, TELUR = it("Beras 5kg", 1, 68000), it("Minyak Goreng 2L", 1, 36000), it("Telur 1kg", 1, 29000)
GULA, KOPI, SABUN, TEH = it("Gula 1kg", 1, 17000), it("Kopi Sachet", 10, 15000), it("Sabun", 2, 9000), it("Teh Celup", 1, 12000)
# pages: [BERAS, MINYAK, TELUR, GULA, KOPI] + [GULA, KOPI, SABUN, TEH]: 5 + 4 lines, 2 overlapping.
# distinct items: 68+36+29+17+15+9+12 = 186.000; discount 6.000 -> 180.000
PAGE1 = [BERAS, MINYAK, TELUR, GULA, KOPI]
PAGE2 = [GULA, KOPI, SABUN, TEH]


# ---- stitching ---------------------------------------------------------------------------------------------


def test_two_overlapping_pages_are_stitched_and_every_line_links_to_the_page_it_came_from():
    a = run([receipt_page(0, PAGE1), receipt_page(1, PAGE2, discount=6000, total=180000)])
    assert not isinstance(a, str)
    assert [i["name"] for i in a.items] == ["Beras 5kg", "Minyak Goreng 2L", "Telur 1kg", "Gula 1kg",
                                           "Kopi Sachet", "Sabun", "Teh Celup"]
    assert [i["page"] for i in a.items] == [0, 0, 0, 0, 0, 1, 1]  # the repeated lines are credited to page 0
    assert (a.verification, a.pages, a.final_total) == ("stitched_multi_page", 2, D(180000))
    assert a.roles == ["receipt_primary", "receipt_continuation"]


def test_the_grand_total_comes_from_the_last_page_that_prints_one():
    a = run([receipt_page(0, PAGE1, total=999999), receipt_page(1, PAGE2, discount=6000, total=180000)])
    assert a.final_total == D(180000)  # page 1's number is a running total, not the receipt's


def test_pages_with_no_overlap_are_simply_concatenated():
    a = run([receipt_page(0, [BERAS, MINYAK]), receipt_page(1, [TELUR, GULA], total=150000)])
    assert [i["name"] for i in a.items] == ["Beras 5kg", "Minyak Goreng 2L", "Telur 1kg", "Gula 1kg"]


def test_overlap_matching_ignores_case_and_spacing():
    p2 = [it("  GULA   1KG ", 1, 17000), KOPI, SABUN, TEH]
    a = run([receipt_page(0, PAGE1), receipt_page(1, p2, discount=6000, total=180000)])
    assert len(a.items) == 7


def test_a_genuine_repeat_at_the_page_boundary_is_kept_when_the_total_says_so():
    aqua = it("Aqua 600ml", 1, 3000)
    page1, page2 = [BERAS, aqua], [aqua, MINYAK]
    # two bottles really were bought: 68.000 + 3.000 + 3.000 + 36.000 = 110.000 (no de-duplication)
    two = run([receipt_page(0, page1), receipt_page(1, page2, total=110000)])
    assert [i["name"] for i in two.items].count("Aqua 600ml") == 2
    # one bottle: the repeat is page overlap: 68.000 + 3.000 + 36.000 = 107.000 (de-duplicated)
    one = run([receipt_page(0, page1), receipt_page(1, page2, total=107000)])
    assert [i["name"] for i in one.items].count("Aqua 600ml") == 1


def test_if_no_reading_adds_up_the_album_is_refused():
    assert run([receipt_page(0, PAGE1), receipt_page(1, PAGE2, total=123456)]) == "total_mismatch"


def test_three_pages_chain_their_overlaps():
    a = run([receipt_page(0, [BERAS, MINYAK, TELUR]), receipt_page(1, [MINYAK, TELUR, GULA, KOPI]),
             receipt_page(2, [GULA, KOPI, SABUN, TEH], discount=6000, total=180000)])
    assert [i["name"] for i in a.items] == ["Beras 5kg", "Minyak Goreng 2L", "Telur 1kg", "Gula 1kg",
                                           "Kopi Sachet", "Sabun", "Teh Celup"]
    assert [i["page"] for i in a.items] == [0, 0, 0, 1, 1, 2, 2] and a.pages == 3


def test_max_overlap_and_the_candidates_are_ordered_most_deduplicated_first():
    items = [ReceiptItem.model_validate(i) for i in PAGE1]
    nxt = [ReceiptItem.model_validate(i) for i in PAGE2]
    assert max_overlap(items, nxt) == 2 and max_overlap(nxt, items) == 0
    assert list(overlap_candidates([items, nxt])) == [[2], [1], [0]]
    merged, pages = stitch([items, nxt], [2])
    assert len(merged) == 7 and pages == [0] * 5 + [1] * 2


# ---- dual verification with a payment slip ---------------------------------------------------------------------


def test_a_matching_slip_verifies_the_receipt_and_its_total_is_what_was_charged():
    a = run([receipt_page(0, PAGE1, total=None), receipt_page(1, PAGE2, discount=6000, total=180000), slip(2, 180000)])
    assert (a.verification, a.final_total, a.tip) == ("dual_verified", D(180000), None)
    assert a.roles == ["receipt_primary", "receipt_continuation", "payment_proof_slip"]
    assert a.slip == {"total": "180000", "time": "2026-10-23T12:41", "card_last4": "4321", "batch": "000123", "kind": "edc"}


def test_a_slip_within_rounding_still_verifies_and_the_charged_amount_wins():
    a = run([receipt_page(0, [it("Nasi", 1, 58300)], total=58300), slip(1, 58400)])
    assert (a.verification, a.final_total) == ("dual_verified", D(58400))


def test_a_tip_that_only_the_slip_shows_is_split_out_as_its_own_line():
    page = receipt_page(0, [it("Steak", 1, 90000), it("Es Teh", 2, 10000)], total=100000)
    a = run([page, slip(1, 110000)])
    assert (a.verification, a.tip, a.final_total) == ("split_reconciled", D(10000), D(110000))
    tip = a.items[-1]
    assert (tip["name"], tip["total_price"], tip["page"]) == (TIP_LINE_NAME, "10000", 1)  # linked to the slip
    assert sum(D(i["total_price"]) for i in a.items) == a.final_total  # the lines add up to what was charged


@pytest.mark.parametrize(
    ("slip_total", "why"),
    [(150000, "far more than a tip explains"), (90000, "charged less than the receipt"), (99000, "slightly less")],
)
def test_a_slip_that_disagrees_with_the_receipt_is_refused(slip_total, why):
    page = receipt_page(0, [it("Steak", 1, 90000), it("Es Teh", 2, 10000)], total=100000)
    assert run([page, slip(1, slip_total)]) == "slip_total_mismatch", why


def test_the_tip_allowance_is_25_percent_or_ten_thousand_rupiah_whichever_is_larger():
    big = receipt_page(0, [it("Paket", 1, 200000)], total=200000)
    assert run([big, slip(1, 250000)]).tip == D(50000)  # exactly 25%
    assert run([big, slip(1, 250100)]) == "slip_total_mismatch"
    small = receipt_page(0, [it("Kopi", 1, 20000)], total=20000)
    assert run([small, slip(1, 30000)]).tip == D(10000)  # the Rp 10.000 floor beats 25% of 20.000
    assert run([small, slip(1, 30100)]) == "slip_total_mismatch"


def test_a_slip_whose_amount_could_not_be_read_is_refused_not_ignored():
    assert run([receipt_page(0, [it("Nasi", 1, 58300)], total=58300), slip(1, None)]) == "slip_unreadable"


def test_stitched_pages_plus_a_slip_are_reported_as_dual_verified():
    a = run([receipt_page(0, PAGE1), receipt_page(1, PAGE2, discount=6000, total=180000), slip(2, 180000)])
    assert a.verification == "dual_verified" and a.pages == 2


# ---- what is refused outright ------------------------------------------------------------------------------------


def test_a_slip_alone_cannot_be_itemised():
    assert run([slip(0, 58300)]) == "no_receipt_page"


def test_photos_that_are_neither_receipt_nor_slip_are_ignored_not_filed_as_evidence():
    a = run([receipt_page(0, [it("Nasi", 1, 58300)], total=58300), {"index": 1, "kind": "other"}])
    assert a.roles == ["receipt_primary", None]
    assert run([{"index": 0, "kind": "other"}, {"index": 1, "kind": "banana"}]) == "no_receipt_page"


def test_a_receipt_without_a_printed_total_is_refused():
    assert run([receipt_page(0, PAGE1), receipt_page(1, PAGE2)]) == "no_total"


def test_too_many_slips_and_too_many_pages_are_refused():
    ok = receipt_page(0, [it("Nasi", 1, 58300)], total=58300)
    assert run([ok, slip(1, 58300), slip(2, 58300)]) == "too_many_slips"
    pages = [receipt_page(i, [it(f"Item {i}", 1, 1000 * (i + 1))], total=10000) for i in range(4)]
    assert run(pages) == "too_many_pages"


def test_the_receipt_fields_are_validated_like_a_single_photo():
    bad = receipt_page(0, [{"name": "x", "quantity": 1, "unit_price": 100, "total_price": 500}], total=500)
    assert run([bad]) == "item_line_mismatch"
    assert run([receipt_page(0, [], total=500)]) == "no_items"


def test_a_single_receipt_photo_with_no_slip_is_single_verified():
    a = run([receipt_page(0, [it("Nasi", 1, 58300)], total=58300)])
    assert (a.verification, a.pages, a.slip, a.tip) == ("single_verified", 1, None, None)


def test_the_schema_rejects_nonsense():
    for bad in ({"images": []}, {"confidence": 2}, {"images": [{"index": -1}]}):
        with pytest.raises(ValueError):
            MultiRead.model_validate({**album([receipt_page(0, [BERAS], total=68000)]).model_dump(), **bad})
