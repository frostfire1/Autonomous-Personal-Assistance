"use client";

import { useApi } from "@/lib/api";
import { when } from "@/lib/format";

type Exchange = {
  event_id: number; source: string; status: string; text: string; at: string; route: string; latency_s: number | null;
  replies: { kind: string; body: string; at: string }[];
  tx: { id: number; status: string; merchant: string | null; amount: string; category: string | null } | null;
};

export default function Logs() {
  const { data, error } = useApi<Exchange[]>("/inspect/timeline?limit=30", 15_000);
  return (
    <>
      <h1>Conversations</h1>
      <p className="muted">
        What you sent, how the system answered, and the route it took. <code>/private</code> sessions never appear here: their contents exist only in
        the bot's memory while the session lasts, never reach the database, logs, Google Sheets or this page, and all messages are deleted when the
        session ends.
      </p>
      {error && <p className="muted">Failed to load: {error}</p>}
      {!data && !error && <p className="muted">Loading...</p>}
      {(data ?? []).map((x) => (
        <div className="card" key={x.event_id} style={{ marginBottom: 10 }}>
          <div className="muted">
            #{x.event_id} · {when(x.at)} · {x.source} · route {x.route} · {x.status}{x.latency_s != null ? ` · ${x.latency_s}s` : ""}
          </div>
          <pre>{x.text}</pre>
          {x.replies.map((r, i) => (
            <pre key={i} className="muted">↳ ({r.kind}) {r.body}</pre>
          ))}
          {x.tx && <div className="muted">transaction #{x.tx.id}: {x.tx.merchant ?? "-"} · {x.tx.amount} · {x.tx.category ?? "-"} · {x.tx.status}</div>}
        </div>
      ))}
      {data && data.length === 0 && <p className="muted">No conversations yet.</p>}
    </>
  );
}
