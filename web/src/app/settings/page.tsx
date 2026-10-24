"use client";

import { useEffect, useRef, useState } from "react";
import { api, audioWithInfo, del, post, put, useApi } from "@/lib/api";

type Prefs = {
  language: string;
  voice: string;
  city: string;
  lat: number | null;
  lon: number | null;
  gps: boolean;
  topics: string[];
  keywords: string[];
  currency: string;
};
type Voice = { key: string; label: string; engine: string; gender: string };
type PrefsView = {
  prefs: Prefs;
  languages: { key: string; label: string }[];
  topics: { key: string; label: string }[];
  currencies?: { key: string; label: string }[];
  voices: Voice[];
};

export default function SettingsPage() {
  const { data: server, error } = useApi<unknown>("/health");
  const { data: view, reload } = useApi<PrefsView>("/prefs");
  const [form, setForm] = useState<Prefs | null>(null);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [city, setCity] = useState("");
  const [words, setWords] = useState("");
  const [msg, setMsg] = useState("");
  const [playing, setPlaying] = useState(false);
  const player = useRef<HTMLAudioElement | null>(null);
  const { data: memory, reload: reloadMemory } = useApi<{ facts: string[]; diary: { date: string; summary: string }[]; learned: { kind: string; text: string; date?: string }[] }>("/memory");
  const { data: formats, reload: reloadFormats } = useApi<Record<string, { formats: number; skip: number; retired: number }>>("/templates");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (view) {
      setForm(view.prefs);
      setVoices(view.voices);
      setCity(view.prefs.city);
      setWords(view.prefs.keywords.join(", "));
    }
  }, [view]);

  async function save(change: Partial<Prefs>) {
    setMsg("Saving...");
    try {
      await put<PrefsView>("/prefs", change);
      setMsg("Saved. Applies to the next briefing.");
      reload();
    } catch (e) {
      const m = (e as Error).message;
      setMsg(m === "city not found" ? "City not found." : `Failed to save: ${m}`);
    }
  }

  async function chooseLanguage(language: string) {
    if (!form) return;
    setForm({ ...form, language });
    const list = await api<{ voices: Voice[] }>(`/prefs/voices?lang=${language}`).catch(() => null);
    if (list) setVoices(list.voices);
    save({ language });
  }

  function toggleTopic(key: string, on: boolean) {
    if (!form) return;
    const topics = on ? [...form.topics, key] : form.topics.filter((x) => x !== key);
    setForm({ ...form, topics });
    save({ topics });
  }

  async function sample() {
    if (!form) return;
    setPlaying(true);
    setMsg("Generating voice sample...");
    try {
      const a = await audioWithInfo(`/prefs/preview?voice=${encodeURIComponent(form.voice)}&lang=${form.language}`);
      const el = new Audio(a.url);
      player.current = el;
      el.onended = () => setPlaying(false);
      await el.play();
      setMsg(a.backup ? `This is a backup voice (${a.engine}): your chosen voice is currently unavailable, usually because the free quota ran out. Try again in a few minutes.` : "");
    } catch {
      setPlaying(false);
      setMsg("The voice sample could not be created (the voice service is unavailable).");
    }
  }

  const known = voices.some((v) => v.key === form?.voice);

  return (
    <>
      <h1>Settings</h1>
      <div className="card">
        <div className="muted">Server connection</div>
        <div>{error ? "Not connected" : server ? "Connected" : "Checking..."}</div>
      </div>

      <h2>Morning briefing</h2>
      {!form || !view ? <p className="muted">Loading...</p> : (
        <div className="card">
          <div className="row">
            <label>Language{" "}
              <select value={form.language} onChange={(e) => chooseLanguage(e.target.value)}>
                {view.languages.map((l) => <option key={l.key} value={l.key}>{l.label}</option>)}
              </select>
            </label>
          </div>
          {view.currencies && (
            <div className="row">
              <label>Home currency{" "}
                <select value={form.currency} onChange={(e) => { const currency = e.target.value; setForm({ ...form, currency }); save({ currency }); }}>
                  {view.currencies.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
                </select>
              </label>
            </div>
          )}
          <p className="muted">One ledger, one currency, no conversion. It sets how money is written and which bank-notification amounts are read (a notification in another currency is not recorded). Reload this page after changing it.</p>
          <div className="row">
            <label>Voice{" "}
              <select value={form.voice} onChange={(e) => { const voice = e.target.value; setForm({ ...form, voice }); save({ voice }); }}>
                {!known && <option value={form.voice}>{form.voice}</option>}
                {voices.map((v) => <option key={v.key} value={v.key}>{v.label}</option>)}
              </select>
            </label>
            <button onClick={sample} disabled={playing}>{playing ? "Playing..." : "Play sample"}</button>
          </div>
          <p className="muted">The voice list follows the language and voice engine (Gemini, Microsoft, or a custom provider). Voice models are configured on the AI Models page.</p>

          <h3>News topics</h3>
          <div className="row">
            {view.topics.map((t) => (
              <label key={t.key}>
                <input type="checkbox" checked={form.topics.includes(t.key)} onChange={(e) => toggleTopic(t.key, e.target.checked)} />{" "}
                {t.label}
              </label>
            ))}
          </div>
          <div className="row">
            <label>Followed keywords{" "}
              <input value={words} placeholder="e.g. bitcoin, kubernetes, Telkom" onChange={(e) => setWords(e.target.value)} />
            </label>
            <button
              onClick={() => save({ keywords: words.split(",").map((w) => w.trim()).filter(Boolean) })}
              disabled={words === form.keywords.join(", ")}
            >
              Save keywords
            </button>
          </div>
          <p className="muted">News from any source containing your keywords appears in the &quot;Followed&quot; section. CVEs and their technologies/versions are under the Cyber security and CVE topic.</p>

          <h3>Weather</h3>
          <div className="row">
            <label>City{" "}
              <input value={city} placeholder="e.g. Surabaya" onChange={(e) => setCity(e.target.value)} disabled={form.gps} />
            </label>
            <button onClick={() => save({ city })} disabled={form.gps || city.trim() === form.city}>Save city</button>
          </div>
          <div className="row">
            <label>
              <input type="checkbox" checked={form.gps} onChange={(e) => { const gps = e.target.checked; setForm({ ...form, gps }); save({ gps }); }} />{" "}
              Weather follows the phone's GPS location (the Android app sends a rough position each time it opens)
            </label>
          </div>
          {form.gps && <p className="muted">{form.lat != null ? `Last position received: ${form.lat}, ${form.lon}` : "No position from the phone yet: open the Android app and allow location access."}</p>}
          {msg && <p className="muted">{msg}</p>}
        </div>
      )}

      <h2>What the assistant knows about you</h2>
      <div className="card">
        <p className="muted">
          Your personal dataset, built from your chats: notes you asked it to keep, things it noticed on its own (only what you said about your
          own life), and a daily diary. It uses them to know the context. Delete anything that is wrong; /private chats are never included.
        </p>
        {!memory ? <p className="muted">Loading...</p> : (
          <>
            <h3>Notes you asked it to keep</h3>
            {memory.facts.length === 0 ? <p className="muted">None yet. Tell it in Telegram: &quot;remember that I am vegetarian&quot;.</p> : (
              <ol>
                {memory.facts.map((f, i) => (
                  <li key={i}>{f} <button className="link" onClick={async () => { await del(`/memory/${i + 1}`); reloadMemory(); }}>Forget</button></li>
                ))}
              </ol>
            )}
            <h3>What it learned by itself</h3>
            {memory.learned.length === 0 ? <p className="muted">Nothing yet. After a day of chatting it picks out lasting things (a person, a preference, a goal, a routine).</p> : (
              <ol>
                {memory.learned.map((l, i) => (
                  <li key={i}><b>{l.kind}</b>: {l.text} <span className="muted">{l.date}</span>{" "}
                    <button className="link" onClick={async () => { await del(`/memory/learned/${i + 1}`); reloadMemory(); }}>Delete</button></li>
                ))}
              </ol>
            )}
            <h3>Diary</h3>
            {memory.diary.length === 0 ? <p className="muted">No entries yet. One is written each night at 23:55 when you chatted that day.</p> : (
              <ul>
                {[...memory.diary].reverse().map((e) => (
                  <li key={e.date}><b>{e.date}</b>: {e.summary}{" "}
                    <button className="link" onClick={async () => { await del(`/memory/diary/${e.date}`); reloadMemory(); }}>Delete</button></li>
                ))}
              </ul>
            )}
            <div className="row">
              <button onClick={async () => { setNote("Writing today's diary..."); const r = await post<{ written: string | null }>("/memory/diary/today", {}); setNote(r.written ? "Today's entry written." : "Too little chat today to write an entry."); reloadMemory(); }}>Write today&apos;s diary now</button>
              <button onClick={async () => {
                const data = await api<unknown>("/memory/export");
                const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
                const a = document.createElement("a"); a.href = url; a.download = "assistant-memory.json"; a.click(); URL.revokeObjectURL(url);
              }}>Export everything (JSON)</button>
            </div>
          </>
        )}
      </div>

      <h2>Notification formats learned</h2>
      <div className="card">
        <p className="muted">The AI reads an unfamiliar bank notification once; after that the same wording in the same app is handled without the AI. Clear an app if a learned format turned out wrong; the AI then reads the next one again.</p>
        {!formats ? <p className="muted">Loading...</p> : Object.keys(formats).length === 0 ? <p className="muted">Nothing learned yet.</p> : (
          <table>
            <thead><tr><th>App</th><th>Formats</th><th>Not a payment</th><th>Retired</th><th /></tr></thead>
            <tbody>
              {Object.entries(formats).map(([pkg, f]) => (
                <tr key={pkg}>
                  <td>{pkg}</td><td>{f.formats}</td><td>{f.skip}</td><td>{f.retired}</td>
                  <td><button onClick={async () => { await del(`/templates?package=${encodeURIComponent(pkg)}`); setNote(`Cleared ${pkg}.`); reloadFormats(); }}>Clear</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {note && <p className="muted">{note}</p>}
      </div>

      <h2>Configured elsewhere</h2>
      <ul>
        <li>AI providers, each agent's model, disabled agents, voice engine and model: the AI Models page.</li>
        <li>API key, signing secret, wake-up time and monitored bank apps: the Android app.</li>
        <li>Morning briefing schedule, Google Sheets, Gemini and Firecrawl keys: the <code>/opt/apa/.env</code> file on the server.</li>
        <li>Budgets: type them in Telegram (for example &quot;food budget 1.5 million&quot;); the AI asks for your confirmation first.</li>
      </ul>
      <p className="muted">The choices above are stored on the server, so they are the same in the app and on the web.</p>
    </>
  );
}
