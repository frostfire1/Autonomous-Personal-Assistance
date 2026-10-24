"""The interactive menu and buttons against real Postgres: views, button taps, and the buttons a
summary or alert message carries."""

from datetime import datetime
from decimal import Decimal

import pytest

from apa.bot.menu import BOT_COMMANDS, MAIN, handle_callback, menu_key, view_for
from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import Transaction
from apa.db.models.ops import TelegramMessage
from apa.integrations.notify import buttons_for
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import TZ, FakeNotifier, commit, new_event, parsed, rows, tx_for

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")


class FakeArq:
    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


def now():
    return datetime.now(TZ)


async def committed(stack, p=None, notifier=None):
    eid = await new_event(stack, p or parsed(), at=now())
    assert await commit(stack, eid, None, notifier) == "committed"
    return await tx_for(stack, eid)


async def held(stack):
    eid = await new_event(stack, parsed(amount="3000000"), at=now())
    assert await commit(stack, eid) == "quarantined"
    return await tx_for(stack, eid)


def labels(rows_):
    return [label for row in rows_ for label, _ in row]


# ---- pure pieces -------------------------------------------------------------------------------


def test_every_main_menu_button_leads_somewhere_and_every_command_has_a_view():
    keys = {data.split(":")[1] for row in MAIN for _, data in row}
    assert keys == {"today", "recent", "held", "budget", "unlock", "help", "log"}
    for name, _ in BOT_COMMANDS:
        assert menu_key(f"/{name}") or name in ("unlock", "private", "selesai")  # commands, not views


@pytest.mark.parametrize("text,key", [("/start", "menu"), ("/menu@MyBot", "menu"), ("/hari_ini", "today"),
                                      ("/Help", "help"), ("/undo_8f9a2b", None), ("kopi", None), ("/x", None)])
def test_menu_key(text, key):
    assert menu_key(text) == key


def test_a_summary_gets_an_undo_button_and_a_held_alert_gets_release_and_dismiss():
    assert buttons_for("✅ Dicatat: X · Rp 1 · Undo: /undo_c32109") == [[("↩️ Undo", "c:undo:c32109")]]
    held_text = "⚠️ ditahan · /release_0a1b2c untuk melepas · /dismiss_0a1b2c untuk membuang"
    assert buttons_for(held_text) == [[("✅ Lepas", "c:release:0a1b2c"), ("🗑️ Buang", "c:dismiss:0a1b2c")]]
    assert ("🔓 Cabut lockdown", "m:unlock") in buttons_for("🚨 LOCKDOWN · /unlock untuk mencabut")[-1]
    assert buttons_for("plain text with /undo_ and no token") == []


def test_every_callback_payload_fits_telegrams_64_byte_limit():
    assert len("c:dismiss:0a1b2c") < 64 and len(f"t:release:{2**63}") < 64


# ---- views -------------------------------------------------------------------------------------


async def test_empty_views_say_so_and_offer_a_way_back(stack):
    for key in ("today", "recent", "held", "budget"):
        v = await view_for(stack, key, TZ)
        assert "⬅️ Menu" in labels(v.rows) and v.replace, key
    assert (await view_for(stack, "menu", TZ)).rows == MAIN


async def test_today_sums_committed_spending_by_category(stack):
    await committed(stack, parsed(amount="18000"))
    await committed(stack, parsed(amount="30000", merchant="WARTEG"))
    text = (await view_for(stack, "today", TZ)).text
    assert "2 transaksi" in text and "Rp 48.000" in text and "Food & Dining" in text


async def test_recent_lists_committed_transactions_with_an_undo_button_each_and_hides_held_ones(stack):
    tx = await committed(stack)
    await held(stack)
    v = await view_for(stack, "recent", TZ)
    assert f"t:undo:{tx.id}" in [d for row in v.rows for _, d in row]
    assert "3.000.000" not in v.text and "KOPI KENANGAN" in v.text


async def test_held_lists_only_quarantined_with_release_and_dismiss(stack):
    await committed(stack)
    h = await held(stack)
    v = await view_for(stack, "held", TZ)
    payloads = [d for row in v.rows for _, d in row]
    assert f"t:release:{h.id}" in payloads and f"t:dismiss:{h.id}" in payloads
    assert "Rp 18.000" not in v.text


async def test_budget_view_flags_overspending(stack):
    month = now().date().replace(day=1)
    async with stack() as s:
        s.add(BudgetRule(category="Food & Dining", period_month=month, base_limit=Decimal(100_000),
                         current_limit=Decimal(100_000), spent_amount=Decimal(85_000)))
        await s.commit()
    text = (await view_for(stack, "budget", TZ)).text
    assert "🟡 Food & Dining" in text and "85%" in text


# ---- button taps -------------------------------------------------------------------------------


async def test_tapping_undo_on_a_summary_reverses_it_and_replies_in_a_new_message(stack):
    n = FakeNotifier()
    tx = await committed(stack, notifier=n)
    (tm,) = await rows(stack, TelegramMessage, TelegramMessage.transaction_id == tx.id, TelegramMessage.kind == "summary")
    assert buttons_for(n.sent[-1]) == [[("↩️ Undo", f"c:undo:{tm.undo_token}")]]

    v = await handle_callback(stack, FakeArq(), f"c:undo:{tm.undo_token}", TZ)
    assert v and not v.replace and v.rows == [] and "Dibatalkan" in v.text
    assert (await rows(stack, Transaction, Transaction.id == tx.id))[0].status == "rolled_back"

    again = await handle_callback(stack, FakeArq(), f"c:undo:{tm.undo_token}", TZ)  # a second tap is harmless
    assert "sudah dibatalkan" in again.text


async def test_undo_from_the_recent_list_edits_in_place_and_a_stale_tap_gets_an_answer(stack):
    tx = await committed(stack)
    v = await handle_callback(stack, FakeArq(), f"t:undo:{tx.id}", TZ)
    assert v.replace and "Dibatalkan" in v.text and v.rows
    assert "sudah dibatalkan" in (await handle_callback(stack, FakeArq(), f"t:undo:{tx.id}", TZ)).text


async def test_release_enqueues_the_debate_and_dismiss_drops_the_transaction(stack):
    a, b = await held(stack), await held(stack)
    arq = FakeArq()
    v = await handle_callback(stack, arq, f"t:release:{a.id}", TZ)
    assert arq.jobs == [("release_quarantined", a.id)] and "diproses" in v.text
    d = await handle_callback(stack, arq, f"t:dismiss:{b.id}", TZ)
    assert "Dibuang" in d.text and (await rows(stack, Transaction, Transaction.id == b.id))[0].status != "quarantined"


async def test_releasing_a_committed_transaction_is_refused_and_enqueues_nothing(stack):
    tx = await committed(stack)
    arq = FakeArq()
    v = await handle_callback(stack, arq, f"t:release:{tx.id}", TZ)
    assert arq.jobs == [] and "belum tercatat" in v.text


@pytest.mark.parametrize("data", ["", "x", "m:nope:extra", "t:undo:abc", "t:wipe:1", "c:undo", "c:wipe:abcdef", "t:undo:1:2"])
async def test_unknown_or_malformed_buttons_do_nothing(stack, data):
    arq = FakeArq()
    v = await handle_callback(stack, arq, data, TZ)
    assert (v is None or v.rows in (MAIN, [[("⬅️ Menu", "m:menu")]])) and arq.jobs == []
    assert await rows(stack, Transaction) == []


async def test_unlock_button_clears_the_lockdown(stack):
    v = await handle_callback(stack, FakeArq(), "m:unlock", TZ)
    assert v.replace and "Lockdown" in v.text
