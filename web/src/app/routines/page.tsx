"use client";

import { useState } from "react";
import { del, post, put, useApi } from "@/lib/api";

type Routine = { id: string; name: string; kind: "remind" | "ask" | "tool"; enabled: boolean; at: string; days: number[]; every_min: number; text: string; server: string; tool: string; arguments: Record<string, unknown>; notify: string; last_run: number };
type Server = { id: string; name: string; enabled: boolean; tools: { name: string; description: string }[]; off: string[] };

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function RoutinesPage() {
  const { data, reload } = useApi<{ routines: Routine[] }>("/routines");
  const { data: skills } = useApi<{ servers: Server[] }>("/skills");
  const [msg, setMsg] = useState("");
  const blank = { name: "", kind: "remind" as Routine["kind"], mode: "time", at: "12:30", days: [] as number[], every_min: 60, text: "", server: "", tool: "", args: "{}", notify: "error" };
  const [f, setF] = useState(blank);

  async function run(fn: () => Promise<unknown>, ok: string) {
    try { await fn(); setMsg(ok); reload(); } catch (e) { setMsg(`Not done: ${(e as Error).message}`); }
  }
  const tools = (skills?.servers ?? []).filter((s) => s.enabled).find((s) => s.id === f.server)?.tools.filter((t) => true) ?? [];

  function save() {
    let args: Record<string, unknown> = {};
    try { args = JSON.parse(f.args || "{}"); } catch { setMsg("The arguments are not valid JSON."); return; }
    const body: Record<string, unknown> = { name: f.name, kind: f.kind, text: f.text, notify: f.kind === "tool" ? f.notify : "always" };
    if (f.mode === "time") { body.at = f.at; body.days = f.days; } else { body.every_min = f.every_min; }
    if (f.kind === "tool") { body.server = f.server; body.tool = f.tool; body.arguments = args; }
    run(async () => { await post("/routines", body); setF(blank); }, "Routine created.");
  }

  return (
    <>
      <h1>Routines</h1>
      <p className="muted">
        Things the assistant does by itself on a schedule: a <b>reminder</b> in your own style, a <b>task</b> it does like a chat (with its look-ups and skills),
        or one <b>skill call</b> with fixed arguments (for example a smart-home action: it runs without asking, because you set it up here). You can also say
        &quot;remind me to log lunch at 12:30 every day&quot; in Telegram: it shows a card to confirm.
      </p>
      <div className="card">
        <b>New routine</b>
        <div className="row">
          <input placeholder="name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value as Routine["kind"] })}>
            <option value="remind">Remind me</option>
            <option value="ask">Assistant does a task</option>
            <option value="tool">Call a skill (automation)</option>
          </select>
          <select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}>
            <option value="time">At a time of day</option>
            <option value="every">Every N minutes</option>
          </select>
          {f.mode === "time" ? <input type="time" value={f.at} onChange={(e) => setF({ ...f, at: e.target.value })} /> : <input type="number" min={15} value={f.every_min} onChange={(e) => setF({ ...f, every_min: Number(e.target.value) })} />}
        </div>
        {f.mode === "time" && (
          <div className="row">
            {DAYS.map((d, i) => (
              <label key={d}><input type="checkbox" checked={f.days.includes(i)} onChange={(e) => setF({ ...f, days: e.target.checked ? [...f.days, i] : f.days.filter((x) => x !== i) })} /> {d}</label>
            ))}
            <span className="muted">(none ticked = every day)</span>
          </div>
        )}
        {f.kind !== "tool" ? (
          <input style={{ width: "100%" }} placeholder={f.kind === "remind" ? "what to remind you about (log lunch)" : "what the assistant should do (ask what I ate and log it)"} value={f.text} onChange={(e) => setF({ ...f, text: e.target.value })} />
        ) : (
          <div className="row">
            <select value={f.server} onChange={(e) => setF({ ...f, server: e.target.value, tool: "" })}>
              <option value="">skill server...</option>
              {(skills?.servers ?? []).filter((s) => s.enabled).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            <select value={f.tool} onChange={(e) => setF({ ...f, tool: e.target.value })}>
              <option value="">tool...</option>
              {tools.filter((t) => !(skills?.servers.find((s) => s.id === f.server)?.off ?? []).includes(t.name)).map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
            </select>
            <input style={{ flex: 1 }} placeholder='arguments as JSON, e.g. {"device": "lamp", "on": true}' value={f.args} onChange={(e) => setF({ ...f, args: e.target.value })} />
            <select value={f.notify} onChange={(e) => setF({ ...f, notify: e.target.value })}>
              <option value="error">tell me only if it fails</option>
              <option value="always">tell me every time</option>
            </select>
          </div>
        )}
        <div className="row"><button disabled={!f.name.trim()} onClick={save}>Create</button><span className="muted">{msg}</span></div>
      </div>
      {!data ? <p className="muted">Loading...</p> : data.routines.length === 0 ? <p className="muted">No routines yet.</p> : data.routines.map((r) => (
        <div className="card" key={r.id} style={{ marginBottom: 8, opacity: r.enabled ? 1 : 0.6 }}>
          <div className="row">
            <b>{r.name}</b>
            <span className="muted">{r.kind === "remind" ? "reminder" : r.kind === "ask" ? "task" : `skill ${r.server}/${r.tool}`} · {r.every_min ? `every ${r.every_min} min` : `${r.at} · ${r.days.length && r.days.length < 7 ? r.days.map((d) => DAYS[d]).join(", ") : "every day"}`}{r.last_run ? ` · last ran ${new Date(r.last_run * 1000).toLocaleString()}` : ""}</span>
          </div>
          {r.text && <p>{r.text}</p>}
          <div className="row">
            <button onClick={() => run(() => put(`/routines/${r.id}`, { enabled: !r.enabled }), r.enabled ? "Switched off." : "Switched on.")}>{r.enabled ? "Switch off" : "Switch on"}</button>
            <button onClick={() => run(() => post(`/routines/${r.id}/run`, {}), "Queued: it runs in a moment.")}>Run now</button>
            <button className="link" onClick={() => { if (confirm(`Delete ${r.name}?`)) run(() => del(`/routines/${r.id}`), "Deleted."); }}>Delete</button>
          </div>
        </div>
      ))}
    </>
  );
}
