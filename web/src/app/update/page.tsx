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
  releases_url?: string;
  apk_url?: string;
  exe_url?: string;
};

type Phase = "idle" | "checking" | "updating" | "done";

const DEFAULT_REPO = "https://github.com/frostfire1/Autonomous-Personal-Assistance";

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
      setLog([`Error checking GitHub: ${(e as Error).message}`]);
    } finally {
      setPhase("idle");
    }
  }

  // Auto-check on page load
  useEffect(() => {
    doCheck();
  }, []);

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
  const repoUrl = check?.repo_url || DEFAULT_REPO;
  const releasesUrl = check?.releases_url || `${repoUrl}/releases/latest`;
  const apkDownloadUrl = check?.apk_url || "/api/update/download/apk";
  const exeDownloadUrl = check?.exe_url || "/api/update/download/exe";

  return (
    <>
      <h1>Updates & Downloads</h1>
      <p className="muted">
        Check GitHub (<code>{repoUrl.replace("https://github.com/", "")}</code>) for new commits, download client
        binaries (Android APK &amp; Windows Tracker EXE), or deploy updates.
      </p>

      {/* ── Status card ── */}
      <div className="card" style={{ marginBottom: 20 }}>
        <div className="row" style={{ alignItems: "flex-start", gap: 24, flexWrap: "wrap" }}>
          <div>
            <div className="muted" style={{ fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>Server Status</div>
            {health ? <span className="pill good">online</span> : <span className="pill warn">connecting…</span>}
          </div>
          {check && (
            <>
              <div>
                <div className="muted" style={{ fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>Local commit</div>
                <code style={{ fontSize: "0.88rem" }}>{check.local_sha}</code>
              </div>
              <div>
                <div className="muted" style={{ fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>GitHub main commit</div>
                {check.repo_url && check.latest_commit.sha
                  ? <a href={`${check.repo_url}/commit/${check.latest_commit.sha}`} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)", fontSize: "0.88rem" }}><code>{check.remote_sha}</code></a>
                  : <code style={{ fontSize: "0.88rem" }}>{check.remote_sha}</code>}
              </div>
              <div>
                <div className="muted" style={{ fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>Update Status</div>
                {check.up_to_date
                  ? <span className="pill good">Up to date ✓</span>
                  : <span className="pill warn">{check.behind} commit{check.behind !== 1 ? "s" : ""} behind</span>}
              </div>
            </>
          )}
        </div>
        {check && !check.up_to_date && check.latest_commit.message && (
          <div style={{ marginTop: 12, padding: "10px 12px", background: "var(--bg)", borderRadius: 8, border: "1px solid var(--line)" }}>
            <div style={{ fontWeight: 600, marginBottom: 2 }}>{check.latest_commit.message}</div>
            <div className="muted" style={{ fontSize: "0.83rem" }}>
              {check.latest_commit.author} · {fmtDate(check.latest_commit.date)}
            </div>
          </div>
        )}
        {check?.up_to_date && <p className="muted" style={{ margin: "10px 0 0" }}>You are running the latest version.</p>}
      </div>

      {/* ── Client Downloads Card ── */}
      <div className="card" style={{ marginBottom: 20 }}>
        <h2 style={{ marginTop: 0, marginBottom: 8, fontSize: "1.15rem" }}>📱 Client Applications (APK &amp; EXE)</h2>
        <p className="muted" style={{ fontSize: "0.9rem", marginBottom: 16 }}>
          Download client apps to connect your phone and computer to your Autonomous Personal Assistant.
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16 }}>
          {/* Android APK */}
          <div style={{ padding: 14, background: "var(--bg)", borderRadius: 8, border: "1px solid var(--line)" }}>
            <div style={{ fontWeight: 600, fontSize: "0.95rem", marginBottom: 4 }}>🤖 Android Phone App</div>
            <p className="muted" style={{ fontSize: "0.82rem", margin: "0 0 12px" }}>
              Bank &amp; e-wallet notification listener, Health Connect sync, and morning audio briefing.
            </p>
            <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <a
                href={apkDownloadUrl}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  background: "var(--accent)",
                  color: "#fff",
                  padding: "6px 12px",
                  borderRadius: 6,
                  textDecoration: "none",
                  fontWeight: 500,
                  fontSize: "0.85rem"
                }}
              >
                ⬇️ Download APK (apa-latest.apk)
              </a>
              <a
                href={`${repoUrl}/blob/main/docs/ANDROID.md`}
                target="_blank"
                rel="noopener noreferrer"
                style={{ fontSize: "0.8rem", color: "var(--accent)" }}
              >
                Setup Guide →
              </a>
            </div>
          </div>

          {/* Windows EXE */}
          <div style={{ padding: 14, background: "var(--bg)", borderRadius: 8, border: "1px solid var(--line)" }}>
            <div style={{ fontWeight: 600, fontSize: "0.95rem", marginBottom: 4 }}>💻 Windows Desktop Tracker</div>
            <p className="muted" style={{ fontSize: "0.82rem", margin: "0 0 12px" }}>
              Lives silently in Windows System Tray. Tracks screen time &amp; active apps, pushes usage summaries to server.
            </p>
            <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <a
                href={exeDownloadUrl}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  background: "var(--accent)",
                  color: "#fff",
                  padding: "6px 12px",
                  borderRadius: 6,
                  textDecoration: "none",
                  fontWeight: 500,
                  fontSize: "0.85rem"
                }}
              >
                ⬇️ Download EXE (APA-Tracker.exe)
              </a>
              <a
                href={releasesUrl}
                target="_blank"
                rel="noopener noreferrer"
                style={{ fontSize: "0.8rem", color: "var(--accent)" }}
              >
                All Releases →
              </a>
            </div>
          </div>
        </div>
      </div>

      {/* ── Actions ── */}
      <div className="row" style={{ marginBottom: 16 }}>
        <button onClick={doCheck} disabled={busy}>
          {phase === "checking" ? "Checking GitHub…" : "Check GitHub for updates"}
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
            : "✗ Update finished with notes — see the log below for details."}
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
        <li><b>Check for updates</b> compares local deployment with commits on <code>frostfire1/Autonomous-Personal-Assistance:main</code> via GitHub API.</li>
        <li><b>Update backend + web</b> runs <code>git pull --rebase</code> then <code>deploy/deploy.sh up</code> on the server.</li>
        <li><b>Client Apps</b>: Android APK and Windows Tracker EXE are automatically built by GitHub Actions and published to GitHub Releases on every commit.</li>
        <li>Direct downloads are served by the API at <code>/update/download/apk</code> and <code>/update/download/exe</code>.</li>
      </ul>
    </>
  );
}
