"""News and cyber, pure logic: hostile feeds, the fetcher's safety limits, clustering, scoring, CVE tiers and the
guard that keeps a spoken number honest. No network and no database."""

import json
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from apa.news import fetch
from apa.news.cyber import Cve, parse_kev, parse_nvd, severity, tier
from apa.news.fetch import FetchError, fetch_bytes, is_public_url, parse_feed
from apa.news.rank import Article, cluster, rank, score, sensitive_claim, tokens
from apa.news.summarize import attribution, fallback, grounded, summarize

NOW = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Presiden teken aturan baru</title><link>https://a.example/1</link>
<pubDate>Sat, 24 Oct 2026 05:00:00 GMT</pubDate><description>&lt;p&gt;Isi &lt;b&gt;berita&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title></title><link>https://a.example/2</link></item>
<item><title>Tanpa tautan</title></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Judul atom</title><link href="https://b.example/x"/><updated>2026-10-24T04:00:00Z</updated>
<summary>Ringkas</summary></entry></feed>"""


# ---- parsing hostile input -----------------------------------------------------------------------------------


def test_rss_items_are_parsed_and_tags_stripped_and_incomplete_items_dropped():
    (e,) = parse_feed(RSS)
    assert (e.title, e.url, e.summary) == ("Presiden teken aturan baru", "https://a.example/1", "Isi berita")
    assert e.published == datetime(2026, 10, 24, 5, tzinfo=UTC)


def test_atom_entries_are_parsed():
    (e,) = parse_feed(ATOM)
    assert (e.title, e.url, e.published) == ("Judul atom", "https://b.example/x", datetime(2026, 10, 24, 4, tzinfo=UTC))


@pytest.mark.parametrize(
    "xml",
    [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><rss><channel><item>'
        b"<title>&a;</title><link>https://x</link></item></channel></rss>",  # external entity
        b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><rss>&b;</rss>',  # expansion
        b"not xml at all",
        b"",
        b"<rss><channel><item>",
    ],
)
def test_a_hostile_or_broken_feed_is_refused_not_half_read(xml):
    with pytest.raises(FetchError):
        parse_feed(xml)


# ---- the fetcher's safety limits ---------------------------------------------------------------------------------


def stub_dns(monkeypatch, mapping):
    def fake(host, port, *a, **k):
        if host not in mapping:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], 0))]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", fake)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/x", "http://localhost/x", "http://192.168.2.191:8000/health", "http://10.10.10.12:3900/",
     "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd", "ftp://example.com/x", "gopher://x",
     "https://user:pw@example.com/feed", "https:///nohost", "javascript:alert(1)"],
)
async def test_internal_and_non_http_addresses_are_refused(monkeypatch, url):
    stub_dns(monkeypatch, {"localhost": "127.0.0.1", "example.com": "93.184.216.34"})
    assert await is_public_url(url) is False


async def test_a_public_http_address_is_allowed_and_a_host_with_any_private_record_is_not(monkeypatch):
    stub_dns(monkeypatch, {"news.example": "93.184.216.34", "evil.example": "10.0.0.5"})
    assert await is_public_url("https://news.example/feed") is True
    assert await is_public_url("https://evil.example/feed") is False  # a public name pointing inside
    assert await is_public_url("https://nxdomain.example/") is False


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_redirect_to_an_internal_address_is_blocked_on_that_hop(monkeypatch):
    stub_dns(monkeypatch, {"news.example": "93.184.216.34", "inside.lan": "192.168.1.10"})
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"location": "http://inside.lan/secret"})

    with pytest.raises(FetchError, match="blocked"):
        await fetch_bytes(client_for(handler), "https://news.example/feed")
    assert seen == ["https://news.example/feed"]  # the internal host was never contacted


async def test_redirects_are_followed_a_few_times_and_then_stop(monkeypatch):
    stub_dns(monkeypatch, {"news.example": "93.184.216.34"})

    def handler(req):
        return httpx.Response(302, headers={"location": "https://news.example/again"})

    with pytest.raises(FetchError, match="redirects"):
        await fetch_bytes(client_for(handler), "https://news.example/feed")


async def test_an_oversized_response_is_cut_off(monkeypatch):
    stub_dns(monkeypatch, {"news.example": "93.184.216.34"})
    big = httpx.Response(200, content=b"x" * 50_000)
    with pytest.raises(FetchError, match="too large"):
        await fetch_bytes(client_for(lambda r: big), "https://news.example/feed", max_bytes=10_000)
    ok = await fetch_bytes(client_for(lambda r: httpx.Response(200, content=b"<rss/>")), "https://news.example/f")
    assert ok == b"<rss/>"


async def test_http_errors_and_network_errors_become_fetch_errors(monkeypatch):
    stub_dns(monkeypatch, {"news.example": "93.184.216.34"})
    with pytest.raises(FetchError, match="http 503"):
        await fetch_bytes(client_for(lambda r: httpx.Response(503)), "https://news.example/f")

    def boom(req):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(FetchError, match="ConnectTimeout"):
        await fetch_bytes(client_for(boom), "https://news.example/f")


# ---- clustering and scoring ----------------------------------------------------------------------------------------


def art(source, title, hours_ago=1, topic="nasional", summary=""):
    return Article(source, title, f"https://{source.lower().replace(' ', '')}.example/{abs(hash(title)) % 10**6}",
                   NOW - timedelta(hours=hours_ago), summary, topic)


def test_the_same_event_from_several_outlets_becomes_one_cluster():
    arts = [art("Antara", "Pemerintah umumkan kenaikan tarif listrik mulai November"),
            art("CNN Indonesia", "Tarif listrik naik mulai November, pemerintah umumkan"),
            art("Detik", "Pemerintah umumkan tarif listrik naik November"),
            art("Antara", "Timnas menang telak atas lawan di laga persahabatan")]
    cs = cluster(arts)
    assert sorted(len(c.articles) for c in cs) == [1, 3]
    big = next(c for c in cs if len(c.articles) == 3)
    assert big.sources == ["Antara", "CNN Indonesia", "Detik"]


def test_unrelated_headlines_and_different_topics_never_merge():
    a = art("Antara", "Presiden lantik menteri baru")
    b = art("Detik", "Gempa magnitudo 6 guncang wilayah selatan")
    c = Article("Hacker", "Presiden lantik menteri baru", "https://h.example/1", NOW, "", "cyber")
    assert len(cluster([a, b, c])) == 3


def test_tokens_ignore_stopwords_and_short_words():
    assert tokens("Yang dan di ke the of ab Presiden Prabowo") == frozenset({"presiden", "prabowo"})


def test_national_policy_news_covered_by_many_outlets_scores_above_the_line_and_noise_does_not():
    policy = [art(s, "Pemerintah tetapkan tarif listrik baru, menteri jelaskan subsidi") for s in
              ("Antara", "CNN Indonesia", "Detik")]
    celeb = [art("Detik", "Artis ternama gelar konser amal di kota")]
    local = [art("Detik", "Warga desa gotong royong perbaiki jalan kabupaten")]
    ranked = rank(policy + celeb + local, NOW, topic="nasional")
    top = ranked[0]
    assert top.qualifies and len(top.cluster.sources) == 3
    assert not any(r.qualifies for r in ranked[1:])
    assert all(r.score < 24 for r in ranked[1:])


def test_a_stale_story_scores_lower_than_the_same_story_fresh():
    fresh = rank([art("Antara", "Presiden umumkan kebijakan pajak baru", hours_ago=1)], NOW, topic="nasional")[0]
    stale = rank([art("Antara", "Presiden umumkan kebijakan pajak baru", hours_ago=30)], NOW, topic="nasional")[0]
    assert fresh.score > stale.score


def test_the_middle_band_is_marked_ambiguous_for_a_model_to_decide():
    r = rank([art("Antara", "Kementerian buka pendaftaran program baru", hours_ago=10)], NOW, topic="nasional")[0]
    assert (r.qualifies, r.ambiguous) == (False, True) or r.qualifies  # one outlet + one central keyword


def test_cyber_news_mentioning_indonesia_or_exploitation_ranks_high():
    arts = [art("The Hacker News", "Actively exploited zero-day hits Indonesian bank servers", topic="cyber"),
            art("BleepingComputer", "Zero-day exploited in the wild, Indonesia banks affected", topic="cyber"),
            art("Krebs", "A thoughtful essay on password managers", topic="cyber")]
    ranked = rank(arts, NOW, topic="cyber")
    assert ranked[0].qualifies and "zero-day" in ranked[0].cluster.headline.lower()
    assert not ranked[-1].qualifies


def test_the_cluster_key_is_stable_across_runs():
    a = [art("Antara", "Presiden lantik menteri baru hari ini")]
    assert cluster(a)[0].key == cluster(list(a))[0].key and len(cluster(a)[0].key) == 24


def test_scores_never_raise_on_missing_dates():
    c = cluster([Article("Antara", "Presiden lantik menteri", "https://a.example/z", None, "", "nasional")])[0]
    assert isinstance(score(c, NOW), int)


@pytest.mark.parametrize("text,sens", [("Korban tewas 12 orang", True), ("Kerugian 5 miliar rupiah", True),
                                       ("Presiden membuka acara", False), ("Harga naik 5 persen", False)])
def test_sensitive_claims_are_recognised(text, sens):
    assert sensitive_claim(text) is sens


# ---- spoken text stays honest ----------------------------------------------------------------------------------------


class SayLLM:
    def __init__(self, text=None, fail=False):
        self.text, self.fail = text, fail

    async def complete(self, route, system, user, **kw):
        if self.fail:
            raise RuntimeError("down")
        from apa.integrations.llm import LLMResult

        return LLMResult(self.text, "fake", 10, 10, 0)


def policy_cluster(n=3, title="Pemerintah umumkan tarif listrik naik 12 persen mulai November"):
    return cluster([art(s, title) for s in ("Antara", "CNN Indonesia", "Detik")[:n]])[0]


async def test_a_summary_that_only_restates_the_sources_is_spoken():
    c = policy_cluster()
    text = await summarize(SayLLM("Tarif listrik naik 12 persen mulai November, menurut Antara dan Detik."), c)
    assert text.startswith("Tarif listrik naik 12 persen")


async def test_a_summary_with_an_invented_number_falls_back_to_the_headline():
    c = policy_cluster()
    text = await summarize(SayLLM("Tarif listrik naik 25 persen mulai November."), c)  # 25 is not in any source
    assert text == fallback(c) and "25" not in text and "12 persen" in text


async def test_a_model_outage_or_empty_answer_still_yields_a_spoken_item():
    c = policy_cluster()
    assert await summarize(SayLLM(fail=True), c) == fallback(c)
    assert await summarize(SayLLM(""), c) == fallback(c)


async def test_a_sensitive_claim_from_one_outlet_is_labelled_as_such():
    c = cluster([art("Detik", "Banjir tewaskan 12 korban di wilayah timur")])[0]
    text = await summarize(SayLLM("Banjir menewaskan 12 korban, menurut Detik."), c)
    assert text.endswith("Baru dilaporkan satu sumber.")
    two = cluster([art("Detik", "Banjir tewaskan 12 korban di wilayah timur"),
                   art("Antara", "Banjir tewaskan 12 korban di wilayah timur")])[0]
    assert "satu sumber" not in await summarize(SayLLM("Banjir menewaskan 12 korban."), two)


def test_attribution_names_the_outlets_in_natural_order():
    assert attribution(policy_cluster(1)) == "menurut Antara"
    assert attribution(policy_cluster(3)) == "menurut Antara, CNN Indonesia dan Detik"


def test_grounded_compares_digits_ignoring_separators():
    c = cluster([art("Antara", "Rupiah menguat ke Rp 16.250 per dolar")])[0]
    assert grounded("Rupiah menguat ke 16250 per dolar", c) and not grounded("Rupiah menguat ke 16.300", c)


# ---- CVE parsing and tiers --------------------------------------------------------------------------------------------


NVD = json.dumps({"vulnerabilities": [
    {"cve": {"id": "CVE-2026-0001", "descriptions": [{"lang": "en", "value": "RCE in Proxmox VE"}],
             "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.8}}]},
             "configurations": [{"nodes": [{"cpeMatch": [{"criteria": "cpe:2.3:a:proxmox:virtual_environment:8.0:*"}]}]}]}},
    {"cve": {"id": "CVE-2026-0002", "descriptions": [{"lang": "en", "value": "Fresh, not scored yet"}], "metrics": {}}},
    {"cve": {"id": "CVE-2026-0003", "descriptions": [{"lang": "es", "value": "solo español"}],
             "metrics": {"cvssMetricV2": [{"cvssData": {"baseScore": 5.0}}]}}},
]}).encode()

KEV = json.dumps({"vulnerabilities": [
    {"cveID": "CVE-2026-0100", "vendorProject": "Acme", "product": "Gate", "dateAdded": "2026-10-22",
     "shortDescription": "Actively exploited"},
    {"cveID": "CVE-2020-0001", "vendorProject": "Old", "product": "Thing", "dateAdded": "2020-01-01"},
    {"cveID": "CVE-2026-0101", "vendorProject": "Bad", "product": "Date", "dateAdded": "garbage"},
]}).encode()


def test_nvd_items_get_scores_products_and_english_descriptions():
    a, b, c = parse_nvd(NVD)
    assert (a.cve_id, a.cvss, a.products) == ("CVE-2026-0001", Decimal("9.8"), ["proxmox virtual environment (8.0)"])
    assert b.cvss is None and b.summary == "Fresh, not scored yet"  # unscored CVEs are kept
    assert c.summary == "" and c.cvss == Decimal("5.0")


def test_only_recently_added_kev_entries_are_taken_and_bad_dates_are_ignored():
    got = parse_kev(KEV, NOW)
    assert list(got) == ["CVE-2026-0100"] and got["CVE-2026-0100"].kev


@pytest.mark.parametrize(
    ("cve", "watch", "want"),
    [
        (Cve("c", "x", Decimal("9.8"), kev=True), [], "urgent"),
        (Cve("c", "RCE in proxmox ve", Decimal("9.1")), ["proxmox"], "urgent"),  # on the owner's watchlist
        (Cve("c", "x", Decimal("9.8")), [], "normal"),  # critical but nobody exploits it and it is not watched
        (Cve("c", "x", Decimal("7.5"), kev=True), [], "normal"),
        (Cve("c", "x", Decimal("7.5"), epss=Decimal("0.5")), [], "normal"),
        (Cve("c", "x", Decimal("7.5"), epss=Decimal("0.01")), [], "archive"),
        (Cve("c", "x", None, kev=True), [], "normal"),  # exploited and not scored yet: never lost
        (Cve("c", "x", None), [], "archive"),
        (Cve("c", "x", Decimal("5.0"), kev=True), [], "archive"),
    ],
)
def test_cve_tiers(cve, watch, want):
    assert tier(cve, watch) == want


@pytest.mark.parametrize(("score_", "sev"), [(Decimal("9.0"), "critical"), (Decimal("8.9"), "high"),
                                              (Decimal("4.0"), "medium"), (Decimal("3.9"), "low"), (None, "unscored")])
def test_severity_bands(score_, sev):
    assert severity(score_) == sev


# ---- DNS64 networks ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("64:ff9b::5db8:d822", True),  # NAT64 for 93.184.216.34: an IPv4-only site seen through DNS64
        ("64:ff9b::c0a8:101", False),  # NAT64 for 192.168.1.1: an internal host reached through the translator
        ("64:ff9b::7f00:1", False),  # NAT64 for 127.0.0.1
        ("64:ff9b::a9fe:a9fe", False),  # NAT64 for 169.254.169.254, the cloud metadata address
        ("::ffff:93.184.216.34", True),
        ("::ffff:192.168.1.1", False),  # IPv4-mapped private
        ("2606:4700::6812:864", True),  # an ordinary public IPv6
        ("fd00::1", False),
        ("::1", False),
    ],
)
async def test_dns64_addresses_are_judged_by_the_ipv4_they_carry(monkeypatch, address, public):
    stub_dns(monkeypatch, {"news.example": address})
    assert await is_public_url("https://news.example/feed") is public


# ---- speech text (tts/speakable.py) ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("n", "said"), [
    (0, "nol"), (11, "sebelas"), (15, "lima belas"), (21, "dua puluh satu"), (100, "seratus"), (111, "seratus sebelas"),
    (1000, "seribu"), (1001, "seribu satu"), (2026, "dua ribu dua puluh enam"), (11000, "sebelas ribu"),
    (1_500_000, "satu juta lima ratus ribu"), (58300, "lima puluh delapan ribu tiga ratus"),
])
def test_numbers_are_spelled_the_indonesian_way(n, said):
    from apa.tts.speakable import words

    assert words(n) == said


@pytest.mark.parametrize(("written", "spoken"), [
    ("Kemarin pengeluaranmu Rp 58.300.", "Kemarin pengeluaranmu lima puluh delapan ribu tiga ratus rupiah."),
    ("Rp1.250.000", "satu juta dua ratus lima puluh ribu rupiah"),
    ("Rp 1,5 juta", "satu koma lima juta rupiah"),
    ("sudah 85% terpakai", "sudah delapan puluh lima persen terpakai"),
    ("Food & Dining", "Food dan Dining"),
    ("skor 71 🔥", "skor tujuh puluh satu"),
    ("CVE-2026-0001 kritis", "C V E dua ribu dua puluh enam, nol nol nol satu kritis"),
    ("jam 06:30", "jam enam tiga puluh"),
    ("**penting**", "penting"),
])
def test_money_numbers_symbols_and_identifiers_are_made_speakable(written, spoken):
    from apa.tts.speakable import speakable

    assert speakable(written) == spoken


# ---- affected technology and versions, wake-time schedule ---------------------------------------------------------------------


def _nvd_with(matches):
    return json.dumps({"vulnerabilities": [{"cve": {
        "id": "CVE-2026-0500", "descriptions": [{"lang": "en", "value": "Flaw"}],
        "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.8}}]},
        "configurations": [{"nodes": [{"cpeMatch": matches}]}]}}]}).encode()


def test_nvd_gives_the_technology_with_its_vulnerable_version_range():
    (c,) = parse_nvd(_nvd_with([
        {"vulnerable": True, "criteria": "cpe:2.3:a:apache:http_server:*:*:*:*:*:*:*:*", "versionEndExcluding": "2.4.62"},
        {"vulnerable": True, "criteria": "cpe:2.3:a:vendor:app:*:*:*:*:*:*:*:*", "versionStartIncluding": "2.0",
         "versionEndExcluding": "2.4"},
        {"vulnerable": True, "criteria": "cpe:2.3:a:acme:gate:1.2.3:*:*:*:*:*:*:*"},
        {"vulnerable": True, "criteria": "cpe:2.3:a:acme:anyver:*:*:*:*:*:*:*:*"},
        {"vulnerable": False, "criteria": "cpe:2.3:o:linux:linux_kernel:*:*:*:*:*:*:*:*"},  # only the platform it runs on
    ]))
    assert c.products == ["acme anyver", "acme gate (1.2.3)", "apache http server (< 2.4.62)", "vendor app (>= 2.0, < 2.4)"]


def test_a_product_keeps_at_most_three_ranges_and_a_cve_at_most_eight_products():
    ranges = [{"vulnerable": True, "criteria": "cpe:2.3:a:v:p:*:*:*:*:*:*:*:*", "versionEndExcluding": f"{i}.0"} for i in range(1, 6)]
    many = [{"vulnerable": True, "criteria": f"cpe:2.3:a:v:prod{i}:*:*:*:*:*:*:*:*"} for i in range(12)]
    (c,) = parse_nvd(_nvd_with(ranges + many))
    assert len(c.products) == 8 and next(p for p in c.products if p.startswith("v p ")) == "v p (< 1.0; < 2.0; < 3.0)"


@pytest.mark.parametrize(("entry", "spoken"), [
    ("apache http server (< 2.4.62)", "apache http server, di bawah versi 2.4.62"),
    ("vendor app (>= 2.0, < 2.4.62)", "vendor app, mulai versi 2.0 dan di bawah versi 2.4.62"),
    ("acme gate (1.2.3)", "acme gate, versi 1.2.3"),
    ("x y (<= 5.1; < 6.0)", "x y, sampai versi 5.1"),
    ("plain product", "plain product"),
])
def test_a_technology_is_spoken_with_its_version_in_words(entry, spoken):
    from apa.news.cyber import spoken_product

    assert spoken_product(entry) == spoken


def test_version_numbers_are_read_digit_group_by_digit_group_not_as_thousands():
    from apa.tts.speakable import speakable

    assert speakable("di bawah versi 2.4.62") == "di bawah versi dua titik empat titik enam puluh dua"
    assert speakable("mulai versi 10.250") == "mulai versi sepuluh titik dua ratus lima puluh"
    assert speakable("Rp 10.250") == "sepuluh ribu dua ratus lima puluh rupiah"  # money keeps its thousands meaning


@pytest.mark.parametrize(("wake", "lead", "at"), [
    ("06:30", 25, (6, 5)), ("06:10", 25, (5, 45)), ("00:10", 25, (23, 45)), ("07:00", 0, (7, 0)),
    ("garbage", 25, (6, 5)), ("25:00", 25, (6, 5)), ("6", 25, (6, 5)),
])
def test_the_briefing_is_prepared_the_lead_time_before_the_wake_time(wake, lead, at):
    from apa.news.jobs import precompute_time

    assert precompute_time(wake, lead) == at


# ---- language, voice choice and weather (pure) ------------------------------------------------------------------------------


def test_the_greeting_and_money_read_naturally_in_both_languages():
    from apa.news.i18n import greeting, money

    at = datetime(2026, 10, 24, 6, 30)
    assert greeting(at, "id") == "Selamat pagi. Hari ini Sabtu, 24 Oktober."
    assert greeting(at, "en") == "Good morning. Today is Saturday, October 24."
    assert greeting(at.replace(hour=14), "en").startswith("Good afternoon") and greeting(at.replace(hour=20), "en").startswith("Good evening")
    assert greeting(at, "xx") == greeting(at, "id")  # an unknown language falls back, never an error
    assert greeting(at, "ja") == greeting(at, "en")  # translated languages are built from the English frames
    assert money(58300, "id") == "Rp 58.300" and money(58300, "en") == "58,300 rupiah" and money(1_250_000, "en") == "1,250,000 rupiah"


@pytest.mark.parametrize(("code", "id_words", "en_words"), [
    (0, "cerah", "clear"), (2, "berawan sebagian", "partly cloudy"), (45, "berkabut", "foggy"), (53, "gerimis", "drizzle"),
    (61, "hujan", "rain"), (65, "hujan lebat", "heavy rain"), (81, "hujan lokal", "scattered showers"),
    (96, "badai petir", "thunderstorms"), (999, "badai petir", "thunderstorms"),
])
def test_weather_codes_become_a_few_words_in_either_language(code, id_words, en_words):
    from apa.news.i18n import weather_words

    assert weather_words(code, "id") == id_words and weather_words(code, "en") == en_words


def test_the_weather_sentence_says_rain_odds_only_when_they_are_worth_saying():
    from apa.weather import Weather, sentence

    wet = Weather("Surabaya", 61, 24, 31, 70)
    assert sentence(wet, "id") == "Cuaca hari ini di Surabaya: hujan, suhu 24 sampai 31 derajat, peluang hujan 70 persen."
    assert sentence(wet, "en") == "Weather in Surabaya today: rain, 24 to 31 degrees, 70 percent chance of rain."
    dry = Weather("Jakarta", 1, 25, 33, 10)
    assert sentence(dry, "id") == "Cuaca hari ini di Jakarta: cerah berawan, suhu 25 sampai 33 derajat."
    assert "chance" not in sentence(Weather("X", 0, 20, 30, None), "en")


def test_preferences_accept_only_known_voices_languages_and_paired_coordinates():
    from apa.prefs import Prefs

    ok = Prefs(language="en", voice="Puck", city="  Surabaya   Timur ", lat=-7.26, lon=112.75, gps=True)
    assert (ok.voice, ok.city, ok.language) == ("puck", "Surabaya Timur", "en")
    for bad in ({"voice": "robot"}, {"language": "xx"}, {"lat": 1.0}, {"lon": 1.0}, {"lat": 91, "lon": 0}, {"lon": 181, "lat": 0},
                {"city": "x" * 61}):
        with pytest.raises(ValueError):
            Prefs(**bad)


def test_a_microsoft_voice_that_only_speaks_indonesian_is_swapped_for_an_english_one():
    from apa.prefs import VOICES, voice_name

    assert voice_name("gadis", "id") == ("edge", "id-ID-GadisNeural")
    assert voice_name("gadis", "en") == ("edge", "en-US-AvaMultilingualNeural")
    assert voice_name("ardi", "en") == ("edge", "en-US-AndrewMultilingualNeural")
    assert voice_name("puck", "id") == voice_name("puck", "en") == ("gemini", "Puck")  # Gemini voices speak both
    assert {"zephyr", "puck", "gadis", "ardi"} <= set(VOICES) and len(VOICES) >= 8


def test_the_voice_order_puts_the_chosen_engine_first_and_never_names_an_engine_without_its_key(monkeypatch):
    from apa.core.settings import get_settings
    from apa.tts import voice

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("TTS_ENGINE", "auto")
    get_settings.cache_clear()
    assert voice.plan("puck", "en") == [("gemini", "Puck"), ("edge", "en-US-AndrewMultilingualNeural")]  # backup: same gender
    assert voice.plan("gadis", "id") == [("edge", "id-ID-GadisNeural"), ("gemini", None)]
    assert voice.plan("zephyr", "ja")[1] == ("edge", "ja-JP-NanamiNeural")  # a Japanese backup for a Japanese briefing
    assert voice.plan(None, "id") == [("gemini", None), ("edge", None)]  # no choice made: the configured order, as before
    monkeypatch.delenv("GEMINI_API_KEY")
    get_settings.cache_clear()
    assert voice.plan("puck", "en") == [("edge", "en-US-AndrewMultilingualNeural")]  # Puck needs Gemini, which has no key here: only the fallback
    get_settings.cache_clear()


def test_the_cache_key_changes_with_voice_and_language_but_not_between_identical_requests(monkeypatch):
    from apa.core.settings import get_settings
    from apa.tts.voice import _key

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    get_settings.cache_clear()
    a = _key("Selamat pagi", "zephyr", "id")
    assert a == _key("Selamat pagi", "zephyr", "id")
    assert len({a, _key("Selamat pagi", "puck", "id"), _key("Selamat pagi", "zephyr", "en"), _key("Selamat siang", "zephyr", "id")}) == 4
    get_settings.cache_clear()


async def test_english_speech_is_not_run_through_the_indonesian_number_speller(monkeypatch):
    from apa.core.settings import get_settings
    from apa.tts import voice

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    get_settings.cache_clear()
    got = {}

    async def gem(text, name=None, lang="id"):
        got.update(text=text, name=name, lang=lang)
        return b"RIFF" + b"x" * 3000

    monkeypatch.setattr(voice, "_gemini", gem)
    await voice.synthesize("You spent 58,300 rupiah. 🔥 **Nice**", voice="puck", lang="en")
    assert got == {"text": "You spent 58,300 rupiah. Nice", "name": "Puck", "lang": "en"}  # digits kept, emoji and markup gone
    await voice.synthesize("Kemarin pengeluaranmu Rp 58.300", voice="puck", lang="id")
    assert "lima puluh delapan ribu" in got["text"] and got["lang"] == "id"  # Indonesian is still spelled out
    get_settings.cache_clear()


# ---- more languages, news topics and words to follow ------------------------------------------------------------------------


def test_languages_without_tables_are_translated_only_if_every_figure_survives():
    from apa.news.translate import faithful

    assert faithful("Spent 58,300 rupiah on 3 items", "3品目に58.300ルピアを使いました")  # separators may differ
    assert faithful("CVE-2026-1234 scored 9.8", "CVE-2026-1234のスコアは9.8")
    assert not faithful("Spent 58,300 rupiah", "ルピアを使いました")  # a missing figure is rejected
    assert faithful("Up 5 percent", "5と7パーセント上昇")  # extra numbers are tolerated, missing ones are not


def test_translate_returns_the_english_when_the_model_changes_a_figure():
    import asyncio

    from apa.news.translate import translate

    class Llm:
        def __init__(self, out):
            self.out = out

        async def complete(self, *a, **k):
            return type("R", (), {"content": self.out})()

    src = "Spent 58,300 rupiah."
    assert asyncio.run(translate(Llm("58.300ルピアを使いました。"), None, src, "ja")) == "58.300ルピアを使いました。"
    assert asyncio.run(translate(Llm("ルピアを使いました。"), None, src, "ja")) == src  # figure lost: kept as English
    assert asyncio.run(translate(Llm("x"), None, src, "en")) == src and asyncio.run(translate(Llm("x"), None, src, "id")) == src


def test_prefs_accept_every_listed_language_and_topic_and_refuse_others():
    from apa.news.i18n import LANGS
    from apa.news.sources import FEEDS, TOPICS
    from apa.prefs import LANGUAGES, Prefs

    assert set(LANGUAGES) == set(LANGS)
    assert {f.topic for f in FEEDS} == set(TOPICS)  # every topic has at least one feed, every feed a known topic
    for lang in LANGUAGES:
        assert Prefs(language=lang).language == lang
    assert Prefs(topics=["dunia", "cyber", "dunia"]).topics == ["dunia", "cyber"]  # no repeats, the owner's order
    assert Prefs(keywords=[" bitcoin ", "", "kubernetes  cluster"] + [f"k{i}" for i in range(20)]).keywords[:2] == ["bitcoin", "kubernetes cluster"]
    assert len(Prefs(keywords=[f"k{i}" for i in range(20)]).keywords) == 12
    for bad in ({"language": "xx"}, {"topics": ["Bad Topic!"]}):
        with pytest.raises(ValueError):
            Prefs(**bad)


def test_a_backup_microsoft_voice_matches_the_chosen_voice_in_gender_and_language():
    from apa.prefs import edge_name, voice_name

    assert voice_name("zephyr", "ja") == ("gemini", "Zephyr")  # Gemini speaks every language
    assert voice_name("gadis", "ja") == ("edge", "ja-JP-NanamiNeural") and voice_name("ardi", "de") == ("edge", "de-DE-ConradNeural")
    assert edge_name("m", "id") == "id-ID-ArdiNeural" and edge_name("f", "xx") == "en-US-AvaMultilingualNeural"


def test_other_topics_rank_by_coverage_and_freshness_and_the_owners_words_lift_stories():
    from apa.news.rank import Article, rank

    now = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
    mk = lambda src, title, topic="teknologi": Article(src, title, f"https://x/{src}/{title}", now - timedelta(hours=1), "", topic)  # noqa: E731
    arts = [mk("A", "Chipmaker unveils new laptop processor"), mk("B", "Chipmaker unveils new laptop processor line"),
            mk("A", "Obscure gadget review roundup today")]
    ranked = rank(arts, now, topic="teknologi")
    assert ranked[0].qualifies and "Chipmaker" in ranked[0].cluster.headline  # two outlets beat one
    boosted = rank(arts, now, topic="teknologi", keywords=("gadget",))
    assert boosted[0].cluster.headline.startswith("Obscure gadget")  # a followed word lifts a story to the top
    mine = rank([mk("A", "Bitcoin jumps", "minat"), mk("A", "Weather is mild", "minat")], now, topic="minat", keywords=("bitcoin",))
    assert [r.qualifies for r in mine if "Bitcoin" in r.cluster.headline] == [True]
    assert not any(r.qualifies for r in mine if "Weather" in r.cluster.headline)  # no word of the owner's, no entry


# ---- the owner's own feeds, topics and CVE rule --------------------------------------------------------------------------------


def test_the_owner_can_switch_feeds_off_add_their_own_and_make_topics():
    from apa.news.config import NewsConfig
    from apa.news.sources import FEEDS, TOPICS

    default = NewsConfig()
    assert default.active_feeds() == tuple(f for f in FEEDS if f.country in ("", "id")) and set(default.catalog()) == set(TOPICS)  # nothing set: the built-in set for Indonesia
    cfg = NewsConfig(
        disabled_feeds=["Detik"],
        topics={"otomotif": {"label": " Auto   motive "}},
        feeds=[{"name": "My Blog", "url": "https://blog.example.com/feed.xml", "topic": "otomotif"},
               {"name": "Hacker Tech", "url": "https://hn.example.com/rss", "topic": "teknologi"}],
    )
    names = [f.name for f in cfg.active_feeds()]
    assert "Detik" not in names and "Antara" in names and names[-2:] == ["My Blog", "Hacker Tech"]
    assert cfg.catalog()["otomotif"] == ("Auto motive", "Auto motive") and cfg.catalog()["cyber"] == TOPICS["cyber"]
    for bad in (
        {"feeds": [{"name": "X1", "url": "ftp://x", "topic": "dunia"}]},  # only http(s)
        {"feeds": [{"name": "X1", "url": "https://x.example/f", "topic": "nope"}]},  # unknown topic
        {"feeds": [{"name": "Antara", "url": "https://x.example/f", "topic": "dunia"}]},  # clashes with a built-in name
        {"feeds": [{"name": "Dup", "url": "https://x.example/f", "topic": "dunia"}] * 2},  # two with one name
        {"topics": {"cyber": {"label": "Mine"}}}, {"topics": {"Bad Key": {"label": "Mine"}}}, {"topics": {"minat": {"label": "Mine"}}},
        {"topics": {f"t{i}": {"label": "Topic"} for i in range(13)}},
    ):
        with pytest.raises(ValueError):
            NewsConfig(**bad)


def test_the_cve_rule_defaults_to_the_original_and_the_owner_can_tune_it():
    import asyncio
    from types import SimpleNamespace as NS

    from apa.news.config import CveRule
    from apa.news.cyber import severe_24h

    now = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)

    def row(cve, cvss, kev=False, products=(), summary="", age_h=2):
        return NS(cve_id=cve, cvss=cvss, epss=None, in_cisa_kev=kev, tier="normal", affected_products=list(products), summary=summary,
                  fetched_at=now - timedelta(hours=age_h))

    rows = [row("CVE-1", 9.8), row("CVE-2", 5.0), row("CVE-3", 4.0, kev=True), row("CVE-4", 6.5, products=["acme router (< 2.0)"]),
            row("CVE-5", 9.0, age_h=48), row("CVE-6", 3.0, summary="Flaw in Contoso Widget")]

    class Result:
        def scalars(self):
            return self

        def all(self):
            return rows

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return Result()

    def ids(rule=None):
        got = asyncio.run(severe_24h(Session, now, rule=rule))  # type: ignore[arg-type]
        return [r["id"] for r in got]

    # the window and the score are applied in code (the fake ignores the SQL filter, so the window is the rule's job only in SQL);
    # what matters here is the selection: score or exploited, watch list first
    base = ids()
    assert "CVE-1" in base and "CVE-3" in base and "CVE-2" not in base and "CVE-4" not in base  # 7.0 minimum, exploited always
    assert ids(CveRule(kev_always=False)).count("CVE-3") == 0  # the exploited rule can be switched off
    assert "CVE-2" in ids(CveRule(min_cvss=5.0)) and "CVE-4" in ids(CveRule(min_cvss=6.0))
    watched = ids(CveRule(min_cvss=9.5, kev_always=False, watch=["ACME ", "contoso"]))
    assert watched[:2] == ["CVE-4", "CVE-6"] or set(watched[:2]) == {"CVE-4", "CVE-6"}  # a watch-list match gets through at any score, first
    with pytest.raises(ValueError):
        CveRule(min_cvss=11)
    assert CveRule(watch=[" ACME  Router ", "", "acme router"]).watch == ["acme router"]  # tidy, lowercase, no repeats


# ---- another country's national news ------------------------------------------------------------------------------------------


def test_the_national_topic_follows_the_home_country():
    from apa.news.config import NewsConfig
    from apa.news.sources import COUNTRIES, FEEDS

    assert {f.country for f in FEEDS if f.topic == "nasional"} == set(COUNTRIES)  # every country has national feeds
    assert all(f.topic == "nasional" for f in FEEDS if f.country)  # only national feeds belong to a country
    id_feeds = [f.name for f in NewsConfig().active_feeds()]
    assert "Antara" in id_feeds and "NPR" not in id_feeds and "BBC World" in id_feeds  # worldwide feeds stay
    jp = NewsConfig(country="jp", disabled_feeds=["NHK"])
    names = [f.name for f in jp.active_feeds()]
    assert "The Japan Times" in names and "NHK" not in names and "Antara" not in names and "Ars Technica" in names
    with pytest.raises(ValueError):
        NewsConfig(country="xx")


def test_the_indonesian_word_lists_only_rank_indonesian_news():
    from apa.news.rank import Article, rank

    now = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
    mk = lambda src, title: Article(src, title, f"https://x/{src}/{title}", now - timedelta(hours=1), "", "nasional")  # noqa: E731
    arts = [mk("NPR", "Senate passes the budget bill after a long night"), mk("CBS News", "Senate passes the budget bill late on Friday"),
            mk("NPR", "Local bakery wins a regional prize")]
    us = rank(arts, now, topic="nasional", country="us")
    assert us[0].qualifies and "Senate" in us[0].cluster.headline  # two outlets, fresh: enough, no Indonesian word list needed
    assert not us[-1].qualifies or us[-1].score < us[0].score
    ind = rank(arts, now, topic="nasional", country="id")
    assert not ind[0].qualifies  # without an Indonesian institution word (and under three outlets) the old rule says no
