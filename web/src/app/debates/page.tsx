"use client";

import { useApi } from "@/lib/api";
import { when } from "@/lib/format";

type Decision = { id: number; time: string; type: string; verdict: string; justification: string; transaction_id: number | null; agents: string[]; scores: Record<string, unknown>; final_score: number | null };

export default function Decisions() {
  const { data, error } = useApi<Decision[]>("/dashboard/decisions?limit=60", 30_000);
  return (
    <>
      <h1>Decisions</h1>
      <p className="muted">Every system decision is recorded with its reasoning (audit log, cannot be changed).</p>
      {error && <p className="muted">Failed to load: {error}</p>}
      {!data && !error && <p className="muted">Loading...</p>}
      <div className="tablewrap">
        <table>
          <thead><tr><th>Time</th><th>Type</th><th>Verdict</th><th>Reason</th></tr></thead>
          <tbody>
            {(data ?? []).map((d) => (
              <tr key={d.id}>
                <td>{when(d.time)}</td>
                <td>{d.type}{d.transaction_id ? ` · #${d.transaction_id}` : ""}</td>
                <td>{d.verdict}{d.final_score != null ? ` (${d.final_score})` : ""}</td>
                <td><pre>{d.justification}</pre>{d.agents.length > 0 && <span className="muted">agents: {d.agents.join(", ")}</span>}</td>
              </tr>
            ))}
            {data && data.length === 0 && <tr><td colSpan={4} className="muted">No decisions yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
