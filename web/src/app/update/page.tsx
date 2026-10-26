"use client";

import { useEffect, useRef, useState } from "react";
import { api, getKey, useApi } from "@/lib/api";

type Commit = { sha: string; message: string; date: string; author: string };
type CheckResult = {
  up_to_date: boolean;
  behind: number;
  local_sha: string;
  remote_sha: string;
  latest_commit: Commit;
  repo_url: string;
};

type Phase = "idle" | "checking" | "updating" | "done";

function fmtDate(iso: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return iso; }
}

export default function UpdatePage() {
  const { data: health } = useApi<unknown>("/health");
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [log, setLog] = useState<string[]>([]);
  const [exitOk, setExitOk] = useState<boolean | null>(null);
  const logRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log]);

  async function doCheck() {
    setPhase("checking");
    setCheck(null);
    setLog([]);
    setExitOk(null);
    try {
      const r = await api<CheckResult>("/update/check");
      setCheck(r);
    } catch (e) {
      setLog([`Error: ${(e as Error).message}`]);
    } finally {
      setPhase("idle");
    }
  }

  async function doUpdate(target: "up" | "web") {
    setPhase("updating");
    setLog([`Starting update (target: ${target})…`]);
    setExitOk(null);
    const res = await fetch(`/api/update/apply?target=${target}`, {
      method: "POST",
      headers: { "x-api-key": getKey() },
    });
    if (!res.ok || !res.body) {
      setLog((l) => [...l, `HTTP error: ${res.status} ${res.statusText}`]);
      setPhase("done");
      setExitOk(false);
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const text = line.slice(6);
        if (text.startsWith("[done]")) {
          setExitOk(parseInt(text.split(" ")[1] ?? "1", 10) === 0);
          setPhase("done");
        } else {
          setLog((l) => [...l, text]);
        }
      }
    }
    if (buf.startsWith("data: ")) {
      const text = buf.slice(6);
      if (text.startsWith("[done]")) setExitOk(parseInt(text.split(" ")[1] ?? "1", 10) === 0);
      else if (text) setLog((l) => [...l, text]);
    }
    setPhase((p) => (p === "updating" ? "done" : p));
  }

  const busy = phase === "checking" || phase === "updating";

  return (
    <>
      <h1>Updates</h1>
      <p className="muted">
        Check GitHub for new commits and apply them with one click.
        The server runs <code>git pull</code> then <code>deploy/deploy.sh</code>.
        Live output is streamed below.
      </p>

      {/* ── Status card ── */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="row" style={{ alignItems: "flex-start", gap: 24, flexWrap: "wrap" }}>
          <div>
            <div className="muted" style={{ fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>Running</div>
            {health ? <span className="pill good">online</span> : <span className="pill warn">connecting…</span>}
          </div>
          {check && (
            <>
              <div>
                <div className="muted" style={{ fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>Local commit</div>
                <code style={{ fontSize: "0.88rem" }}>{check.local_sha}</code>
              </div>
              <div>
                <div className="muted" style={{ fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>Remote commit</div>
                {check.repo_url
                  ? <a href={`${check.repo_url}/commit/${check.latest_commit.sha}`} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)", fontSize: "0.88rem" }}><code>{check.remote_sha}</code></a>
                  : <code style={{ fontSize: "0.88rem" }}>{check.remote_sha}</code>}
              </div>
              <div>
                <div className="muted" style={{ fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>Status</div>
                {check.up_to_date
                  ? <span className="pill good">Up to date ✓</span>
                  : <span className="pill warn">{check.behind} commit{check.behind !== 1 ? "s" : ""} behind</span>}
              </div>
            </>
          )}
        </div>
        {check && !check.up_to_date && (
          <div style={{ marginTop: 12, padding: "10px 12px", background: "var(--bg)", borderRadius: 8, border: "1px solid var(--line)" }}>
            <div style={{ fontWeight: 600, marginBottom: 2 }}>{check.latest_commit.message}</div>
            <div className="muted" style={{ fontSize: "0.83rem" }}>{check.latest_commit.author} · {fmtDate(check.latest_commit.date)}</div>
          </div>
        )}
        {check?.up_to_date && <p className="muted" style={{ margin: "10px 0 0" }}>You are running the latest version.</p>}
      </div>

      <div className="row" style={{ marginBottom: 16 }}>
        <button onClick={doCheck} disabled={busy}>
          {phase === "checking" ? "Checking…" : "Check for updates"}
        </button>
        {check && !check.up_to_date && (
          <>
            <button onClick={() => doUpdate("up")} disabled={busy}
              style={{ background: "var(--accent)", color: "#fff", border: "none" }}>
              {phase === "updating" ? "Updating…" : "⬆ Update backend + web"}
            </button>
            <button onClick={() => doUpdate("web")} disabled={busy}>
              {phase === "updating" ? "Updating…" : "Update web only"}
            </button>
          </>
        )}
        {check?.up_to_date && (
          <>
            <button onClick={() => doUpdate("up")} disabled={busy}>Force rebuild backend + web</button>
            <button onClick={() => doUpdate("web")} disabled={busy}>Force rebuild web only</button>
          </>
        )}
      </div>

      {phase === "done" && exitOk !== null && (
        <div className="card" style={{ marginBottom: 12,
          borderColor: exitOk ? "var(--good)" : "var(--bad)",
          color: exitOk ? "var(--good)" : "var(--bad)", fontWeight: 600 }}>
          {exitOk
            ? "✓ Update finished successfully. The new version is now running."
            : "✗ Update failed — see the log below for details."}
        </div>
      )}

      {log.length > 0 && (
        <>
          <h2>Log</h2>
          <pre ref={logRef} style={{
            background: "var(--card)", border: "1px solid var(--line)", borderRadius: 10,
            padding: "12px 14px", maxHeight: 480, overflowY: "auto",
            fontSize: "0.82rem", lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-all",
          }}>
            {log.map((l, i) => {
              const color = l.startsWith("[error]") ? "var(--bad)"
                : l.startsWith("[git]") ? "var(--muted)"
                : l.startsWith("[deploy]") ? "var(--accent)"
                : "inherit";
              return <span key={i} style={{ color, display: "block" }}>{l}</span>;
            })}
            {phase === "updating" && <span style={{ color: "var(--muted)", display: "block" }}>▌</span>}
          </pre>
        </>
      )}

      <h2>How it works</h2>
      <ul className="muted">
        <li><b>Check for updates</b> runs <code>git fetch</code> and compares your HEAD with <code>origin/main</code>.</li>
        <li><b>Update backend + web</b> runs <code>git pull --rebase</code> then <code>deploy/deploy.sh up</code>.</li>
        <li><b>Update web only</b> runs <code>deploy/deploy.sh web</code> — faster for dashboard-only changes.</li>
        <li>The API container restarts as part of <code>deploy.sh up</code>; this page reconnects automatically.</li>
        <li>Requires the project to be a git clone with <code>deploy/deploy.sh</code> present.</li>
      </ul>
    </>
  );
}
