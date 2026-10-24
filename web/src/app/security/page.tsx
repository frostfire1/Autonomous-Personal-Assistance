"use client";

import { useApi } from "@/lib/api";

type Cve = { id: string; cvss: number | null; epss: number | null; kev: boolean; tier: string; products: string[]; summary: string };
type Security = { total: number; by_severity: Record<string, number>; kev: number; severe: Cve[] };

export default function Security() {
  const { data, error } = useApi<Security>("/dashboard/security?limit=30", 300_000);
  if (error) return <p className="muted">Failed to load: {error}</p>;
  if (!data) return <p className="muted">Loading...</p>;
  const s = data.by_severity;
  return (
    <>
      <h1>Security</h1>
      <div className="grid">
        <div className="card"><div className="muted">New CVEs, 24 h</div><div className="big">{data.total}</div></div>
        <div className="card"><div className="muted">Critical</div><div className="big">{s.critical ?? 0}</div></div>
        <div className="card"><div className="muted">High</div><div className="big">{s.high ?? 0}</div></div>
        <div className="card"><div className="muted">Exploited (CISA)</div><div className="big">{data.kev}</div></div>
      </div>
      <h2>High and critical, most concerning first</h2>
      <div className="tablewrap">
        <table>
          <thead><tr><th>CVE</th><th>Score</th><th>Technology and version</th><th>Summary</th></tr></thead>
          <tbody>
            {data.severe.map((c) => (
              <tr key={c.id}>
                <td>{c.id}{c.kev && <> <span className="pill bad">exploited</span></>}</td>
                <td>{c.cvss ?? "?"}{c.epss != null ? <div className="muted">EPSS {Math.round(c.epss * 100)}%</div> : null}</td>
                <td>{c.products.length ? c.products.map((p) => <div key={p}>{p}</div>) : <span className="muted">not yet identified</span>}</td>
                <td><span className="muted">{c.summary.slice(0, 220)}</span></td>
              </tr>
            ))}
            {data.severe.length === 0 && <tr><td colSpan={4} className="muted">No new high or critical CVEs.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
