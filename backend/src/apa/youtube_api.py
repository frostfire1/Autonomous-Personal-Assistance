"""The owner's YouTube account through the YouTube Data API v3 (read-only): the videos they liked and the channels they follow.

The API does not give out watch history (that comes from the Takeout import, see youtube.py); liked videos and subscriptions are
the part it does offer, and they are strong signals of interest. Connecting uses Google's device flow, which needs no redirect
address and so works on a home server: the dashboard shows a short code, the owner types it at google.com/device on any
device, and the refresh token is stored encrypted (same store as the other OAuth tokens). Switches on when YOUTUBE_CLIENT_ID and
YOUTUBE_CLIENT_SECRET are in .env (a Google Cloud OAuth client of type "TVs and Limited Input devices", YouTube Data API v3
enabled); the quota used is a handful of units a day of the 10,000 given."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core import secrets
from apa.core.settings import get_settings
from apa.db.models.intel import MemoryItem
from apa.db.models.ops import OAuthToken

PROVIDER = "youtube"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
MAX_LIKED_PAGES = 4  # 200 videos
MAX_SUB_PAGES = 6  # 300 channels
MAX_ACTIVITY_PAGES = 4  # 200 recent actions (likes, saves, uploads) with their dates


class YouTubeError(Exception):
    """Safe to show: never carries a token."""


def configured() -> bool:
    s = get_settings()
    return bool(s.youtube_client_id and s.youtube_client_secret)


async def start(http: httpx.AsyncClient) -> dict[str, Any]:
    """Begin connecting: -> {"user_code", "verification_url", "device_code", "interval", "expires_in"}."""
    if not configured():
        raise YouTubeError("YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET are not set in .env (see docs/ACTIVITYWATCH.md)")
    r = await http.post(DEVICE_URL, data={"client_id": get_settings().youtube_client_id, "scope": SCOPE})
    if r.status_code != 200:
        raise YouTubeError(f"Google refused the request (http {r.status_code}): check the client id and that the YouTube Data API is enabled")
    d = r.json()
    return {k: d.get(k) for k in ("user_code", "verification_url", "device_code", "interval", "expires_in")}


async def _save(sm: async_sessionmaker, access: str, refresh: str | None, expires_in: int, scope: str | None) -> None:
    async with sm() as s:
        row = (await s.execute(select(OAuthToken).where(OAuthToken.provider == PROVIDER))).scalar_one_or_none()
        when = datetime.now(UTC) + timedelta(seconds=expires_in)
        if row is None:
            if not refresh:
                raise YouTubeError("Google sent no refresh token")
            s.add(OAuthToken(provider=PROVIDER, access_token_enc=secrets.encrypt(access), refresh_token_enc=secrets.encrypt(refresh),
                             expires_at=when, scope=scope))
        else:
            row.access_token_enc, row.expires_at = secrets.encrypt(access), when
            if refresh:
                row.refresh_token_enc = secrets.encrypt(refresh)
        await s.commit()


async def poll(sm: async_sessionmaker, http: httpx.AsyncClient, device_code: str) -> str:
    """-> "pending" while the owner has not entered the code, "connected" once they did. Anything else raises YouTubeError."""
    s = get_settings()
    r = await http.post(TOKEN_URL, data={"client_id": s.youtube_client_id, "client_secret": s.youtube_client_secret,
                                         "device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 200:
        await _save(sm, body["access_token"], body.get("refresh_token"), int(body.get("expires_in", 3600)), body.get("scope"))
        return "connected"
    if body.get("error") in ("authorization_pending", "slow_down"):
        return "pending"
    raise YouTubeError({"access_denied": "You declined access on Google's page.", "expired_token": "The code expired: start again."}.get(
        body.get("error"), f"Google answered http {r.status_code}"))


async def connected(sm: async_sessionmaker) -> bool:
    async with sm() as s:
        return (await s.execute(select(OAuthToken.id).where(OAuthToken.provider == PROVIDER))).first() is not None


async def disconnect(sm: async_sessionmaker) -> None:
    """Forget the account and everything that came from it."""
    async with sm() as s:
        await s.execute(delete(OAuthToken).where(OAuthToken.provider == PROVIDER))
        await s.execute(delete(MemoryItem).where(MemoryItem.kind == "watch", MemoryItem.source == "api"))
        await s.commit()


async def _token(sm: async_sessionmaker, http: httpx.AsyncClient) -> str:
    async with sm() as s:
        row = (await s.execute(select(OAuthToken).where(OAuthToken.provider == PROVIDER))).scalar_one_or_none()
    if row is None:
        raise YouTubeError("YouTube is not connected")
    try:
        if row.expires_at - datetime.now(UTC) > timedelta(minutes=5):
            return secrets.decrypt(row.access_token_enc)
        refresh = secrets.decrypt(row.refresh_token_enc)
    except secrets.SecretsUnavailable as e:
        raise YouTubeError(str(e)) from e
    s = get_settings()
    r = await http.post(TOKEN_URL, data={"client_id": s.youtube_client_id, "client_secret": s.youtube_client_secret,
                                         "refresh_token": refresh, "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise YouTubeError(f"token refresh failed (http {r.status_code}): connect again")
    body = r.json()
    await _save(sm, body["access_token"], None, int(body.get("expires_in", 3600)), body.get("scope"))
    return body["access_token"]


async def _pages(http: httpx.AsyncClient, token: str, path: str, params: dict, pages: int) -> list[dict]:
    items: list[dict] = []
    nxt = None
    for _ in range(pages):
        r = await http.get(f"{API}/{path}", params={**params, "maxResults": 50, **({"pageToken": nxt} if nxt else {})},
                           headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise YouTubeError(f"YouTube answered http {r.status_code} for {path}")
        d = r.json()
        items += d.get("items", [])
        nxt = d.get("nextPageToken")
        if not nxt:
            break
    return items


ACTIVITY_KINDS = {"like": "liked", "favorite": "liked", "playlistItem": "saved", "upload": "uploaded"}


def shape(liked: list[dict], subs: list[dict], acts: list[dict] | None = None) -> list[dict]:
    """The stored items: {"k": "liked", "c": channel, "t": title}, {"k": "subscribed", "c": channel name, "t": ""} and, from the
    account's activity feed, the same liked/saved/uploaded videos with the day they happened ("d"). A liked video that the
    feed dates replaces the undated one (the feed names the acting channel, so the video's channel comes from the likes list)."""
    out = []
    dated = []
    channel_of = {" ".join(str((v.get("snippet") or {}).get("title", "")).split())[:140]: " ".join(str((v.get("snippet") or {}).get("channelTitle", "")).split())[:60] for v in liked}
    for a in acts or []:
        sn = a.get("snippet") or {}
        kind, title, day = ACTIVITY_KINDS.get(sn.get("type")), " ".join(str(sn.get("title", "")).split())[:140], str(sn.get("publishedAt", ""))[:10]
        if kind and title and len(day) == 10:
            dated.append({"k": kind, "c": channel_of.get(title, ""), "t": title, "d": day})
    dated_titles = {d["t"] for d in dated if d["k"] == "liked"}
    out += dated
    for v in liked:
        sn = v.get("snippet") or {}
        if sn.get("title") and " ".join(sn["title"].split())[:140] not in dated_titles:
            out.append({"k": "liked", "c": " ".join(str(sn.get("channelTitle", "")).split())[:60], "t": " ".join(sn["title"].split())[:140]})
    for c in subs:
        if t := " ".join(str((c.get("snippet") or {}).get("title", "")).split())[:60]:
            out.append({"k": "subscribed", "c": t, "t": ""})
    return out


async def sync(sm: async_sessionmaker, http: httpx.AsyncClient, today: date) -> int:
    """Read liked videos and subscriptions and replace what an earlier sync stored. -> items stored."""
    from apa import assistant_memory

    token = await _token(sm, http)
    liked = await _pages(http, token, "videos", {"part": "snippet", "myRating": "like"}, MAX_LIKED_PAGES)
    subs = await _pages(http, token, "subscriptions", {"part": "snippet", "mine": "true"}, MAX_SUB_PAGES)
    acts = await _pages(http, token, "activities", {"part": "snippet,contentDetails", "mine": "true"}, MAX_ACTIVITY_PAGES)
    return await assistant_memory.replace_api_watch(sm, today, shape(liked, subs, acts))
