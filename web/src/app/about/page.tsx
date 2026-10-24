"use client";

import { useState } from "react";
import { del, getKey, post, put, useApi } from "@/lib/api";

type Entry = {
  ref: string; domain: string; key: string; value: string; why: string;
  source: string; confidence: number; locked: boolean; updated: string | null; editable: boolean;
};

const HINT: Record<string, string> = {
  identity: "name, nickname", comfort: "language, tone, length, emoji", personality: "how you are", hobbies: "what you enjoy",
  tastes: "food, music, likes and dislikes", routines: "sleep, meals, active hours", wellbeing: "health and rest",
  money: "spending habits", people: "people in your life", goals: "what you want", plans: "what is coming up", notes: "things you asked it to remember",
  other: "anything else",
};

export default function AboutPage() {
  const { data, reload } = useApi<{ domains: string[]; entries: Entry[] }>("/about");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  const [nd, setNd] = useState({ domain: "comfort", key: "", value: "" });

  async function run(f: () => Promise<unknown>, ok: string) {
    try { await f(); setMsg(ok); reload(); } catch (e) { setMsg(`Not done: ${(e as Error).message}`); }
  }

  const byDomain = (d: string) => (data?.entries ?? []).filter((e) => e.domain === d);

  return (
    <>
      <h1>About me</h1>
      <p className="muted">
        Everything the assistant knows about you, in one place. Entries you write or lock are certain and the assistant never changes them; the
        others it noticed or computed and may be out of date: correct or delete anything that is wrong. Grey entries are computed from your data:
        to correct one, write your own entry for it below. Everything is stored encrypted.{" "}
        <button className="link" onClick={async () => {
          const res = await fetch("/api/about/export", { headers: { "x-api-key": getKey() } });
          const url = URL.createObjectURL(await res.blob());
          const a = document.createElement("a"); a.href = url; a.download = "about-me.jsonl"; a.click(); URL.revokeObjectURL(url);
        }}>Export as JSON lines</button>
      </p>
      <div className="card">
        <b>Add something about you</b>
        <div className="row">
          <select value={nd.domain} onChange={(e) => setNd({ ...nd, domain: e.target.value })}>
            {(data?.domains ?? ["comfort"]).filter((d) => d !== "notes").map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <input placeholder="key (language, nickname, favourite_game...)" value={nd.key} onChange={(e) => setNd({ ...nd, key: e.target.value })} />
          <input style={{ flex: 1 }} placeholder="value" value={nd.value} onChange={(e) => setNd({ ...nd, value: e.target.value })} />
          <button disabled={!nd.key.trim() || !nd.value.trim()} onClick={() => run(async () => {
            await post("/about/fact", { domain: nd.domain, key: nd.key, value: nd.value, locked: true });
            setNd({ ...nd, key: "", value: "" });
          }, "Saved.")}>Save</button>
        </div>
        <span className="muted">{msg}</span>
      </div>
      {!data ? <p className="muted">Loading...</p> : data.domains.map((d) => {
        const items = byDomain(d);
        if (!items.length) return null;
        return (
          <div key={d}>
            <h2>{d} <span className="muted" style={{ fontSize: 13 }}>{HINT[d] ?? ""}</span></h2>
            {items.map((e) => (
              <div className="card" key={e.ref} style={{ marginBottom: 8, opacity: e.editable ? 1 : 0.8 }}>
                <div className="muted">
                  {e.key.startsWith("note_") || e.key.startsWith("learned_") ? "" : `${e.key} · `}
                  {e.locked ? "set by you (locked)" : e.source === "computed" ? "computed from your data" : e.source === "owner" ? "set by you" : e.source === "telegram" ? "from Telegram" : "noticed by the assistant"}
                  {e.why ? ` · ${e.why}` : ""}{e.updated ? ` · ${e.updated}` : ""}
                </div>
                {e.editable ? (
                  <textarea rows={2} style={{ width: "100%" }} value={draft[e.ref] ?? e.value} onChange={(ev) => setDraft({ ...draft, [e.ref]: ev.target.value })} />
                ) : <p>{e.value}</p>}
                <div className="row">
                  {e.editable && draft[e.ref] !== undefined && draft[e.ref] !== e.value && (
                    <button onClick={() => run(async () => { await put("/about/item", { ref: e.ref, value: draft[e.ref] }); setDraft({ ...draft, [e.ref]: undefined as unknown as string }); }, "Saved.")}>Save change</button>
                  )}
                  {e.ref.startsWith("fact:") && (
                    <button onClick={() => run(() => put("/about/item", { ref: e.ref, value: e.value, locked: !e.locked }), e.locked ? "Unlocked." : "Locked.")}>
                      {e.locked ? "Unlock" : "Lock (assistant cannot change)"}
                    </button>
                  )}
                  {e.editable && <button className="link" onClick={() => { if (confirm("Delete this entry for good?")) run(() => del(`/about/item?ref=${encodeURIComponent(e.ref)}`), "Deleted."); }}>Delete</button>}
                </div>
              </div>
            ))}
          </div>
        );
      })}
    </>
  );
}
