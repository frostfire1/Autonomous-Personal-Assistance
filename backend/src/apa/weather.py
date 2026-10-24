"""Today's weather for the briefing, from Open-Meteo (free, no account, no key). Only a place name or coordinates leave the
server, never anything about the owner. A failure just means the briefing has no weather line; it never blocks it."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from apa.core.logging import get_logger
from apa.news.fetch import FetchError, fetch_bytes
from apa.news.i18n import S, table, weather_words

log = get_logger(__name__)

DEFAULT_PLACE = ("Jakarta", -6.2088, 106.8456)
CACHE_KEY = "apa:weather:"
CACHE_TTL_S = 3 * 3600
FORECAST = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weather_code,temperature_2m_max,"
    "temperature_2m_min,precipitation_probability_max&timezone={tz}&forecast_days=1"
)
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=id&format=json"


@dataclass(frozen=True)
class Weather:
    place: str
    code: int
    tmin: int
    tmax: int
    rain_pct: int | None


async def geocode(city: str) -> tuple[str, float, float] | None:
    """('Surabaya', -7.2575, 112.7521) for a place name, or None when it is unknown or the service is unreachable."""
    city = " ".join(city.split())[:60]
    if not city:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            hits = json.loads(await fetch_bytes(http, GEOCODE.format(q=quote(city)), max_bytes=100_000)).get("results") or []
    except (FetchError, ValueError, httpx.HTTPError):
        return None
    if not hits:
        return None
    h = hits[0]
    try:
        return str(h["name"]), float(h["latitude"]), float(h["longitude"])
    except (KeyError, TypeError, ValueError):
        return None


async def forecast(place: str, lat: float, lon: float, tz_name: str, redis: Any | None = None) -> Weather | None:
    """Today's forecast, cached a few hours (the briefing is built, previewed and re-sent more than once a day)."""
    key = f"{CACHE_KEY}{lat:.2f}:{lon:.2f}:{datetime.now().date()}"
    if redis is not None:
        try:
            if hit := await redis.get(key):
                d = json.loads(hit)
                return Weather(place, d["code"], d["tmin"], d["tmax"], d["rain"])
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            raw = await fetch_bytes(http, FORECAST.format(lat=lat, lon=lon, tz=quote(tz_name)), max_bytes=100_000)
        daily = json.loads(raw)["daily"]
        rain = daily["precipitation_probability_max"][0]
        w = Weather(place, int(daily["weather_code"][0]), round(daily["temperature_2m_min"][0]),
                    round(daily["temperature_2m_max"][0]), None if rain is None else int(rain))
    except (FetchError, ValueError, KeyError, IndexError, TypeError, httpx.HTTPError) as e:
        log.warning("weather_failed", error=type(e).__name__)
        return None
    if redis is not None:
        try:
            await redis.set(key, json.dumps({"code": w.code, "tmin": w.tmin, "tmax": w.tmax, "rain": w.rain_pct}), ex=CACHE_TTL_S)
        except Exception:
            pass
    return w


def sentence(w: Weather, lang: str = "id") -> str:
    """'Cuaca hari ini di Surabaya: hujan, suhu 24 sampai 31 derajat, peluang hujan 70 persen.' Rain odds under 30% are
    not worth saying."""
    lang = table(lang)
    text = S[lang]["weather"].format(place=w.place, desc=weather_words(w.code, lang), tmin=w.tmin, tmax=w.tmax)
    if w.rain_pct is not None and w.rain_pct >= 30:
        text += S[lang]["weather_rain"].format(pct=w.rain_pct)
    return text + "."
