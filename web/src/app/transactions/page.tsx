"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { rupiah, when } from "@/lib/format";

type Tx = { id: number; time: string; merchant: string | null; direction: string; amount: number; category: string | null; wallet: string; status: string; source: string };

const LABEL: Record<string, string> = { committed: "committed", quarantined: "held", rolled_back: "rolled back" };

export default function Transactions() {
  const [rows, setRows] = useState<Tx[]>([]);
  const [status, setStatus] = useState("");
  const [more, setMore] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (reset: boolean, beforeId?: number) => {
      try {
        const q = new URLSearchParams({ limit: "50" });
        if (status) q.set("status", status);
        if (beforeId) q.set("before_id", String(beforeId));
        const page = await api<Tx[]>(`/dashboard/transactions?${q}`);
        setRows((cur) => (reset ? page : [...cur, ...page]));
        setMore(page.length === 50);
        setError(null);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [status],
  );

  useEffect(() => {
    load(true);
  }, [load]);

  return (
    <>
      <h1>Transactions</h1>
      <div className="row">
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          {Object.entries(LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>
      {error && <p className="muted">Failed to load: {error}</p>}
      <div className="tablewrap">
        <table>
          <thead><tr><th>Time</th><th>Merchant</th><th>Category</th><th>Wallet</th><th>Source</th><th className="num">Amount</th><th>Status</th></tr></thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id}>
                <td>{when(t.time)}</td><td>{t.merchant ?? "-"}</td><td>{t.category ?? "-"}</td><td>{t.wallet}</td><td>{t.source}</td>
                <td className="num">{t.direction === "in" ? "+" : "-"}{rupiah(t.amount)}</td>
                <td><span className={`pill ${t.status === "committed" ? "good" : t.status === "quarantined" ? "warn" : "bad"}`}>{LABEL[t.status] ?? t.status}</span></td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={7} className="muted">No transactions yet.</td></tr>}
          </tbody>
        </table>
      </div>
      {more && rows.length > 0 && <div className="row"><button onClick={() => load(false, rows[rows.length - 1].id)}>Load older</button></div>}
    </>
  );
}
