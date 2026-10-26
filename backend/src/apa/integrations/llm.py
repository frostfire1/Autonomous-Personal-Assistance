"""Thin client for the LiteLLM proxy (OpenAI-compatible). All inference goes through it."""

import asyncio
import base64
import json
import re
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from apa import llm_config
from apa.core.settings import get_settings

# The proxy gives each attempt 22 s (vision 60 s) and does not retry the same model, so a stalled call
# costs one attempt and then a fallback answers. The client must wait for that, hence the margin:
# 60 s covers one stall plus one fallback (22 + a slow answer), 150 s covers a stalled vision attempt
# plus a vision fallback. Tests pin these relationships against infra/litellm/config.yaml.
DEFAULT_TIMEOUT_S = 60.0
VISION_TIMEOUT_S = 150.0

_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*(.*?)\s*```\s*$", re.DOTALL)


class LLMDisabled(Exception):
    """The owner switched this role or agent off in the dashboard (settings, AI models)."""


@dataclass(frozen=True)
class LLMResult:
    content: str
    model: str  # model actually served (reflects any fallback)
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None

    def json(self) -> dict[str, Any]:
        """The reply parsed as JSON. Models often wrap a valid object in a ```json fence even in JSON
        mode (7 of 10 debater failures in a live run were exactly that), so the fence is stripped."""
        m = _FENCE.match(self.content)
        return json.loads(m.group(1) if m else self.content)


class LLMClient:
    """One instance per process. `max_concurrency` caps requests in flight across everything that
    shares it (every concurrent debate and extraction), because the provider throttles bursts well
    below its stated per-minute limit: 2 of 5 parallel calls were rejected in a quiet minute.
    A cap per debate would not help when several transactions arrive together."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        max_concurrency: int | None = None,
        max_rpm: int | None = None,
        rate_window_s: float = 60.0,
    ):
        s = get_settings()
        self._use_config = client is None  # tests hand in a fake transport and must not touch the database
        self._direct: httpx.AsyncClient | None = None
        self._client = client or httpx.AsyncClient(
            base_url=s.litellm_base_url,
            headers={"Authorization": f"Bearer {s.litellm_master_key}"},
            timeout=DEFAULT_TIMEOUT_S,
        )
        cap = s.llm_max_concurrency if max_concurrency is None else max_concurrency
        self._slots = asyncio.Semaphore(max(1, cap))  # 0 or negative is a misconfiguration: serialise
        # Sliding-window requests-per-minute limiter: 0 (or less) disables it.
        self._rpm = s.llm_max_rpm if max_rpm is None else max_rpm
        self._window_s = rate_window_s
        self._sent: deque[float] = deque()
        self._rate_lock = asyncio.Lock()

    async def config(self) -> "llm_config.LLMConfig":
        return await llm_config.current() if self._use_config else llm_config.LLMConfig()

    async def _post(self, body: dict[str, Any], route: str, agent: str | None, timeout: float | None, vision: bool) -> httpx.Response:
        """Through the LiteLLM proxy, unless the owner gave this role or agent its own provider and model: then straight
        to that provider (an OpenAI-compatible /chat/completions) with its key. A switched-off one raises LLMDisabled."""
        slot = (await self.config()).slot_for(route, agent)
        if slot is not None and not slot.enabled:
            raise LLMDisabled(agent or route)
        if slot is not None and slot.provider:
            prov = (await self.config()).providers[slot.provider]
            if self._direct is None:
                self._direct = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
            headers = {"Authorization": f"Bearer {prov.api_key}"} if prov.api_key else {}
            return await self._direct.post(
                f"{prov.base_url}/chat/completions", json={**body, "model": slot.model}, headers=headers,
                timeout=timeout or (VISION_TIMEOUT_S if vision else DEFAULT_TIMEOUT_S),
            )
        return await self._client.post(
            "/chat/completions", json=body, timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
        )

    async def _wait_for_rate_slot(self) -> None:
        """Block until sending now keeps us within `rpm` requests per window. Callers queue in order
        of arrival and never hold a concurrency slot while waiting here."""
        if self._rpm <= 0:
            return
        while True:
            async with self._rate_lock:
                now = time.monotonic()
                while self._sent and now - self._sent[0] >= self._window_s:
                    self._sent.popleft()
                if len(self._sent) < self._rpm:
                    self._sent.append(now)
                    return
                wait = self._sent[0] + self._window_s - now
            await asyncio.sleep(max(wait, 0.01))  # sleep outside the lock so others can queue

    async def complete(
        self,
        route: str,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_tokens: int = 400,
        temperature: float = 0.2,
        images: Sequence[tuple[bytes, str]] | None = None,
        timeout: float | None = None,
        agent: str | None = None,
    ) -> LLMResult:
        """`images` is (bytes, mime) pairs for vision routes, sent as base64 data URLs. `timeout` (seconds)
        overrides the client default for this one call; vision passes VISION_TIMEOUT_S."""
        cfg = await self.config()
        tokens_mult, _, _ = cfg.budget(route, agent)
        effective_max_tokens = llm_config.scale_tokens(max_tokens, tokens_mult)
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}}
                for data, mime in images
            ]
        body: dict[str, Any] = {
            "model": route,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "max_tokens": effective_max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        await self._wait_for_rate_slot()  # first: never hold a concurrency slot while waiting for the rate
        async with self._slots:  # released on success, error and cancellation alike
            r = await self._post(body, route, agent, timeout, bool(images))
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return LLMResult(
            content=data["choices"][0]["message"]["content"],
            model=data.get("model", route),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cached_tokens=details.get("cached_tokens"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._direct is not None:
            await self._direct.aclose()
