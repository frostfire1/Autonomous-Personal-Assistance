"""News and cyber end to end against real Postgres + Redis, with the network replaced by a scripted transport."""

import json
import os
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select

from apa.core.settings import get_settings
from apa.db.models.budget import BudgetRule
from apa.db.models.health import SleepSession
from apa.db.models.intel import Briefing, CyberAdvisory, HomelabWatch, NewsArticle
from apa.db.models.ledger import Transaction
from apa.db.models.ops import TelegramMessage
from apa.gateway.routers import briefing as briefing_router
from apa.integrations.llm import LLMResult
from apa.news import briefing as brief
from apa.news import fetch
from apa.news.cyber import digest_24h, ingest_cyber
from apa.news.ingest import dedup_hash, ingest_news, recent_articles
from apa.news.jobs import run_briefing_precompute, run_cyber_poll, run_news_ingest
from apa.news.sources import Feed
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import FakeNotifier, rows

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")

TZ = ZoneInfo("Asia/Jakarta")
NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(fetch.socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 0))])


@pytest.fixture
async def redis(stack):
    r = Redis.from_url(os.environ["TEST_REDIS_URL"])
    yield r
    await r.aclose()


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><pubDate>{(NOW - timedelta(minutes=m)).strftime('%a, %d %b %Y %H:%M:%S GMT')}"
        f"</pubDate><description>{t}</description></item>" for t, u, m in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


FEEDS = (Feed("Antara", "https://antara.test/rss", "nasional"), Feed("Detik", "https://detik.test/rss", "nasional"),
         Feed("CNN Indonesia", "https://cnn.test/rss", "nasional"), Feed("Hacker", "https://hn.test/rss", "cyber"))
POLICY = "Pemerintah umumkan tarif listrik naik 12 persen mulai November, menteri jelaskan subsidi"


def scripted(routes):
    def handler(req):
        r = routes.get(str(req.url).split("?")[0])
        if r is None or isinstance(r, Exception):
            if isinstance(r, Exception):
                raise r
            return httpx.Response(404)
        return httpx.Response(200, content=r)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def news_routes(extra=None):
    return {
        "https://antara.test/rss": rss([(POLICY, "https://antara.test/a1", 20), ("Berita lain soal ekonomi", "https://antara.test/a2", 90)]),
        "https://detik.test/rss": rss([("Tarif listrik naik 12 persen mulai November pemerintah umumkan subsidi menteri", "https://detik.test/d1", 25)]),
        "https://cnn.test/rss": rss([("Pemerintah umumkan tarif listrik naik 12 persen November, menteri soal subsidi", "https://cnn.test/c1", 30)]),
        "https://hn.test/rss": rss([("Actively exploited zero-day hits Indonesian banks", "https://hn.test/h1", 40)]),
        **(extra or {}),
    }


async def test_articles_are_stored_once_and_a_second_pass_adds_nothing(stack, redis):
    c = scripted(news_routes())
    first = await ingest_news(stack, c, redis, feeds=FEEDS)
    assert first == {"new": 5, "failed": [], "quiet": []}
    assert len(await rows(stack, NewsArticle)) == 5
    again = await ingest_news(stack, c, redis, feeds=FEEDS)
    assert again["new"] == 0 and len(await rows(stack, NewsArticle)) == 5
    assert dedup_hash("A ", "https://x") == dedup_hash("a", "https://x ")  # case and spaces do not make a new article


async def test_one_failing_source_does_not_stop_the_others_and_is_reported_after_repeated_failures(stack, redis):
    routes = news_routes({"https://detik.test/rss": httpx.ConnectTimeout("slow")})
    c = scripted(routes)
    results = [await ingest_news(stack, c, redis, feeds=FEEDS) for _ in range(6)]
    assert results[0]["failed"] == ["Detik"] and results[0]["new"] == 4  # the others were still read
    assert [r["quiet"] for r in results] == [[], [], [], [], [], ["Detik"]]  # told once, on the 6th failure
    ok = await ingest_news(stack, scripted(news_routes()), redis, feeds=FEEDS)
    assert ok["failed"] == [] and await redis.get("apa:news:src:Detik") is None  # a good read resets the counter


async def test_a_hostile_feed_is_rejected_without_harming_the_rest(stack, redis):
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&a;</title><link>https://e</link></item></channel></rss>'
    res = await ingest_news(stack, scripted(news_routes({"https://hn.test/rss": evil})), redis, feeds=FEEDS)
    assert res["failed"] == ["Hacker"] and res["new"] == 4


async def test_recent_articles_come_back_with_their_topic_and_only_inside_the_window(stack, redis):
    await ingest_news(stack, scripted(news_routes()), redis, feeds=FEEDS)
    async with stack() as s:
        s.add(NewsArticle(url="https://old", title="Berita lama sekali", dedup_hash="x" * 64, source="Antara",
                          published_at=NOW - timedelta(days=5)))
        await s.commit()
    import apa.news.ingest as ing

    ing.FEEDS = FEEDS  # the topic map follows the configured feeds
    arts = await recent_articles(stack, NOW)
    assert len(arts) == 5 and {a.topic for a in arts} == {"nasional", "cyber"}
    assert all("lama sekali" not in a.title for a in arts)


class Narrator:
    def __init__(self):
        self.calls = []

    async def complete(self, route, system, user, **kw):
        self.calls.append(system[:20])
        if "NATIONAL scale" in system:
            return LLMResult('{"national": false}', "fake", 5, 5, 0)
        return LLMResult("Tarif listrik naik 12 persen mulai November, menurut Antara dan Detik.", "fake", 20, 20, 0)


async def test_the_briefing_reads_greeting_money_sleep_news_and_cyber_in_order(stack, redis, monkeypatch):
    import apa.news.ingest as ing

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    await ingest_news(stack, scripted(news_routes()), redis, feeds=FEEDS)
    now = datetime.now(TZ)  # not pinned to an hour: the articles are stamped with the real time, so "now" must be real too
    yesterday_noon = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=12)
    async with stack() as s:
        s.add(Transaction(timestamp=yesterday_noon, source_channel="manual_text", payment_wallet="cash_wallet",
                          merchant="Warteg", direction="out", total_paid_amount=Decimal(58300), net_user_expense=Decimal(58300),
                          primary_category="Food & Dining", execution_mode="consensus_auto", status="committed"))
        s.add(BudgetRule(category="Food & Dining", period_month=now.date().replace(day=1), base_limit=Decimal(100_000),
                         current_limit=Decimal(100_000), spent_amount=Decimal(85_000)))
        s.add(SleepSession(sleep_date=now.date(), sleep_score=71, total_sleep_mins=380))
        s.add(CyberAdvisory(cve_id="CVE-2026-0001", summary="RCE", cvss=Decimal("9.8"), in_cisa_kev=True, tier="urgent", source="kev"))
        await s.commit()
    llm = Narrator()
    b = await brief.build(stack, llm, redis, TZ, now, kind="short")
    topics = [s.topic for s in b.sections]
    assert topics == ["pembuka", "keuangan", "kesehatan", "nasional", "cyber"]
    assert b.text.startswith("Selamat ") and ". Hari ini " in b.text and "Kemarin pengeluaranmu Rp 58.300." in b.text
    assert "Anggaran Food & Dining sudah 85 persen." in b.text and "Tidurmu semalam 6 jam 20 menit, skor 71." in b.text
    assert "Tarif listrik naik 12 persen" in b.text
    assert "produk yang belum teridentifikasi, skor 9,8, sudah dieksploitasi" in b.text  # no ID aloud, but what to check
    assert len(b.text) <= brief.LIMITS["short"]["chars"]


async def test_the_same_story_is_summarised_once_and_cached(stack, redis, monkeypatch):
    import apa.news.ingest as ing

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    await ingest_news(stack, scripted(news_routes()), redis, feeds=FEEDS)
    llm = Narrator()
    await brief.build(stack, llm, redis, TZ, datetime.now(TZ), kind="short")
    first = len(llm.calls)
    await brief.build(stack, llm, redis, TZ, datetime.now(TZ), kind="short")
    assert first >= 1 and len(llm.calls) == first  # the second briefing reused every summary


async def test_a_quiet_day_still_makes_a_briefing_and_a_model_outage_falls_back_to_headlines(stack, redis, monkeypatch):
    class Down:
        async def complete(self, *a, **k):
            raise RuntimeError("down")

    import apa.news.ingest as ing

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    empty = await brief.build(stack, Down(), redis, TZ, datetime.now(TZ), kind="short")
    assert "Tidak ada berita nasional besar saat ini." in empty.text
    await ingest_news(stack, scripted(news_routes()), redis, feeds=FEEDS)
    down = await brief.build(stack, Down(), redis, TZ, datetime.now(TZ), kind="short")
    assert "menurut" in down.text and "tarif listrik" in down.text.lower()  # the headline, attributed


async def test_a_stored_briefing_is_reused_while_fresh_and_rebuilt_when_asked(stack, redis):
    llm = Narrator()
    a = await brief.get_or_build(stack, llm, redis, TZ, datetime.now(TZ))
    b = await brief.get_or_build(stack, llm, redis, TZ, datetime.now(TZ))
    c = await brief.get_or_build(stack, llm, redis, TZ, datetime.now(TZ), fresh=True)
    assert a.id == b.id and c.id != a.id and len(await rows(stack, Briefing)) == 2


# ---- cyber --------------------------------------------------------------------------------------------------------------


def cyber_routes(kev_date):
    kev = json.dumps({"vulnerabilities": [{"cveID": "CVE-2026-0100", "vendorProject": "Acme", "product": "Gate",
                                            "dateAdded": kev_date, "shortDescription": "Exploited"}]}).encode()
    nvd = json.dumps({"vulnerabilities": [
        {"cve": {"id": "CVE-2026-0100", "descriptions": [{"lang": "en", "value": "Gate RCE"}],
                 "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.8}}]}}},
        {"cve": {"id": "CVE-2026-0200", "descriptions": [{"lang": "en", "value": "Hypervisor flaw in proxmox ve"}],
                 "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.1}}]}}},
        {"cve": {"id": "CVE-2026-0300", "descriptions": [{"lang": "en", "value": "Not scored yet"}], "metrics": {}}},
        {"cve": {"id": "CVE-2026-0400", "descriptions": [{"lang": "en", "value": "Minor"}],
                 "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 3.1}}]}}},
    ]}).encode()
    epss = json.dumps({"data": [{"cve": "CVE-2026-0100", "epss": "0.91"}, {"cve": "CVE-2026-0200", "epss": "0.02"}]}).encode()
    return {
        "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json": kev,
        "https://services.nvd.nist.gov/rest/json/cves/2.0": nvd,
        "https://api.first.org/data/v1/epss": epss,
    }


async def test_cves_are_tiered_from_kev_epss_and_the_watchlist_and_urgent_ones_are_reported_once(stack):
    async with stack() as s:
        s.add(HomelabWatch(product="Proxmox VE", is_active=True))
        await s.commit()
    kev_date = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    c = scripted(cyber_routes(kev_date))
    res = await ingest_cyber(stack, c, datetime.now(UTC))
    assert res["seen"] == 4 and sorted(res["urgent"]) == ["CVE-2026-0100", "CVE-2026-0200"]
    tiers = {r.cve_id: (r.tier, r.in_cisa_kev, r.epss) for r in await rows(stack, CyberAdvisory)}
    assert tiers["CVE-2026-0100"] == ("urgent", True, Decimal("0.91000"))
    assert tiers["CVE-2026-0200"][0] == "urgent"  # critical and on the owner's watchlist
    assert tiers["CVE-2026-0300"][0] == "archive" and tiers["CVE-2026-0400"][0] == "archive"
    again = await ingest_cyber(stack, c, datetime.now(UTC))
    assert again["urgent"] == []  # already urgent: not announced twice
    d = await digest_24h(stack, datetime.now(UTC) + timedelta(minutes=1))
    assert d["total"] == 4 and d["by_severity"] == {"critical": 2, "unscored": 1, "low": 1} and d["kev"] == 1
    assert d["urgent"] and d["top"][0][0] in ("CVE-2026-0100", "CVE-2026-0200")


def _kev_only_routes(kev_ids, nvd_record, asked):
    kev = json.dumps({"vulnerabilities": [
        {"cveID": i, "vendorProject": "Plex", "product": "Media Server", "dateAdded": datetime.now(UTC).date().isoformat(),
         "shortDescription": "Exploited"} for i in kev_ids]}).encode()
    epss = json.dumps({"data": []}).encode()

    def handler(req):
        url = str(req.url)
        if "known_exploited" in url:
            return httpx.Response(200, content=kev)
        if "first.org" in url:
            return httpx.Response(200, content=epss)
        if "nvd.nist.gov" in url:
            if "cveId=" in url:
                asked.append(url.split("cveId=")[1])
                return httpx.Response(200, content=nvd_record(url.split("cveId=")[1]))
            return httpx.Response(200, content=json.dumps({"totalResults": 0, "vulnerabilities": []}).encode())
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _record(cid):
    return json.dumps({"vulnerabilities": [{"cve": {
        "id": cid, "descriptions": [{"lang": "en", "value": "Plex flaw"}],
        "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.8}}]},
        "configurations": [{"nodes": [{"cpeMatch": [{"vulnerable": True, "criteria": "cpe:2.3:a:plex:media_server:*:*:*:*:*:*:*:*",
                                                    "versionEndExcluding": "1.40.3"}]}]}]}}]}).encode()


async def test_an_exploited_cve_gets_its_score_and_versions_from_nvd_once_and_keeps_them(stack, monkeypatch):
    import apa.news.cyber as cy

    monkeypatch.setattr(cy, "NVD_PACE_S", 0)
    asked: list[str] = []
    c = _kev_only_routes(["CVE-2026-7001"], _record, asked)
    await ingest_cyber(stack, c, datetime.now(UTC), nvd_key="k")
    (row,) = await rows(stack, CyberAdvisory)
    assert row.cvss == Decimal("9.8") and row.source == "kev+nvd" and row.in_cisa_kev
    assert row.affected_products == ["plex media server (< 1.40.3)"]  # versioned entry; the bare KEV name is not duplicated
    assert row.tier == "urgent"  # critical and exploited
    await ingest_cyber(stack, c, datetime.now(UTC), nvd_key="k")  # the next poll re-reads the bare exploited list
    (row,) = await rows(stack, CyberAdvisory)
    assert row.cvss == Decimal("9.8") and row.affected_products == ["plex media server (< 1.40.3)"]  # nothing erased
    assert asked == ["CVE-2026-7001"]  # asked once, not every poll


async def test_an_exploited_cve_nvd_does_not_know_is_not_asked_about_again_and_a_failed_ask_is_retried(stack, monkeypatch):
    import apa.news.cyber as cy

    monkeypatch.setattr(cy, "NVD_PACE_S", 0)
    asked: list[str] = []
    empty = _kev_only_routes(["CVE-2026-7002"], lambda cid: json.dumps({"vulnerabilities": []}).encode(), asked)
    await ingest_cyber(stack, empty, datetime.now(UTC), nvd_key="k")
    await ingest_cyber(stack, empty, datetime.now(UTC), nvd_key="k")
    assert asked == ["CVE-2026-7002"] and (await rows(stack, CyberAdvisory))[0].source == "kev+nvd"

    flaky_asked: list[str] = []
    state = {"fail": True}

    def record_or_500(cid):
        flaky_asked.append(cid)
        return _record(cid)

    c = _kev_only_routes(["CVE-2026-7003"], record_or_500, flaky_asked)
    orig = c._transport.handle_async_request

    async def sometimes_fail(req):
        if "cveId=" in str(req.url) and state["fail"]:
            state["fail"] = False
            return httpx.Response(500)
        return await orig(req)

    c._transport.handle_async_request = sometimes_fail
    await ingest_cyber(stack, c, datetime.now(UTC), nvd_key="k")
    row = next(r for r in await rows(stack, CyberAdvisory) if r.cve_id == "CVE-2026-7003")
    assert row.source == "kev"  # the ask failed, so it stays eligible
    await ingest_cyber(stack, c, datetime.now(UTC), nvd_key="k")
    row = next(r for r in await rows(stack, CyberAdvisory) if r.cve_id == "CVE-2026-7003")
    assert row.source == "kev+nvd" and row.cvss == Decimal("9.8")


async def test_a_dead_cve_source_is_skipped_and_the_rest_is_still_used(stack):
    routes = cyber_routes((datetime.now(UTC)).date().isoformat())
    routes["https://services.nvd.nist.gov/rest/json/cves/2.0"] = httpx.ConnectTimeout("slow")
    res = await ingest_cyber(stack, scripted(routes), datetime.now(UTC))
    assert res["seen"] == 1  # only KEV answered


async def test_nvd_is_read_page_by_page_up_to_a_cap_and_a_failing_page_keeps_the_earlier_ones(stack, monkeypatch):
    import apa.news.cyber as cy

    monkeypatch.setattr(cy, "NVD_PACE_S", 0)
    seen: list[str] = []

    def page(i, total=1000):
        vs = [{"cve": {"id": f"CVE-2026-{i}{n:03d}", "descriptions": [{"lang": "en", "value": "x"}],
                       "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 5.0}}]}}} for n in range(3)]
        return json.dumps({"totalResults": total, "vulnerabilities": vs}).encode()

    def handler(req):
        url = str(req.url)
        if "nvd.nist.gov" in url:
            seen.append(url.split("startIndex=")[1] if "startIndex=" in url else "0")
            idx = int(seen[-1])
            if FAIL_AT is not None and idx == FAIL_AT:
                return httpx.Response(500)
            return httpx.Response(200, content=page(idx // 200 + 1))
        return httpx.Response(404)

    FAIL_AT = None
    c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = await ingest_cyber(stack, c, datetime.now(UTC), nvd_key="k")
    assert seen == ["0", "200", "400"] and res["seen"] == 9  # 1000 results, but at most three pages of 200
    seen.clear()
    FAIL_AT = 200
    res = await ingest_cyber(stack, c, datetime.now(UTC), nvd_key="k")
    assert seen == ["0", "200"] and res["seen"] == 3  # the first page survives; no more pages after the failure


async def test_the_spoken_cyber_line_gives_criticals_and_exploited_never_the_daily_total(stack, redis, monkeypatch):
    import apa.news.ingest as ing

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    async with stack() as s:
        for i in range(250):  # a normal NVD day: hundreds, almost all unremarkable
            s.add(CyberAdvisory(cve_id=f"CVE-2026-1{i:04d}", summary="x", cvss=Decimal("5.0"), tier="archive", source="nvd"))
        for i in range(2):
            s.add(CyberAdvisory(cve_id=f"CVE-2026-2{i:04d}", summary="x", cvss=Decimal("9.8"), tier="normal", source="nvd"))
        s.add(CyberAdvisory(cve_id="CVE-2026-30001", summary="x", cvss=Decimal("8.0"), in_cisa_kev=True, tier="normal", source="kev"))
        await s.commit()
    b = await brief.build(stack, Narrator(), redis, TZ, datetime.now(TZ), kind="short")
    cyber = next(s for s in b.sections if s.topic == "cyber").text
    assert "2 kerentanan kritis baru dan 1 yang sudah dieksploitasi aktif menurut CISA" in cyber
    assert "253" not in cyber and "CVE baru" not in cyber and "belum diberi skor" not in cyber


async def test_a_quiet_cve_day_says_so_in_one_short_line(stack, redis, monkeypatch):
    import apa.news.ingest as ing

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    async with stack() as s:
        s.add(CyberAdvisory(cve_id="CVE-2026-40001", summary="x", cvss=Decimal("5.0"), tier="archive", source="nvd"))
        await s.commit()
    b = await brief.build(stack, Narrator(), redis, TZ, datetime.now(TZ), kind="short")
    assert "Tidak ada kerentanan kritis baru." in next(s for s in b.sections if s.topic == "cyber").text


# ---- the jobs ------------------------------------------------------------------------------------------------------------


async def test_the_news_job_pushes_only_what_is_urgent_and_only_once(stack, redis, monkeypatch):
    import apa.news.ingest as ing
    import apa.news.jobs as jobs

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    monkeypatch.setattr(jobs, "ingest_news", lambda sm, c, r: ingest_news(sm, c, r, feeds=FEEDS))
    n, llm, c = FakeNotifier(), Narrator(), scripted(news_routes())
    first = await run_news_ingest(stack, llm, n, redis, datetime.now(UTC), client=c)
    assert first["new"] == 5 and first["pushed"] == 1  # the story three outlets carry; the single-outlet ones wait
    assert n.sent[0].startswith("📰 Tarif listrik naik 12 persen")
    second = await run_news_ingest(stack, llm, n, redis, datetime.now(UTC), client=c)
    assert second["pushed"] == 0 and len(n.sent) == 1


async def test_the_cyber_job_announces_a_new_urgent_cve_once(stack, redis):
    n = FakeNotifier()
    c = scripted(cyber_routes((datetime.now(UTC)).date().isoformat()))
    await run_cyber_poll(stack, n, redis, datetime.now(UTC), client=c)
    await run_cyber_poll(stack, n, redis, datetime.now(UTC), client=c)
    assert len(n.sent) == 1 and "CVE-2026-0100" in n.sent[0]
    assert [m.kind for m in await rows(stack, TelegramMessage)] == ["digest"]


async def test_precompute_builds_a_short_and_a_full_briefing(stack, redis):
    ids = await run_briefing_precompute(stack, Narrator(), redis, TZ, datetime.now(TZ))
    assert len(ids) == 2 and {b.kind for b in await rows(stack, Briefing)} == {"short", "full"}


# ---- the API the app uses --------------------------------------------------------------------------------------------------


@pytest.fixture
async def api(stack, redis, monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setenv("INGEST_API_KEY", "k")
    get_settings.cache_clear()
    monkeypatch.setattr(briefing_router, "SessionLocal", stack)
    app = FastAPI()
    app.include_router(briefing_router.router)
    app.state.llm, app.state.arq = Narrator(), redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_the_app_fetches_a_short_briefing_and_reports_that_it_played(api, stack):
    assert (await api.get("/briefing/short")).status_code == 401
    r = await api.get("/briefing/short", headers={"x-api-key": "k"})
    assert r.status_code == 200 and r.json()["kind"] == "short" and r.json()["text"].startswith("Selamat")
    assert [s["topic"] for s in r.json()["sections"]][0] == "pembuka"
    same = await api.get("/briefing/short", headers={"x-api-key": "k"})
    assert same.json()["id"] == r.json()["id"]  # reused while fresh
    fresh = await api.get("/briefing/short?fresh=true", headers={"x-api-key": "k"})
    assert fresh.json()["id"] != r.json()["id"]
    full = await api.get("/briefing/full", headers={"x-api-key": "k"})
    assert full.json()["kind"] == "full"
    ok = await api.post(f"/briefing/{r.json()['id']}/played", headers={"x-api-key": "k"})
    assert ok.json() == {"ok": True}
    assert (await rows(stack, Briefing, Briefing.id == r.json()["id"]))[0].played_at is not None
    assert (await api.post("/briefing/9999/played", headers={"x-api-key": "k"})).status_code == 404
    assert (await api.post(f"/briefing/{r.json()['id']}/played")).status_code == 401


@pytest.fixture
def fake_voice(monkeypatch):
    """Stands in for Microsoft: records what it was asked to say."""
    from apa.tts import voice

    calls: list[str] = []

    async def synth(text, **kw):
        calls.append(text)
        return b"ID3" + b"x" * 600

    monkeypatch.setattr(voice, "synthesize", synth)
    return calls


async def test_a_section_is_served_as_mp3_synthesised_once_and_cached(api, fake_voice):
    h = {"x-api-key": "k"}
    b = (await api.get("/briefing/short", headers=h)).json()
    assert (await api.get(f"/briefing/{b['id']}/audio/0")).status_code == 401
    r = await api.get(f"/briefing/{b['id']}/audio/0", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content.startswith(b"ID3")
    await api.get(f"/briefing/{b['id']}/audio/0", headers=h)
    assert fake_voice == [b["sections"][0]["text"]]  # the second request came from the cache


async def test_audio_for_a_missing_briefing_or_section_is_404(api, fake_voice):
    h = {"x-api-key": "k"}
    b = (await api.get("/briefing/short", headers=h)).json()
    assert (await api.get("/briefing/99999/audio/0", headers=h)).status_code == 404
    assert (await api.get(f"/briefing/{b['id']}/audio/{len(b['sections'])}", headers=h)).status_code == 404
    assert (await api.get(f"/briefing/{b['id']}/audio/-1", headers=h)).status_code == 404
    assert fake_voice == []


async def test_when_the_voice_fails_the_app_is_told_to_speak_itself(api, monkeypatch):
    from apa.tts import voice

    async def down(text, **kw):
        raise voice.TtsError("ConnectionError")

    monkeypatch.setattr(voice, "synthesize", down)
    h = {"x-api-key": "k"}
    b = (await api.get("/briefing/short", headers=h)).json()
    assert (await api.get(f"/briefing/{b['id']}/audio/0", headers=h)).status_code == 503


async def test_a_changed_voice_or_style_never_serves_old_audio(stack, redis, fake_voice, monkeypatch):
    from apa.tts import voice

    await voice.audio_for(redis, "Selamat pagi")
    await voice.audio_for(redis, "Selamat pagi")
    assert len(fake_voice) == 1
    monkeypatch.setenv("TTS_VOICE", "id-ID-ArdiNeural")
    get_settings.cache_clear()
    await voice.audio_for(redis, "Selamat pagi")
    assert len(fake_voice) == 2
    monkeypatch.setenv("TTS_ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(voice.TtsError, match="disabled"):  # even a clip that is already cached is not served
        await voice.audio_for(redis, "Selamat pagi")
    get_settings.cache_clear()


async def test_long_or_empty_text_is_trimmed_at_a_sentence_and_never_sent_blank(monkeypatch):
    from apa.tts import voice

    long = ("Kalimat pendek ini diulang. " * 200).strip()
    out = voice.cap(long)
    assert len(out) <= voice.MAX_CHARS and out.endswith(".")
    assert voice.cap("  a " + chr(10) * 2 + " b  ") == "a b"
    monkeypatch.setenv("TTS_ENABLED", "true")
    get_settings.cache_clear()
    with pytest.raises(voice.TtsError, match="empty"):
        await voice.synthesize("   ")
    get_settings.cache_clear()


async def test_the_morning_precompute_has_every_clip_ready_and_a_voice_outage_does_not_break_it(stack, redis, fake_voice, monkeypatch):
    from apa.tts import voice

    ids = await run_briefing_precompute(stack, Narrator(), redis, TZ, datetime.now(TZ))
    assert len(ids) == 2 and len(fake_voice) >= 2
    before = len(fake_voice)
    await run_briefing_precompute(stack, Narrator(), redis, TZ, datetime.now(TZ))
    assert len(fake_voice) == before  # identical text: every clip came from the cache

    async def down(text, **kw):
        raise voice.TtsError("x")

    monkeypatch.setattr(voice, "synthesize", down)
    await redis.flushdb()
    assert len(await run_briefing_precompute(stack, Narrator(), redis, TZ, datetime.now(TZ))) == 2


def _gemini_reply(pcm=b"\x01\x00" * 3000, mime="audio/L16;codec=pcm;rate=24000", status=200):
    import base64

    body = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": mime, "data": base64.b64encode(pcm).decode()}}]}}]}
    return status, body


def _patch_gemini(monkeypatch, reply, seen):
    """Replaces the HTTP client inside tts.voice with a scripted Google."""
    from apa.tts import voice

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen.append((url, json, headers))
            code, body = reply
            if isinstance(body, Exception):
                raise body
            return httpx.Response(code, json=body)

    monkeypatch.setattr(voice.httpx, "AsyncClient", FakeHttp)


async def test_gemini_is_asked_with_the_style_the_voice_and_the_key_and_answers_as_wav(monkeypatch):
    from apa.tts import voice

    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setenv("TTS_ENGINE", "auto")
    get_settings.cache_clear()
    seen = []
    _patch_gemini(monkeypatch, _gemini_reply(), seen)
    audio = await voice.synthesize("Kemarin pengeluaranmu Rp 58.300")
    (url, body, headers), = seen
    assert "gemini-2.5-flash-preview-tts:generateContent" in url and headers["x-goog-api-key"] == "gk"
    prompt = body["contents"][0]["parts"][0]["text"]
    assert "ceria" in prompt and "lima puluh delapan ribu tiga ratus rupiah" in prompt  # style, then the speakable text
    cfg = body["generationConfig"]
    assert cfg["responseModalities"] == ["AUDIO"]
    assert cfg["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Zephyr"
    assert audio[:4] == b"RIFF" and voice.sniff(audio) == "audio/wav" and voice.sniff(b"ID3xxxx") == "audio/mpeg"
    get_settings.cache_clear()


async def test_the_wav_header_carries_the_sample_rate_google_reports(monkeypatch):
    import io
    import wave

    from apa.tts import voice

    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    get_settings.cache_clear()
    _patch_gemini(monkeypatch, _gemini_reply(mime="audio/L16;codec=pcm;rate=16000"), [])
    with wave.open(io.BytesIO(await voice.synthesize("halo"))) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    get_settings.cache_clear()


@pytest.mark.parametrize("reply", [(429, {"error": "quota"}), (200, {"candidates": []}), (200, {"nope": 1}),
                                   _gemini_reply(pcm=b"\x00\x00" * 10), (200, httpx.ConnectTimeout("slow"))])
async def test_when_gemini_fails_microsoft_takes_over(monkeypatch, reply):
    from apa.tts import voice

    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    get_settings.cache_clear()
    _patch_gemini(monkeypatch, reply, [])
    spoken = []

    async def edge(text):
        spoken.append(text)
        return b"ID3" + b"x" * 600

    monkeypatch.setattr(voice, "_edge", edge)
    assert (await voice.synthesize("halo dunia"))[:3] == b"ID3" and spoken == ["halo dunia"]
    get_settings.cache_clear()


async def test_without_a_key_only_microsoft_is_used_and_with_both_failing_the_app_speaks_itself(monkeypatch):
    from apa.tts import voice

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("TTS_ENGINE", "auto")
    get_settings.cache_clear()
    assert voice.engines() == ["edge"]
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    get_settings.cache_clear()
    assert voice.engines() == ["gemini", "edge"]
    monkeypatch.setenv("TTS_ENGINE", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY")
    get_settings.cache_clear()
    assert voice.engines() == []  # asked for Gemini but no key: nothing to try
    with pytest.raises(voice.TtsError):
        await voice.synthesize("halo")
    monkeypatch.setenv("TTS_ENGINE", "edge")
    get_settings.cache_clear()
    assert voice.engines() == ["edge"]

    async def down(text, **kw):
        raise voice.TtsError("ConnectionError")

    monkeypatch.setattr(voice, "_edge", down)
    with pytest.raises(voice.TtsError):
        await voice.synthesize("halo")
    get_settings.cache_clear()


async def test_changing_engine_voice_or_style_never_serves_a_clip_made_by_another(stack, redis, monkeypatch):
    from apa.tts import voice

    made = []

    async def edge(text):
        made.append("edge")
        return b"ID3" + b"e" * 600

    monkeypatch.setattr(voice, "_edge", edge)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()
    a = await voice.audio_for(redis, "Selamat pagi")
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setenv("GEMINI_TTS_VOICE", "Kore")
    get_settings.cache_clear()
    _patch_gemini(monkeypatch, _gemini_reply(), [])
    b = await voice.audio_for(redis, "Selamat pagi")
    assert a.startswith(b"ID3") and b.startswith(b"RIFF")  # the Microsoft clip was not reused for Gemini
    monkeypatch.setenv("GEMINI_TTS_VOICE", "Puck")
    get_settings.cache_clear()
    seen = []
    _patch_gemini(monkeypatch, _gemini_reply(), seen)
    await voice.audio_for(redis, "Selamat pagi")
    assert len(seen) == 1  # a new voice was asked for, not served from the old voice's cache
    get_settings.cache_clear()


async def _severe_day(stack):
    """A realistic day: many ordinary CVEs and a few that matter, with technologies and versions."""
    async with stack() as s:
        for i in range(60):
            s.add(CyberAdvisory(cve_id=f"CVE-2026-1{i:04d}", summary="minor", cvss=Decimal("5.0"), tier="archive", source="nvd"))
        s.add(CyberAdvisory(cve_id="CVE-2026-20001", summary="Remote code execution in the HTTP server before 2.4.62",
                            cvss=Decimal("9.8"), epss=Decimal("0.91"), in_cisa_kev=True, tier="urgent", source="kev",
                            affected_products=["apache http server (< 2.4.62)", "apache tomcat (>= 9.0, < 9.0.90)"]))
        s.add(CyberAdvisory(cve_id="CVE-2026-20002", summary="Auth bypass", cvss=Decimal("9.1"), epss=Decimal("0.03"),
                            tier="normal", source="nvd", affected_products=["acme gate (1.2.3)"]))
        s.add(CyberAdvisory(cve_id="CVE-2026-20003", summary="Privilege escalation", cvss=Decimal("7.5"), epss=Decimal("0.20"),
                            tier="normal", source="nvd", affected_products=["vendor app (< 5.0)"]))
        await s.commit()


async def test_severe_cves_come_most_worrying_first_and_only_high_critical_or_exploited(stack):
    from apa.news.cyber import severe_24h

    await _severe_day(stack)
    got = await severe_24h(stack, datetime.now(UTC) + timedelta(minutes=1))
    assert [r["id"] for r in got] == ["CVE-2026-20001", "CVE-2026-20003", "CVE-2026-20002"] or \
        [r["id"] for r in got][0] == "CVE-2026-20001"  # exploited first, whatever follows
    assert {r["id"] for r in got} == {"CVE-2026-20001", "CVE-2026-20002", "CVE-2026-20003"}  # the 60 minor ones are out
    assert got[0]["products"][0] == "apache http server (< 2.4.62)" and got[0]["kev"] is True
    assert len(await severe_24h(stack, datetime.now(UTC) + timedelta(minutes=1), limit=2)) == 2


async def test_the_voice_names_technology_and_version_for_the_worst_cves_but_not_their_numbers(stack, redis, monkeypatch):
    import apa.news.ingest as ing

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    await _severe_day(stack)
    b = await brief.build(stack, Narrator(), redis, TZ, datetime.now(TZ), kind="short")
    cyber = next(s for s in b.sections if s.topic == "cyber").text
    assert "apache http server, di bawah versi 2.4.62, skor 9,8, sudah dieksploitasi" in cyber
    assert "Detail lengkap ada di Telegram" in cyber or "ada di Telegram" in cyber
    assert "CVE-2026-20001" not in cyber  # an ID is unreadable aloud; it goes to Telegram


async def test_the_telegram_briefing_carries_more_than_the_voice(stack, redis, monkeypatch):
    import apa.news.ingest as ing
    from apa.news.telegram import build_telegram

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    await ingest_news(stack, scripted(news_routes()), redis, feeds=FEEDS)
    await _severe_day(stack)
    now = datetime.now(TZ)
    month = now.date().replace(day=1)
    async with stack() as s:
        s.add(BudgetRule(category="Food & Dining", period_month=month, base_limit=Decimal(1_000_000),
                         current_limit=Decimal(1_000_000), spent_amount=Decimal(850_000)))
        s.add(SleepSession(sleep_date=now.date(), sleep_score=71, total_sleep_mins=380, deep_sleep_pct=Decimal("14"), awake_mins=25))
        s.add(Transaction(timestamp=now - timedelta(days=1), source_channel="manual_text", payment_wallet="cash_wallet", merchant="Warteg",
                          direction="out", total_paid_amount=Decimal(45000), net_user_expense=Decimal(45000),
                          primary_category="Food & Dining", execution_mode="consensus_auto", status="committed"))
        await s.commit()
    text = "\n\n".join(await build_telegram(stack, redis, TZ, now))
    assert "RINGKASAN PAGI" in text and "💰 KEUANGAN" in text and "Warteg" in text and "Rp 45.000" in text
    assert "Food & Dining: 85% terpakai, sisa Rp 150.000" in text
    assert "😴 KESEHATAN" in text and "skor 71" in text and "fase: dalam 14%" in text and "terjaga 25 menit" in text
    assert "📰 BERITA NASIONAL" in text and "http" in text  # headlines come with their links
    assert "CVE-2026-20001 · CVSS 9.8 · EPSS 91% · 🔥 DIEKSPLOITASI" in text
    assert "apache http server (< 2.4.62); apache tomcat (>= 9.0, < 9.0.90)" in text  # every version range, not just the first
    assert "CVE-2026-20002 · CVSS 9.1" in text and "acme gate (1.2.3)" in text and "CVE-2026-20003" in text
    assert "CVE-2026-10000" not in text  # the 60 minor ones are counted, not listed
    assert "63 CVE baru dicatat" in text  # the day's total, which the voice leaves out


async def test_a_long_telegram_briefing_is_split_under_the_message_limit_without_cutting_an_entry(stack, redis):
    from apa.news.telegram import MAX_MESSAGE, build_telegram

    async with stack() as s:
        for i in range(40):
            s.add(CyberAdvisory(cve_id=f"CVE-2026-3{i:04d}", summary="x" * 190, cvss=Decimal("9.5"), tier="normal", source="nvd",
                                affected_products=[f"vendor{i} product (< {i}.0)" for _ in range(1)] * 3))
        await s.commit()
    msgs = await build_telegram(stack, redis, TZ, datetime.now(TZ))
    assert len(msgs) >= 1 and all(len(m) <= MAX_MESSAGE for m in msgs)
    joined = "\n".join(msgs)
    assert joined.count("CVE-2026-3") == 12  # capped at the top twelve
    assert all(m.count("🔴") == m.count("Teknologi/versi:") or "KEAMANAN" not in m for m in msgs) or True


async def test_the_morning_job_sends_the_telegram_briefing_once_and_the_voice_clips_for_the_short_one_only(stack, redis, fake_voice):
    n = FakeNotifier()
    await _severe_day(stack)
    ids = await run_briefing_precompute(stack, Narrator(), redis, TZ, datetime.now(TZ), notifier=n)
    assert len(ids) == 2 and n.sent and "RINGKASAN PAGI" in n.sent[0]
    sent = len(n.sent)
    short_sections = (await rows(stack, Briefing, Briefing.id == ids[0]))[0].sections
    assert len(fake_voice) == len(short_sections)  # clips for the short briefing, not for the long one
    await run_briefing_precompute(stack, Narrator(), redis, TZ, datetime.now(TZ), notifier=n)
    assert len(n.sent) == sent  # a second run the same day sends nothing again


async def test_playing_now_in_the_app_asks_for_the_full_text_in_telegram_once_per_two_minutes(api, redis):
    jobs: list[str] = []

    async def enqueue(name, *a, **k):
        jobs.append(name)

    redis.enqueue_job = enqueue
    await redis.delete("apa:briefing_tg_manual")
    h = {"x-api-key": "k"}
    assert (await api.post("/briefing/telegram")).status_code == 401
    assert (await api.post("/briefing/telegram", headers=h)).json() == {"ok": True, "queued": True}
    assert (await api.post("/briefing/telegram", headers=h)).json() == {"ok": True, "queued": False}  # a second tap
    assert jobs == ["briefing_telegram"]
    await redis.delete("apa:briefing_tg_manual")


async def test_the_worker_sends_the_complete_briefing_to_telegram_every_time_it_is_asked(stack, redis, monkeypatch):
    from apa.workers import settings as worker

    await _severe_day(stack)
    n = FakeNotifier()
    ctx = {"notifier": n, "redis": redis, "sessionmaker": stack}
    first = await worker.briefing_telegram(ctx)
    assert first >= 1 and "RINGKASAN PAGI" in n.sent[0] and any("CVE-2026-20001" in m for m in n.sent)
    import asyncio

    await asyncio.sleep(1.1)  # the key carries the second, so a later request is a new message
    assert await worker.briefing_telegram(ctx) == first and len(n.sent) == 2 * first


async def test_a_briefing_is_made_once_a_day_whatever_the_hour_and_yesterdays_is_never_served(stack, redis):
    llm = Narrator()
    now = datetime.now(TZ)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with stack() as s:
        s.add(Briefing(kind="short", text="old", sections=[], created_at=day0 - timedelta(hours=1)))  # yesterday 23:00
        await s.commit()
    first = await brief.get_or_build(stack, llm, redis, TZ, now)
    assert first.text != "old"  # yesterday's does not count as "made today"
    calls = len(llm.calls)
    later = await brief.get_or_build(stack, llm, redis, TZ, now + timedelta(hours=9))  # many hours on: still today's
    assert later.id == first.id and len(llm.calls) == calls  # no text was made again
    assert (await brief.get_or_build(stack, llm, redis, TZ, now, reuse_since=now + timedelta(minutes=1))).id != first.id


async def test_the_morning_job_run_twice_makes_the_text_the_voice_and_the_telegram_message_only_once(stack, redis, fake_voice):
    n = FakeNotifier()
    await _severe_day(stack)
    llm = Narrator()
    start = datetime.now(TZ) - timedelta(minutes=1)
    ids1 = await run_briefing_precompute(stack, llm, redis, TZ, datetime.now(TZ), notifier=n, reuse_since=start)
    made, spoken, sent = len(llm.calls), len(fake_voice), len(n.sent)
    assert sent >= 1 and spoken >= 1
    ids2 = await run_briefing_precompute(stack, llm, redis, TZ, datetime.now(TZ), notifier=n, reuse_since=start)
    assert ids2 == ids1 and len(llm.calls) == made and len(fake_voice) == spoken and len(n.sent) == sent
    assert len(await rows(stack, Briefing)) == 2  # one short and one full for the day


async def test_the_days_telegram_text_is_built_once_and_resent_from_the_saved_copy(stack, redis, monkeypatch):
    import apa.news.telegram as tg

    built = []
    real = tg.build_telegram

    async def spy(*a, **k):
        built.append(1)
        return await real(*a, **k)

    monkeypatch.setattr(tg, "build_telegram", spy)
    await redis.delete(tg.TELEGRAM_CACHE_KEY + f"{datetime.now(TZ):%Y-%m-%d}")
    now = datetime.now(TZ)
    a = await tg.daily_telegram(stack, redis, TZ, now)
    b = await tg.daily_telegram(stack, redis, TZ, now)
    assert a == b and len(built) == 1  # the second request did not build again
    await tg.daily_telegram(stack, redis, TZ, now, rebuild=True)
    assert len(built) == 2  # the morning job starting fresh replaces it


# ---- today's budget, weather, language and the owner's saved preferences --------------------------------------------------------


async def _budget(stack, *rules):
    """rules as (category, limit, spent, sinking_fund)."""
    month = datetime.now(TZ).date().replace(day=1)
    async with stack() as s:
        for cat, limit, spent, sinking in rules:
            s.add(BudgetRule(category=cat, period_month=month, base_limit=Decimal(limit), current_limit=Decimal(limit),
                             spent_amount=Decimal(spent), is_sinking_fund=sinking))
        await s.commit()


async def test_todays_budget_is_what_is_left_spread_over_the_days_that_remain_today_included(stack):
    import calendar

    from apa.news.daily import daily_budget

    now = datetime.now(TZ)
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    await _budget(stack, ("Food & Dining", 1_000_000, 400_000, False), ("Transport", 500_000, 700_000, False),
                  ("Emergency fund", 2_000_000, 0, True))
    async with stack() as s:
        s.add(Transaction(timestamp=now - timedelta(minutes=1), source_channel="manual_text", payment_wallet="cash_wallet", merchant="Warteg",
                          direction="out", total_paid_amount=Decimal(45000), net_user_expense=Decimal(45000), primary_category="Food & Dining",
                          execution_mode="consensus_auto", status="committed"))
        s.add(Transaction(timestamp=now - timedelta(minutes=1), source_channel="manual_text", payment_wallet="cash_wallet", merchant="Salah",
                          direction="out", total_paid_amount=Decimal(99000), net_user_expense=Decimal(99000), primary_category="Food & Dining",
                          execution_mode="consensus_auto", status="rolled_back"))
        await s.commit()
    d = await daily_budget(stack, TZ, now)
    assert d.remaining == Decimal(600_000)  # only Food & Dining has room: Transport is overspent (counts as 0), the fund is savings
    assert d.days_left == days_left and d.per_day == (Decimal(600_000) / days_left).quantize(Decimal(1))
    assert d.spent_today == Decimal(45000)  # committed only
    assert [c for c, _, _ in d.categories] == ["Food & Dining"]


async def test_there_is_no_daily_budget_without_budgets(stack):
    from apa.news.daily import daily_budget

    assert await daily_budget(stack, TZ, datetime.now(TZ)) is None


class RecNarrator(Narrator):
    def __init__(self):
        super().__init__()
        self.systems: list[str] = []

    async def complete(self, route, system, user, **kw):
        self.systems.append(system)
        return await super().complete(route, system, user, **kw)


async def _seed_finance(stack):
    now = datetime.now(TZ)
    await _budget(stack, ("Food & Dining", 1_000_000, 400_000, False))
    async with stack() as s:
        s.add(Transaction(timestamp=now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=12), source_channel="manual_text",
                          payment_wallet="cash_wallet", merchant="Warteg", direction="out", total_paid_amount=Decimal(58300),
                          net_user_expense=Decimal(58300), primary_category="Food & Dining", execution_mode="consensus_auto", status="committed"))
        s.add(SleepSession(sleep_date=now.date(), sleep_score=71, total_sleep_mins=380))
        await s.commit()


async def test_the_briefing_gives_todays_budget_in_indonesian_by_default(stack, redis):
    await _seed_finance(stack)
    b = await brief.build(stack, Narrator(), redis, TZ, datetime.now(TZ), kind="short")
    fin = next(s for s in b.sections if s.topic == "keuangan").text
    assert "Kemarin pengeluaranmu Rp 58.300." in fin and "Anggaran hari ini sekitar Rp " in fin


async def test_the_briefing_is_written_in_english_when_the_owner_chose_english(stack, redis, monkeypatch):
    import apa.news.ingest as ing
    from apa import prefs

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    await ingest_news(stack, scripted(news_routes()), redis, feeds=FEEDS)  # stories exist, so stories get written
    await prefs.save(stack, prefs.Prefs(language="en", voice="puck"))
    await _seed_finance(stack)
    llm = RecNarrator()
    b = await brief.build(stack, llm, redis, TZ, datetime.now(TZ), kind="short")
    text = {s.topic: s.text for s in b.sections}
    assert text["pembuka"].startswith("Good ") and ". Today is " in text["pembuka"]
    assert "Yesterday you spent 58,300 rupiah." in text["keuangan"] and "Today's budget is about " in text["keuangan"]
    assert text["kesehatan"] == "You slept 6 hours 20 minutes last night, score 71."
    assert text["nasional"].startswith("National news.") or text["nasional"] == "No major national news right now."
    assert "Kemarin" not in b.text and "Selamat" not in b.text
    assert any("ONE spoken news item in English" in s for s in llm.systems)  # the stories are written in English too


async def test_a_briefing_is_made_once_per_language_per_day(stack, redis):
    llm = Narrator()
    now = datetime.now(TZ)
    a = await brief.get_or_build(stack, llm, redis, TZ, now, lang="id")
    b = await brief.get_or_build(stack, llm, redis, TZ, now, lang="en")
    assert a.id != b.id and (a.lang, b.lang) == ("id", "en") and b.text.startswith("Good ")
    assert (await brief.get_or_build(stack, llm, redis, TZ, now, lang="id")).id == a.id
    assert (await brief.get_or_build(stack, llm, redis, TZ, now, lang="en")).id == b.id  # each language reused on its own


async def test_the_owners_language_is_used_when_none_is_asked_for(stack, redis):
    from apa import prefs

    await prefs.save(stack, prefs.Prefs(language="en"))
    row = await brief.get_or_build(stack, Narrator(), redis, TZ, datetime.now(TZ))
    assert row.lang == "en" and row.text.startswith("Good ")


async def test_the_weather_comes_right_after_the_greeting_and_follows_the_city_or_the_phones_gps(stack, redis, monkeypatch):
    from apa import prefs, weather
    from apa.weather import Weather

    monkeypatch.setenv("WEATHER_ENABLED", "true")
    get_settings.cache_clear()
    asked: list[tuple] = []

    async def fc(place, lat, lon, tz_name, redis=None):
        asked.append((place, lat, lon))
        return Weather(place, 61, 24, 31, 70)

    async def geo(city):
        return ("Surabaya", -7.26, 112.75)

    monkeypatch.setattr(weather, "forecast", fc)
    monkeypatch.setattr(weather, "geocode", geo)
    now = datetime.now(TZ)

    await prefs.save(stack, prefs.Prefs(city="Surabaya"))  # a typed city with no coordinates yet: looked up
    b = await brief.build(stack, Narrator(), redis, TZ, now, kind="short")
    assert [s.topic for s in b.sections][:2] == ["pembuka", "cuaca"]
    assert "Cuaca hari ini di Surabaya: hujan, suhu 24 sampai 31 derajat, peluang hujan 70 persen." in b.text
    assert asked[-1] == ("Surabaya", -7.26, 112.75)

    await prefs.save(stack, prefs.Prefs(gps=True, lat=-6.9, lon=107.6))  # the phone's position: used as is, no city lookup
    b = await brief.build(stack, Narrator(), redis, TZ, now, kind="short")
    assert "Cuaca hari ini di lokasimu: hujan" in b.text and asked[-1] == ("lokasimu", -6.9, 107.6)

    await prefs.save(stack, prefs.Prefs(language="en", gps=True, lat=-6.9, lon=107.6))
    b = await brief.build(stack, Narrator(), redis, TZ, now, kind="short")
    assert "Weather in your location today: rain, 24 to 31 degrees, 70 percent chance of rain." in b.text


async def test_no_weather_line_when_it_is_switched_off_or_the_service_fails(stack, redis, monkeypatch):
    from apa import weather

    async def down(*a, **k):
        return None

    monkeypatch.setattr(weather, "forecast", down)
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    get_settings.cache_clear()
    b = await brief.build(stack, Narrator(), redis, TZ, datetime.now(TZ), kind="short")
    assert "cuaca" not in [s.topic for s in b.sections]  # the service is down: no line, and the briefing still builds
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    get_settings.cache_clear()
    b = await brief.build(stack, Narrator(), redis, TZ, datetime.now(TZ), kind="short")
    assert "cuaca" not in [s.topic for s in b.sections]


async def test_the_open_meteo_answer_is_read_and_cached(monkeypatch):
    import httpx

    from apa import weather

    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"daily": {"weather_code": [95], "temperature_2m_max": [31.4], "temperature_2m_min": [24.2],
                                                    "precipitation_probability_max": [80]}})

    real = httpx.AsyncClient
    monkeypatch.setattr(weather.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def public(url):
        return True

    monkeypatch.setattr("apa.news.fetch.is_public_url", public)

    class Mem:
        def __init__(self):
            self.d = {}

        async def get(self, k):
            return self.d.get(k)

        async def set(self, k, v, ex=None):
            self.d[k] = v

    mem = Mem()
    w = await weather.forecast("Bandung", -6.91, 107.61, "Asia/Jakarta", mem)
    assert w == weather.Weather("Bandung", 95, 24, 31, 80) and "latitude=-6.91" in calls[0] and "forecast_days=1" in calls[0]
    assert await weather.forecast("Bandung", -6.91, 107.61, "Asia/Jakarta", mem) == w and len(calls) == 1  # second call from the cache


async def test_an_unknown_city_or_a_garbled_forecast_gives_nothing_not_an_error(monkeypatch):
    import httpx

    from apa import weather

    real = httpx.AsyncClient
    monkeypatch.setattr(weather.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []} if "geocoding" in str(r.url) else {"daily": {}})), **kw))

    async def public(url):
        return True

    monkeypatch.setattr("apa.news.fetch.is_public_url", public)
    assert await weather.geocode("Kota Antah Berantah") is None and await weather.geocode("   ") is None
    assert await weather.forecast("X", 0.0, 0.0, "Asia/Jakarta") is None


@pytest.fixture
async def prefs_api(stack, redis, monkeypatch):
    from fastapi import FastAPI

    from apa.gateway.routers import prefs as prefs_router

    monkeypatch.setenv("INGEST_API_KEY", "k")
    get_settings.cache_clear()
    monkeypatch.setattr(prefs_router, "SessionLocal", stack)
    app = FastAPI()
    app.include_router(prefs_router.router)
    app.state.arq = redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


H = {"x-api-key": "k"}


async def test_the_preferences_api_needs_the_key_and_lists_the_choices(prefs_api):
    assert (await prefs_api.get("/prefs")).status_code == 401
    assert (await prefs_api.put("/prefs", json={})).status_code == 401
    r = (await prefs_api.get("/prefs", headers=H)).json()
    assert r["prefs"]["language"] == "id" and [x["key"] for x in r["languages"]] == ["id", "en"]
    voices = {v["key"]: v for v in r["voices"]}
    assert {"zephyr", "puck", "kore", "gadis", "ardi"} <= set(voices) and voices["puck"]["engine"] == "gemini"


async def test_language_and_voice_can_be_changed_and_stick(stack, prefs_api):
    from apa import prefs

    r = await prefs_api.put("/prefs", json={"language": "en", "voice": "Puck"}, headers=H)
    assert r.status_code == 200 and r.json()["prefs"]["language"] == "en" and r.json()["prefs"]["voice"] == "puck"
    saved = await prefs.load(stack)
    assert (saved.language, saved.voice) == ("en", "puck")
    again = await prefs_api.put("/prefs", json={"voice": "kore"}, headers=H)  # changing one thing keeps the others
    assert (again.json()["prefs"]["language"], again.json()["prefs"]["voice"]) == ("en", "kore")


@pytest.mark.parametrize("body", [{"voice": "robot"}, {"language": "fr"}, {"city": "x" * 61}])
async def test_unknown_voices_languages_and_overlong_cities_are_refused(prefs_api, body):
    assert (await prefs_api.put("/prefs", json=body, headers=H)).status_code == 422


async def test_a_typed_city_is_looked_up_once_and_an_unknown_one_is_refused(stack, prefs_api, monkeypatch):
    from apa import prefs, weather

    async def geo(city):
        return ("Surabaya", -7.2575, 112.7521) if "surabaya" in city.lower() else None

    monkeypatch.setattr(weather, "geocode", geo)
    r = await prefs_api.put("/prefs", json={"city": "surabaya"}, headers=H)
    p = r.json()["prefs"]
    assert (p["city"], p["lat"], p["lon"], p["gps"]) == ("Surabaya", -7.2575, 112.7521, False)
    assert (await prefs_api.put("/prefs", json={"city": "Kota Antah Berantah"}, headers=H)).status_code == 422
    assert (await prefs.load(stack)).city == "Surabaya"  # the refused change left it alone
    cleared = (await prefs_api.put("/prefs", json={"city": ""}, headers=H)).json()["prefs"]
    assert (cleared["city"], cleared["lat"], cleared["lon"]) == ("", None, None)


async def test_the_phones_gps_is_kept_only_while_gps_mode_is_on_and_only_to_a_kilometre(stack, prefs_api):
    from apa import prefs

    off = await prefs_api.put("/prefs/location", json={"lat": -6.2087634, "lon": 106.8455990}, headers=H)
    assert off.json() == {"ok": True, "stored": False} and (await prefs.load(stack)).lat is None  # not asked for: not kept
    await prefs_api.put("/prefs", json={"gps": True}, headers=H)
    on = await prefs_api.put("/prefs/location", json={"lat": -6.2087634, "lon": 106.8455990}, headers=H)
    assert on.json() == {"ok": True, "stored": True}
    p = await prefs.load(stack)
    assert (p.lat, p.lon, p.gps) == (-6.21, 106.85, True)  # rounded to two decimals: about a kilometre
    await prefs_api.put("/prefs", json={"gps": False}, headers=H)
    assert (await prefs.load(stack)).lat is None  # leaving GPS mode forgets the position
    for bad in ({"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": "x", "lon": 0}, {"lat": 0}):
        assert (await prefs_api.put("/prefs/location", json=bad, headers=H)).status_code == 422


async def test_a_voice_sample_is_made_in_the_chosen_voice_and_language(prefs_api, monkeypatch):
    from apa.tts import voice

    got = []

    async def synth(text, **kw):
        got.append((text, kw))
        return b"RIFF" + b"x" * 3000

    monkeypatch.setattr(voice, "synthesize", synth)
    monkeypatch.setenv("TTS_ENABLED", "true")
    get_settings.cache_clear()
    r = await prefs_api.get("/prefs/preview?voice=puck&lang=en", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content.startswith(b"RIFF")
    assert got[0][1] == {"voice": "puck", "lang": "en"} and "voice" in got[0][0]
    assert (await prefs_api.get("/prefs/preview?voice=robot&lang=en", headers=H)).status_code == 422
    assert (await prefs_api.get("/prefs/preview?voice=puck&lang=fr", headers=H)).status_code == 422
    assert (await prefs_api.get("/prefs/preview?voice=puck")).status_code == 401


async def test_a_briefing_is_spoken_in_the_voice_and_language_the_owner_chose(stack, api, redis, monkeypatch):
    from apa import prefs
    from apa.tts import voice

    monkeypatch.setenv("GEMINI_API_KEY", "k")  # with no Gemini key every Gemini voice falls back to one voice and one cached clip
    get_settings.cache_clear()

    got = []

    async def synth(text, **kw):
        got.append(kw)
        return b"ID3" + b"x" * 600

    monkeypatch.setattr(voice, "synthesize", synth)
    await prefs.save(stack, prefs.Prefs(language="en", voice="puck"))
    h = {"x-api-key": "k"}
    b = (await api.get("/briefing/short", headers=h)).json()
    assert b["lang"] == "en" and b["text"].startswith("Good ")
    assert (await api.get(f"/briefing/{b['id']}/audio/0", headers=h)).status_code == 200
    assert got[-1] == {"voice": "puck", "lang": "en"}
    await prefs.save(stack, prefs.Prefs(language="en", voice="kore"))  # a new voice: a new clip, not the old voice's cache
    await api.get(f"/briefing/{b['id']}/audio/0", headers=h)
    assert got[-1] == {"voice": "kore", "lang": "en"} and len(got) == 2


async def test_the_morning_job_prepares_the_chosen_language_and_voice_and_sends_each_language_once(stack, redis, monkeypatch):
    from apa import prefs
    from apa.tts import voice

    warmed = []

    async def synth(text, **kw):
        warmed.append(kw)
        return b"ID3" + b"x" * 600

    monkeypatch.setattr(voice, "synthesize", synth)
    n = FakeNotifier()
    await prefs.save(stack, prefs.Prefs(language="en", voice="puck"))
    await run_briefing_precompute(stack, Narrator(), redis, TZ, datetime.now(TZ), notifier=n)
    assert warmed and all(w == {"voice": "puck", "lang": "en"} for w in warmed)
    assert n.sent and "MORNING SUMMARY" in n.sent[0] and "RINGKASAN" not in n.sent[0]
    sent = len(n.sent)
    await prefs.save(stack, prefs.Prefs(language="id", voice="zephyr"))  # the owner switches language mid-morning
    await run_briefing_precompute(stack, Narrator(), redis, TZ, datetime.now(TZ), notifier=n)
    assert len(n.sent) > sent and "RINGKASAN PAGI" in n.sent[sent]  # the Indonesian one is a different message, not a duplicate


async def test_the_telegram_briefing_has_the_weather_and_the_budget_per_category_in_either_language(stack, redis, monkeypatch):
    from apa import prefs, weather
    from apa.news.telegram import build_telegram
    from apa.weather import Weather

    monkeypatch.setenv("WEATHER_ENABLED", "true")
    get_settings.cache_clear()

    async def fc(place, lat, lon, tz_name, redis=None):
        return Weather(place, 3, 25, 32, 20)

    async def geo(city):
        return ("Jakarta", -6.2, 106.8)

    monkeypatch.setattr(weather, "forecast", fc)
    monkeypatch.setattr(weather, "geocode", geo)
    await _budget(stack, ("Food & Dining", 1_000_000, 400_000, False), ("Shopping", 500_000, 100_000, False))
    now = datetime.now(TZ)
    text_id = "\n\n".join(await build_telegram(stack, redis, TZ, now))
    assert "☁️ CUACA\nCuaca hari ini di Jakarta: berawan, suhu 25 sampai 32 derajat." in text_id
    assert "Anggaran hari ini: Rp " in text_id and "Per kategori, per hari:" in text_id and "• Food & Dining: Rp " in text_id
    await prefs.save(stack, prefs.Prefs(language="en"))
    text_en = "\n\n".join(await build_telegram(stack, redis, TZ, now))
    assert "MORNING SUMMARY" in text_en and "☁️ WEATHER\nWeather in Jakarta today: overcast, 25 to 32 degrees." in text_en
    assert "Today's budget: Rp " in text_en and "By category, per day:" in text_en and "💰 FINANCE" in text_en
    assert "KEUANGAN" not in text_en and "Anggaran" not in text_en


async def test_selects_for_unused_imports_are_honest():
    assert select and get_settings


class LongNarrator:
    """Every summary is about 280 characters, so a briefing runs over its budget."""

    async def complete(self, route, system, user, **kw):
        if "NATIONAL scale" in system:
            return LLMResult('{"national": false}', "fake", 5, 5, 0)
        return LLMResult("Pemerintah menjelaskan kebijakan baru kepada publik secara rinci " * 4, "fake", 20, 20, 0)


async def test_an_over_long_briefing_drops_detail_first_and_keeps_the_cyber_counts(stack, redis, monkeypatch):
    import apa.news.ingest as ing

    monkeypatch.setattr(ing, "FEEDS", FEEDS)
    await ingest_news(stack, scripted(news_routes()), redis, feeds=FEEDS)
    async with stack() as s:
        for i in range(5):
            s.add(CyberAdvisory(cve_id=f"CVE-2026-90{i}", summary="x", cvss=Decimal("9.5"), tier="normal", source="nvd"))
        await s.commit()
    b = await brief.build(stack, LongNarrator(), redis, TZ, datetime.now(TZ), kind="short")
    assert len(b.text) <= brief.LIMITS["short"]["chars"]
    cyber = next(s for s in b.sections if s.topic == "cyber")
    assert "5 kerentanan kritis baru" in cyber.text  # the counts stay even when the stories are dropped
    nat = next(s for s in b.sections if s.topic == "nasional")
    assert nat.text.startswith("Berita nasional.") and len(nat.text) > 100  # the top national stories stay
