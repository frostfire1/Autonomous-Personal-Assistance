"""The words of the briefing in the languages it can be read in (Indonesian, English). Facts come from SQL and code; this
module is only the sentence frames around them, so a figure never depends on a translation."""

from datetime import datetime

LANGS = ("id", "en", "ja", "zh", "ko", "de", "fr", "es")  # id and en have written tables; the rest are translated from en

DAYS = {
    "id": ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}
MONTHS = {
    "id": ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
}


def norm(lang: str | None) -> str:
    return lang if lang in LANGS else "id"


def table(lang: str | None) -> str:
    """The language whose sentence frames are used: Indonesian for Indonesian, English for everything else (which is then
    translated, see news.translate)."""
    return "id" if norm(lang) == "id" else "en"


def money(n, lang: str = "id") -> str:
    """For speech: 'Rp 58.300' in Indonesian, '58,300 rupiah' in English (a voice reads that naturally)."""
    n = int(n)
    if table(lang) == "en":
        return f"{n:,} rupiah"
    return f"Rp {n:,}".replace(",", ".")


def greeting(now: datetime, lang: str = "id") -> str:
    lang = table(lang)
    day, month = DAYS[lang][now.weekday()], MONTHS[lang][now.month - 1]
    if lang == "en":
        part = "morning" if now.hour < 11 else "afternoon" if now.hour < 18 else "evening"
        return f"Good {part}. Today is {day}, {month} {now.day}."
    part = "pagi" if now.hour < 11 else "siang" if now.hour < 15 else "sore" if now.hour < 18 else "malam"
    return f"Selamat {part}. Hari ini {day}, {now.day} {month}."


S = {
    "id": {
        "spent_yesterday": "Kemarin pengeluaranmu {amount}.",
        "budget_tight": "Anggaran {cat} sudah {pct} persen.",
        "budget_today": "Anggaran hari ini sekitar {amount}.",
        "budget_today_spent": " Sudah terpakai {spent}.",
        "sleep": "Tidurmu semalam {h} jam {m} menit",
        "sleep_score": ", skor {score}.",
        "weather": "Cuaca hari ini di {place}: {desc}, suhu {tmin} sampai {tmax} derajat",
        "weather_rain": ", peluang hujan {pct} persen",
        "national_head": "Berita nasional. ",
        "head_bisnis": "Berita bisnis. ", "head_teknologi": "Berita teknologi. ", "head_dunia": "Berita dunia. ",
        "head_sains": "Berita sains. ", "head_minat": "Yang kamu ikuti. ",
        "no_national": "Tidak ada berita nasional besar saat ini.",
        "cyber_head": "Berita keamanan siber. ",
        "cyber_crit": "{n} kerentanan kritis baru",
        "cyber_kev": "{n} yang sudah dieksploitasi aktif menurut CISA",
        "cyber_there_are": "Ada {parts}.",
        "cyber_and": " dan ",
        "cyber_none": "Tidak ada kerentanan kritis baru.",
        "cyber_check": "Yang paling perlu dicek: {items}. Nomor CVE dan detail lengkap ada di Telegram.",
        "cyber_urgent": "Perlu perhatian segera: {ids}.",
        "score": "skor {score}",
        "no_score": "belum diberi skor",
        "exploited": "sudah dieksploitasi",
        "unknown_product": "produk yang belum teridentifikasi",
        "price": "Harga bahan pokok naik sekitar {pct} persen. Perkiraan tambahan biaya {amount} per bulan, dan anggaran sudah disesuaikan.",
        "summary_lang": "Indonesian",
        "according": "menurut {names}",
        "and": " dan ",
        "one_source": " Baru dilaporkan satu sumber.",
        "wmo": {0: "cerah", 1: "cerah berawan", 2: "berawan sebagian", 3: "berawan", 45: "berkabut", 51: "gerimis", 61: "hujan",
                65: "hujan lebat", 71: "bersalju", 80: "hujan lokal", 95: "badai petir"},
        "v_lte": "sampai versi", "v_gte": "mulai versi", "v_lt": "di bawah versi", "v_gt": "di atas versi", "v_eq": "versi",
        "v_join": " dan ",
    },
    "en": {
        "spent_yesterday": "Yesterday you spent {amount}.",
        "budget_tight": "Your {cat} budget is {pct} percent used.",
        "budget_today": "Today's budget is about {amount}.",
        "budget_today_spent": " You have spent {spent} so far.",
        "sleep": "You slept {h} hours {m} minutes last night",
        "sleep_score": ", score {score}.",
        "weather": "Weather in {place} today: {desc}, {tmin} to {tmax} degrees",
        "weather_rain": ", {pct} percent chance of rain",
        "national_head": "National news. ",
        "head_bisnis": "Business news. ", "head_teknologi": "Technology news. ", "head_dunia": "World news. ",
        "head_sains": "Science news. ", "head_minat": "What you follow. ",
        "no_national": "No major national news right now.",
        "cyber_head": "Security news. ",
        "cyber_crit": "{n} new critical vulnerabilities",
        "cyber_kev": "{n} already being exploited according to CISA",
        "cyber_there_are": "There are {parts}.",
        "cyber_and": " and ",
        "cyber_none": "No new critical vulnerabilities.",
        "cyber_check": "Worth checking first: {items}. CVE numbers and full details are in Telegram.",
        "cyber_urgent": "Needs attention now: {ids}.",
        "score": "score {score}",
        "no_score": "not scored yet",
        "exploited": "already exploited",
        "unknown_product": "an unidentified product",
        "price": "Staple prices are up about {pct} percent. Estimated extra cost {amount} a month, and the budget has been adjusted.",
        "summary_lang": "English",
        "according": "according to {names}",
        "and": " and ",
        "one_source": " Reported by one source so far.",
        "wmo": {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 51: "drizzle", 61: "rain",
                65: "heavy rain", 71: "snow", 80: "scattered showers", 95: "thunderstorms"},
        "v_lte": "up to version", "v_gte": "from version", "v_lt": "below version", "v_gt": "above version", "v_eq": "version",
        "v_join": " and ",
    },
}


def t(lang: str, key: str, **kw) -> str:
    return S[table(lang)][key].format(**kw)


def _weather_group(code: int) -> int:
    if code in (0, 1, 2, 3):
        return code
    if code in (45, 48):
        return 45
    if 51 <= code <= 57:
        return 51
    if code == 65 or code == 67:
        return 65
    if 61 <= code <= 66:
        return 61
    if 71 <= code <= 77:
        return 71
    if 80 <= code <= 86:
        return 80
    return 95 if code >= 95 else 3


def weather_words(code: int, lang: str = "id") -> str:
    """WMO weather code -> a few words (codes are grouped: 51-57 drizzle, 61-66 rain, 80-86 showers, 95+ storms)."""
    return S[table(lang)]["wmo"][_weather_group(code)]
