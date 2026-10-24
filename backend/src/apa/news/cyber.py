"""Cyber intelligence collection (blueprint v2 4.4c): new CVEs from NVD, the CISA exploited-in-the-wild catalogue
(KEV) and the FIRST exploit-probability score (EPSS). Collection, filtering and scoring are code; a model never
decides a tier and never writes a figure.

Tiers: urgent = Critical AND (in KEV or matches the owner's watchlist); normal = other Critical, or High with KEV /
EPSS >= 0.1 / a watchlist match; archive = the rest. CVEs without a score yet (new ones often have none) are kept
and counted separately so an important one is not lost for lack of a CVSS number."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.logging import get_logger
from apa.db.models.intel import CyberAdvisory, HomelabWatch
from apa.news.fetch import FetchError, fetch_bytes
from apa.news.i18n import t
from apa.news.sources import EPSS_URL, KEV_URL, NVD_URL

log = get_logger(__name__)

EPSS_HIGH = Decimal("0.1")
KEV_FRESH = timedelta(days=7)


@dataclass
class Cve:
    cve_id: str
    summary: str
    cvss: Decimal | None = None
    products: list[str] = field(default_factory=list)
    kev: bool = False
    epss: Decimal | None = None
    nvd: bool = False  # NVD's record (score, versions) is merged in


def severity(cvss: Decimal | None) -> str:
    if cvss is None:
        return "unscored"
    return "critical" if cvss >= 9 else "high" if cvss >= 7 else "medium" if cvss >= 4 else "low"


def tier(c: Cve, watch: list[str]) -> str:
    sev = severity(c.cvss)
    hit = any(w in " ".join([*c.products, c.summary]).lower() for w in watch)
    if sev == "critical" and (c.kev or hit):
        return "urgent"
    if sev == "critical" or (sev == "high" and (c.kev or hit or (c.epss or 0) >= EPSS_HIGH)):
        return "normal"
    if c.kev and sev == "unscored":
        return "normal"  # actively exploited and not scored yet: not to be lost
    return "archive"


def parse_kev(raw: bytes, now: datetime) -> dict[str, Cve]:
    out: dict[str, Cve] = {}
    for v in json.loads(raw).get("vulnerabilities", []):
        added = v.get("dateAdded", "")
        try:
            fresh = now - datetime.fromisoformat(added).replace(tzinfo=now.tzinfo) <= KEV_FRESH
        except ValueError:
            fresh = False
        if fresh:
            name = f"{v.get('vendorProject', '')} {v.get('product', '')}".strip()
            out[v["cveID"]] = Cve(
                v["cveID"],
                (v.get("shortDescription") or v.get("vulnerabilityName") or "")[:600],
                products=[name.lower()] if name else [],
                kev=True,
            )
    return out


def parse_nvd(raw: bytes) -> list[Cve]:
    out = []
    for item in json.loads(raw).get("vulnerabilities", []):
        c = item.get("cve", {})
        desc = next((d["value"] for d in c.get("descriptions", []) if d.get("lang") == "en"), "")
        score = None
        for key in ("cvssMetricV31", "cvssMetricV40", "cvssMetricV30", "cvssMetricV2"):
            m = (c.get("metrics") or {}).get(key)
            if m:
                score = Decimal(str(m[0]["cvssData"]["baseScore"]))
                break
        if c.get("id"):
            out.append(Cve(c["id"], desc[:600], score, affected(c)))
    return out


def version_spec(match: dict, cpe_version: str) -> str:
    """'< 2.4.62', '>= 2.0, < 2.4.62' or an exact '1.2.3'; '' when NVD gives no version (every version, or unknown)."""
    lo = match.get("versionStartIncluding") or ""
    lo_x = match.get("versionStartExcluding") or ""
    hi = match.get("versionEndIncluding") or ""
    hi_x = match.get("versionEndExcluding") or ""
    parts = [f">= {lo}" if lo else "", f"> {lo_x}" if lo_x else "", f"<= {hi}" if hi else "", f"< {hi_x}" if hi_x else ""]
    spec = ", ".join(p for p in parts if p)
    if not spec and cpe_version not in ("", "*", "-"):
        spec = cpe_version
    return spec


def affected(cve: dict) -> list[str]:
    """The technologies a CVE hits, with their vulnerable version ranges: 'apache http server (< 2.4.62)'. Only entries
    NVD marks as vulnerable (not 'runs on' platforms); at most 3 ranges per product and 8 products."""
    found: dict[str, list[str]] = {}
    for conf in cve.get("configurations", []) or []:
        for node in conf.get("nodes", []) or []:
            for match in node.get("cpeMatch", []) or []:
                if match.get("vulnerable") is False:
                    continue
                parts = (match.get("criteria") or "").split(":")
                if len(parts) <= 4:
                    continue
                name = f"{parts[3]} {parts[4]}".replace("_", " ")
                spec = version_spec(match, parts[5] if len(parts) > 5 else "")
                specs = found.setdefault(name, [])
                if spec and spec not in specs and len(specs) < 3:
                    specs.append(spec)
    return [f"{n} ({'; '.join(s)})" if s else n for n, s in sorted(found.items())[:8]]


async def fetch_epss(client: httpx.AsyncClient, ids: list[str]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        try:
            raw = await fetch_bytes(client, f"{EPSS_URL}?cve={','.join(chunk)}", max_bytes=500_000)
        except FetchError as e:
            log.warning("epss_failed", error=str(e))
            continue
        for row in json.loads(raw).get("data", []):
            out[row["cve"]] = Decimal(str(row["epss"]))
    return out


def _versioned(products: list[str]) -> bool:
    return any(" (" in p for p in products)


def _merge_products(a: list[str], b: list[str]) -> list[str]:
    """KEV names a product without versions ('plex media server'); NVD names the same one with them
    ('plex media server (< 1.40)'). Keep the versioned entry and drop the bare duplicate of it."""
    merged = sorted({*a, *b})
    versioned = {p.partition(" (")[0] for p in merged if " (" in p}
    return [p for p in merged if " (" in p or p not in versioned][:8]


ENRICH_PER_RUN = 5  # NVD allows few requests; the exploited list is short, and what is filled in once stays


async def _enrich_kev(sm, client, cves: dict[str, "Cve"], now: datetime, *, paced: bool) -> int:
    """The exploited-in-the-wild list (CISA KEV) gives no score and no versions. For the ones still without them, ask
    NVD about that one CVE and merge what it says. Marked in the row's source ('kev+nvd') so each is fetched once."""
    candidates = [i for i, c in cves.items() if c.kev and not c.nvd]
    async with sm() as s:
        pending = list(
            (
                await s.execute(
                    select(CyberAdvisory.cve_id).where(CyberAdvisory.source == "kev").order_by(CyberAdvisory.id.desc())
                    .limit(ENRICH_PER_RUN)
                )
            ).scalars()
        )
        asked_before = set(
            (
                await s.execute(
                    select(CyberAdvisory.cve_id).where(
                        CyberAdvisory.cve_id.in_(candidates or [""]), CyberAdvisory.source == "kev+nvd"
                    )
                )
            ).scalars()
        )
    todo = [i for i in dict.fromkeys([*pending, *candidates]) if i not in asked_before]
    done = 0
    for n, cid in enumerate(todo[:ENRICH_PER_RUN]):
        if paced and n:
            await asyncio.sleep(NVD_PACE_S)
        try:
            found = parse_nvd(await fetch_bytes(client, f"{NVD_URL}?cveId={cid}"))
        except (FetchError, ValueError, KeyError) as e:
            log.warning("nvd_enrich_failed", cve=cid, error=str(e))
            continue
        hit = next((c for c in found if c.cve_id == cid), None)
        if hit is None:  # NVD has no record (yet): note that it was asked, so it is not asked again every poll
            cves.setdefault(cid, Cve(cid, "", kev=True)).nvd = True
            continue
        k = cves.setdefault(cid, Cve(cid, hit.summary, kev=True))
        k.cvss = hit.cvss if hit.cvss is not None else k.cvss
        k.products, k.nvd = _merge_products(k.products, hit.products), True
        done += 1
    return done


NVD_PAGE = 200
NVD_MAX_PAGES = 3  # 600 CVEs: a normal day is a few hundred; more than that is not worth hammering NVD for
NVD_PACE_S = 6.5  # NVD allows 5 requests per 30 s without a key


async def _nvd_window(client: httpx.AsyncClient, start: str, end: str, *, paced: bool) -> list[Cve]:
    """Everything NVD published in the window, a page at a time (the first page alone used to cap the count at 200)."""
    base = f"{NVD_URL}?pubStartDate={start}&pubEndDate={end}&resultsPerPage={NVD_PAGE}"
    first = await fetch_bytes(client, base)
    out = parse_nvd(first)
    total = int(json.loads(first).get("totalResults", 0) or 0)
    for offset in range(NVD_PAGE, min(total, NVD_PAGE * NVD_MAX_PAGES), NVD_PAGE):
        if paced:
            await asyncio.sleep(NVD_PACE_S)
        try:
            out += parse_nvd(await fetch_bytes(client, f"{base}&startIndex={offset}"))
        except (FetchError, ValueError, KeyError) as e:  # keep what we have rather than lose the first page
            log.warning("nvd_page_failed", offset=offset, error=str(e))
            break
    return out


async def ingest_cyber(
    sm: async_sessionmaker, client: httpx.AsyncClient, now: datetime, *, nvd_key: str = ""
) -> dict:
    """-> {'seen': n, 'urgent': [cve ids newly urgent]}. A source that fails is skipped, not fatal."""
    cves: dict[str, Cve] = {}
    try:
        cves.update(parse_kev(await fetch_bytes(client, KEV_URL), now))
    except (FetchError, ValueError, KeyError) as e:
        log.warning("kev_failed", error=str(e))
    try:
        start = (now - timedelta(hours=24)).astimezone().strftime("%Y-%m-%dT%H:%M:%S.000")
        end = now.astimezone().strftime("%Y-%m-%dT%H:%M:%S.000")
        for c in await _nvd_window(client, start, end, paced=not nvd_key):
            if c.cve_id in cves:  # KEV said it is exploited; NVD adds the score and products
                k = cves[c.cve_id]
                k.cvss, k.products, k.nvd = c.cvss, _merge_products(k.products, c.products), True
            else:
                cves[c.cve_id] = c
    except (FetchError, ValueError, KeyError) as e:
        log.warning("nvd_failed", error=str(e))
    await _enrich_kev(sm, client, cves, now, paced=not nvd_key)
    interesting = [i for i, c in cves.items() if c.kev or (c.cvss or 0) >= 7]
    for i, e in (await fetch_epss(client, interesting)).items():
        if i in cves:  # only what was asked for: a stray answer must not crash the poll
            cves[i].epss = e
    async with sm() as s:
        watch = [
            w.lower()
            for w in (await s.execute(select(HomelabWatch.product).where(HomelabWatch.is_active))).scalars()
        ]
        known = {
            r.cve_id: r
            for r in (
                await s.execute(select(CyberAdvisory).where(CyberAdvisory.cve_id.in_(list(cves))))
            ).scalars()
        }
        before = {cid: r.tier for cid, r in known.items()}
        urgent = []
        for cid, c in cves.items():
            old = known.get(cid)
            if old is not None:  # a bare re-read of the exploited list must not erase what NVD told us earlier
                if old.source == "kev+nvd":
                    c.nvd = True  # asked before: stays marked
                if c.cvss is None and old.cvss is not None:
                    c.cvss = old.cvss
                if not _versioned(c.products) and _versioned(old.affected_products or []):
                    c.products = list(old.affected_products)
                    c.nvd = True
            t = tier(c, watch)
            stmt = insert(CyberAdvisory).values(
                cve_id=cid,
                summary=c.summary,
                cvss=c.cvss,
                epss=c.epss,
                in_cisa_kev=c.kev,
                affected_products=c.products,
                tier=t,
                source=("kev+nvd" if c.nvd else "kev") if c.kev else "nvd",
            )
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["cve_id"],
                    set_={
                        "cvss": c.cvss,
                        "epss": c.epss,
                        "in_cisa_kev": c.kev or stmt.excluded.in_cisa_kev,
                        "tier": t,
                        "affected_products": c.products,
                        "source": ("kev+nvd" if c.nvd else "kev") if c.kev else "nvd",
                    },
                )
            )
            if t == "urgent" and before.get(cid) != "urgent":
                urgent.append(cid)
        await s.commit()
    log.info("cyber_ingested", seen=len(cves), urgent=len(urgent))
    return {"seen": len(cves), "urgent": urgent}


async def digest_24h(sm: async_sessionmaker, now: datetime, rule: Any = None) -> dict:
    """Counts and highlights for the last 24 h (or the owner's window), from the data only (blueprint v2 4.4c G)."""
    hours = rule.hours if rule else 24
    async with sm() as s:
        rows = (
            (
                await s.execute(
                    select(CyberAdvisory).where(CyberAdvisory.fetched_at >= now - timedelta(hours=hours))
                )
            )
            .scalars()
            .all()
        )
    by: dict[str, int] = {}
    for r in rows:
        by[severity(r.cvss)] = by.get(severity(r.cvss), 0) + 1
    top = sorted(
        (r for r in rows if r.tier in ("urgent", "normal")),
        key=lambda r: (r.tier != "urgent", -(r.epss or 0), r.cve_id),
    )[:3]
    return {
        "total": len(rows),
        "by_severity": by,
        "kev": sum(1 for r in rows if r.in_cisa_kev),
        "urgent": [r.cve_id for r in rows if r.tier == "urgent"],
        "top": [(r.cve_id, r.summary[:120]) for r in top],
    }


def _watched(r: Any, watch: list[str]) -> bool:
    """A word of the owner's watch list is in the CVE's vendor/product names or its summary."""
    text = (" ".join(r.affected_products or []) + " " + (r.summary or "")).lower()
    return any(w in text for w in watch)


async def severe_24h(sm: async_sessionmaker, now: datetime, *, limit: int = 12, rule: Any = None) -> list[dict]:
    """The CVEs that matter in the window (default 24 h), most worrying first: on the owner's watch list, exploited, urgent,
    then by exploit probability and score. By default high and critical plus anything in the exploited catalogue; the
    owner can change the minimum score, switch the exploited rule off and add a watch list. Facts only; nothing a model wrote."""
    min_cvss, kev_always, watch, hours = (rule.min_cvss, rule.kev_always, rule.watch, rule.hours) if rule else (7.0, True, [], 24)
    async with sm() as s:
        rows = (
            await s.execute(select(CyberAdvisory).where(CyberAdvisory.fetched_at >= now - timedelta(hours=hours)))
        ).scalars().all()
    watched = {r.cve_id for r in rows if watch and _watched(r, watch)}
    worth = [r for r in rows if r.cve_id in watched or (kev_always and r.in_cisa_kev) or (r.cvss or 0) >= min_cvss]
    worth.sort(key=lambda r: (r.cve_id not in watched, not r.in_cisa_kev, r.tier != "urgent", -(r.epss or 0), -(r.cvss or 0), r.cve_id))
    return [
        {"id": r.cve_id, "cvss": r.cvss, "epss": r.epss, "kev": r.in_cisa_kev, "tier": r.tier, "watch": r.cve_id in watched,
         "products": list(r.affected_products or []), "summary": r.summary}
        for r in worth[:limit]
    ]


_SAY = (("<=", "v_lte"), (">=", "v_gte"), ("<", "v_lt"), (">", "v_gt"))


def spoken_product(entry: str, lang: str = "id") -> str:
    """'apache http server (>= 2.0, < 2.4.62)' -> 'apache http server, mulai versi 2.0 dan di bawah versi 2.4.62' (or the
    English equivalent). Only the first range is spoken; the full list goes to Telegram."""
    name, _, rest = entry.partition(" (")
    spec = rest.rstrip(")").split(";")[0].strip()
    if not spec:
        return name
    said = []
    for part in (p.strip() for p in spec.split(",")):
        for sym, key in _SAY:
            if part.startswith(sym):
                said.append(f"{t(lang, key)} {part[len(sym):].strip()}")
                break
        else:
            said.append(f"{t(lang, 'v_eq')} {part}")
    return f"{name}, " + t(lang, "v_join").join(said)
