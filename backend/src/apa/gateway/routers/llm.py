"""AI settings for the web dashboard (same key as /ingest): add providers by URL and key, list the models a provider offers,
choose the model for each AI role and each debating agent, switch agents off, and choose the speech engine and model.
Saved keys never leave the server; the dashboard only sees the last four characters."""

import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apa import llm_config as C
from apa.agents.personas import AGENTS
from apa.core.settings import get_settings
from apa.gateway.routers.ingest import reason, require_ingest_key

router = APIRouter(prefix="/llm", tags=["llm"], dependencies=[Depends(require_ingest_key)])

TIMEOUT_S = 20
_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal"}  # cloud metadata endpoints are never a model provider


def _view(cfg: C.LLMConfig) -> dict:
    return {
        "config": C.public(cfg),
        "routes": [{"key": k, "label": v} for k, v in C.ROUTES.items()],
        "agents": [
            {"id": a.id, "name": a.name, "route": a.route, "debater": a.in_debate, "protected": a.protected,
             "uses_ai": a.kind != "engine"}
            for a in AGENTS.values() if a.kind != "engine" or a.protected
        ],
        "gemini_key_set": bool(get_settings().gemini_api_key),
        # the size choices the dashboard offers, with what each one does, so the page needs no copy of the numbers
        "presets": [
            {"key": k, "label": C.PRESET_LABELS[k], "tokens_pct": round(PRESET[0] * 100), "turns": PRESET[1],
             "context_pct": round(PRESET[2] * 100)}
            for k, PRESET in C.PRESETS.items()
        ],
    }


@router.get("")
async def get_llm() -> dict:
    return _view(await C.load())


class ProviderIn(BaseModel):
    base_url: str
    api_key: str | None = None  # None keeps the saved key, "" clears it


class LLMIn(BaseModel):
    providers: dict[str, ProviderIn] = Field(default_factory=dict)
    routes: dict[str, C.Slot] = Field(default_factory=dict)
    agents: dict[str, C.Slot] = Field(default_factory=dict)
    voice: C.VoiceCfg = Field(default_factory=C.VoiceCfg)
    custom: dict[str, C.CustomAgent] = Field(default_factory=dict)


@router.put("")
async def put_llm(body: LLMIn) -> dict:
    old = await C.load()
    provs = {}
    for name, p in body.providers.items():
        key = p.api_key if p.api_key is not None else (old.providers[name].api_key if name in old.providers else "")
        provs[name] = {"base_url": p.base_url, "api_key": key}
    try:
        new = C.LLMConfig(providers=provs, routes=body.routes, agents=body.agents, voice=body.voice, custom=body.custom)
    except ValueError as e:
        raise HTTPException(422, reason(e)) from None
    for p in new.providers.values():
        if urlparse(p.base_url).hostname in _BLOCKED_HOSTS:
            raise HTTPException(422, "that address cannot be a provider")
    await C.save(new)
    return _view(new)


class ModelsIn(BaseModel):
    provider: str | None = None  # a saved provider (its saved key is used) ...
    base_url: str | None = None  # ... or an address and key typed in the form, not saved yet
    api_key: str = ""


def _target(body: ModelsIn, cfg: C.LLMConfig) -> C.Provider:
    if body.provider:
        if body.provider not in cfg.providers:
            raise HTTPException(404, "unknown provider")
        saved = cfg.providers[body.provider]
        return C.Provider(base_url=body.base_url or saved.base_url, api_key=body.api_key or saved.api_key)
    if not body.base_url:
        raise HTTPException(422, "give a provider or an address")
    try:
        prov = C.Provider(base_url=body.base_url, api_key=body.api_key)
    except ValueError as e:
        raise HTTPException(422, reason(e)) from None
    if urlparse(prov.base_url).hostname in _BLOCKED_HOSTS:
        raise HTTPException(422, "that address cannot be a provider")
    return prov


def _ids(data: dict) -> list[str]:
    """Model ids from an OpenAI-style listing ({"data": [{"id": ...}]}); other common shapes are tolerated."""
    rows = data.get("data") or data.get("models") or []
    out = [r.get("id") or r.get("name") for r in rows if isinstance(r, dict)] + [r for r in rows if isinstance(r, str)]
    return sorted({str(x) for x in out if x})


@router.post("/models")
async def list_models(body: ModelsIn) -> dict:
    """The models a provider offers: GET {base_url}/models with its key."""
    prov = _target(body, await C.load())
    headers = {"Authorization": f"Bearer {prov.api_key}"} if prov.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
            r = await http.get(f"{prov.base_url}/models", headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"cannot reach the provider ({type(e).__name__})") from None
    if r.status_code in (401, 403):
        raise HTTPException(502, "the provider refused the key")
    if r.status_code != 200:
        raise HTTPException(502, f"the provider answered {r.status_code}")
    try:
        models = _ids(r.json())
    except ValueError:
        raise HTTPException(502, "the provider's list is not JSON") from None
    return {"models": models}


class TestIn(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=120)


@router.post("/test")
async def test_model(body: TestIn) -> dict:
    """One tiny question to a provider's model, to see that the key and the model name work."""
    cfg = await C.load()
    if body.provider not in cfg.providers:
        raise HTTPException(404, "unknown provider")
    prov = cfg.providers[body.provider]
    headers = {"Authorization": f"Bearer {prov.api_key}"} if prov.api_key else {}
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
            r = await http.post(f"{prov.base_url}/chat/completions", headers=headers, json={
                "model": body.model, "max_tokens": 5, "messages": [{"role": "user", "content": "Reply with OK."}]})
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"cannot reach the provider ({type(e).__name__})"}
    ms = int((time.perf_counter() - t0) * 1000)
    return {"ok": r.status_code == 200, "ms": ms, "detail": "" if r.status_code == 200 else f"HTTP {r.status_code}"}


@router.get("/voice-models")
async def voice_models() -> dict:
    """Gemini models that can speak (their names contain "tts"), using the server's Gemini key."""
    key = get_settings().gemini_api_key
    if not key:
        return {"models": [], "detail": "no Gemini key on the server"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
            r = await http.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                               headers={"x-goog-api-key": key})
        names = [m["name"].removeprefix("models/") for m in r.json().get("models", []) if "tts" in m.get("name", "")]
    except (httpx.HTTPError, ValueError, KeyError):
        return {"models": [], "detail": "cannot list Gemini models"}
    return {"models": sorted(names)}
