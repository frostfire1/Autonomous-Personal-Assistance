"""Fetching and parsing feeds. Web content is untrusted data (blueprint v2 4.4 E): the fetcher refuses internal
addresses (anti-SSRF, checked on every redirect hop), caps size and time, and parses XML with defusedxml so a
hostile feed cannot expand entities or read files. Nothing here follows instructions found in a page."""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx
from defusedxml import ElementTree as ET  # noqa: N817
from defusedxml.common import DefusedXmlException

MAX_BYTES = 3_000_000
TIMEOUT_S = 15.0
MAX_REDIRECTS = 3
USER_AGENT = "Mozilla/5.0 (compatible; apa-feedbot/1.0)"


class FetchError(Exception):
    """A source could not be read. The message is safe to log."""


@dataclass(frozen=True)
class Entry:
    title: str
    url: str
    published: datetime | None
    summary: str


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_v4(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """A DNS64 network answers an IPv4-only site with a 64:ff9b::/96 address that carries the real IPv4. Python calls
    that range "reserved", which would block every such site; the question that matters is whether the EMBEDDED
    IPv4 is public. The same unwrapping covers IPv4-mapped addresses (::ffff:a.b.c.d), so a private IPv4 cannot be
    smuggled in through either form."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _public_ip(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = _embedded_v4(ipaddress.ip_address(info[4][0]))
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return bool(infos)


async def is_public_url(url: str) -> bool:
    """http(s) only, no credentials in the URL, and the host resolves to public addresses only."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return False
    return await asyncio.to_thread(_public_ip, p.hostname)


async def fetch_bytes(
    client: httpx.AsyncClient, url: str, *, max_bytes: int = MAX_BYTES, headers: dict[str, str] | None = None
) -> bytes:
    """GET with redirects handled by hand so every hop is checked, a hard size cap and a timeout."""
    for _ in range(MAX_REDIRECTS + 1):
        if not await is_public_url(url):
            raise FetchError("blocked address")
        try:
            async with client.stream("GET", url, headers={"User-Agent": USER_AGENT, **(headers or {})}, timeout=TIMEOUT_S) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code != 200:
                    raise FetchError(f"http {r.status_code}")
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise FetchError("too large")
                return bytes(body)
        except httpx.HTTPError as e:
            raise FetchError(type(e).__name__) from e
    raise FetchError("too many redirects")


def _text(el, *names: str) -> str:
    for n in names:
        found = el.find(n)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def _when(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # Atom: ISO 8601
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _strip_tags(s: str) -> str:
    out, depth = [], 0
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return " ".join("".join(out).split())


def parse_feed(xml: bytes, *, limit: int = 60) -> list[Entry]:
    """RSS 2.0 and Atom. Malformed or hostile XML raises FetchError, never a partial guess."""
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, DefusedXmlException, ValueError) as e:
        raise FetchError("not a feed") from e
    out: list[Entry] = []
    atom = "{http://www.w3.org/2005/Atom}"
    for item in root.iter("item"):  # RSS
        title = _strip_tags(_text(item, "title"))
        url = _text(item, "link")
        if title and url:
            out.append(
                Entry(
                    title[:300],
                    url,
                    _when(_text(item, "pubDate")),
                    _strip_tags(_text(item, "description"))[:600],
                )
            )
    for item in root.iter(f"{atom}entry"):  # Atom
        title = _strip_tags(_text(item, f"{atom}title"))
        link = item.find(f"{atom}link")
        url = (link.get("href") if link is not None else "") or ""
        if title and url:
            out.append(
                Entry(
                    title[:300],
                    url,
                    _when(_text(item, f"{atom}updated", f"{atom}published")),
                    _strip_tags(_text(item, f"{atom}summary", f"{atom}content"))[:600],
                )
            )
    return out[:limit]
