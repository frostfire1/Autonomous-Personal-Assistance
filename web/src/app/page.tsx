"use client";

import { useApi } from "@/lib/api";
import { hm, rupiah, when } from "@/lib/format";

type Tx = { id: number; time: string; merchant: string | null; direction: string; amount: number; category: string | null; wallet: string; status: string };
type Overview = {
  today: string; spent_today: number; spent_month: number; income_month: number;
  budgets: { category: string; limit: number; spent: number; percent: number | null; essential: boolean }[];
  recent: Tx[]; waiting: Record<string, number>; quarantined: number;
  sleep: { date: string; minutes: number | null; score: number | null } | null;
  cve: { total: number; critical: number; high: number; kev: number };
  price: string | null;
  weather: string | null;
  daily: { per_day: number; remaining: number; days_left: number; spent_today: number; categories: { category: string; per_day: number; left: number }[] } | null;
};

function StatusPill({ s }: { s: string }) {
  const cls = s === "committed" ? "good" : s === "quarantined" ? "warn" : "bad";
  const label = s === "committed" ? "committed" : s === "quarantined" ? "held" : "rolled back";
  return <span className={`pill ${cls}`}>{label}</span>;
}

export default function Overview() {
  const { data: o, error } = useApi<Overview>("/dashboard/overview", 60_000);
  if (error) return <p className="muted">Failed to load: {error}</p>;
  if (!o) return <p className="muted">Loading...</p>;
  const waiting = Object.entries(o.waiting).reduce((n, [, c]) => n + c, 0);
  return (
    <>
      <h1>Overview</h1>
      <div className="grid">
        <div className="card"><div className="muted">Spent today</div><div className="big">{rupiah(o.spent_today)}</div></div>
        <div className="card"><div className="muted">Spent this month</div><div className="big">{rupiah(o.spent_month)}</div></div>
        <div className="card"><div className="muted">Income this month</div><div className="big">{rupiah(o.income_month)}</div></div>
        <div className="card">
          <div className="muted">Last sleep</div>
          <div className="big">{o.sleep ? hm(o.sleep.minutes) : "-"}</div>
          <div className="muted">{o.sleep?.score != null ? `score ${o.sleep.score}` : "no data yet"}</div>
        </div>
        <div className="card">
          <div className="muted">Security, 24 h</div>
          <div className="big">{o.cve.critical} critical</div>
          <div className="muted">{o.cve.high} high · {o.cve.kev} exploited · {o.cve.total} logged</div>
        </div>
        <div className="card">
          <div className="muted">Needs attention</div>
          <div className="big">{waiting + o.quarantined}</div>
          <div className="muted">{o.quarantined} transactions held · {waiting} messages pending</div>
        </div>
      </div>
      {o.weather && <p className="card" style={{ marginTop: 12 }}>☁️ {o.weather}</p>}
      {o.price && <p className="card" style={{ marginTop: 12 }}>📈 {o.price}</p>}

      <h2>Today's budget</h2>
      {o.daily === null ? <p className="muted">No budget for this month yet.</p> : (
        <div className="grid">
          <div className="card"><div className="muted">Available to spend today</div><div className="big">{rupiah(o.daily.per_day)}</div>
            <div className="muted">{rupiah(o.daily.remaining)} left for {o.daily.days_left} days · spent today {rupiah(o.daily.spent_today)}</div></div>
          {o.daily.categories.map((c) => (
            <div className="card" key={c.category}><div>{c.category}</div><div className="big">{rupiah(c.per_day)}</div>
              <div className="muted">per day · left {rupiah(c.left)}</div></div>
          ))}
        </div>
      )}

      <h2>This month's budget</h2>
      {o.budgets.length === 0 ? <p className="muted">No budget for this month yet.</p> : (
        <div className="grid">
          {o.budgets.map((b) => {
            const p = b.percent ?? 0;
            return (
              <div className="card" key={b.category}>
                <div>{b.category}{b.essential ? " · essential" : ""}</div>
                <div className="muted">{rupiah(b.spent)} of {rupiah(b.limit)} ({p}%)</div>
                <div className={`bar ${p >= 100 ? "bad" : p >= 80 ? "warn" : ""}`}><span style={{ width: `${Math.min(p, 100)}%` }} /></div>
              </div>
            );
          })}
        </div>
      )}

      <h2>Recent transactions</h2>
      <div className="tablewrap">
        <table>
          <thead><tr><th>Time</th><th>Merchant</th><th>Category</th><th className="num">Amount</th><th>Status</th></tr></thead>
          <tbody>
            {o.recent.map((t) => (
              <tr key={t.id}>
                <td>{when(t.time)}</td><td>{t.merchant ?? "-"}</td><td>{t.category ?? "-"}</td>
                <td className="num">{t.direction === "in" ? "+" : "-"}{rupiah(t.amount)}</td><td><StatusPill s={t.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
