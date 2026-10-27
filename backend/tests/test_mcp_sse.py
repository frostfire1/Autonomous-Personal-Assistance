"""Tests for enhanced MCP client supporting SSE, auto-all-read policy, and personal servers."""
import asyncio

from apa import mcp_client


def test_catalog_and_catalog_chars_limit():
    assert mcp_client.CATALOG_CHARS >= 4000

    tool_list = [
        {"name": f"tool_{i}", "description": f"Detailed tool description {i} for health and nutrition", "args": ["arg1", "arg2?"], "read_only": True}
        for i in range(25)
    ]
    server = mcp_client.ServerCfg(
        id="health",
        name="Personal Health",
        url="http://localhost:8000/sse",
        tools=tool_list,
        enabled=True,
    )

    catalog_text = mcp_client.catalog([server])
    assert "health.tool_0" in catalog_text
    assert "health.tool_20" in catalog_text
    assert len(catalog_text) <= mcp_client.CATALOG_CHARS


def test_set_policy_auto_all_read(monkeypatch):
    tools = [
        {"name": "get_vitals", "description": "Get vitals", "args": [], "read_only": True},
        {"name": "get_sleep", "description": "Get sleep", "args": [], "read_only": True},
        {"name": "log_meal", "description": "Log meal", "args": [], "read_only": False},
        {"name": "disabled_read", "description": "Disabled", "args": [], "read_only": True},
    ]
    server = mcp_client.ServerCfg(
        id="health",
        name="Health",
        url="http://localhost:8000/sse",
        tools=tools,
        auto=[],
        off=["disabled_read"],
    )

    saved = []
    async def fake_load(sm):
        return [server]

    async def fake_save(servers, sm):
        saved.append(servers)

    monkeypatch.setattr(mcp_client, "load", fake_load)
    monkeypatch.setattr(mcp_client, "save", fake_save)

    # Call set_policy with auto_all_read=True
    ok = asyncio.run(mcp_client.set_policy(None, "health", tool=None, auto_all_read=True))
    assert ok is True
    updated_server = saved[0][0]
    # get_vitals and get_sleep should be auto-allowed
    assert "get_vitals" in updated_server.auto
    assert "get_sleep" in updated_server.auto
    # log_meal is not read_only, so not auto
    assert "log_meal" not in updated_server.auto
    # disabled_read was off, so not in auto
    assert "disabled_read" not in updated_server.auto


def test_connect_chooses_sse_for_sse_url(monkeypatch):
    server = mcp_client.ServerCfg(
        id="health",
        name="Health",
        url="http://localhost:8000/sse",
    )

    called_sse = []
    called_streamable = []

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    def fake_sse_client(url, **kw):
        called_sse.append(url)
        return "SSE_STREAM"

    def fake_streamable(url, **kw):
        called_streamable.append(url)
        return "STREAMABLE_STREAM"

    monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse_client)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", fake_streamable)
    monkeypatch.setattr("mcp.Client", lambda stream, **kw: FakeClient())

    async def run_test():
        async with mcp_client.connect(server) as client:
            assert client is not None

    asyncio.run(run_test())
    assert len(called_sse) == 1
    assert "http://localhost:8000/sse" in called_sse[0]
    assert len(called_streamable) == 0
