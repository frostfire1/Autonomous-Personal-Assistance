"""Unit tests for the search and maps tools."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from apa import maps, search
from apa.chat.tools import READ_TOOLS, run_read


def test_search_and_maps_registered_in_read_tools():
    assert "search" in READ_TOOLS
    assert "maps" in READ_TOOLS


def test_clean_query():
    assert search._clean_query("  halo  dunia  ") == "halo dunia"
    assert search._clean_query("a" * 200) == "a" * search.MAX_QUERY_LEN


def test_decode_bing_url():
    # Valid base64 encoding of https://example.com/test
    encoded = "aHR0cHM6Ly9leGFtcGxlLmNvbS90ZXN0"
    bing_href = f"https://www.bing.com/ck/a?!&&p=123&u=a1{encoded}&ntb=1"
    assert search._decode_bing_url(bing_href) == "https://example.com/test"
    # When no u=a1 parameter, returns original
    plain = "https://example.com/plain"
    assert search._decode_bing_url(plain) == plain


def test_google_maps_urls():
    s_url = maps.google_maps_search_url("NCSC BSSN Ragunan")
    assert "https://www.google.com/maps/search/?api=1&query=" in s_url
    assert "NCSC%20BSSN%20Ragunan" in s_url

    d_url = maps.google_maps_directions_url("Bonsir", "NCSC", mode="transit")
    assert "https://www.google.com/maps/dir/?api=1" in d_url
    assert "origin=Bonsir" in d_url
    assert "destination=NCSC" in d_url
    assert "travelmode=transit" in d_url

    motor_url = maps.google_maps_directions_url("Bonsir", "NCSC", mode="motor")
    assert "travelmode=two_wheeler" in motor_url


@pytest.mark.asyncio
async def test_maps_route_directions():
    with patch("apa.maps.geocode", new_callable=AsyncMock) as mock_geo, \
         patch("apa.maps.get_route", new_callable=AsyncMock) as mock_route:
        mock_geo.side_effect = [
            {"name": "Bonsir", "display_name": "Kebon Sirih, Menteng, Jakarta", "lat": -6.18, "lon": 106.83},
            {"name": "NCSC", "display_name": "BSSN Ragunan, Jakarta Selatan", "lat": -6.29, "lon": 106.82},
        ]
        mock_route.return_value = (14.5, 25.0)

        result = await maps.route_directions("Bonsir", "NCSC", mode="transit")
        assert "RUTE & ESTIMASI PERJALANAN: Bonsir" in result
        assert "TransJakarta" in result
        assert "KRL Commuter Line" in result
        assert "Ojek Online" in result
        assert "Buka Rute Angkutan Umum di Google Maps" in result
        assert "Buka Rute Motor / Ojek di Google Maps" in result


@pytest.mark.asyncio
async def test_search_run_tool():
    with patch("apa.search.search_web", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = [
            {"title": "NCC 2026", "snippet": "Cybersecurity event", "url": "https://ncsc.co.id"}
        ]
        result = await search.run_search("ncsc jakarta", limit=3)
        assert "HASIL PENCARIAN WEB untuk 'ncsc jakarta':" in result
        assert "[NCC 2026](https://ncsc.co.id)" in result
        assert "Cybersecurity event" in result


@pytest.mark.asyncio
async def test_read_tools_execution_via_run_read():
    now, tz = datetime.now(UTC), ZoneInfo("Asia/Jakarta")
    with patch("apa.search.run_search", new_callable=AsyncMock) as mock_search, \
         patch("apa.maps.route_directions", new_callable=AsyncMock) as mock_maps:
        mock_search.return_value = "Search result ok"
        mock_maps.return_value = "Maps route ok"

        res_search = await run_read(None, tz, now, "search", {"query": "weather"})
        assert res_search == "Search result ok"

        res_maps = await run_read(None, tz, now, "maps", {"origin": "A", "destination": "B"})
        assert res_maps == "Maps route ok"
