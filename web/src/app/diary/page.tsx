"use client";

import { useState } from "react";
import { del, post, put, useApi } from "@/lib/api";

type Entry = { id: number; date: string | null; text: string; shared: boolean; created_at: string };

export default function DiaryPage() {
  const { data, reload } = useApi<{ entries: Entry[] }>("/journal");
  const [text, setText] = useState("");
  const [shared, setShared] = useState(false);
  const [msg, setMsg] = useState("");
  const [yt, setYt] = useState("");

  async function save() {
    if (!text.trim()) return;
    setMsg("Saving...");
    try {
      await post("/journal", { text, shared });
      setText("");
      setShared(false);
      setMsg("Saved, encrypted.");
      reload();
    } catch (e) {
      setMsg(`Not saved: ${(e as Error).message}`);
    }
  }

  return (
    <>
      <h1>Diary</h1>
      <div className="card">
        <p className="muted">
          Write what you did, what you liked and what you did not. Entries are encrypted on the server and are <b>private by default</b>:
          the AI never sees them. Tick &quot;Let the assistant read this&quot; only for an entry you want it to know about (long numbers are hidden
          from it even then). In Telegram you can also type <code>/diary your entry</code>: it is saved encrypted and your message is deleted from the chat.
        </p>
        <textarea rows={6} style={{ width: "100%" }} value={text} onChange={(e) => setText(e.target.value)} placeholder="Today I..." />
        <div className="row">
          <label><input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} /> Let the assistant read this</label>
          <button onClick={save} disabled={!text.trim()}>Save entry</button>
          <span className="muted">{msg}</span>
        </div>
        <p className="muted">This page is only as private as the connection to it: use your home network or Tailscale or WireGuard, not the open internet.</p>
      </div>

      <h2>YouTube history</h2>
      <div className="card">
        <p className="muted">
          So the assistant knows what you watch: in Google Takeout choose <b>YouTube and YouTube Music &gt; History</b>, format <b>JSON</b>, download it
          and pick <code>watch-history.json</code> here. Only the channel and title of the last 90 days are kept, encrypted. YouTube gives no live
          access to your history, so repeat this now and then.
        </p>
        <input type="file" accept="application/json,.json" onChange={async (e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          setYt("Reading...");
          try {
            const r = await post<{ watched: number; days: number }>("/import/youtube", JSON.parse(await f.text()));
            setYt(`Imported ${r.watched} videos from ${r.days} days.`);
          } catch (err) {
            setYt(`Not imported: ${(err as Error).message}`);
          }
          e.target.value = "";
        }} />
        <span className="muted"> {yt}</span>
      </div>

      <h2>Your entries</h2>
      {!data ? <p className="muted">Loading...</p> : data.entries.length === 0 ? <p className="muted">No entries yet.</p> : (
        data.entries.map((e) => (
          <div className="card" key={e.id} style={{ marginBottom: 8 }}>
            <div className="muted">{e.date} · {e.shared ? "shared with the assistant" : "private"}</div>
            <p style={{ whiteSpace: "pre-wrap" }}>{e.text}</p>
            <div className="row">
              <button onClick={async () => { await put(`/journal/${e.id}/share`, { shared: !e.shared }); reload(); }}>
                {e.shared ? "Make private" : "Let the assistant read this"}
              </button>
              <button className="link" onClick={async () => { if (confirm("Delete this entry for good?")) { await del(`/journal/${e.id}`); reload(); } }}>Delete</button>
            </div>
          </div>
        ))
      )}
    </>
  );
}
