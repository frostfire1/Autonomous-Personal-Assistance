"""The feeds the system reads. Each URL was fetched from the server on 2026-10-24 and returned a valid feed.
Not included because they did not: Kompas (404), Kontan (HTML, not a feed), BSSN (404), CISA advisories RSS
(5.6 MB, over the size cap; the KEV catalogue covers what matters from CISA)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    topic: str  # a key of TOPICS
    expected_every_min: int = 60  # silence beyond 6x this raises a "source quiet" note
    country: str = ""  # national feeds only: the country (a key of COUNTRIES) whose news this is


# The sections the owner can switch on. 'nasional' and 'cyber' are the originals (with their own scoring); the others are
# ranked by how many outlets cover a story and how fresh it is. Key -> (label for the owner, section title in English).
TOPICS: dict[str, tuple[str, str]] = {
    "nasional": ("Berita nasional", "National news"),
    "cyber": ("Keamanan siber dan CVE", "Cybersecurity and CVEs"),
    "bisnis": ("Bisnis dan pasar", "Business and markets"),
    "teknologi": ("Teknologi", "Technology"),
    "dunia": ("Berita dunia", "World news"),
    "sains": ("Sains", "Science"),
}

# The countries whose national news can be chosen (the home country). Their feeds are below; any country can also be
# covered by adding feeds of your own to the national topic.
COUNTRIES: dict[str, str] = {
    "id": "Indonesia", "us": "United States", "gb": "United Kingdom", "au": "Australia", "sg": "Singapore", "my": "Malaysia",
    "in": "India", "jp": "Japan", "kr": "South Korea", "de": "Germany", "fr": "France", "es": "Spain",
}

FEEDS: tuple[Feed, ...] = (
    Feed("Antara", "https://www.antaranews.com/rss/terkini.xml", "nasional", 30, "id"),
    Feed("CNN Indonesia", "https://www.cnnindonesia.com/nasional/rss", "nasional", 30, "id"),
    Feed("CNBC Indonesia", "https://www.cnbcindonesia.com/news/rss", "nasional", 30, "id"),
    Feed("Detik", "https://news.detik.com/rss", "nasional", 30, "id"),
    Feed("Tempo", "https://rss.tempo.co/nasional", "nasional", 60, "id"),
    Feed("Republika", "https://www.republika.co.id/rss", "nasional", 60, "id"),
    Feed("NPR", "https://feeds.npr.org/1001/rss.xml", "nasional", 30, "us"),
    Feed("CBS News", "https://www.cbsnews.com/latest/rss/main", "nasional", 30, "us"),
    Feed("ABC News US", "http://feeds.abcnews.com/abcnews/usheadlines", "nasional", 30, "us"),
    Feed("BBC UK", "http://feeds.bbci.co.uk/news/uk/rss.xml", "nasional", 30, "gb"),
    Feed("The Guardian UK", "https://www.theguardian.com/uk-news/rss", "nasional", 30, "gb"),
    Feed("Sky News", "https://feeds.skynews.com/feeds/rss/uk.xml", "nasional", 60, "gb"),
    Feed("ABC News Australia", "https://www.abc.net.au/news/feed/51120/rss.xml", "nasional", 30, "au"),
    Feed("The Guardian Australia", "https://www.theguardian.com/australia-news/rss", "nasional", 60, "au"),
    Feed("CNA", "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml", "nasional", 30, "sg"),
    Feed("The Straits Times", "https://www.straitstimes.com/news/singapore/rss.xml", "nasional", 30, "sg"),
    Feed("Malay Mail", "https://www.malaymail.com/feed/rss/malaysia", "nasional", 30, "my"),
    Feed("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss", "nasional", 30, "in"),
    Feed("NDTV", "https://feeds.feedburner.com/ndtvnews-india-news", "nasional", 30, "in"),
    Feed("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms", "nasional", 30, "in"),
    Feed("NHK", "https://www3.nhk.or.jp/rss/news/cat0.xml", "nasional", 60, "jp"),
    Feed("The Japan Times", "https://www.japantimes.co.jp/feed/", "nasional", 60, "jp"),
    Feed("Yonhap", "https://en.yna.co.kr/RSS/news.xml", "nasional", 60, "kr"),
    Feed("Tagesschau", "https://www.tagesschau.de/index~rss2.xml", "nasional", 30, "de"),
    Feed("Spiegel", "https://www.spiegel.de/schlagzeilen/index.rss", "nasional", 30, "de"),
    Feed("Le Monde", "https://www.lemonde.fr/rss/une.xml", "nasional", 30, "fr"),
    Feed("franceinfo", "https://www.francetvinfo.fr/titres.rss", "nasional", 30, "fr"),
    Feed("El Pais", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada", "nasional", 30, "es"),
    Feed("The Hacker News", "https://feeds.feedburner.com/TheHackersNews", "cyber", 120),
    Feed("BleepingComputer", "https://www.bleepingcomputer.com/feed/", "cyber", 120),
    Feed("Krebs on Security", "https://krebsonsecurity.com/feed/", "cyber", 720),
    Feed("CNBC Indonesia Market", "https://www.cnbcindonesia.com/market/rss", "bisnis", 30),
    Feed("Detik Finance", "https://finance.detik.com/rss", "bisnis", 30),
    Feed("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "teknologi", 120),
    Feed("Detik Inet", "https://inet.detik.com/rss", "teknologi", 60),
    Feed("CNBC Indonesia Tech", "https://www.cnbcindonesia.com/tech/rss", "teknologi", 120),
    Feed("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml", "dunia", 60),
    Feed("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "dunia", 60),
    Feed("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml", "sains", 240),
)

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
EPSS_URL = "https://api.first.org/data/v1/epss"
