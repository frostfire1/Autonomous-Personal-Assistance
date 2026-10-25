"use client";

import { useState } from "react";
import { del, post, put, useApi } from "@/lib/api";

type Tool = { name: string; description: string; args: string[]; read_only: boolean };
type Server = { id: string; name: string; url: string; header: string; has_token: boolean; has_login: boolean; role: string; topics: string[]; enabled: boolean; auto: string[]; off: string[]; tools: Tool[]; refreshed_at: number };

export default function SkillsPage() {
  const { data, reload } = useApi<{ servers: Server[]; mcp_server_on: boolean }>("/skills");
  const [f, setF] = useState({ name: "", url: "", token: "", header: "Authorization" });
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [login, setLogin] = useState<{ pending: string; auth_url: string; redirect: string } | null>(null);
  const [pasted, setPasted] = useState("");
  const [spec, setSpec] = useState<Record<string, { role: string; topics: string }>>({});
  const NUTRITION = {
    name: "Nutrition", url: "https://nutrition-mcp.com/mcp",
    role: "You are the owner's nutrition coach. When they mention a meal, a drink, water, a weigh-in or a food question, use the nutrition skills: look foods up (search_foods, get_food_macros), then log them. Ask one short question if the portion is unclear. Be encouraging and never shame food choices. Meal photos arrive as a description: estimate macros from it and say it is an estimate.",
    topics: "makan, minum, sarapan, makan siang, makan malam, camilan, kalori, protein, karbo, lemak, gula, kafein, air putih, berat badan, diet, meal, breakfast, lunch, dinner, snack, calories, macros, water, weight",
  };

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
        <div className="row">
          <button disabled={busy || !f.name.trim() || !f.url.trim()} onClick={() => run(async () => {
            const r = await post<{ pending?: string; auth_url?: string; redirect?: string }>("/skills/login/start", { name: f.name, url: f.url });
            if (r.pending) setLogin({ pending: r.pending, auth_url: r.auth_url!, redirect: r.redirect! });
          }, "Open the login page below.")}>Connect with login</button>
          <button onClick={() => { setF({ ...f, name: NUTRITION.name, url: NUTRITION.url }); setMsg("Nutrition tracker filled in: it asks for a login (tap Connect with login)."); }}>Example: nutrition tracker</button>
        </div>
        {login && (
          <div className="card" style={{ marginTop: 8 }}>
            <ol>
              <li>Open <a href={login.auth_url} target="_blank" rel="noreferrer">the login page</a> and sign in.</li>
              <li>The browser then shows an error page. That is expected: copy the <b>whole address</b> from the address bar (it starts with <code>{login.redirect}</code>).</li>
              <li>Paste it here:</li>
            </ol>
            <input style={{ width: "100%" }} placeholder={`${login.redirect}?code=...`} value={pasted} onChange={(e) => setPasted(e.target.value)} />
            <button disabled={busy || !pasted.includes("code=")} onClick={() => run(async () => { await post("/skills/login/finish", { pending: login.pending, redirected_url: pasted }); setLogin(null); setPasted(""); }, "Signed in and connected.")}>Finish</button>
          </div>
        )}
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
            <span className="muted">{s.url}{s.has_token ? " · token saved" : ""}{s.has_login ? " · signed in" : ""}</span>
            <button onClick={() => run(() => put(`/skills/${s.id}`, { enabled: !s.enabled }), s.enabled ? "Switched off." : "Switched on.")}>{s.enabled ? "Switch off" : "Switch on"}</button>
            <button onClick={() => run(() => post(`/skills/${s.id}/refresh`, {}), "Tools refreshed.")}>Refresh tools</button>
            <button className="link" onClick={() => { if (confirm(`Remove ${s.name}?`)) run(() => del(`/skills/${s.id}`), "Removed."); }}>Remove</button>
          </div>
          <details>
            <summary className="muted">Specialist: let this skill take over the conversation for its topics</summary>
            <p className="muted">When a message contains one of the topic words, the assistant switches to this role and uses only this server&apos;s tools; follow-ups stay with it for 20 minutes.</p>
            <textarea rows={3} style={{ width: "100%" }} placeholder="Role: how the assistant behaves while it is in charge" value={spec[s.id]?.role ?? s.role} onChange={(e) => setSpec({ ...spec, [s.id]: { role: e.target.value, topics: spec[s.id]?.topics ?? s.topics.join(", ") } })} />
            <input style={{ width: "100%" }} placeholder="topic words, comma separated" value={spec[s.id]?.topics ?? s.topics.join(", ")} onChange={(e) => setSpec({ ...spec, [s.id]: { role: spec[s.id]?.role ?? s.role, topics: e.target.value } })} />
            <div className="row">
              <button onClick={() => run(() => put(`/skills/${s.id}`, { role: spec[s.id]?.role ?? s.role, topics: (spec[s.id]?.topics ?? s.topics.join(", ")).split(",").map((x) => x.trim()).filter(Boolean) }), "Saved.")}>Save specialist</button>
              {s.url.includes("nutrition-mcp.com") && <button onClick={() => setSpec({ ...spec, [s.id]: { role: NUTRITION.role, topics: NUTRITION.topics } })}>Fill in the nutrition coach</button>}
            </div>
          </details>
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
