"use client";

import Link from "next/link";
import { useApi } from "@/lib/api";

type Item = { key: string; label: string; ok: boolean; detail: string; fix: string };

const LINKS: Record<string, [string, string]> = {
  providers: ["/models", "Open AI Models"],
  storage: ["/storage", "Open Storage"],
};

export default function SetupPage() {
  const { data, error, reload } = useApi<{ items: Item[]; ready: boolean }>("/setup/status", 15000);
  return (
    <>
      <h1>Get started</h1>
      <p className="muted">
        Everything you set up once lives here. AI providers, agents, voice, news and storage are configured on these web pages, without touching files.
        Only secrets read when the container starts (Telegram token, Gemini key, Firecrawl) live in the <code>.env</code> file.
      </p>
      {error && <p className="muted">Could not load status: {error}</p>}
      {data && (
        <>
          <div className="card"><div className="muted">System core</div><div className="big">{data.ready ? "Ready" : "Something is not running"}</div></div>
          <div className="tablewrap" style={{ marginTop: 12 }}>
            <table>
              <thead><tr><th /><th>Component</th><th>Status</th><th>How to fix / configure</th></tr></thead>
              <tbody>
                {data.items.map((i) => (
                  <tr key={i.key}>
                    <td>{i.ok ? "✅" : "⚠️"}</td>
                    <td><b>{i.label}</b></td>
                    <td>{i.detail}</td>
                    <td>{i.ok ? (LINKS[i.key] ? <Link href={LINKS[i.key][0]}>{LINKS[i.key][1]}</Link> : "") : (LINKS[i.key] ? <Link href={LINKS[i.key][0]}>{i.fix}</Link> : i.fix)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row"><button onClick={reload}>Check again</button></div>
        </>
      )}
      <h2>Next steps</h2>
      <ol>
        <li><Link href="/models">AI Models</Link>: add providers (URL and key), choose a model for each role and agent, create your own agents.</li>
        <li><Link href="/news">News and CVEs</Link>: switch feeds off, add your own feeds and topics, tune the CVE rule and its watch list.</li>
        <li><Link href="/storage">Storage</Link>: this server's disk or S3 (with a test button).</li>
        <li><Link href="/settings">Settings</Link>: language, voice, news topics, followed keywords, weather.</li>
      </ol>
    </>
  );
}
