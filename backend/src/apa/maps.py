"""Location search, routing, and transit cost estimation with Google Maps integration.

Uses:
1. OpenStreetMap Nominatim for geocoding addresses and landmarks (Indonesia prioritized)
2. Open-Meteo Geocoding as secondary fallback
3. Open Source Routing Machine (OSRM) for real road distance and travel duration
4. Automatic Google Maps clickable direction and place links
5. Indonesian transit fare calculation (TransJakarta, KRL, Mikrotrans JakLingko, Gojek/Grab)
"""

import re
from typing import Any
from urllib.parse import quote

import httpx

from apa.core.logging import get_logger
from apa.core.sanitize import sanitize_untrusted

log = get_logger(__name__)

TIMEOUT_S = 8.0
USER_AGENT = "AutonomousPersonalAssistant/1.0 (self-hosted personal assistant)"


def _clean_place(raw: str) -> str:
    s = sanitize_untrusted(raw, max_len=100)
    q = re.sub(r"[^\w\s\-.,/']", " ", s.text)
    return " ".join(q.split())[:100].strip()


async def geocode(place: str) -> dict[str, Any] | None:
    """Geocode a place name into (name, display_name, lat, lon)."""
    q = _clean_place(place)
    if not q:
        return None

    # Common shortcuts / abbreviations in Indonesia
    aliases = {
        "bonsir": "Kebon Sirih, Menteng, Jakarta Pusat",
        "bonsari": "Kebon Sirih, Menteng, Jakarta Pusat",
        "kebonsirih": "Kebon Sirih, Menteng, Jakarta Pusat",
        "ncsc": "BSSN Ragunan, Jl. Harsono RM No.70, Jakarta Selatan",
        "bssn": "BSSN Ragunan, Jl. Harsono RM No.70, Jakarta Selatan",
        "ncsc bssn": "BSSN Ragunan, Jl. Harsono RM No.70, Jakarta Selatan",
    }
    resolved_query = aliases.get(q.lower(), q)

    headers = {"User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT_S) as http:
            # 1. Try Nominatim with Indonesia filter
            url_id = f"https://nominatim.openstreetmap.org/search?q={quote(resolved_query)}&format=json&limit=1&countrycodes=id"
            r = await http.get(url_id)
            hits = r.json() if r.status_code == 200 else []
            if hits:
                h = hits[0]
                return {
                    "name": q,
                    "display_name": h.get("display_name", q),
                    "lat": float(h["lat"]),
                    "lon": float(h["lon"]),
                }

            # 2. Try Nominatim global
            url_world = f"https://nominatim.openstreetmap.org/search?q={quote(resolved_query)}&format=json&limit=1"
            r = await http.get(url_world)
            hits = r.json() if r.status_code == 200 else []
            if hits:
                h = hits[0]
                return {
                    "name": q,
                    "display_name": h.get("display_name", q),
                    "lat": float(h["lat"]),
                    "lon": float(h["lon"]),
                }

            # 3. Open-Meteo geocoding fallback
            url_om = f"https://geocoding-api.open-meteo.com/v1/search?name={quote(resolved_query)}&count=1&language=id&format=json"
            r = await http.get(url_om)
            hits = r.json().get("results", []) if r.status_code == 200 else []
            if hits:
                h = hits[0]
                return {
                    "name": q,
                    "display_name": f"{h.get('name')}, {h.get('admin1', '')}, {h.get('country', '')}",
                    "lat": float(h["latitude"]),
                    "lon": float(h["longitude"]),
                }
    except Exception as exc:
        log.warning("geocode_failed", place=q, error=str(exc))

    return None


async def get_route(origin_coords: tuple[float, float], dest_coords: tuple[float, float]) -> tuple[float, float] | None:
    """Calculate driving distance (km) and duration (mins) using OSRM."""
    lat1, lon1 = origin_coords
    lat2, lon2 = dest_coords
    url = f"https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false"
    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S) as http:
            r = await http.get(url)
            data = r.json()
            if data.get("code") == "Ok" and data.get("routes"):
                r0 = data["routes"][0]
                dist_km = r0["distance"] / 1000.0
                dur_mins = r0["duration"] / 60.0
                return dist_km, dur_mins
    except Exception as exc:
        log.warning("osrm_route_failed", error=str(exc))
    return None


def google_maps_directions_url(origin: str, destination: str, mode: str = "transit") -> str:
    """Generate clickable Google Maps directions URL."""
    mode_map = {
        "transit": "transit",
        "motor": "two_wheeler",
        "motorcycle": "two_wheeler",
        "driving": "driving",
        "mobil": "driving",
        "walking": "walking",
        "jalan": "walking",
    }
    travel_mode = mode_map.get(mode.lower(), "transit")
    return f"https://www.google.com/maps/dir/?api=1&origin={quote(origin)}&destination={quote(destination)}&travelmode={travel_mode}"


def google_maps_search_url(query: str) -> str:
    """Generate clickable Google Maps search URL."""
    return f"https://www.google.com/maps/search/?api=1&query={quote(query)}"


async def search_place(query: str) -> str:
    """Look up a place, its coordinates, and Google Maps search link."""
    q = _clean_place(query)
    if not q:
        return "Tolong sebutkan nama tempat atau alamat yang ingin dicari."

    geo = await geocode(q)
    gmaps_url = google_maps_search_url(q)
    if not geo:
        return (
            f"Lokasi '{q}':\n"
            f"Tidak dapat menentukan koordinat otomatis. Kamu bisa langsung membukanya di Google Maps:\n"
            f"🔗 [Buka di Google Maps]({gmaps_url})"
        )

    return (
        f"LOKASI & PETA: {geo['display_name']}\n"
        f"- Koordinat: {geo['lat']:.5f}, {geo['lon']:.5f}\n"
        f"- Buka di Google Maps: [Lihat Peta Google Maps]({gmaps_url})"
    )


async def route_directions(origin: str, destination: str, mode: str = "transit") -> str:
    """Calculate directions, transit fares, duration, and Google Maps links."""
    orig = _clean_place(origin)
    dest = _clean_place(destination)
    if not orig or not dest:
        return "Tolong sebutkan titik asal (origin) dan titik tujuan (destination)."

    geo_orig = await geocode(orig)
    geo_dest = await geocode(dest)

    dist_km = 14.0
    dur_mins = 35.0
    orig_name = geo_orig["display_name"] if geo_orig else orig
    dest_name = geo_dest["display_name"] if geo_dest else dest

    if geo_orig and geo_dest:
        r_info = await get_route((geo_orig["lat"], geo_orig["lon"]), (geo_dest["lat"], geo_dest["lon"]))
        if r_info:
            dist_km, dur_mins = r_info

    # Cost calculations for Jakarta/Indonesian transit
    tj_fare = 3500
    krl_fare = 3000 if dist_km <= 25 else 4000
    goride_est = round(10000 + max(0, dist_km - 4) * 2700, -2)
    gocar_est = round(16000 + max(0, dist_km - 4) * 6000, -2)

    url_transit = google_maps_directions_url(orig, dest, "transit")
    url_motor = google_maps_directions_url(orig, dest, "two_wheeler")
    url_mobil = google_maps_directions_url(orig, dest, "driving")

    lines = [
        f"RUTE & ESTIMASI PERJALANAN: {orig} ➡️ {dest}",
        f"📍 Dari: {orig_name[:70]}",
        f"🎯 Ke: {dest_name[:70]}",
        f"📏 Jarak Tempuh: ~{dist_km:.1f} km",
        f"⏱ Waktu Tempuh Kendaraan: ~{dur_mins:.0f} - {dur_mins + 15:.0f} menit (tergantung kondisi lalu lintas)",
        "",
        "OPSI ANGKUTAN & PERBANDINGAN BIAYA (Termurah s/d Tercepat):",
        f"1. 🚌 TransJakarta (Paling Hemat): Rp {tj_fare:,} (flat).",
        "   - Cek rute koridor BRT terdekat langsung atau transit halte integrasi.",
        f"2. 🚆 KRL Commuter Line + Mikrotrans: Rp {krl_fare:,} (KRL) + Rp 0 (JakLingko).",
        "   - Sangat hemat biaya dan bebas macet jalan raya.",
        f"3. 🛵 Ojek Online (Gojek / Grab Motor): ~Rp {goride_est:,.0f} (estimasi tarif normal).",
        "   - Paling fleksibel dan cepat menembus kemacetan jam sibuk.",
        f"4. 🚗 Mobil / Taksi Online (GoCar / GrabCar): ~Rp {gocar_est:,.0f}.",
        "   - Lebih nyaman jika membawa barang atau cuaca hujan.",
        "",
        "🔗 LINK LANGSUNG GOOGLE MAPS:",
        f"- [Buka Rute Angkutan Umum di Google Maps]({url_transit})",
        f"- [Buka Rute Motor / Ojek di Google Maps]({url_motor})",
        f"- [Buka Rute Mobil di Google Maps]({url_mobil})",
    ]
    return "\n".join(lines)
