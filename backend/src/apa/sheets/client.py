"""A small Google Sheets v4 client: service-account sign-in (a signed JWT exchanged for a short-lived token) and the four
calls the sync needs. Plain httpx so there is nothing extra to install and tests can script the whole service.

HONEST STATUS: written from Google's documented REST shape and exercised only against a scripted transport; no real
spreadsheet existed when this was built. Share the sheet with the service account's e-mail (Editor) before the first run."""

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")


class SheetsError(Exception):
    """Google could not be reached or refused. The message never contains a token or the key file's content."""


def spreadsheet_id(configured_id: str, url: str) -> str:
    """The id from SHEETS_SPREADSHEET_ID, else from a pasted sheet link."""
    if configured_id.strip():
        return configured_id.strip()
    m = _ID.search(url or "")
    return m.group(1) if m else ""


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_assertion(creds: dict, now: int | None = None) -> str:
    """The signed JWT Google trades for an access token (RS256, one hour)."""
    now = int(time.time()) if now is None else now
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": creds["client_email"], "scope": SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(creds["private_key"].encode(), password=None)
    sig = key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{claims}.{_b64(sig)}"


class SheetsClient:
    def __init__(self, http: httpx.AsyncClient, credentials_file: str, sheet_id: str) -> None:
        self._http, self._file, self.sheet_id = http, credentials_file, sheet_id
        self._token: tuple[str, float] | None = None

    async def _auth(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        try:
            creds = json.loads(Path(self._file).read_text(encoding="utf-8"))
            assertion = make_assertion(creds)
        except (OSError, ValueError, KeyError) as e:
            raise SheetsError(f"credentials file unusable ({type(e).__name__})") from None
        try:
            r = await self._http.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        except httpx.HTTPError as e:
            raise SheetsError(f"token request failed ({type(e).__name__})") from None
        if r.status_code != 200 or "access_token" not in r.json():
            raise SheetsError(f"google refused the service account (HTTP {r.status_code})")
        self._token = (r.json()["access_token"], time.time() + int(r.json().get("expires_in", 3600)))
        return self._token[0]

    async def _call(self, method: str, path: str, **kw: Any) -> dict:
        headers = {"Authorization": f"Bearer {await self._auth()}"}
        try:
            r = await self._http.request(method, f"{API}/{self.sheet_id}{path}", headers=headers, **kw)
        except httpx.HTTPError as e:
            raise SheetsError(f"sheets request failed ({type(e).__name__})") from None
        if r.status_code >= 400:
            hint = " (is the sheet shared with the service account?)" if r.status_code in (403, 404) else ""
            raise SheetsError(f"sheets answered HTTP {r.status_code}{hint}")
        return r.json() if r.content else {}

    async def tabs(self) -> list[str]:
        return [s["properties"]["title"] for s in (await self._call("GET", "?fields=sheets.properties.title")).get("sheets", [])]

    async def ensure_tab(self, title: str) -> None:
        if title not in await self.tabs():
            await self._call("POST", ":batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": title}}}]})

    async def values(self, rng: str) -> list[list[Any]]:
        return (await self._call("GET", f"/values/{rng}")).get("values", [])

    async def update(self, rng: str, rows: list[list[Any]]) -> None:
        await self._call("PUT", f"/values/{rng}", params={"valueInputOption": "RAW"}, json={"values": rows})

    async def append(self, rng: str, rows: list[list[Any]]) -> None:
        await self._call("POST", f"/values/{rng}:append", params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                         json={"values": rows})

    async def clear(self, rng: str) -> None:
        await self._call("POST", f"/values/{rng}:clear", json={})
