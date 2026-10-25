"""Signing in to an MCP server that wants a login (OAuth 2.1 with PKCE), for example a nutrition tracker with your own account.

The SDK does the protocol (discovery, registering this app as a client, PKCE, token refresh). What is left is the part a server at
home cannot do by itself: the browser. So the owner does it by hand, once:

  1. Skills > "Connect with login": the server's login page address is shown;
  2. open it, sign in (Google, e-mail...), allow access; the browser then goes to an address that starts with
     `http://localhost:8787/callback?code=...` and shows an error page: that is expected, nothing listens there;
  3. copy that whole address from the browser's address bar and paste it back on the Skills page.

The tokens are kept encrypted (SECRETS_KEY); the SDK refreshes them by itself, and when the login has expired for good the skill says
so and the owner signs in again. The server registers this app under the name "APA personal assistant"."""

import asyncio
import json
import secrets as pysecrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy.ext.asyncio import async_sessionmaker

from apa import mcp_client
from apa.core import secrets
from apa.core.logging import get_logger

log = get_logger(__name__)

REDIRECT = "http://localhost:8787/callback"
PENDING_TTL_S = 600
_pending: dict[str, "Pending"] = {}


class MemoryStorage:
    """The SDK's token storage, in memory until the sign-in has worked."""

    def __init__(self) -> None:
        self.tokens: Any = None
        self.client_info: Any = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens) -> None:
        self.tokens = tokens

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info) -> None:
        self.client_info = client_info

    def dump(self) -> str:
        return json.dumps({"tokens": self.tokens.model_dump(mode="json") if self.tokens else None,
                           "client_info": self.client_info.model_dump(mode="json") if self.client_info else None})

    def load(self, raw: str) -> None:
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

        d = json.loads(raw)
        self.tokens = OAuthToken(**d["tokens"]) if d.get("tokens") else None
        self.client_info = OAuthClientInformationFull(**d["client_info"]) if d.get("client_info") else None


class SavedStorage(MemoryStorage):
    """The same, kept encrypted in the server's configuration: refreshed tokens are written back."""

    def __init__(self, sm: async_sessionmaker, server_id: str, raw: str):
        super().__init__()
        self._sm, self._id = sm, server_id
        self.load(raw)

    async def set_tokens(self, tokens) -> None:
        await super().set_tokens(tokens)
        servers = await mcp_client.load(self._sm)
        for c in servers:
            if c.id == self._id:
                c.oauth_enc = secrets.encrypt(self.dump())
        await mcp_client.save(servers, self._sm)


def provider(url: str, storage: MemoryStorage, redirect_handler, callback_handler):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    meta = OAuthClientMetadata(redirect_uris=[REDIRECT], client_name="APA personal assistant", grant_types=["authorization_code", "refresh_token"],
                               response_types=["code"], token_endpoint_auth_method="none")
    return OAuthClientProvider(server_url=url, client_metadata=meta, storage=storage, redirect_handler=redirect_handler, callback_handler=callback_handler)


async def saved_provider(c: "mcp_client.ServerCfg", sm: async_sessionmaker):
    """The auth for a server that was signed in to before; it cannot ask for a new login in the background."""

    async def no_redirect(_url: str) -> None:
        raise mcp_client.McpError("The login has expired: sign in again on the Skills page.")

    async def no_callback():
        raise mcp_client.McpError("The login has expired: sign in again on the Skills page.")

    return provider(c.url, SavedStorage(sm, c.id, secrets.decrypt(c.oauth_enc)), no_redirect, no_callback)


class Pending:
    def __init__(self, name: str, url: str):
        self.id = pysecrets.token_hex(8)
        self.name, self.url = name, url
        self.auth_url: asyncio.Future = asyncio.get_running_loop().create_future()
        self.code: asyncio.Future = asyncio.get_running_loop().create_future()
        self.storage = MemoryStorage()
        self.task: asyncio.Task | None = None
        self.born = time.monotonic()


async def _run(p: Pending, sm: async_sessionmaker) -> "mcp_client.ServerCfg":
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import AuthorizationCodeResult

    async def redirect(url: str) -> None:
        if not p.auth_url.done():
            p.auth_url.set_result(url)

    async def callback():
        d = await asyncio.wait_for(p.code, PENDING_TTL_S)
        return AuthorizationCodeResult(code=d["code"], state=d.get("state"), iss=d.get("iss"))  # the server's own name, checked by the SDK

    auth = provider(p.url, p.storage, redirect, callback)
    async with httpx2.AsyncClient(auth=auth, timeout=mcp_client.CALL_TIMEOUT_S) as http:
        async with Client(streamable_http_client(p.url, http_client=http)) as client:
            tools = await asyncio.wait_for(client.list_tools(), mcp_client.CALL_TIMEOUT_S)
    servers = await mcp_client.load(sm)
    c = mcp_client.ServerCfg(id=mcp_client.slug(p.name), name=p.name.strip()[:40], url=p.url, oauth_enc=secrets.encrypt(p.storage.dump()),
                             tools=[mcp_client._summary(t) for t in tools.tools][:60], refreshed_at=time.time())
    await mcp_client.save([x for x in servers if x.id != c.id] + [c], sm)
    return c


def why(exc: BaseException) -> str:
    """The reason inside an exception group (the SDK nests its errors), in a few words the owner can read."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {str(exc)[:160]}" if str(exc) else type(exc).__name__


def _sweep() -> None:
    for k, p in list(_pending.items()):
        if time.monotonic() - p.born > PENDING_TTL_S:
            if p.task and not p.task.done():
                p.task.cancel()
            _pending.pop(k, None)


async def start(sm: async_sessionmaker, name: str, url: str) -> dict[str, Any]:
    """Begin a sign-in. -> {"auth_url", "pending"} to open in the browser, or {"server": ...} when no login was needed."""
    _sweep()
    url = mcp_client.check_url(url)
    sid = mcp_client.slug(name)
    servers = await mcp_client.load(sm)
    if len(sid) < 2 or (any(x.id == sid for x in servers)):
        raise mcp_client.McpError("Give the server a short, unique name.")
    if len(servers) >= mcp_client.MAX_SERVERS:
        raise mcp_client.McpError(f"At most {mcp_client.MAX_SERVERS} servers.")
    try:
        secrets.encrypt("probe")
    except secrets.SecretsUnavailable as e:
        raise mcp_client.McpError("A login cannot be stored: SECRETS_KEY is not set on the server.") from e
    p = Pending(name, url)
    p.task = asyncio.create_task(_run(p, sm))
    _pending[p.id] = p
    done, _ = await asyncio.wait({p.auth_url, p.task}, return_when=asyncio.FIRST_COMPLETED, timeout=25)
    if p.auth_url.done() and not p.auth_url.cancelled():
        return {"pending": p.id, "auth_url": p.auth_url.result(), "redirect": REDIRECT}
    if p.task.done():
        _pending.pop(p.id, None)
        try:
            return {"server": mcp_client.public(p.task.result())}
        except Exception as exc:
            raise mcp_client.McpError(f"Could not connect: {why(exc)}. Check the address.") from exc
    p.task.cancel()
    _pending.pop(p.id, None)
    raise mcp_client.McpError("The server did not answer in time.")


async def finish(pending_id: str, redirected_url: str) -> "mcp_client.ServerCfg":
    """The owner pasted the address the browser ended on: finish the sign-in and keep the server."""
    p = _pending.get(pending_id)
    if p is None or p.task is None:
        raise mcp_client.McpError("That sign-in expired: start again.")
    q = parse_qs(urlparse(redirected_url.strip()).query)
    if q.get("error"):
        _pending.pop(pending_id, None)
        p.task.cancel()
        raise mcp_client.McpError(f"The login was refused ({q['error'][0]}).")
    code = (q.get("code") or [""])[0]
    if not code:
        raise mcp_client.McpError("That address has no code: copy the whole address the browser ended on, it starts with " + REDIRECT)
    if not p.code.done():
        p.code.set_result({"code": code, "state": (q.get("state") or [None])[0], "iss": (q.get("iss") or [None])[0]})
    try:
        return await asyncio.wait_for(p.task, mcp_client.CALL_TIMEOUT_S + 10)
    except Exception as exc:
        raise mcp_client.McpError(f"The sign-in did not complete: {why(exc)}. Start again.") from exc
    finally:
        _pending.pop(pending_id, None)
