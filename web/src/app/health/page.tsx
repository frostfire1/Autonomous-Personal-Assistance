"use client";

import { useApi } from "@/lib/api";
import { hm } from "@/lib/format";

type Night = { date: string; minutes: number | null; score: number | null; deep_pct: number | null; rem_pct: number | null; awake_mins: number | null; fatigue_defense: boolean };

export default function Health() {
  const { data, error } = useApi<Night[]>("/dashboard/health?days=30", 300_000);
  return (
    <>
      <h1>Health</h1>
      <p className="muted">Sleep over the last 30 days from Health Connect. A score below 70 activates fatigue defense.</p>
      {error && <p className="muted">Failed to load: {error}</p>}
      <div className="tablewrap">
        <table>
          <thead><tr><th>Date</th><th>Duration</th><th>Score</th><th>Deep</th><th>REM</th><th>Awake</th><th></th></tr></thead>
          <tbody>
            {(data ?? []).map((n) => (
              <tr key={n.date}>
                <td>{n.date}</td><td>{hm(n.minutes)}</td><td>{n.score ?? "-"}</td>
                <td>{n.deep_pct != null ? `${n.deep_pct}%` : "-"}</td><td>{n.rem_pct != null ? `${n.rem_pct}%` : "-"}</td>
                <td>{n.awake_mins != null ? `${n.awake_mins} m` : "-"}</td>
                <td>{n.fatigue_defense && <span className="pill warn">defense active</span>}</td>
              </tr>
            ))}
            {data && data.length === 0 && <tr><td colSpan={7} className="muted">No sleep data yet. Allow Health Connect in the Android app.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
