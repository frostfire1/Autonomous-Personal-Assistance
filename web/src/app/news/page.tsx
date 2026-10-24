"use client";

import { useEffect, useState } from "react";
import { post, put, useApi } from "@/lib/api";

type Feed = { name: string; url: string; topic: string; every_min: number };
type Cve = { min_cvss: number; kev_always: boolean; watch: string[]; hours: number };
type Config = { country: string; disabled_feeds: string[]; feeds: Feed[]; topics: Record<string, { label: string }>; cve: Cve };
type View = {
  config: Config;
  builtin_feeds: { name: string; url: string; topic: string; enabled: boolean }[];
  topics: { key: string; label: string; builtin: boolean }[];
  countries: { key: string; label: string }[];
  limits: { feeds: number; topics: number };
};

export default function NewsPage() {
  const { data: view, reload } = useApi<View>("/news-config");
  const [cfg, setCfg] = useState<Config | null>(null);
  const [watch, setWatch] = useState("");
  const [msg, setMsg] = useState("");
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [newTopic, setNewTopic] = useState("");

  useEffect(() => {
    if (view) {
      setCfg(view.config);
      setWatch(view.config.cve.watch.join(", "));
    }
  }, [view]);

  if (!cfg || !view) return <><h1>News and CVEs</h1><p className="muted">Loading...</p></>;
  const topicKeys = view.topics.map((t) => t.key).concat(Object.keys(cfg.topics).filter((k) => !view.topics.some((t) => t.key === k)));
  const label = (k: string) => view.topics.find((t) => t.key === k)?.label ?? cfg.topics[k]?.label ?? k;

  function toggleBuiltin(name: string, on: boolean) {
    setCfg({ ...cfg!, disabled_feeds: on ? cfg!.disabled_feeds.filter((n) => n !== name) : [...cfg!.disabled_feeds, name] });
  }
  function addFeed() {
    setCfg({ ...cfg!, feeds: [...cfg!.feeds, { name: `My feed ${cfg!.feeds.length + 1}`, url: "https://", topic: topicKeys[0] ?? "nasional", every_min: 60 }] });
  }
  function setFeed(i: number, f: Partial<Feed>) {
    setCfg({ ...cfg!, feeds: cfg!.feeds.map((x, j) => (j === i ? { ...x, ...f } : x)) });
  }
  function removeFeed(i: number) {
    setCfg({ ...cfg!, feeds: cfg!.feeds.filter((_, j) => j !== i) });
  }
  function addTopic() {
    const key = newTopic.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 24);
    if (key.length < 2) return;
    setCfg({ ...cfg!, topics: { ...cfg!.topics, [key]: { label: newTopic.trim() } } });
    setNewTopic("");
  }
  function removeTopic(key: string) {
    const topics = { ...cfg!.topics };
    delete topics[key];
    setCfg({ ...cfg!, topics, feeds: cfg!.feeds.filter((f) => f.topic !== key) });
  }
  async function testFeed(i: number) {
    const id = `f${i}`;
    setNotes((n) => ({ ...n, [id]: "Reading the feed..." }));
    try {
      const r = await post<{ ok: boolean; count?: number; sample?: string[]; detail: string }>("/news-config/test-feed", { url: cfg!.feeds[i].url });
      setNotes((n) => ({ ...n, [id]: r.ok ? `Works: ${r.count} articles, e.g. "${r.sample?.[0] ?? ""}"` : `Problem: ${r.detail}` }));
    } catch (e) {
      setNotes((n) => ({ ...n, [id]: `Problem: ${(e as Error).message}` }));
    }
  }
  async function save() {
    setMsg("Saving...");
    const cve = { ...cfg!.cve, watch: watch.split(",").map((w) => w.trim()).filter(Boolean) };
    try {
      await put("/news-config", { ...cfg!, cve });
      setMsg("Saved. New feeds are read on the next news check (every few minutes); the rest applies to the next briefing.");
      reload();
    } catch (e) {
      setMsg(`Not saved: ${(e as Error).message}`);
    }
  }

  return (
    <>
      <h1>News and CVEs</h1>
      <p className="muted">
        The built-in feeds, the topics and the CVE rule are only the starting point. Switch built-in feeds off, add any RSS or Atom feed, make your own
        topics, and tune the CVE rule. Turn topics on for the morning briefing in Settings.
      </p>

      <h2>National news: your country</h2>
      <div className="row">
        <label>Home country{" "}
          <select value={cfg.country} onChange={(e) => setCfg({ ...cfg, country: e.target.value })}>
            {view.countries.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
          </select>
        </label>
        <button onClick={save}>Save and load its feeds</button>
      </div>
      <p className="muted">
        The built-in national feeds and the national ranking follow this country. Feeds in other languages are summarized in your briefing language.
        Any other country, or more outlets, can be covered with &quot;Your feeds&quot; below, set to the National topic.
      </p>

      <h2>Built-in feeds</h2>
      <div className="tablewrap">
        <table>
          <thead><tr><th>On</th><th>Feed</th><th>Topic</th></tr></thead>
          <tbody>
            {view.builtin_feeds.map((f) => (
              <tr key={f.name}>
                <td><input type="checkbox" checked={!cfg.disabled_feeds.includes(f.name)} onChange={(e) => toggleBuiltin(f.name, e.target.checked)} /></td>
                <td><b>{f.name}</b><div className="muted">{f.url}</div></td>
                <td>{label(f.topic)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Your feeds</h2>
      {cfg.feeds.map((f, i) => (
        <div className="card" key={i} style={{ marginBottom: 8 }}>
          <div className="row">
            <input value={f.name} onChange={(e) => setFeed(i, { name: e.target.value })} placeholder="name" style={{ width: 170 }} />
            <input value={f.url} onChange={(e) => setFeed(i, { url: e.target.value })} placeholder="https://example.com/feed.xml" style={{ flex: 1, minWidth: 240 }} />
            <select value={f.topic} onChange={(e) => setFeed(i, { topic: e.target.value })}>
              {topicKeys.map((k) => <option key={k} value={k}>{label(k)}</option>)}
            </select>
            <button onClick={() => testFeed(i)}>Test</button>
            <button className="link" onClick={() => removeFeed(i)}>Remove</button>
          </div>
          {notes[`f${i}`] && <div className="muted">{notes[`f${i}`]}</div>}
        </div>
      ))}
      <button onClick={addFeed} disabled={cfg.feeds.length >= view.limits.feeds}>+ Add a feed</button>

      <h2>Your topics</h2>
      <div className="row">
        {Object.entries(cfg.topics).map(([k, t]) => (
          <span key={k} className="card" style={{ padding: "4px 10px" }}>{t.label} <button className="link" onClick={() => removeTopic(k)}>x</button></span>
        ))}
        <input value={newTopic} onChange={(e) => setNewTopic(e.target.value)} placeholder="new topic, e.g. Cars" />
        <button onClick={addTopic} disabled={Object.keys(cfg.topics).length >= view.limits.topics}>Add topic</button>
      </div>
      <p className="muted">A topic collects the feeds you assign to it. Turn it on for the briefing in Settings.</p>

      <h2>CVE rule</h2>
      <div className="card">
        <div className="row">
          <label>Minimum score (CVSS){" "}
            <input type="number" min={0} max={10} step={0.5} value={cfg.cve.min_cvss} onChange={(e) => setCfg({ ...cfg, cve: { ...cfg.cve, min_cvss: Number(e.target.value) } })} style={{ width: 80 }} />
          </label>
          <label>Look back{" "}
            <input type="number" min={6} max={168} value={cfg.cve.hours} onChange={(e) => setCfg({ ...cfg, cve: { ...cfg.cve, hours: Number(e.target.value) } })} style={{ width: 80 }} /> hours
          </label>
          <label>
            <input type="checkbox" checked={cfg.cve.kev_always} onChange={(e) => setCfg({ ...cfg, cve: { ...cfg.cve, kev_always: e.target.checked } })} />{" "}
            Always include CVEs CISA lists as exploited
          </label>
        </div>
        <div className="row">
          <label>Watch list (vendors or products, comma separated){" "}
            <input value={watch} onChange={(e) => setWatch(e.target.value)} placeholder="e.g. mikrotik, proxmox, nginx" style={{ width: 360 }} />
          </label>
        </div>
        <p className="muted">
          Default: score 7.0 or higher (high and critical) plus anything exploited, over the last 24 hours. A CVE that mentions anything on your watch list is
          always included, whatever its score, and listed first (marked with an eye in Telegram).
        </p>
      </div>

      <div className="row"><button onClick={save}>Save</button><span className="muted">{msg}</span></div>
    </>
  );
}
