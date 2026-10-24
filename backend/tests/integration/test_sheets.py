"""Google Sheets mirror, against a scripted copy of the Sheets API (nothing real was available when this was built)."""

import base64
import json
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from redis.asyncio import Redis
from sqlalchemy import select

from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import Transaction
from apa.sheets import client as sc
from apa.sheets import sync
from apa.sheets.client import SheetsClient, SheetsError, make_assertion, spreadsheet_id
from tests.integration.conftest import HAVE_INFRA

infra = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")
TZ = ZoneInfo("Asia/Jakarta")
NOW = datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0)
SID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


# ---- pure ---------------------------------------------------------------------------------------------------------------


def test_the_sheet_id_comes_from_the_setting_or_a_pasted_link():
    assert spreadsheet_id("abc", "") == "abc"
    assert spreadsheet_id("", f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=0") == SID
    assert spreadsheet_id("  ", "https://example.com/not-a-sheet") == ""
    assert spreadsheet_id("", "") == ""
    assert spreadsheet_id("explicit", f"https://docs.google.com/spreadsheets/d/{SID}/edit") == "explicit"


def _key():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return k, pem


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_the_assertion_is_a_valid_rs256_jwt_for_the_sheets_scope():
    k, pem = _key()
    jwt = make_assertion({"client_email": "bot@proj.iam.gserviceaccount.com", "private_key": pem}, now=1_700_000_000)
    head, claims, sig = jwt.split(".")
    assert json.loads(_unb64(head)) == {"alg": "RS256", "typ": "JWT"}
    c = json.loads(_unb64(claims))
    assert c["iss"] == "bot@proj.iam.gserviceaccount.com" and c["scope"] == sc.SCOPE and c["aud"] == sc.TOKEN_URL
    assert c["iat"] == 1_700_000_000 and c["exp"] == 1_700_003_600
    k.public_key().verify(_unb64(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())  # raises if wrong


# ---- a scripted Sheets API ----------------------------------------------------------------------------------------------


class FakeSheets:
    """Just enough of the API: tabs, values get/put/append/clear, addSheet, and the token endpoint."""

    def __init__(self, fail_token=False, status=None):
        self.tabs: dict[str, list[list]] = {}
        self.calls: list[tuple[str, str]] = []
        self.token_requests = 0
        self.fail_token, self.status = fail_token, status
        self.auth: set[str] = set()

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == sc.TOKEN_URL:
            self.token_requests += 1
            return httpx.Response(401, json={}) if self.fail_token else httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        self.auth.add(req.headers.get("authorization", ""))
        path = unquote(url.split(f"{sc.API}/{SID}", 1)[1].split("?")[0])
        self.calls.append((req.method, path))
        if self.status:
            return httpx.Response(self.status, json={"error": "x"})
        if path == "" and req.method == "GET":
            return httpx.Response(200, json={"sheets": [{"properties": {"title": t}} for t in self.tabs]})
        if path == ":batchUpdate":
            self.tabs.setdefault(json.loads(req.content)["requests"][0]["addSheet"]["properties"]["title"], [])
            return httpx.Response(200, json={})
        m = re.match(r"/values/([^!]+)!([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?(:append|:clear)?$", path)
        tab, _, r1, _, _, suffix = m.groups()
        rows = self.tabs[tab]
        if suffix == ":append":
            rows.extend(json.loads(req.content)["values"])
            return httpx.Response(200, json={})
        if suffix == ":clear":
            rows.clear()
            return httpx.Response(200, json={})
        if req.method == "PUT":
            start = int(r1) - 1
            for i, row in enumerate(json.loads(req.content)["values"]):
                while len(rows) <= start + i:
                    rows.append([])
                rows[start + i] = row
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"values": rows} if rows else {})


@pytest.fixture
def creds(tmp_path):
    _, pem = _key()
    f = tmp_path / "sa.json"
    f.write_text(json.dumps({"client_email": "bot@p.iam.gserviceaccount.com", "private_key": pem}))
    return str(f)


@pytest.fixture
async def api(creds):
    fake = FakeSheets()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        yield fake, SheetsClient(http, creds, SID)


async def test_the_client_signs_in_once_and_reuses_the_token(api):
    fake, c = api
    await c.ensure_tab("A")
    await c.ensure_tab("A")  # exists now: no second addSheet
    assert fake.token_requests == 1 and fake.auth == {"Bearer tok"}
    assert [x for x in fake.calls if x[1] == ":batchUpdate"] == [("POST", ":batchUpdate")]


async def test_google_refusing_the_service_account_is_a_clear_error(creds):
    fake = FakeSheets(fail_token=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        with pytest.raises(SheetsError, match="refused the service account"):
            await SheetsClient(http, creds, SID).tabs()


@pytest.mark.parametrize("status", [403, 404])
async def test_an_unshared_sheet_says_so(creds, status):
    fake = FakeSheets(status=status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        with pytest.raises(SheetsError, match="shared with the service account"):
            await SheetsClient(http, creds, SID).tabs()


async def test_a_missing_or_broken_credentials_file_is_an_error_that_leaks_nothing(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"private_key": "TOPSECRET"}')
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeSheets().handler)) as http:
        for path in (str(tmp_path / "missing.json"), str(bad)):
            with pytest.raises(SheetsError) as e:
                await SheetsClient(http, path, SID).tabs()
            assert "TOPSECRET" not in str(e.value)


# ---- the mirror ---------------------------------------------------------------------------------------------------------


async def add_tx(stack, merchant="Kopi", amount=18000, status="committed", category="Food & Dining", notes=None, direction="out"):
    async with stack() as s:
        t = Transaction(timestamp=NOW - timedelta(hours=1), source_channel="manual_text", payment_wallet="cash_wallet", merchant=merchant,
                        direction=direction, total_paid_amount=Decimal(amount), net_user_expense=Decimal(amount), primary_category=category,
                        execution_mode="consensus_auto", status=status, notes=notes)
        s.add(t)
        await s.commit()
        return t.id


@pytest.fixture
async def redis(stack):
    r = Redis.from_url(os.environ["TEST_REDIS_URL"])
    yield r
    await r.aclose()


@infra
async def test_the_first_run_writes_a_header_and_every_transaction(stack, api):
    fake, c = api
    a, b = await add_tx(stack, "Kopi", 18000), await add_tx(stack, "Gaji", 5_000_000, direction="in", category="Income")
    out = await sync.sync_transactions(stack, c, TZ)
    assert out == {"appended": 2, "corrected": 0}
    rows = fake.tabs[sync.TX_TAB]
    assert rows[0] == sync.TX_HEADER and [r[0] for r in rows[1:]] == [a, b]
    assert rows[1][2:8] == ["Kopi", "out", 18000, "Food & Dining", "cash_wallet", "committed"]
    assert rows[2][3] == "in" and rows[2][4] == 5_000_000


@infra
async def test_running_again_adds_nothing_and_a_new_payment_is_appended_once(stack, api):
    fake, c = api
    await add_tx(stack)
    await sync.sync_transactions(stack, c, TZ)
    assert await sync.sync_transactions(stack, c, TZ) == {"appended": 0, "corrected": 0}
    new = await add_tx(stack, "Roti", 12000)
    assert await sync.sync_transactions(stack, c, TZ) == {"appended": 1, "corrected": 0}
    assert [r[0] for r in fake.tabs[sync.TX_TAB][1:]] == [fake.tabs[sync.TX_TAB][1][0], new]


@infra
async def test_an_undo_or_a_category_fix_updates_the_existing_row_in_place(stack, api):
    fake, c = api
    keep, undone = await add_tx(stack, "Kopi"), await add_tx(stack, "Salah", 99000)
    await sync.sync_transactions(stack, c, TZ)
    async with stack() as s:
        t = await s.get(Transaction, undone)
        t.status, t.primary_category = "rolled_back", "Shopping"
        await s.commit()
    out = await sync.sync_transactions(stack, c, TZ)
    assert out == {"appended": 0, "corrected": 1}
    rows = fake.tabs[sync.TX_TAB]
    assert len(rows) == 3 and rows[2][7] == "rolled_back" and rows[2][5] == "Shopping" and rows[1][0] == keep


@infra
async def test_the_sheet_alone_decides_what_is_already_there_so_a_wiped_sheet_refills(stack, api):
    fake, c = api
    await add_tx(stack, "A")
    await add_tx(stack, "B")
    await sync.sync_transactions(stack, c, TZ)
    fake.tabs[sync.TX_TAB].clear()
    assert (await sync.sync_transactions(stack, c, TZ))["appended"] == 2
    assert len(fake.tabs[sync.TX_TAB]) == 3  # header + both


@infra
async def test_text_that_looks_like_a_formula_is_sent_raw_so_it_stays_text(stack, creds):
    fake = FakeSheets()
    sent = []

    def spy(req):
        if "valueInputOption" in str(req.url):
            sent.append(str(req.url))
        return fake.handler(req)

    await add_tx(stack, '=HYPERLINK("http://evil","x")', notes="=1+1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(spy)) as http:
        await sync.sync_transactions(stack, SheetsClient(http, creds, SID), TZ)
    assert sent and all("valueInputOption=RAW" in u for u in sent)
    assert fake.tabs[sync.TX_TAB][1][2] == '=HYPERLINK("http://evil","x")'  # stored as given; RAW means it is text


@infra
async def test_a_first_sync_of_a_long_history_catches_up_in_bounded_steps(stack, api, monkeypatch):
    fake, c = api
    monkeypatch.setattr(sync, "MAX_APPEND", 3)
    for i in range(7):
        await add_tx(stack, f"M{i}")
    assert [(await sync.sync_transactions(stack, c, TZ))["appended"] for _ in range(4)] == [3, 3, 1, 0]
    assert len(fake.tabs[sync.TX_TAB]) == 8


@infra
async def test_the_budget_tab_shows_limits_spent_and_percent_and_is_throttled(stack, api, redis):
    fake, c = api
    month = NOW.date().replace(day=1)
    async with stack() as s:
        s.add(BudgetRule(category="Food & Dining", period_month=month, base_limit=Decimal(1_000_000), current_limit=Decimal(1_000_000),
                         spent_amount=Decimal(250_000)))
        await s.commit()
    first = await sync.run_sync(stack, c, redis, TZ, NOW)
    assert first["budgets"] == 1
    assert fake.tabs[sync.BUDGET_TAB] == [sync.BUDGET_HEADER, [month.isoformat(), "Food & Dining", 1_000_000, 1_000_000, 250_000, 750_000, 25.0]]
    assert "budgets" not in await sync.run_sync(stack, c, redis, TZ, NOW)  # refreshed within the last 15 minutes


def test_a_first_sync_is_capped_at_five_hundred_rows_a_run():
    assert sync.MAX_APPEND == 500


@infra
async def test_a_category_that_disappears_does_not_linger_in_the_budget_tab(stack, api):
    fake, c = api
    month = NOW.date().replace(day=1)
    async with stack() as s:
        for cat in ("Food & Dining", "Shopping"):
            s.add(BudgetRule(category=cat, period_month=month, base_limit=Decimal(100), current_limit=Decimal(100)))
        await s.commit()
    assert await sync.sync_budgets(stack, c, NOW) == 2
    async with stack() as s:
        await s.delete((await s.execute(select(BudgetRule).where(BudgetRule.category == "Shopping"))).scalar_one())
        await s.commit()
    assert await sync.sync_budgets(stack, c, NOW) == 1
    assert [r[1] for r in fake.tabs[sync.BUDGET_TAB][1:]] == ["Food & Dining"]
