"use client";

import { useEffect, useState } from "react";
import { api, post, put, useApi } from "@/lib/api";

type Slot = { enabled: boolean; provider: string; model: string };
type Provider = { base_url: string; api_key_hint?: string };
type Voice = { engine: string; gemini_model: string; custom_provider: string; custom_model: string; custom_voices: string[] };
type Custom = { name: string; persona: string; hint: string; data: string[]; enabled: boolean; provider: string; model: string };
type Config = { providers: Record<string, Provider>; routes: Record<string, Slot>; agents: Record<string, Slot>; voice: Voice; custom: Record<string, Custom> };
type View = {
  config: Config;
  routes: { key: string; label: string }[];
  agents: { id: string; name: string; route: string; debater: boolean; protected: boolean; uses_ai: boolean }[];
  gemini_key_set: boolean;
};

const OFF: Slot = { enabled: true, provider: "", model: "" };

export default function ModelsPage() {
  const { data: view, reload } = useApi<View>("/llm");
  const [cfg, setCfg] = useState<Config | null>(null);
  const [keys, setKeys] = useState<Record<string, string>>({}); // keys typed this session; saved keys are never sent back
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [geminiModels, setGeminiModels] = useState<string[]>([]);
  const [voices, setVoices] = useState("");
  const [msg, setMsg] = useState("");
  const [notes, setNotes] = useState<Record<string, string>>({});

  useEffect(() => {
    if (view) {
      setCfg(view.config);
      setVoices(view.config.voice.custom_voices.join(", "));
    }
  }, [view]);

  if (!cfg || !view) return <><h1>AI Models</h1><p className="muted">Loading...</p></>;
  const names = Object.keys(cfg.providers);
  const slot = (kind: "routes" | "agents", k: string): Slot => cfg[kind][k] ?? OFF;
  const setSlot = (kind: "routes" | "agents", k: string, s: Partial<Slot>) =>
    setCfg({ ...cfg, [kind]: { ...cfg[kind], [k]: { ...slot(kind, k), ...s } } });
  const setProv = (name: string, p: Partial<Provider>) => setCfg({ ...cfg, providers: { ...cfg.providers, [name]: { ...cfg.providers[name], ...p } } });

  function addProvider() {
    let i = names.length + 1;
    while (`provider${i}` in cfg!.providers) i++;
    setCfg({ ...cfg!, providers: { ...cfg!.providers, [`provider${i}`]: { base_url: "https://" } } });
  }
  function removeProvider(name: string) {
    const providers = { ...cfg!.providers };
    delete providers[name];
    const drop = (m: Record<string, Slot>) => Object.fromEntries(Object.entries(m).map(([k, s]) => [k, s.provider === name ? { ...s, provider: "", model: "" } : s]));
    setCfg({ ...cfg!, providers, routes: drop(cfg!.routes), agents: drop(cfg!.agents), voice: cfg!.voice.custom_provider === name ? { ...cfg!.voice, custom_provider: "", custom_model: "" } : cfg!.voice });
  }
  function rename(old: string, next: string) {
    const clean = next.toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 30);
    if (!clean || clean === old || clean in cfg!.providers) return;
    const providers = Object.fromEntries(Object.entries(cfg!.providers).map(([k, v]) => [k === old ? clean : k, v]));
    const re = (m: Record<string, Slot>) => Object.fromEntries(Object.entries(m).map(([k, s]) => [k, s.provider === old ? { ...s, provider: clean } : s]));
    setKeys(Object.fromEntries(Object.entries(keys).map(([k, v]) => [k === old ? clean : k, v])));
    setCfg({ ...cfg!, providers, routes: re(cfg!.routes), agents: re(cfg!.agents), voice: cfg!.voice.custom_provider === old ? { ...cfg!.voice, custom_provider: clean } : cfg!.voice });
  }

  async function fetchModels(name: string) {
    setNotes((n) => ({ ...n, [name]: "Fetching model list..." }));
    try {
      const p = cfg!.providers[name];
      const r = await post<{ models: string[] }>("/llm/models", { base_url: p.base_url, api_key: keys[name] ?? "", provider: view!.config.providers[name] ? name : undefined });
      setModels((m) => ({ ...m, [name]: r.models }));
      setNotes((n) => ({ ...n, [name]: `${r.models.length} models available.` }));
    } catch (e) {
      setNotes((n) => ({ ...n, [name]: `Failed: ${(e as Error).message}` }));
    }
  }
  async function test(id: string, s: Slot) {
    setNotes((n) => ({ ...n, [id]: "Testing..." }));
    try {
      const r = await post<{ ok: boolean; ms?: number; detail: string }>("/llm/test", { provider: s.provider, model: s.model });
      setNotes((n) => ({ ...n, [id]: r.ok ? `Works (${r.ms} ms)` : `Failed: ${r.detail}` }));
    } catch (e) {
      setNotes((n) => ({ ...n, [id]: `Failed: ${(e as Error).message} (save first)` }));
    }
  }
  async function geminiList() {
    const r = await api<{ models: string[]; detail?: string }>("/llm/voice-models").catch(() => null);
    setGeminiModels(r?.models ?? []);
  }

  function addAgent() {
    let i = Object.keys(cfg!.custom).length + 1;
    while (`C-agen${i}` in cfg!.custom) i++;
    setCfg({ ...cfg!, custom: { ...cfg!.custom, [`C-agen${i}`]: { name: `Agent ${i}`, persona: "", hint: "", data: ["finance"], enabled: true, provider: "", model: "" } } });
  }
  function setAgent(id: string, c: Partial<Custom>) {
    setCfg({ ...cfg!, custom: { ...cfg!.custom, [id]: { ...cfg!.custom[id], ...c } } });
  }
  function removeAgent(id: string) {
    const custom = { ...cfg!.custom };
    delete custom[id];
    setCfg({ ...cfg!, custom });
  }

  async function save() {
    setMsg("Saving...");
    const keep = (m: Record<string, Slot>) => Object.fromEntries(Object.entries(m).filter(([, s]) => !s.enabled || s.provider));
    const providers = Object.fromEntries(Object.entries(cfg!.providers).map(([n, p]) => [n, { base_url: p.base_url, api_key: keys[n] }]));
    try {
      await put("/llm", { providers, routes: keep(cfg!.routes), agents: keep(cfg!.agents), custom: cfg!.custom, voice: { ...cfg!.voice, custom_voices: voices.split(",").map((v) => v.trim()).filter(Boolean) } });
      setKeys({});
      setMsg("Saved. Applies within a few seconds, no restart needed.");
      reload();
    } catch (e) {
      setMsg(`Failed to save: ${(e as Error).message}`);
    }
  }

  const row = (kind: "routes" | "agents", id: string, title: string, hint: string, core = false, ai = true) => {
    const s = slot(kind, id);
    const list = models[s.provider] ?? [];
    return (
      <tr key={`${kind}${id}`}>
        <td><b>{title}</b><div className="muted">{hint}</div></td>
        <td>{core ? <span title="System core: cannot be disabled or removed">🔒 core</span> : <input type="checkbox" checked={s.enabled} onChange={(e) => setSlot(kind, id, { enabled: e.target.checked })} />}</td>
        {!ai ? <td colSpan={3} className="muted">run by code, no AI</td> : <>
        <td>
          <select value={s.provider} onChange={(e) => setSlot(kind, id, { provider: e.target.value, model: e.target.value ? s.model : "" })}>
            <option value="">Default (LiteLLM)</option>
            {names.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </td>
        <td>
          <input list={`m-${kind}-${id}`} value={s.model} disabled={!s.provider} placeholder={s.provider ? "choose or type a model" : "-"} onChange={(e) => setSlot(kind, id, { model: e.target.value })} />
          <datalist id={`m-${kind}-${id}`}>{list.map((m) => <option key={m} value={m} />)}</datalist>
        </td>
        <td>
          <button disabled={!s.provider || !s.model || !view.config.providers[s.provider]} onClick={() => test(`${kind}${id}`, s)}>Test</button>
          <div className="muted">{notes[`${kind}${id}`]}</div>
        </td></>}
      </tr>
    );
  };

  return (
    <>
      <h1>AI Models</h1>
      <p className="muted">
        Add as many AI providers as you like (an OpenAI-compatible URL plus an API key), fetch their model lists, then choose a model for
        each role and each agent, or disable agents. Anything left on &quot;Default&quot; still goes through LiteLLM as before. Keys are stored on the server and never sent back to this page.
      </p>

      <h2>AI providers</h2>
      {names.map((n) => (
        <div className="card" key={n} style={{ marginBottom: 8 }}>
          <div className="row">
            <input defaultValue={n} onBlur={(e) => rename(n, e.target.value)} aria-label="name" style={{ width: 130 }} />
            <input value={cfg.providers[n].base_url} onChange={(e) => setProv(n, { base_url: e.target.value })} placeholder="https://api.example.com/v1" style={{ flex: 1, minWidth: 220 }} />
            <input type="password" value={keys[n] ?? ""} onChange={(e) => setKeys({ ...keys, [n]: e.target.value })} placeholder={cfg.providers[n].api_key_hint ? `key saved (${cfg.providers[n].api_key_hint})` : "API key"} style={{ width: 200 }} />
            <button onClick={() => fetchModels(n)}>Fetch model list</button>
            <button className="link" onClick={() => removeProvider(n)}>Delete</button>
          </div>
          {notes[n] && <div className="muted">{notes[n]}</div>}
        </div>
      ))}
      <button onClick={addProvider}>+ Add provider</button>

      <h2>AI roles</h2>
      <div className="tablewrap">
        <table>
          <thead><tr><th>Role</th><th>Enabled</th><th>Provider</th><th>Model</th><th /></tr></thead>
          <tbody>{view.routes.map((r) => row("routes", r.key, r.key.replace("router-", ""), r.label))}</tbody>
        </table>
      </div>

      <h2>Agents</h2>
      <p className="muted">Debater agents vote on transaction decisions; disabled ones do not take part (the quorum adjusts). An agent's model choice overrides the role's choice.</p>
      <div className="tablewrap">
        <table>
          <thead><tr><th>Agent</th><th>Enabled</th><th>Provider</th><th>Model</th><th /></tr></thead>
          <tbody>{view.agents.map((a) => row("agents", a.id, `${a.id} ${a.name}`, a.protected ? "system core" : a.debater ? "takes part in debates" : "answers in chat", a.protected, a.uses_ai))}</tbody>
        </table>
      </div>

      <h2>Your own agents</h2>
      <p className="muted">
        Create your own agent: give it a name, describe how it thinks, say when the Master should route questions to it, and choose which data it may read.
        Agents only read and answer in the Telegram chat; they cannot change financial records. Maximum 12.
      </p>
      {Object.entries(cfg.custom).map(([id, c]) => (
        <div className="card" key={id} style={{ marginBottom: 8 }}>
          <div className="row">
            <b>{id}</b>
            <input value={c.name} onChange={(e) => setAgent(id, { name: e.target.value })} placeholder="agent name" />
            <label><input type="checkbox" checked={c.enabled} onChange={(e) => setAgent(id, { enabled: e.target.checked })} /> enabled</label>
            <button className="link" onClick={() => removeAgent(id)}>Delete</button>
          </div>
          <div className="row"><textarea rows={3} style={{ width: "100%" }} value={c.persona} onChange={(e) => setAgent(id, { persona: e.target.value })} placeholder="How it thinks and answers, e.g. A firm but friendly fitness coach; tie answers to sleep data." /></div>
          <div className="row"><input style={{ width: "100%" }} value={c.hint} onChange={(e) => setAgent(id, { hint: e.target.value })} placeholder="When the Master routes to this agent, e.g. fitness: exercise, steps, sleep" /></div>
          <div className="row">
            Data it may read:
            {[["finance", "finance"], ["health", "health"]].map(([k, l]) => (
              <label key={k}><input type="checkbox" checked={c.data.includes(k)} onChange={(e) => setAgent(id, { data: e.target.checked ? [...c.data, k] : c.data.filter((x) => x !== k) })} /> {l}</label>
            ))}
            <select value={c.provider} onChange={(e) => setAgent(id, { provider: e.target.value, model: e.target.value ? c.model : "" })}>
              <option value="">Default model (LiteLLM)</option>
              {names.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
            <input list={`m-c-${id}`} value={c.model} disabled={!c.provider} placeholder={c.provider ? "model" : "-"} onChange={(e) => setAgent(id, { model: e.target.value })} />
            <datalist id={`m-c-${id}`}>{(models[c.provider] ?? []).map((m) => <option key={m} value={m} />)}</datalist>
          </div>
        </div>
      ))}
      <button onClick={addAgent} disabled={Object.keys(cfg.custom).length >= 12}>+ Add agent</button>

      <h2>Voice</h2>
      <div className="card">
        <div className="row">
          <label>Engine{" "}
            <select value={cfg.voice.engine} onChange={(e) => setCfg({ ...cfg, voice: { ...cfg.voice, engine: e.target.value } })}>
              <option value="auto">Automatic (Gemini, then custom provider, then Microsoft)</option>
              <option value="gemini">Gemini</option>
              <option value="custom">Custom provider (OpenAI speech)</option>
              <option value="edge">Microsoft</option>
            </select>
          </label>
        </div>
        <div className="row">
          <label>Gemini voice model{" "}
            <input list="gem-tts" value={cfg.voice.gemini_model} placeholder={view.gemini_key_set ? "empty = server default" : "no Gemini key on the server"} onChange={(e) => setCfg({ ...cfg, voice: { ...cfg.voice, gemini_model: e.target.value } })} style={{ width: 280 }} />
            <datalist id="gem-tts">{geminiModels.map((m) => <option key={m} value={m} />)}</datalist>
          </label>
          <button onClick={geminiList} disabled={!view.gemini_key_set}>Fetch Gemini voice models</button>
        </div>
        <div className="row">
          <label>Custom voice provider{" "}
            <select value={cfg.voice.custom_provider} onChange={(e) => setCfg({ ...cfg, voice: { ...cfg.voice, custom_provider: e.target.value, custom_model: e.target.value ? cfg.voice.custom_model : "" } })}>
              <option value="">(none)</option>
              {names.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <label>Model{" "}
            <input list="cust-tts" value={cfg.voice.custom_model} disabled={!cfg.voice.custom_provider} placeholder="e.g. tts-1" onChange={(e) => setCfg({ ...cfg, voice: { ...cfg.voice, custom_model: e.target.value } })} />
            <datalist id="cust-tts">{(models[cfg.voice.custom_provider] ?? []).map((m) => <option key={m} value={m} />)}</datalist>
          </label>
        </div>
        <div className="row">
          <label>Voice names for that model (comma-separated){" "}
            <input value={voices} disabled={!cfg.voice.custom_provider} placeholder="e.g. alloy, nova, shimmer" onChange={(e) => setVoices(e.target.value)} style={{ width: 280 }} />
          </label>
        </div>
        <p className="muted">Gemini (30) and Microsoft (per language) voices appear automatically in Settings; custom provider voices appear in that list once saved.</p>
      </div>

      <div className="row"><button onClick={save}>Save</button><span className="muted">{msg}</span></div>
      <p className="muted">Note: the Mental health and Research roles use the provider you choose; the contents of /private sessions will reach that provider.</p>
    </>
  );
}
