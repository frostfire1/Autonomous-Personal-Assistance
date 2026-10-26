"""Structural checks on infra/litellm/config.yaml. No network, no LiteLLM needed.

The availability facts are dated probes of two providers' accounts (see the comment at the top of the
config). Capability is PER PROVIDER: glm-5.3 reads images on Osiris and not on kunciai. If a key or plan
changes, re-probe and update the facts: the point of this file is that a chain can never silently contain a
model that is not offered, one known not to work, or one not verified for the job it is given.
"""

from collections import Counter
from pathlib import Path

import pytest
import yaml

CONFIG = Path(__file__).resolve().parents[2] / "infra" / "litellm" / "config.yaml"

PROVIDERS = {
    "osiris": {
        "base_env": "OSIRIS_API_BASE",
        "key_env": "OSIRIS_API_KEY",
        "prefix": "",
        # the key's allowed list, as given by the Osiris dashboard
        "offered": {
            "deepseek-v4-flash", "deepseek-v4-pro", "glm-4.6", "glm-4.6v", "glm-4.7", "glm-5.1", "glm-5.2",
            "glm-5v-turbo", "kimi-k2.5", "kimi-k2.7", "minimax-m3", "osiris-claude-haiku-4.5", "qwen3.7-max",
            "qwen3.7-plus", "osiris-claude-sonnet-4.5", "glm-5.3", "kimi-k3", "qwen3.8-max",
        },
        # model_not_found (allowed but nothing behind them) or erroring 502 at probe time, 2026-10-23/24
        "not_working": {
            "glm-4.6", "glm-4.6v", "glm-4.7", "minimax-m3", "osiris-claude-haiku-4.5",
            "osiris-claude-sonnet-4.5", "qwen3.7-max", "qwen3.7-plus", "deepseek-v4-flash", "qwen3.8-max",
            "glm-5.1",
        },
        "vision_ok": {"glm-5v-turbo", "glm-5.3", "glm-5.2", "kimi-k2.5", "kimi-k2.7", "kimi-k3"},
        "no_valid_json": {"glm-5v-turbo", "kimi-k2.5"},
    },
    "kunci": {
        "base_env": "KUNCI_API_BASE",
        "key_env": "KUNCI_API_KEY",
        "prefix": "kunciai/",
        # the chat models in kunciai's /models listing (image/video generators left out)
        "offered": {
            "claude-fable-5.1", "claude-haiku-4.5", "claude-haiku-5.5", "claude-opus-4.6", "claude-opus-4.7",
            "claude-opus-4.8", "claude-opus-5", "claude-opus-5.5", "claude-sonnet-4.6", "claude-sonnet-5",
            "deepseek-v4-flash", "deepseek-v4-flash-0731", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro",
            "deepseek-v4-pro-0813", "deepseek-v4.1-flash", "gemini-3.1-pro-high", "gemini-3.1-pro-low",
            "gemini-3.6-flash-high", "gemini-3.7-flash-high", "glm-5.0-turbo", "glm-5.1", "glm-5.2", "glm-5.3",
            "glm-5.3-flash", "glm-5.3-flashx", "glm-5v-turbo", "gpt-5.4-mini", "gpt-5.5", "grok-4.5", "grok-4.6",
            "hy3-preview", "hy4-preview", "kimi-k2.5", "kimi-k2.6", "kimi-k2.7", "kimi-k2.8-preview", "kimi-k3",
            "minimax-m2.7", "minimax-m3", "qwen3.7-max", "qwen3.7-plus", "qwen3.8-flash", "qwen3.8-max",
        },
        # 502 / 503 at probe time, 2026-10-24
        "not_working": {"glm-5.3-flash", "kimi-k2.7", "kimi-k3", "deepseek-v4-flash-vision-exp"},
        "vision_ok": {"glm-5v-turbo"},  # glm-5.3 here answered "I can't see the receipt"
        "no_valid_json": {"glm-5v-turbo", "kimi-k2.5"},  # answered inside a markdown fence
    },
}
JSON_ROUTES = {"router-routine-classifier", "router-arbiter-persona", "router-macro-reasoning"}
VISION_CHAIN = "router-vision-ocr"
LONG_CHAT_TIMEOUT_ALLOWED = {"fallback-osiris-kimi-k3"}  # slow research model (ping ~16 s), documented in the config


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def provider_of(params: dict) -> str:
    base = params["api_base"]
    return next(name for name, p in PROVIDERS.items() if base == f"os.environ/{p['base_env']}")


@pytest.fixture(scope="module")
def deployed(cfg):
    """model_name -> (provider, model id as that provider knows it)."""
    out = {}
    for m in cfg["model_list"]:
        p = m["litellm_params"]
        provider = provider_of(p)
        out[m["model_name"]] = (provider, p["model"].removeprefix("openai/").removeprefix(PROVIDERS[provider]["prefix"]))
    return out


def chains(cfg):
    return {next(iter(f)): next(iter(f.values())) for f in cfg["router_settings"]["fallbacks"]}


def deployment(cfg, name):
    return next(m for m in cfg["model_list"] if m["model_name"] == name)["litellm_params"]


# ---- every deployment is real, reachable and uses the right credentials -----------------------------------------


def test_model_names_are_unique(cfg):
    assert not [n for n, c in Counter(m["model_name"] for m in cfg["model_list"]).items() if c > 1]


def test_every_deployment_reads_its_providers_base_url_and_key_from_the_environment(cfg):
    for m in cfg["model_list"]:
        p = m["litellm_params"]
        prov = PROVIDERS[provider_of(p)]
        assert p["api_base"] == f"os.environ/{prov['base_env']}" and p["api_key"] == f"os.environ/{prov['key_env']}"
        assert "sk-" not in str(p)


def test_every_model_is_offered_by_its_own_provider(deployed):
    for name, (provider, model) in deployed.items():
        assert model in PROVIDERS[provider]["offered"], f"{name}: {provider} does not list {model}"


def test_no_model_that_is_known_not_to_work_is_used(deployed):
    for name, (provider, model) in deployed.items():
        assert model not in PROVIDERS[provider]["not_working"], f"{name}: {provider}/{model} was failing at probe time"


# ---- the chains ------------------------------------------------------------------------------------------------------


def test_every_fallback_target_is_a_defined_entry(cfg, deployed):
    for route, chain in chains(cfg).items():
        assert route in deployed, route
        missing = [t for t in chain if t not in deployed]
        assert not missing, f"{route} falls back to undefined {missing}"


def test_every_primary_route_has_a_chain_of_at_least_three_distinct_fallbacks(cfg, deployed):
    primaries = [n for n in deployed if n.startswith("router-")]
    assert set(primaries) == set(chains(cfg)), "a route without a chain, or a chain without a route"
    for route, chain in chains(cfg).items():
        hops = [deployed[t] for t in chain]
        assert len(set(hops)) == len(hops) >= 3, f"{route}: {hops}"


def test_a_fallback_never_repeats_the_primary_exactly(cfg, deployed):
    """The same MODEL on the other provider is fine; the same provider+model is a no-op."""
    for route, chain in chains(cfg).items():
        assert deployed[route] not in {deployed[t] for t in chain}, route


def test_the_first_fallback_of_every_route_is_on_the_other_provider(cfg, deployed):
    """Both providers rate-limit PER KEY: a fallback on the same key is refused too when it is throttled."""
    for route, chain in chains(cfg).items():
        assert deployed[chain[0]][0] != deployed[route][0], f"{route} falls back to the same provider first"


def test_fallbacks_are_not_primaries_of_other_chains(cfg):
    assert not [t for chain in chains(cfg).values() for t in chain if t.startswith("router-")]


def test_vision_route_only_uses_models_verified_to_read_an_image_on_their_own_provider(cfg, deployed):
    for name in [VISION_CHAIN, *chains(cfg)[VISION_CHAIN]]:
        provider, model = deployed[name]
        assert model in PROVIDERS[provider]["vision_ok"], f"{name}: {provider}/{model} is not verified to read images"


@pytest.mark.parametrize("route", sorted(JSON_ROUTES))
def test_json_routes_only_use_models_verified_to_return_unfenced_json(cfg, deployed, route):
    for name in [route, *chains(cfg)[route]]:
        provider, model = deployed[name]
        assert model not in PROVIDERS[provider]["no_valid_json"], f"{name}: {provider}/{model}"


def test_the_primary_provider_is_kunciai_and_osiris_is_only_a_fallback(cfg, deployed):
    """The decision recorded in docs/decisions.md: kunciai leads because it measured better on every axis."""
    assert {deployed[n][0] for n in deployed if n.startswith("router-")} == {"kunci"}
    assert any(deployed[t][0] == "osiris" for chain in chains(cfg).values() for t in chain)


# ---- timeouts, retries, cooldown ---------------------------------------------------------------------------------------


def test_retries_are_off_and_failures_cool_a_model_down(cfg):
    rs = cfg["router_settings"]
    assert rs["num_retries"] == 0 and rs["allowed_fails"] >= 1 and rs["cooldown_time"] >= 30


def test_the_chat_timeout_is_short_enough_to_fall_back_and_long_enough_to_answer(cfg):
    # slowest NORMAL chat answer seen live: 11 s; 3 of 60 calls hung until the old 45 s limit
    assert 15 <= cfg["router_settings"]["timeout"] <= 30


def test_a_model_that_keeps_timing_out_is_cooled_down_not_paid_for_on_every_call(cfg):
    """allowed_fails alone does not cool a model down after a timeout (measured against a stalling upstream);
    this policy does. 1 = tolerate one stall as a one-off, cool down on the second."""
    assert 0 <= cfg["router_settings"]["allowed_fails_policy"]["TimeoutErrorAllowedFails"] <= 2


def test_the_vision_route_and_all_its_fallbacks_have_their_own_longer_timeout(cfg):
    for name in [VISION_CHAIN, *chains(cfg)[VISION_CHAIN]]:
        assert deployment(cfg, name)["timeout"] >= 45, f"{name} would cut a multi-photo reading short"


def test_the_vision_chain_shares_no_entry_with_a_chat_chain(cfg):
    """Otherwise raising a vision fallback's timeout would also slow every chat route down."""
    c = chains(cfg)
    vision = set(c[VISION_CHAIN])
    chat = {t for route, chain in c.items() if route != VISION_CHAIN for t in chain}
    assert not vision & chat, vision & chat
    assert all(n.startswith("fallback-vision-") for n in vision)


def test_chat_deployments_inherit_the_short_timeout_except_the_documented_slow_one(cfg):
    vision = {VISION_CHAIN, *chains(cfg)[VISION_CHAIN]}
    for m in cfg["model_list"]:
        if m["model_name"] not in vision | LONG_CHAT_TIMEOUT_ALLOWED:
            assert "timeout" not in m["litellm_params"], f"{m['model_name']} overrides the chat timeout"


def test_the_client_can_wait_for_one_stalled_attempt_plus_a_fallback(cfg):
    from apa.integrations.llm import DEFAULT_TIMEOUT_S, VISION_TIMEOUT_S

    assert DEFAULT_TIMEOUT_S >= 2 * cfg["router_settings"]["timeout"] + 10  # a stall, then a slow fallback answer
    assert VISION_TIMEOUT_S >= 2 * deployment(cfg, VISION_CHAIN)["timeout"] + 10


def test_the_proxy_never_logs_message_text_so_a_private_session_leaves_no_trace_in_it(cfg):
    assert cfg["litellm_settings"]["turn_off_message_logging"] is True
    assert cfg["general_settings"]["store_prompts_in_spend_logs"] is False
    assert "database_url" not in {k.lower() for k in cfg["general_settings"]}  # no database behind the proxy
    assert not any(k in cfg["litellm_settings"] for k in ("success_callback", "callbacks", "cache"))  # nothing that copies calls



# ---- the AI settings edited in the dashboard --------------------------------------------------------------------------------


def test_ai_settings_refuse_inconsistent_choices():
    import pytest

    from apa.llm_config import LLMConfig

    prov = {"p": {"base_url": "https://api.example.com/v1/", "api_key": "sk-secret-1234"}}
    ok = LLMConfig(providers=prov, routes={"router-vision-ocr": {"provider": "p", "model": "vl-1"}},
                   agents={"A-02": {"enabled": False}})
    assert ok.providers["p"].base_url == "https://api.example.com/v1"  # trailing slash removed
    for bad in (
        {"providers": prov, "routes": {"router-vision-ocr": {"provider": "p"}}},  # provider without a model
        {"providers": prov, "routes": {"router-vision-ocr": {"provider": "nope", "model": "m"}}},  # undefined provider
        {"providers": prov, "routes": {"router-made-up": {"enabled": False}}},  # unknown role
        {"providers": prov, "agents": {"A-01": {"enabled": False}}},  # an engine, not an LLM agent
        {"providers": {"Bad Name!": prov["p"]}},
        {"providers": {"p": {"base_url": "ftp://x"}}},
    ):
        with pytest.raises(ValueError):
            LLMConfig(**bad)


def test_an_agents_own_setting_wins_over_its_role_and_keys_are_never_shown():
    from apa.llm_config import LLMConfig, public

    cfg = LLMConfig(
        providers={"p": {"base_url": "https://x.example/v1", "api_key": "sk-secret-abcd1234"}},
        routes={"router-routine-classifier": {"provider": "p", "model": "small"}},
        agents={"A-02": {"provider": "p", "model": "big"}, "A-03": {"enabled": False}},
    )
    assert cfg.slot_for("router-routine-classifier", "A-02").model == "big"
    assert cfg.slot_for("router-routine-classifier", "A-05").model == "small"  # no agent setting: the role's
    assert cfg.slot_for("router-vision-ocr") is None  # nothing set: the original LiteLLM route
    assert cfg.agent_enabled("A-02") and not cfg.agent_enabled("A-03") and cfg.agent_enabled("A-04")
    shown = str(public(cfg))
    assert "sk-secret" not in shown and "1234" in shown  # only the last four characters


def test_a_role_with_its_own_provider_is_called_directly_and_a_switched_off_one_is_refused():
    import asyncio

    import httpx
    import pytest

    from apa import llm_config
    from apa.integrations.llm import LLMClient, LLMDisabled

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization"), request.read()))
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "hi"}}]})

    async def run():
        cfg = llm_config.LLMConfig(
            providers={"p": {"base_url": "https://other.example/v1", "api_key": "k-1"}},
            routes={"router-vision-ocr": {"provider": "p", "model": "vl-9"}, "router-deep-research": {"enabled": False}},
        )
        llm = LLMClient(httpx.AsyncClient(base_url="http://litellm/v1", transport=httpx.MockTransport(handler)))

        async def fixed():
            return cfg

        llm.config = fixed  # the settings the dashboard would have saved
        llm._direct = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await llm.complete("router-vision-ocr", "s", "u")
        await llm.complete("router-routine-classifier", "s", "u")
        with pytest.raises(LLMDisabled):
            await llm.complete("router-deep-research", "s", "u")

    asyncio.run(run())
    assert seen[0][0] == "https://other.example/v1/chat/completions" and seen[0][1] == "Bearer k-1" and b'"vl-9"' in seen[0][2]
    assert seen[1][0] == "http://litellm/v1/chat/completions"  # not overridden: through the proxy as before
    assert len(seen) == 2  # the switched-off role made no request


# ---- custom agents and the protected core ------------------------------------------------------------------------------------


def test_the_core_agents_cannot_be_switched_off_but_may_change_model():
    import pytest

    from apa.llm_config import PROTECTED, LLMConfig

    assert PROTECTED == {"A-00", "A-01", "A-10"}  # Master, Operator, Arbiter
    for core in PROTECTED:
        with pytest.raises(ValueError, match="core"):
            LLMConfig(agents={core: {"enabled": False}})
        assert LLMConfig(agents={core: {"enabled": True}}).agent_enabled(core)
    cfg = LLMConfig(providers={"p": {"base_url": "https://x.example/v1"}}, agents={"A-00": {"provider": "p", "model": "router-1"}})
    assert cfg.slot_for("router-routine-classifier", "A-00").model == "router-1"
    assert LLMConfig(agents={"A-02": {"enabled": False}}).agent_enabled("A-02") is False  # the others can be switched off


def test_custom_agents_are_validated_and_routed_to_their_own_model():
    import pytest

    from apa.llm_config import LLMConfig

    prov = {"p": {"base_url": "https://x.example/v1"}}
    cfg = LLMConfig(providers=prov, custom={
        "C-gym": {"name": "  Gym   Coach ", "persona": "Firm but kind.", "hint": "olahraga", "data": ["health"], "provider": "p", "model": "m1"},
        "C-off": {"name": "Idle", "enabled": False},
    })
    assert cfg.custom["C-gym"].name == "Gym Coach"
    assert set(cfg.active_custom()) == {"C-gym"}  # a switched-off one is not offered to the Master
    assert cfg.slot_for("router-routine-classifier", "C-gym").model == "m1"
    assert not cfg.agent_enabled("C-off") and cfg.slot_for("router-routine-classifier", "C-off").enabled is False
    for bad in (
        {"custom": {"gym": {"name": "x"}}},  # id must start with C-
        {"custom": {"C-Gym": {"name": "x"}}},  # lowercase only
        {"custom": {"C-a": {"name": ""}}},  # a name is required
        {"custom": {"C-a": {"name": "x", "data": ["bank"]}}},  # only the data sets that exist
        {"providers": prov, "custom": {"C-a": {"name": "x", "provider": "p"}}},  # provider without a model
        {"custom": {f"C-a{i}": {"name": "x"} for i in range(13)}},  # at most 12
    ):
        with pytest.raises(ValueError):
            LLMConfig(**bad)


def test_the_master_lists_custom_agents_and_may_choose_them():
    from apa.chat.master import MASTER_SYSTEM, parse_plan, system_for
    from apa.llm_config import LLMConfig

    custom = LLMConfig(custom={"C-gym": {"name": "Gym", "hint": "kebugaran: olahraga, langkah"}}).active_custom()
    assert system_for(None) == MASTER_SYSTEM and "C-gym: kebugaran" in system_for(custom)
    raw = '{"intent": "ask", "agents": ["C-gym", "C-ghost", "A-02"], "reply": null, "topic": ""}'
    assert parse_plan(raw, has_draft=False, custom=custom).agents == ["C-gym", "A-02"]  # unknown ids are dropped
    assert parse_plan(raw, has_draft=False).agents == ["A-02"]  # without it, a custom id means nothing


def test_the_default_litellm_example_file_is_valid_and_has_every_role():
    import yaml as _yaml

    from apa.llm_config import ROUTES

    cfg = _yaml.safe_load((Path(__file__).resolve().parents[2] / "infra" / "litellm" / "config.example.yaml").read_text(encoding="utf-8"))
    assert {m["model_name"] for m in cfg["model_list"]} >= set(ROUTES)  # every AI role has a route
    assert cfg["litellm_settings"]["turn_off_message_logging"] is True  # /private stays unlogged


# ---- preset: how much output and context each role or agent gets -------------------------------------------------------------


def test_normal_preset_is_a_no_op_so_a_fresh_install_calls_the_model_exactly_as_before():
    """The default preset must leave every historical call unchanged: same max_tokens, same history length,
    same data block. If this ever drifts, old users will see different answers after an upgrade."""
    from apa.llm_config import PRESETS, LLMConfig, scale_tokens

    assert PRESETS["normal"] == (1.0, 16, 1.0)  # the numbers the code was designed for
    assert scale_tokens(600, 1.0) == 600 and scale_tokens(60, 1.0) == 60 and scale_tokens(6000, 1.0) == 6000
    assert scale_tokens(None, 1.0) is None  # None (let the model decide) is never turned into a number
    cfg = LLMConfig()
    assert cfg.preset_for("router-routine-classifier") == "normal"
    assert cfg.budget("router-routine-classifier", "A-02") == (1.0, 16, 1.0)


def test_the_preset_applies_to_the_role_and_the_agent_wins_when_both_are_set():
    """An owner who picks 'generous' for a role gets it everywhere in that role, except where they picked a
    different preset for one agent. The custom agent's own preset is used for calls under its id."""
    from apa.llm_config import LLMConfig

    cfg = LLMConfig(
        routes={"router-routine-classifier": {"preset": "generous"}},  # no provider: still through LiteLLM, bigger budget
        agents={"A-02": {"preset": "thrifty"}},  # the finance agent trims its own answers
        custom={"C-gym": {"name": "Coach", "preset": "generous"}},
    )
    assert cfg.budget("router-routine-classifier")[1] == 32  # the role on its own: generous history
    assert cfg.budget("router-routine-classifier", "A-02") == (0.5, 6, 0.5)  # the agent wins
    assert cfg.budget("router-routine-classifier", "A-05") == (2.0, 32, 1.5)  # no agent setting: the role's
    assert cfg.budget("router-routine-classifier", "C-gym")[0] == 2.0  # a custom agent uses its own preset


def test_scale_tokens_clamps_to_keep_answers_usable_and_bounded():
    """A multiplied budget can never fall below a usable answer (16 tokens is a sentence) and never exceed
    the ceiling that keeps a slow provider from running away with cost."""
    from apa.llm_config import TOKEN_CEILING, TOKEN_FLOOR, scale_tokens

    assert scale_tokens(60, 0.5) == 30 and scale_tokens(600, 2.0) == 1200  # ordinary scaling
    assert scale_tokens(16, 0.5) == TOKEN_FLOOR  # a tiny classifier answer stays a sentence long
    assert scale_tokens(6000, 2.0) == TOKEN_CEILING  # a huge receipt call cannot double past the cap


def test_the_preset_rescales_max_tokens_at_the_wire_so_every_call_site_benefits_without_edits():
    """The owner's preset must apply to every existing call (debate, chat, vision, routines, ...). Instead
    of editing dozens of sites, scale_tokens is applied inside LLMClient. One test on the hot path proves
    the wiring; the others keep passing because the default is 'normal' = x1."""
    import asyncio

    import httpx

    from apa import llm_config
    from apa.integrations.llm import LLMClient

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen.append(_json.loads(request.read())["max_tokens"])
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"}}]})

    async def run():
        cfg = llm_config.LLMConfig(agents={"A-02": {"preset": "thrifty"}, "A-04": {"preset": "generous"}})
        llm = LLMClient(httpx.AsyncClient(base_url="http://litellm/v1", transport=httpx.MockTransport(handler)))

        async def fixed():
            return cfg

        llm.config = fixed
        await llm.complete("router-routine-classifier", "s", "u", max_tokens=400, agent="A-02")  # thrifty: half
        await llm.complete("router-routine-classifier", "s", "u", max_tokens=400, agent="A-04")  # generous: double
        await llm.complete("router-routine-classifier", "s", "u", max_tokens=400, agent="A-05")  # unset: unchanged

    asyncio.run(run())
    assert seen == [200, 800, 400]


def test_the_preset_survives_a_save_and_a_switched_off_slot_can_still_carry_its_size():
    """A slot that only sets a preset (no provider, no model) must be kept and reloaded, otherwise the
    owner's choice is lost on save. The 'normal' default is dropped by the dashboard before saving."""
    from apa.llm_config import LLMConfig

    cfg = LLMConfig(agents={"A-02": {"preset": "thrifty"}})  # no provider, not switched off: still valid
    round_trip = LLMConfig(**cfg.model_dump())
    assert round_trip.agents["A-02"].preset == "thrifty"
    assert round_trip.budget("router-routine-classifier", "A-02")[0] == 0.5


def test_the_dashboard_sees_the_preset_choices_so_the_page_has_no_private_copy_of_the_numbers():
    """GET /llm returns the list of presets with their effects, so the UI shows one definition only."""
    from apa.gateway.routers.llm import _view
    from apa.llm_config import LLMConfig

    view = _view(LLMConfig())
    keys = {p["key"] for p in view["presets"]}
    assert keys == {"thrifty", "normal", "generous"}
    normal = next(p for p in view["presets"] if p["key"] == "normal")
    assert normal["tokens_pct"] == 100 and normal["turns"] == 16 and normal["context_pct"] == 100
