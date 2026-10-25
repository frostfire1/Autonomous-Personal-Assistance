"""An MCP server for your personal dataset: any MCP client (Claude Desktop, Claude Code, an IDE, your own agent) can read who you are,
how your days went, what you used and watched, and search your memory, with the same API key as the dashboard.

Off by default (MCP_ENABLED=true in .env). Served over Streamable HTTP at `/mcp`; send the key as `X-API-Key: <INGEST_API_KEY>` or
`Authorization: Bearer <INGEST_API_KEY>`. It is read-only: nothing here can change your data.

What it exposes is what the assistant itself may read: the About me card, the diary and day snapshots, the profile, app and content
activity, and a search of your memory (notes, learned items, diary; private journal entries only when you shared them). Money tools
(spending, transactions, budgets, balances) are only offered with MCP_EXPOSE_FINANCE=true. Whatever a connected client reads is sent
to that client's model provider, exactly like a chat message: connect only clients you trust with this data."""

import secrets
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from apa.core.db import SessionLocal
from apa.core.settings import get_settings

INSTRUCTIONS = (
    "Read-only access to one person's own assistant data: who they are (about_me), how their days went (diary), what they used and "
    "watched (activity), their patterns (profile) and a search of what the assistant remembers (search_memory). Treat everything "
    "returned as private; do not repeat it to anyone else or store it outside this conversation."
)


def _now() -> tuple[ZoneInfo, datetime]:
    tz = ZoneInfo(get_settings().timezone)
    return tz, datetime.now(tz)


def authorised(headers: dict[bytes, bytes], key: str) -> bool:
    """The dashboard's key, as `X-API-Key` or as a bearer token."""
    if not key:
        return False
    given = headers.get(b"x-api-key", b"").decode()
    auth = headers.get(b"authorization", b"").decode()
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    return bool(given) and secrets.compare_digest(given, key)


def build() -> Any | None:
    """The MCP server, or None when it is switched off."""
    s = get_settings()
    if not s.mcp_enabled:
        return None
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    from apa import about, assistant_memory
    from apa.chat.tools import run_read

    ro = ToolAnnotations(read_only_hint=True)  # every tool here only reads
    mcp = MCPServer("apa", instructions=INSTRUCTIONS)

    async def read(name: str, args: dict) -> str:
        tz, now = _now()
        return await run_read(SessionLocal, tz, now, name, args)

    @mcp.tool(annotations=ro)
    async def about_me() -> str:
        """Who the owner is: one card of identity, how they like to be talked to, personality, hobbies, tastes, routines, wellbeing, goals."""
        return await about.person_card(SessionLocal, 8000) or "The card is empty so far."

    @mcp.tool(annotations=ro)
    async def diary(days: int = 7) -> str:
        """The diary entries the assistant wrote for the last days (1 to 60), newest first: headline, the entry itself, mood, tags."""
        snaps = await assistant_memory.days(SessionLocal, max(1, min(days, 60)))
        if not snaps:
            return "No diary entries yet."
        return "\n\n".join(
            f"{d['date']} | {d.get('headline', '')} | mood {d.get('mood')} | {', '.join(d.get('tags', []))}\n{d.get('diary') or d.get('note', '')}"
            for d in reversed(snaps))

    @mcp.tool(annotations=ro)
    async def activity(hours: int = 24) -> str:
        """The apps the owner used most recently on the phone and the computer (last used time) and the latest videos and songs."""
        return await read("activity", {"hours": max(1, min(hours, 72))})

    @mcp.tool(annotations=ro)
    async def profile() -> str:
        """The owner's patterns computed from their data: sleep, active hours, food, spending habits, steps, stress, interests, screen time."""
        return await read("profile", {})

    @mcp.tool(annotations=ro)
    async def search_memory(query: str, days: int = 30) -> str:
        """Search the owner's diary, day snapshots, things the assistant learned and chat history of earlier days (a word or phrase)."""
        return await read("recall", {"query": query[:60], "days": max(1, min(days, 365))})

    if s.mcp_expose_finance:
        @mcp.tool(annotations=ro)
        async def spending(period: str = "month", by: str = "category") -> str:
            """Spending summary: period today|week|month, grouped by category|merchant|day."""
            return await read("spending", {"period": period, "by": by})

        @mcp.tool(annotations=ro)
        async def transactions(days: int = 7, category: str | None = None, merchant: str | None = None, limit: int = 10) -> str:
            """Recent recorded transactions, optionally for one category or merchant."""
            return await read("transactions", {"days": days, "category": category, "merchant": merchant, "limit": limit})

        @mcp.tool(annotations=ro)
        async def budgets() -> str:
            """This month's budgets with spent and limit."""
            return await read("budgets", {})

        @mcp.tool(annotations=ro)
        async def balances() -> str:
            """Wallet balances."""
            return await read("balances", {})

    return mcp


class Gate:
    """ASGI wrapper: only requests with the dashboard's key get in; `/mcp` and `/mcp/` both reach the server."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not authorised(dict(scope["headers"]), get_settings().ingest_api_key):
            from starlette.responses import Response

            return await Response("Missing or invalid API key (X-API-Key or Authorization: Bearer).", status_code=401)(scope, receive, send)
        if scope["path"] in ("/mcp", ""):
            scope = {**scope, "path": "/", "raw_path": b"/"}
        return await self.app(scope, receive, send)


def mount(app: Any) -> Any | None:
    """Attach the server to the FastAPI app (when enabled) and return it; its session manager must run inside the app's lifespan."""
    mcp = build()
    if mcp is None:
        return None
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.routing import Mount, Route

    inner = Gate(mcp.streamable_http_app(
        streamable_http_path="/", json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)))  # a key-protected server, not a localhost one
    app.router.routes.append(Route("/mcp", inner))
    app.router.routes.append(Mount("/mcp", app=inner))
    return mcp


async def run_lifespan(stack: AsyncExitStack, mcp: Any | None) -> None:
    if mcp is not None:
        await stack.enter_async_context(mcp.session_manager.run())
