"use client";

import { useState } from "react";
import { del, post, put, useApi } from "@/lib/api";

type Tool = { name: string; description: string; args: string[]; read_only: boolean };
type Server = { id: string; name: string; url: string; header: string; has_token: boolean; enabled: boolean; auto: string[]; off: string[]; tools: Tool[]; refreshed_at: number };

export default function SkillsPage() {
  const { data, reload } = useApi<{ servers: Server[]; mcp_server_on: boolean }>("/skills");
  const [f, setF] = useState({ name: "", url: "", token: "", header: "Authorization" });
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try { await fn(); setMsg(ok); reload(); } catch (e) { setMsg(`Not done: ${(e as Error).message}`); }
    setBusy(false);
  }

  return (
    <>
      <h1>Skills</h1>
      <p className="muted">
        Connect an MCP server (the open standard for giving an AI tools) and the assistant can use what it offers, for example your notes, a calendar,
        a search service. A tool that only reads runs at once; any tool that could change something asks you first in Telegram, unless you allow it
        here. What a skill returns is treated as untrusted. Connect only servers you trust: the assistant sends them what it needs to use them.
      </p>
      <div className="card">
        <b>Connect a server</b>
        <div className="row">
          <input placeholder="name (Notes)" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          <input style={{ flex: 1 }} placeholder="address (https://host/mcp)" value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} />
        </div>
        <div className="row">
          <input placeholder="header (Authorization)" value={f.header} onChange={(e) => setF({ ...f, header: e.target.value })} />
          <input style={{ flex: 1 }} type="password" placeholder="token (optional, stored encrypted, never shown again)" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value })} />
          <button disabled={busy || !f.name.trim() || !f.url.trim()} onClick={() => run(async () => { await post("/skills", f); setF({ name: "", url: "", token: "", header: "Authorization" }); }, "Connected.")}>
            {busy ? "Connecting..." : "Connect"}
          </button>
        </div>
        <span className="muted">{msg}</span>
      </div>
      <p className="muted">
        {data?.mcp_server_on
          ? "The other direction is on: your own data is offered read-only to other AI apps at /mcp (same key as this dashboard)."
          : "The other direction (offering your own data to other AI apps at /mcp) is off: set MCP_ENABLED=true in .env to turn it on."}
      </p>
      {!data ? <p className="muted">Loading...</p> : data.servers.length === 0 ? <p className="muted">No skills connected yet.</p> : data.servers.map((s) => (
        <div className="card" key={s.id} style={{ marginBottom: 8 }}>
          <div className="row">
            <b>{s.name}</b>
            <span className="muted">{s.url}{s.has_token ? " · token saved" : ""}</span>
            <button onClick={() => run(() => put(`/skills/${s.id}`, { enabled: !s.enabled }), s.enabled ? "Switched off." : "Switched on.")}>{s.enabled ? "Switch off" : "Switch on"}</button>
            <button onClick={() => run(() => post(`/skills/${s.id}/refresh`, {}), "Tools refreshed.")}>Refresh tools</button>
            <button className="link" onClick={() => { if (confirm(`Remove ${s.name}?`)) run(() => del(`/skills/${s.id}`), "Removed."); }}>Remove</button>
          </div>
          {s.tools.length === 0 ? <p className="muted">No tools listed.</p> : (
            <table>
              <thead><tr><th>Tool</th><th>What it does</th><th>Runs</th><th>Allow without asking</th><th>Off</th></tr></thead>
              <tbody>
                {s.tools.map((t) => (
                  <tr key={t.name}>
                    <td><code>{t.name}({t.args.join(", ")})</code></td>
                    <td>{t.description}</td>
                    <td>{t.read_only ? "at once (read-only)" : s.auto.includes(t.name) ? "at once (you allowed)" : "after you confirm"}</td>
                    <td><input type="checkbox" checked={s.auto.includes(t.name)} disabled={t.read_only || s.off.includes(t.name)} onChange={(e) => run(() => put(`/skills/${s.id}`, { tool: t.name, auto: e.target.checked }), "Saved.")} /></td>
                    <td><input type="checkbox" checked={s.off.includes(t.name)} onChange={(e) => run(() => put(`/skills/${s.id}`, { tool: t.name, off: e.target.checked }), "Saved.")} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}
    </>
  );
}
