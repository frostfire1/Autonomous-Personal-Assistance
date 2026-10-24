"""Which apps' notifications are listened to. A built-in list of Indonesian banks and wallets, plus the apps the owner
picked on the phone (the phone scans what is installed and registers the choices here). Everything else is ignored, so a
shopping or social app can never feed the ledger. Stored in `policy_rules` so it survives restarts and is editable."""

import re

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from apa.db.models.budget import PolicyRule
from apa.ingestion.bank_parser import PACKAGE_WALLET

RULE_KEY = "notification_packages"
MAX_APPROVED = 60
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+){1,8}$")

# Words in an app's name that say which wallet its transactions belong to. Order matters: the first match wins.
_KNOWN = (
    ("wondr", "bank_bni"), ("bni", "bank_bni"), ("jago", "bank_jago"), ("jatim", "bank_jatim"), ("jconnect", "bank_jatim"),
    ("livin", "bank_mandiri"), ("mandiri", "bank_mandiri"), ("brimo", "bank_bri"), ("bri", "bank_bri"),
    ("blu", "bank_blu"), ("bca", "bank_bca"), ("jenius", "bank_jenius"), ("octo", "bank_cimb"), ("cimb", "bank_cimb"),
    ("permata", "bank_permata"), ("danamon", "bank_danamon"), ("bsi", "bank_bsi"), ("seabank", "bank_seabank"),
    ("neo", "bank_neo"), ("allo", "bank_allo"), ("superbank", "bank_superbank"), ("gopay", "gopay"), ("ovo", "ovo"),
    ("dana", "dana"), ("linkaja", "linkaja"), ("shopeepay", "shopeepay"),
)


class AppIn(BaseModel):
    package: str = Field(min_length=3, max_length=120)
    label: str = Field(default="", max_length=60)


def wallet_for(label: str, package: str) -> str:
    """'Bank Jatim JConnect' -> 'bank_jatim'; an unknown bank gets a safe generated name, never a guess at a known one."""
    name = f"{label} {package}".lower()
    for word, wallet in _KNOWN:
        if word in name:
            return wallet
    clean = lambda s: re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")  # noqa: E731
    slug = clean(label).removeprefix("bank_") or clean("_".join(package.split(".")[-2:]))
    return f"bank_{slug}"[:24].rstrip("_")


async def approved_extra(s: AsyncSession) -> dict[str, str]:
    row = (await s.execute(select(PolicyRule).where(PolicyRule.rule_key == RULE_KEY, PolicyRule.is_active))).scalar_one_or_none()
    return dict(row.value_json or {}) if row else {}


async def approved_wallets(s: AsyncSession) -> dict[str, str]:
    """Built-in apps plus the owner's picks. A pick never overrides a built-in package's wallet."""
    return {**await approved_extra(s), **PACKAGE_WALLET}


async def register(s: AsyncSession, apps: list[AppIn]) -> dict[str, str]:
    """Replace the owner's picks with `apps` (the phone sends the full current selection). Invalid or built-in packages
    are dropped; at most MAX_APPROVED are kept. -> the stored {package: wallet}."""
    picks: dict[str, str] = {}
    for a in apps:
        if a.package in PACKAGE_WALLET or not _PACKAGE.match(a.package) or len(picks) >= MAX_APPROVED:
            continue
        picks[a.package] = wallet_for(a.label, a.package)
    stmt = insert(PolicyRule).values(
        rule_key=RULE_KEY, scope="global", scope_ref="", value_json=picks, updated_by="android_app", is_active=True)
    await s.execute(stmt.on_conflict_do_update(
        index_elements=["rule_key", "scope", "scope_ref"], set_={"value_json": picks, "updated_by": "android_app"}))
    await s.commit()
    return picks
