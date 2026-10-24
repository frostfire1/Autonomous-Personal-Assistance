"""Probe every allowed model: availability, latency, and whether JSON mode returns usable JSON.
Paced under the 30 requests/minute limit. Never prints the key.

    cd backend && .venv/Scripts/python scripts/probe_models.py

Run it after any key or plan change, then update the "probed" facts in infra/litellm/config.yaml and
the constants in tests/test_litellm_config.py. Takes about 2 minutes and makes up to 36 tiny calls.
Edit ALLOWED below to match the key's allowed-model list."""
import json
import time

import httpx

env = dict(line.strip().split("=", 1) for line in open("../.env", encoding="utf-8") if "=" in line and not line.startswith("#"))
base, key = env["OSIRIS_API_BASE"], env["OSIRIS_API_KEY"]
H = {"Authorization": f"Bearer {key}"}
ALLOWED = """deepseek-v4-flash deepseek-v4-pro glm-4.6 glm-4.6v glm-4.7 glm-5.1 glm-5.2 glm-5v-turbo kimi-k2.5
kimi-k2.7 minimax-m3 osiris-claude-haiku-4.5 qwen3.7-max qwen3.7-plus osiris-claude-sonnet-4.5 glm-5.3 kimi-k3
qwen3.8-max""".split()
GAP = 3.2  # seconds between calls: about 18 per minute


def call(model, content, **extra):
    t = time.perf_counter()
    try:
        r = httpx.post(f"{base}/chat/completions", headers=H, timeout=90, json={
            "model": model, "messages": [{"role": "user", "content": content}], **extra})
    except Exception as e:
        return None, int((time.perf_counter() - t) * 1000), type(e).__name__
    ms = int((time.perf_counter() - t) * 1000)
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code == 200:
        msg = body["choices"][0]["message"]
        return msg, ms, None
    return None, ms, f"{r.status_code} {(body.get('error') or {}).get('type', '')}"


print(f"{'model':26} {'ping':>22} {'json mode':>34}")
for m in ALLOWED:
    msg, ms, err = call(m, "ping", max_tokens=5)
    ping = f"{ms}ms ok" if msg else f"{err}"
    time.sleep(GAP)
    js = "-"
    if msg:
        msg2, ms2, err2 = call(
            m, 'Reply with JSON only: {"ok": true, "n": 3}', max_tokens=200,
            response_format={"type": "json_object"}, temperature=0)
        if msg2:
            content = (msg2.get("content") or "").strip()
            try:
                ok = json.loads(content).get("n") == 3
                js = f"{ms2}ms valid" if ok else f"{ms2}ms json-wrong"
            except Exception:
                js = f"{ms2}ms NOT-JSON" + (" (empty, has reasoning)" if not content and msg2.get("reasoning_content") else "")
        else:
            js = err2
        time.sleep(GAP)
    print(f"{m:26} {ping:>22} {js:>34}", flush=True)
