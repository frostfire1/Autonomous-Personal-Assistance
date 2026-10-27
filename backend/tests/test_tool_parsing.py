"""Tests for DeepSeek DSML and XML tool parsing, reasoning stripping, and self-update root resolution."""


from apa.chat import actions, assistant
from apa.gateway.routers import update


def test_parse_step_dsml_tool_invocation():
    """Test user's exact DSML snippet: deepseek DSML calls maps tool."""
    raw = """<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="maps">
<｜｜DSML｜｜ parameter name="query" string="true">NCSC Indonesia alamat</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>"""
    kind, payload = assistant.parse_step(raw)
    assert kind == "tool"
    assert payload == ("maps", {"query": "NCSC Indonesia alamat"})


def test_parse_step_dsml_with_thinking_tags():
    """Test DSML tool invocation preceded by reasoning <think> block."""
    raw = """<think>
User is asking for NCSC location. I should call the maps tool to find the address.
</think>
<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="maps">
<｜｜DSML｜｜ parameter name="query" string="true">NCSC Jakarta</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>"""
    kind, payload = assistant.parse_step(raw)
    assert kind == "tool"
    assert payload == ("maps", {"query": "NCSC Jakarta"})


def test_parse_step_xml_function_call():
    """Test standard XML function invocation tags."""
    raw = """<tool_call>
<function name="search">
<parameter name="query">cuaca jakarta hari ini</parameter>
<parameter name="count">3</parameter>
</function>
</tool_call>"""
    kind, payload = assistant.parse_step(raw)
    assert kind == "tool"
    assert payload == ("search", {"query": "cuaca jakarta hari ini", "count": 3})


def test_parse_step_json_in_markdown():
    """Test standard JSON tool call wrapped in markdown fence."""
    raw = """```json
{"tool": "maps", "args": {"query": "kantor bssn"}}
```"""
    kind, payload = assistant.parse_step(raw)
    assert kind == "tool"
    assert payload == ("maps", {"query": "kantor bssn"})


def test_parse_step_prose_with_think_and_strip():
    """Test plain prose reply strips <think> tags cleanly."""
    raw = """<think>
I know where NCSC is, I can reply directly.
</think>
NCSC Indonesia beralamat di Ragunan, Jakarta Selatan."""
    kind, payload = assistant.parse_step(raw)
    assert kind == "say"
    assert "<think>" not in payload
    assert "</think>" not in payload
    assert "Ragunan" in payload


def test_parse_step_strips_leaked_markup_from_say():
    """Test that if a model outputs leftover DSML tags in a say reply, they are stripped."""
    raw = """<｜｜DSML｜｜ calls>
Halo Joe! Kantor NCSC ada di Jakarta Selatan.
</｜｜DSML｜｜ calls>"""
    kind, payload = assistant.parse_step(raw)
    assert kind == "say"
    assert "<｜｜DSML｜｜" not in payload
    assert "</｜｜DSML｜｜ calls>" not in payload
    assert "Halo Joe!" in payload


def test_actions_parse_call_dsml():
    """Test that actions.parse_call supports DSML invocation for ledger actions."""
    raw = """<think>Adding coffee expense</think>
<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="add_transaction">
<｜｜DSML｜｜ parameter name="amount">35000</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="merchant">Kopi Kenangan</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="direction">out</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>"""
    res = actions.parse_call(raw)
    assert isinstance(res, tuple)
    tool, args = res
    assert tool == "add_transaction"
    assert args.get("amount") == 35000
    assert args.get("merchant") == "Kopi Kenangan"
    assert args.get("direction") == "out"


def test_update_root_resolution():
    """Test that update._root() resolves the repository root containing .git."""
    root = update._root()
    assert root.exists(), f"Resolved root {root} does not exist"
    assert (root / ".git").exists(), f"Resolved root {root} does not contain .git"
    assert (root / "deploy" / "deploy.sh").exists(), f"Resolved root {root} does not contain deploy/deploy.sh"


def test_update_root_env_override(monkeypatch, tmp_path):
    """Test that update._root() respects PROJECT_ROOT environment variable."""
    fake_root = tmp_path / "fake_repo"
    fake_root.mkdir()
    (fake_root / ".git").mkdir()
    monkeypatch.setenv("PROJECT_ROOT", str(fake_root))
    assert update._root() == fake_root
