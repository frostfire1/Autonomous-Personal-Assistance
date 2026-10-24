"""Deterministic news selection (blueprint v2 4.4c H): cluster the same event across outlets by title similarity,
score it from observable signals, and send only the ambiguous middle band to a model. The number of outlets
covering an event is the main sign that it is national in scale, and it removes duplicates at the same time.
No model is involved in anything here."""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

STOP = frozenset(
    "yang dan di ke dari untuk pada dengan akan ini itu tak tidak ada juga atau oleh sebagai dalam telah sudah "
    "bakal kini usai soal saat hingga serta para lebih saja the a an of to in on for and with as at by is are "
    "from after over new says say".split()
)
SIMILARITY = 0.45
MIN_SHARED = 3
QUALIFY = {"nasional": 24, "cyber": 22}
AMBIGUOUS_FROM = {"nasional": 10, "cyber": 12}
GENERIC_QUALIFY = 10  # every other topic: any story of the window (slow feeds such as fashion publish daily, not hourly);
                      # the briefing takes only the top few by score, so a low bar does not flood it
NATIONAL_ELSEWHERE_QUALIFY = 20  # national news outside Indonesia: a fresh story in one outlet, or any story in two
KEYWORD_BONUS = 15

_WORD = re.compile(r"[a-z0-9]+")
_CVE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)

CENTRAL = (
    "presiden",
    "prabowo",
    "menteri",
    "kementerian",
    "pemerintah",
    "dpr",
    "mpr",
    "ojk",
    "bank indonesia",
    "bi rate",
    "inflasi",
    "apbn",
    "pajak",
    "bbm",
    "listrik",
    "tarif",
    "regulasi",
    "undang-undang",
    "perppu",
    "mahkamah",
    "kpk",
    "polri",
    "tni",
    "bencana",
    "gempa",
    "banjir",
    "erupsi",
    "rupiah",
    "ihsg",
    "subsidi",
    "harga pangan",
    "bps",
)
LOCAL = ("warga", "kota ", "kabupaten", "polres", "polsek", "pemkab", "pemkot", "desa ", "camat", "lurah")
NOISE = (
    "liga",
    "gol ",
    "pertandingan",
    "selebgram",
    "artis",
    "seleb",
    "drakor",
    "konser",
    "resep",
    "zodiak",
    "ramalan",
    "sinetron",
    "gosip",
    "skor ",
    "klasemen",
)
CYBER_WORDS = (
    "ransomware",
    "zero-day",
    "zero day",
    "exploited",
    "breach",
    "leak",
    "kebocoran",
    "backdoor",
    "supply chain",
    "rce",
    "critical",
    "vulnerability",
    "kerentanan",
    "malware",
    "phishing",
    "ddos",
    "retas",
    "peretasan",
)
INDONESIA = ("indonesia", "bssn", "kominfo", "komdigi", "pdn", "instansi", "bank mandiri", "bca", "telkom")


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    url: str
    published: datetime | None
    summary: str
    topic: str


@dataclass
class Cluster:
    topic: str
    articles: list[Article] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return sorted({a.source for a in self.articles})

    @property
    def newest(self) -> datetime | None:
        times = [a.published for a in self.articles if a.published]
        return max(times) if times else None

    @property
    def headline(self) -> str:
        return min(self.articles, key=lambda a: (len(a.title) > 140, a.title)).title

    @property
    def key(self) -> str:
        """Stable across runs while the same stories are in it: the hash of its smallest URL."""
        return hashlib.sha256(min(a.url for a in self.articles).encode()).hexdigest()[:24]


def tokens(title: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(title.lower()) if w not in STOP and len(w) > 2)


def _similar(a: frozenset[str], b: frozenset[str]) -> bool:
    shared = len(a & b)
    return shared >= MIN_SHARED and shared / len(a | b) >= SIMILARITY


def cluster(articles: list[Article]) -> list[Cluster]:
    """Union-find over title similarity. Articles of different topics never merge."""
    toks = [tokens(a.title) for a in articles]
    parent = list(range(len(articles)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(articles)):
        for j in range(i + 1, len(articles)):
            if articles[i].topic == articles[j].topic and _similar(toks[i], toks[j]):
                parent[find(j)] = find(i)
    groups: dict[int, Cluster] = {}
    for i, a in enumerate(articles):
        groups.setdefault(find(i), Cluster(a.topic)).articles.append(a)
    return list(groups.values())


def _hits(text: str, words: tuple[str, ...]) -> int:
    return sum(1 for w in words if w in text)


def has_signal(c: Cluster, country: str = "id") -> bool:
    """Something in the story itself says it matters: a central institution or topic (national), or exploitation,
    Indonesia or a CVE (cyber); or, for national news, wide coverage. Freshness and one outlet are not a signal."""
    text = " ".join(a.title.lower() for a in c.articles)
    if c.topic == "nasional" and country == "id":
        return _hits(text, CENTRAL) >= 1 or len(c.sources) >= 3
    if c.topic != "cyber":
        return True  # no hand-made signal list for the other topics: coverage and freshness decide
    return _hits(text, INDONESIA) >= 1 or _hits(text, CYBER_WORDS) >= 1 or bool(_CVE.search(text))


def score(c: Cluster, now: datetime, country: str = "id") -> int:
    text = " ".join(a.title.lower() for a in c.articles)
    indonesian = c.topic == "nasional" and country == "id"  # the word lists below are Indonesian
    s = min(4, len(c.sources)) * (12 if indonesian else 10)
    age = (now - c.newest) if c.newest else timedelta(days=9)
    s += (
        12
        if age <= timedelta(hours=3)
        else 8
        if age <= timedelta(hours=8)
        else 3
        if age <= timedelta(hours=24)
        else 0
    )
    if indonesian:
        s += min(18, 6 * _hits(text, CENTRAL))
        s -= min(16, 8 * _hits(text, LOCAL))
        s -= 30 * (1 if _hits(text, NOISE) else 0)
    elif c.topic == "cyber":
        s += 20 if _hits(text, INDONESIA) else 0
        s += min(18, 6 * _hits(text, CYBER_WORDS))
        s += 6 if _CVE.search(text) else 0
    return s


@dataclass(frozen=True)
class Ranked:
    cluster: Cluster
    score: int
    qualifies: bool
    ambiguous: bool


def keyword_hits(c: Cluster, keywords: tuple[str, ...]) -> int:
    text = " ".join(a.title.lower() for a in c.articles)
    return sum(1 for k in keywords if k.lower() in text)


def rank(
    articles: list[Article], now: datetime, *, topic: str, keywords: tuple[str, ...] = (), country: str = "id"
) -> list[Ranked]:
    """Best first. `ambiguous` clusters sit between the drop line and the qualify line: a model may decide. Words the
    owner follows lift a story's score; for the 'minat' topic they are also the entry ticket."""
    out = []
    for c in cluster([a for a in articles if a.topic == topic]):
        hits = keyword_hits(c, keywords)
        s = score(c, now, country) + KEYWORD_BONUS * hits
        signal = has_signal(c, country) and (hits >= 1 or topic != "minat")
        need = QUALIFY.get(topic, GENERIC_QUALIFY) if (topic != "nasional" or country == "id") else NATIONAL_ELSEWHERE_QUALIFY
        qualifies = s >= need and signal
        # high on freshness alone, or in the middle band: undecided, a model may rule on national stories
        out.append(Ranked(c, s, qualifies, not qualifies and s >= (AMBIGUOUS_FROM.get(topic, GENERIC_QUALIFY) if (topic != "nasional" or country == "id") else 10)))
    return sorted(out, key=lambda r: (-r.score, r.cluster.key))


def sensitive_claim(text: str) -> bool:
    """Numbers of victims, losses and named accusations need two outlets before they are read out."""
    t = text.lower()
    units = "korban|tewas|meninggal|luka|orang|miliar|triliun|juta|dollar|usd"
    return bool(
        re.search(rf"\b\d[\d.,]*\s*({units})\b", t)
        or re.search(r"\b(korban|tewas|meninggal|kerugian|rugi)\b\D{0,20}\d", t)
    )
